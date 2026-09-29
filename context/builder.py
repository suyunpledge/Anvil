# -*- coding: utf-8 -*-
"""builder.py —— 交付件 C：代码上下文层。

它只回答一个问题：**为了这个任务，该往 prompt 里装什么？**

一期刻意只做「目标文件邻域」，不做全仓索引。理由在规划里写过：
本地模型的上下文是稀缺资源（实测 qwen3:14b + 32768 窗口在有大段前置内容时
要 240 秒，属硬件上限），正确的方向是**把该装的装进去、把不该装的裁掉**，
而不是把窗口调大。

四件事：
  1. `chunk_python`   —— AST 把源码切到函数/类粒度，带行区间的确定性视图
  2. `repo_map`       —— 文件 + 顶层符号 + 一行职责（docstring 首行），只覆盖目标所在目录树
  3. `find_references`—— 复用核心的 search_code（Python/rg 双引擎）找调用点
  4. `build`          —— 按模型画像的 ctx_max 做预算裁剪，优先级明确、可断言

裁剪优先级（高 → 低，超预算从低往高扔）：
    目标函数源码 > 目标文件头部（import/常量）> 邻接函数签名 > 引用行 > repo map > 任务说明
"""
from __future__ import annotations

import ast
import os
import re
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.paths import ensure_core_on_path  # noqa: E402

ensure_core_on_path()


# ============================================================
# token 估算（与网关同一口径，保证预算与审计对得上）
# ============================================================
def est_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + (other // 4 if other else 0))


# ============================================================
# 1. AST 切块
# ============================================================
def chunk_python(path: str) -> Dict[str, Any]:
    """把一个 Python 文件切成块。非 Python / 解析失败时退化成「整文件一块」。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        return {"path": path, "ok": False, "error": str(e), "chunks": [], "lines": 0}

    lines = src.split("\n")
    chunks: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return {"path": path, "ok": True, "chunks": [{"kind": "file", "name": "<whole>",
                                                      "start": 1, "end": len(lines),
                                                      "text": src, "doc": ""}],
                "lines": len(lines), "note": "语法错误，退化为整文件块"}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunks.append(_func_chunk(node, lines, kind="function"))
        elif isinstance(node, ast.ClassDef):
            chunks.append(_class_chunk(node, lines))
        else:
            # 顶层 import / 常量 / 其它语句：合成一个「模块头」块
            chunks.append({"kind": "module", "name": _stmt_name(node),
                           "start": node.lineno, "end": getattr(node, "end_lineno", node.lineno),
                           "text": "\n".join(lines[node.lineno - 1:getattr(node, "end_lineno", node.lineno)]),
                           "doc": "", "qualname": ""})
    return {"path": path, "ok": True, "chunks": chunks, "lines": len(lines)}


def _doc_first_line(node: ast.AST) -> str:
    d = ast.get_docstring(node) or ""
    return d.strip().split("\n")[0][:100] if d else ""


def _signature(node: ast.AST, lines: List[str]) -> str:
    """只取 def 行到冒号结束（含多行签名的适配）。"""
    start = node.lineno
    out = []
    for i in range(start - 1, min(start + 6, len(lines))):
        out.append(lines[i])
        if lines[i].rstrip().endswith(":"):
            break
    return "\n".join(out).rstrip()


def _func_chunk(node: ast.AST, lines: List[str], kind: str = "function",
                prefix: str = "") -> Dict[str, Any]:
    end = getattr(node, "end_lineno", node.lineno)
    return {"kind": kind, "name": node.name, "qualname": prefix + node.name,
            "start": node.lineno, "end": end,
            "text": "\n".join(lines[node.lineno - 1:end]),
            "signature": _signature(node, lines), "doc": _doc_first_line(node)}


def _class_chunk(node: ast.ClassDef, lines: List[str]) -> Dict[str, Any]:
    end = getattr(node, "end_lineno", node.lineno)
    methods = []
    for sub in node.body:
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(_func_chunk(sub, lines, kind="method", prefix=node.name + "."))
    return {"kind": "class", "name": node.name, "qualname": node.name,
            "start": node.lineno, "end": end,
            "text": "\n".join(lines[node.lineno - 1:end]),
            "signature": _signature(node, lines), "doc": _doc_first_line(node),
            "methods": [{"qualname": m["qualname"], "signature": m["signature"],
                         "doc": m["doc"], "start": m["start"], "end": m["end"]}
                        for m in methods]}


def _stmt_name(node: ast.AST) -> str:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "imports"
    if isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        return ",".join(names) or "assign"
    return type(node).__name__


# ============================================================
# 2. repo map
# ============================================================
def repo_map(root: str, max_files: int = 60, max_depth: int = 3,
             exts: tuple = (".py",)) -> Dict[str, Any]:
    """目标所在目录树的「文件 + 顶层符号 + 一行职责」。只读，不建索引。"""
    root = os.path.abspath(root)
    entries: List[Dict[str, Any]] = []
    base_depth = root.rstrip(os.sep).count(os.sep)
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "node_modules", ".venv", ".runtime")]
        if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            if not name.endswith(exts):
                continue
            if len(entries) >= max_files:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            info = _module_summary(full)
            entries.append({"path": rel, "doc": info["doc"], "symbols": info["symbols"]})
        if truncated:
            break
    text = render_repo_map(entries)
    return {"root": root, "entries": entries, "text": text, "truncated": truncated,
            "tokens_est": est_tokens(text)}


def _module_summary(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return {"doc": "", "symbols": []}
    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"kind": "fn", "name": node.name, "doc": _doc_first_line(node)})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"kind": "class", "name": node.name, "doc": _doc_first_line(node)})
    return {"doc": _doc_first_line(tree), "symbols": symbols[:40]}


def render_repo_map(entries: List[Dict[str, Any]]) -> str:
    out = []
    for e in entries:
        head = e["path"] + (("  — " + e["doc"]) if e.get("doc") else "")
        out.append(head)
        for s in e.get("symbols", []):
            mark = "class" if s["kind"] == "class" else "def  "
            out.append("    %s %s%s" % (mark, s["name"],
                                        ("  — " + s["doc"]) if s.get("doc") else ""))
    return "\n".join(out)


# ============================================================
# 3. 引用检索
# ============================================================
def find_references(workdir: str, symbol: str, max_hits: int = 12) -> List[str]:
    """找某个符号的使用点。复用核心的 search_code（默认 Python 引擎，实测更快）。"""
    try:
        from tools import ToolContext, search_code
    except Exception:
        return []
    try:
        ctx = ToolContext(root=os.path.abspath(workdir), target=".", write_scope="target_only")
        res = search_code(ctx, pattern=r"\b%s\b" % re.escape(symbol), glob="*.py",
                          max_hits=max_hits)
        return [l for l in (res.get("hits") or "").split("\n") if l and "无匹配" not in l]
    except Exception:
        return []


# ============================================================
# 4. 预算裁剪 + 组装
# ============================================================
class Budget:
    """上下文预算：装进去的东西有明确优先级，超了就按优先级往回扔。"""

    def __init__(self, limit_tokens: int, reserve_ratio: float = 0.35) -> None:
        #: 预留给「模型输出 + 工具结果回灌」的部分；不预留在长任务上一定爆
        self.limit = max(256, int(limit_tokens * (1.0 - reserve_ratio)))
        self.sections: List[Dict[str, Any]] = []

    def add(self, key: str, priority: int, text: str) -> None:
        if text:
            self.sections.append({"key": key, "priority": priority, "text": text,
                                  "tokens": est_tokens(text)})

    def total(self) -> int:
        return sum(s["tokens"] for s in self.sections)

    def fit(self) -> Dict[str, Any]:
        """按优先级从低到高丢弃，直到进预算。返回 {kept, dropped, tokens}。"""
        kept = sorted(self.sections, key=lambda s: -s["priority"])
        dropped: List[str] = []
        while self.total_of(kept) > self.limit and kept:
            victim = min(kept, key=lambda s: s["priority"])
            kept.remove(victim)
            dropped.append(victim["key"])
        return {"kept": kept, "dropped": dropped, "tokens": self.total_of(kept)}

    @staticmethod
    def total_of(items: List[Dict[str, Any]]) -> int:
        return sum(i["tokens"] for i in items)


def render(kept: List[Dict[str, Any]]) -> str:
    order = ["task", "target_source", "file_head", "neighbors", "references",
             "repo_map", "notes", "semantic"]
    kept = sorted(kept, key=lambda s: order.index(s["key"]) if s["key"] in order else 99)
    parts = []
    for s in kept:
        parts.append("## %s\n%s" % (s["key"], s["text"]))
    return "\n\n".join(parts)


def build(target: str, task: str, spec: Any, workdir: str = "",
          repo_map_files: int = 40, neighbors_max: int = 6) -> Dict[str, Any]:
    """产出改节点要用的上下文包。

    Parameters
    ----------
    target : 目标文件（可相对 workdir，也可绝对）
    task   : 任务描述
    spec   : 模型画像（需有 ctx_max / model 属性；可为 None → 用保守默认）
    workdir: 仓库根（用于 repo map 与引用检索）
    """
    workdir = os.path.abspath(workdir or os.path.dirname(os.path.abspath(target)) or ".")
    target_abs = target if os.path.isabs(target) else os.path.join(workdir, target)
    target_rel = os.path.relpath(target_abs, workdir).replace("\\", "/")

    ctx_max = int(getattr(spec, "ctx_max", 8192) or 8192)
    b = Budget(ctx_max)

    b.add("task", 100, task)

    chunk_info = chunk_python(target_abs)
    chunks = [c for c in chunk_info.get("chunks", [])
              if c["kind"] in ("function", "class", "method")]
    # 目标函数：优先按任务描述里的标识符猜，其次取文件里第一个未实现的函数
    wanted = _guess_target_symbol(task, chunks, target_abs)
    chosen = next((c for c in chunks if c.get("qualname") == wanted), None)
    if chosen is not None:
        head_txt = _file_head(target_abs)
        b.add("file_head", 70, head_txt)
        b.add("target_source", 90, "```python\n%s\n```" % chosen["text"])
        # 邻接签名：排除目标本身，按行距离近的优先
        others = [c for c in chunks if c is not chosen]
        others.sort(key=lambda c: abs(c["start"] - chosen["start"]))
        sig = "\n".join("- %s  (L%d-%d)%s" % (c["qualname"], c["start"], c["end"],
                                              ("  — " + c["doc"]) if c.get("doc") else "")
                        for c in others[:neighbors_max])
        b.add("neighbors", 60, sig)
    else:
        with open(target_abs, "r", encoding="utf-8", errors="replace") as f:
            b.add("target_source", 90, "```python\n%s\n```" % f.read()[:4000])

    # 引用（只有猜到了符号名才有意义）
    if wanted:
        refs = find_references(workdir, wanted.split(".")[-1])
        b.add("references", 50, "\n".join(refs[:12]))

    rm = repo_map(workdir, max_files=repo_map_files)
    b.add("repo_map", 30, rm["text"])

    fitted = b.fit()
    text = render(fitted["kept"])
    return {
        "workdir": workdir, "target": target_rel, "target_abs": target_abs,
        "symbol": wanted or "", "text": text,
        "tokens_est": est_tokens(text), "limit_tokens": b.limit,
        "ctx_max": ctx_max, "kept": [k["key"] for k in fitted["kept"]],
        "dropped": fitted["dropped"], "chunks": len(chunks),
        "repo_map_files": len(rm["entries"]), "repo_map_truncated": rm["truncated"],
        "naive_tokens_full_file": est_tokens(_read(target_abs)),
    }


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _file_head(path: str, max_lines: int = 25) -> str:
    """文件头：import 与常量。模型需要它才能写出能跑通的代码。"""
    src = _read(path).split("\n")
    out = []
    for line in src[:max_lines]:
        s = line.strip()
        if s.startswith(("import ", "from ")) or re.match(r"^[A-Z_][A-Z0-9_]*\s*=", s):
            out.append(line)
    return "\n".join(out)


def _guess_target_symbol(task: str, chunks: List[Dict[str, Any]], target_abs: str) -> str:
    """从任务描述里猜目标符号；猜不到就取源码里第一个「未实现」的函数。"""
    names = {c["qualname"] for c in chunks}
    plain = {c["name"]: c["qualname"] for c in chunks}
    for cand in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task):
        if cand in plain:
            return plain[cand]
    src = _read(target_abs)
    for c in chunks:
        seg = c.get("text", "")
        if "NotImplementedError" in seg or ("pass" == seg.strip().split("\n")[-1].strip()):
            return c["qualname"]
    return chunks[0]["qualname"] if chunks else ""


# ============================================================
# 给工单服务的接入点（外部审查 #6）
# ============================================================
def build_workorder_context(workdir: str, target: str, task: str, spec: Any = None,
                            staging_root: Optional[str] = None) -> Dict[str, Any]:
    """工单服务调用的统一入口。

    为什么要单独包一层：状态机工作的目录是**暂存区**（它自己拷了一份副本），
    而上下文层需要看到「模型将要改的那份文件」。所以这里要先把路径换成暂存区里的副本，
    否则会拿着真实目录的旧内容去组装上下文（与将要改的文件不是同一份）。

    ``staging_root`` 传了就优先用它；否则用 workdir。
    """
    base = os.path.abspath(staging_root or workdir)
    target_rel = target.replace("\\", "/")
    target_abs = target if os.path.isabs(target) else os.path.join(base, target_rel)
    if not os.path.isfile(target_abs):        # 暂存区里找不到就回退真实目录
        base = os.path.abspath(workdir)
        target_abs = target if os.path.isabs(target) else os.path.join(base, target_rel)
    return build(target=target_abs, task=task, spec=spec, workdir=base)

# -*- coding: utf-8 -*-
"""tools.py —— 状态机真正执行的工具（真实文件读写 / 跑测试）。

与兼容层的关系：兼容层只负责「模型说了什么」，本模块负责「框架真的做什么」。
两者之间再插一道引擎级校验（engine._execute_call），因此即便模型的话被放行，
工具层仍有自己的沙箱约束。

沙箱约束（v1 写死）：
  · 所有路径必须落在沙箱根之内（拒绝 ``..`` 与绝对路径逃逸）；
  · 写操作只允许落在工单声明的目标文件上（``write_scope='target_only'``）——
    这条让模型**无法改测试文件来把测试「改绿」**，是本版最关键的一条结构约束。
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from contract import WRITE_TOOLS


class ToolError(Exception):
    """工具层的可预期失败（参数错、路径越界、补丁对不上）。"""


class PatchError(ToolError):
    """补丁无法应用（old_string 找不到 / 多处匹配 / 行号越界）。"""


class VersionConflict(ToolError):
    """乐观并发失败：文件内容自上次读取后已变化（借自 chat-ollama 的 expectedVersion）。"""


def content_version(path: str) -> str:
    """文件内容的版本号：``sha256:<64 hex>``。

    为什么要它（2026-09-28 对标 chat-ollama 后补）：
    我们的 ``apply_patch`` 原来只靠「old_string 唯一匹配」防误改。这在单写者、
    同一瞬间的场景下够用；但只要文件在「模型看到」与「补丁落盘」之间被另一条路径改过，
    匹配仍然可能成功，结果是**静默覆盖**别人刚写的东西。
    版本号把这种情形变成显式失败：读的时候记下版本，写之前比对，不符就拒。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def assert_expected_version(path: str, expected: Optional[str]) -> None:
    """expected 为空则无条件写；给了且不符 → 报冲突且不改文件。"""
    if not expected:
        return
    if not os.path.exists(path):
        raise VersionConflict("期望版本 %s，但文件已不存在" % expected[:20])
    actual = content_version(path)
    if actual != expected:
        raise VersionConflict("文件内容已变化（期望 %s… 实际 %s…），拒绝写入"
                              % (expected[:20], actual[:20]))


# ============================================================
# 上下文
# ============================================================
@dataclass
class ToolContext:
    """工具执行上下文。``root`` 是沙箱根（门面在**暂存区**，而非原始目录）。"""
    root: str
    target: str                      # 目标文件相对路径
    write_scope: str = "target_only"
    timeout_sec: int = 180
    executed: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # ★ 沙箱根必须真解析一次（符号链接）。理由见 resolve() 的说明。
        try:
            self.root = os.path.realpath(self.root)
        except OSError:
            self.root = os.path.abspath(self.root)

    def resolve(self, rel: str, for_write: bool = False) -> str:
        """把相对路径解析到沙箱内的绝对路径，越界即拒。

        与上一版的差别（2026-09-28 对标 chat-ollama 后加固）：
          · 不再只做字符串前缀判断，而是把**已存在的部分**真解析一遍（``realpath``），
            再判越界。原因是软链接：``link -> /etc`` 这种目录在字符串前缀上看是“在沙箱内”，
            一旦写进去就写出了沙箱。实测已固化成回归用例。
          · 拒绝兄弟前缀混淆：``/work-other`` 不能被 ``/work`` 前缀误判为内部路径。
        """
        if rel is None:
            raise ToolError("路径为空")
        rel = str(rel).strip().replace("\\", "/")
        if not rel:
            raise ToolError("路径为空")
        if os.path.isabs(rel) or re.match(r"^[a-zA-Z]:", rel):
            raise ToolError("拒绝绝对路径：%s" % rel)
        if rel.startswith("~"):
            raise ToolError("拒绝 ~ 路径：%s" % rel)

        root = self.root
        abs_path = os.path.normpath(os.path.join(root, rel))
        self._assert_inside(abs_path, root, rel)

        # 已存在的祖先目录/文件真解析一次，挡住「软链接指向沙箱外」
        probe = abs_path
        suffix: List[str] = []
        while not os.path.exists(probe):
            parent, name = os.path.split(probe)
            if parent == probe:
                break
            suffix.insert(0, name)
            probe = parent
        if os.path.exists(probe):
            real = os.path.realpath(probe)
            folded = os.path.join(real, *suffix) if suffix else real
            self._assert_inside(os.path.normpath(folded), root, rel, via_symlink=True)

        if for_write and self.write_scope == "target_only":
            target_abs = os.path.normpath(os.path.join(root, self.target.replace("\\", "/")))
            if abs_path != target_abs:
                raise ToolError("写操作只允许落在目标文件 %s（本单写范围为 target_only）" % self.target)
        return abs_path

    @staticmethod
    def _assert_inside(abs_path: str, root: str, rel: str, via_symlink: bool = False) -> None:
        if abs_path == root or abs_path.startswith(root + os.sep):
            return
        why = "（经由符号链接解析后越界）" if via_symlink else ""
        raise ToolError("路径越出沙箱%s：%s" % (why, rel))

    def note(self, tool: str, args: Dict[str, Any], ok: bool, detail: str = "") -> None:
        self.executed.append({"tool": tool, "args": args, "ok": ok,
                              "detail": detail[:300], "ts": time.time()})


# ============================================================
# 读类工具
# ============================================================
def read_file(ctx: ToolContext, path: str = "", max_lines: int = 400,
              start_line: Optional[int] = None, end_line: Optional[int] = None) -> Dict[str, Any]:
    p = ctx.resolve(path or ctx.target)
    if not os.path.exists(p):
        raise ToolError("文件不存在：%s" % path)
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.split("\n")
    total = len(lines)
    if start_line or end_line:
        s = max(1, int(start_line or 1))
        e = min(total, int(end_line or total))
        chunk = lines[s - 1:e]
        head = "[%s 第 %d-%d 行，共 %d 行]" % (path, s, e, total)
        truncated = False
    else:
        m = max(20, int(max_lines or 400))
        chunk = lines[:m]
        truncated = total > m
        head = "[%s 共 %d 行%s]" % (path, total, "，已截取前 %d 行" % m if truncated else "")
    return {"path": path, "total_lines": total, "text": head + "\n" + "\n".join(chunk),
            "truncated": truncated,
            "version": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()}


def list_dir(ctx: ToolContext, path: str = ".", depth: int = 1) -> Dict[str, Any]:
    base = ctx.resolve(path or ".")
    out: List[str] = []
    base_depth = base.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        d = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        if d >= max(1, int(depth or 1)):
            dirnames[:] = []
            continue
        rel = os.path.relpath(dirpath, ctx.root).replace("\\", "/")
        out.append("[" + rel + "]")
        for name in sorted(filenames)[:60]:
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            out.append("  %-40s %8d" % (name, size))
    return {"path": path, "listing": "\n".join(out)}


# ============================================================
# 搜索：两套引擎，按实测取舍（默认 Python，可选 rg）
# ============================================================
# 本机实测（2026-09-28，三次取最快）：
#   171 个文件：  rg 0.073s  |  py 0.022s   → Python 快 3.3×
#   20,019 个文件：rg 0.063s  |  py 0.062s   → 打平
#
# 结论（与我们最初的预设相反）：**换 rg 的收益不是速度，是安全**。
#   · 速度上：rg 的进程启动开销抵消了大目录的扫描优势，本机没赚到；
#   · 安全上：rg 走 argv 数组 + `shell:false`，模型给的正则只占**一个参数位**，
#     不可能变成 flag 或命令。这是 Python 内联实现天然没有的一层保障。
#
# 所以取舍是：默认走 Python（快、稳、无外部依赖），
# `engine="rg"` 时走 rg（超大仓库或需要那层参数隔离时用）。
# `engine="auto"` 按候选文件规模切（阈值 5000，估数成本低）。
# 不把 rg 当默认，是因为真正的默认应该是「本机实测更快的那一个」。
_RG_AUTO_THRESHOLD = 5000
# rg 候选：优先 PATH；再试 Tuanjie Cowork 的内置二进制（按环境变量拼路径，
# 不把用户名/本机布局硬编码进代码）。都找不到就退回 Python 实现。
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
_RG_CANDIDATES = (
    "rg", "rg.exe",
    os.path.join(_LOCAL_APPDATA, "Programs", "Tuanjie Cowork", "app", "resource",
                 "core", "bin", "win32-x64", "rg.exe"),
)
_RG_CACHE: Dict[str, Optional[str]] = {"path": None, "probed": False}  # type: ignore


def _count_candidates(base: str, glob: str, cap: int = 60000) -> int:
    """粗略数一下候选文件数（用它决定是否值得起一个 rg 进程）。"""
    try:
        rx = re.compile(glob.replace(".", r"\.").replace("*", ".*") + "$")
    except re.error:
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", "node_modules")]
        for name in filenames:
            if rx.search(name):
                n += 1
                if n > cap:
                    return n
    return n


def find_rg() -> Optional[str]:
    """定位 ripgrep 可执行文件；找不到返回 None（调用方退 Python 实现）。"""
    if _RG_CACHE["probed"]:
        return _RG_CACHE["path"]
    _RG_CACHE["probed"] = True
    import shutil as _sh
    for cand in _RG_CANDIDATES:
        if os.path.sep in cand or cand.lower().endswith(".exe"):
            if os.path.isfile(cand):
                _RG_CACHE["path"] = cand
                break
        else:
            got = _sh.which(cand)
            if got:
                _RG_CACHE["path"] = got
                break
    return _RG_CACHE["path"]


def search_with_rg(root: str, pattern: str, glob: str = "*.py", max_hits: int = 20,
                   timeout_sec: int = 60) -> Optional[Dict[str, Any]]:
    """用 rg 搜索。返回 None 表示 rg 不可用/执行失败，调用方应退回。

    ★ 安全要点：argv 数组 + ``shell=False``；模式串前用 ``-e`` 显式标参数，
      避免以 ``-`` 开头的 pattern 被当成选项。
    """
    exe = find_rg()
    if not exe:
        return None
    cmd = [exe, "--line-number", "--no-heading", "--color", "never",
           "--max-count", str(max(1, int(max_hits))),
           "--glob", glob, "-e", pattern, "."]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                              timeout=int(timeout_sec), encoding="utf-8",
                              errors="replace", shell=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode not in (0, 1):      # 1 = 无匹配（正常）
        return None
    hits: List[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        # 输出形如 .\rel\path.py:12:内容 或 ./rel/path.py:12:内容
        # 统一：去掉前导 .\ / ./，并把反斜杠转成正斜杠（与 Python 实现保持同一套路径约定）
        s = re.sub(r"^[.][/\\]", "", line)
        if ":" in s:
            head, _, rest = s.partition(":")
            s = head.replace("\\", "/") + ":" + rest
        hits.append(s[:240])
    # rg 的 --max-count 是「每文件」上限，总数得自己截（否则断言与真实上限不一致）
    truncated = len(hits) >= int(max_hits)
    if len(hits) > int(max_hits):
        hits = hits[:int(max_hits)]
    return {"pattern": pattern, "hits": "\n".join(hits) or "(无匹配)",
            "truncated": truncated, "engine": "rg"}


def search_code(ctx: ToolContext, pattern: str = "", path: str = ".",
                max_hits: int = 20, glob: str = "*.py", engine: str = "python") -> Dict[str, Any]:
    """按正则搜索。``engine``：python（默认）/ rg / auto。

    默认 Python 的依据见本节开头的实测数据；``rg`` 存在的价值是参数隔离（安全）。
    """
    if not pattern:
        raise ToolError("search_code 需要 pattern")
    base = ctx.resolve(path or ".")
    engine = (engine or "python").lower()

    use_rg = False
    if engine == "rg":
        use_rg = True
    elif engine == "auto":
        use_rg = _count_candidates(base, glob) > _RG_AUTO_THRESHOLD
    if use_rg:
        rg_res = search_with_rg(base, pattern, glob=glob, max_hits=max_hits)
        if rg_res is not None:
            return rg_res
        # rg 不可用 → 继续走 Python（不报错）
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError("正则不合法：%s" % e)
    hits: List[str] = []
    fnpat = re.compile(glob.replace(".", r"\.").replace("*", ".*") + "$")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for name in sorted(filenames):
            if not fnpat.search(name):
                continue
            fp = os.path.join(dirpath, name)
            rel = os.path.relpath(fp, ctx.root).replace("\\", "/")
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append("%s:%d:%s" % (rel, i, line.rstrip()[:200]))
                            if len(hits) >= int(max_hits or 20):
                                return {"pattern": pattern, "hits": "\n".join(hits),
                                        "truncated": True, "engine": "python"}
            except OSError:
                continue
    return {"pattern": pattern, "hits": "\n".join(hits) or "(无匹配)",
            "truncated": False, "engine": "python"}


# ============================================================
# 写类工具（★ 只允许目标文件）
# ============================================================
def _split_keep_trailing(text: str) -> List[str]:
    trailing_nl = text.endswith("\n")
    lines = text.split("\n")
    if trailing_nl:
        lines = lines[:-1]
    return lines


def diff_text(before: str, after: str, fromfile: str, tofile: str, n: int = 3) -> str:
    """统一 diff 文本（两边都按行拆分、保留行尾）。"""
    return "".join(difflib.unified_diff(
        [l + "\n" for l in _split_keep_trailing(before)],
        [l + "\n" for l in _split_keep_trailing(after)],
        fromfile=fromfile, tofile=tofile, n=n))


def diff_stats(diff: str) -> Dict[str, int]:
    return {"added": sum(1 for l in diff.split("\n") if l.startswith("+") and not l.startswith("+++")),
            "removed": sum(1 for l in diff.split("\n") if l.startswith("-") and not l.startswith("---"))}


def apply_patch(ctx: ToolContext, path: str = "", old_string: Optional[str] = None,
                new_string: Optional[str] = None, start_line: Optional[int] = None,
                end_line: Optional[int] = None, new_code: Optional[str] = None,
                expected_version: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """最小补丁：两种形态之一。

    A) 文本替换：``old_string`` → ``new_string``（必须在文件中唯一匹配）；
    B) 行区间替换：``start_line``..``end_line``（1-based，含端点）→ ``new_code``。
    """
    p = ctx.resolve(path or ctx.target, for_write=True)
    # ★ 乐观并发（借自 chat-ollama）：先比版本，再动手。
    assert_expected_version(p, expected_version)
    with open(p, "r", encoding="utf-8") as f:
        before = f.read()
    version_before = "sha256:" + hashlib.sha256(before.encode("utf-8")).hexdigest()
    src_lines = before.split("\n")
    trailing_nl = before.endswith("\n")
    body = src_lines[:-1] if trailing_nl else src_lines

    if old_string:
        cnt = before.count(old_string)
        if cnt == 0:
            raise PatchError("old_string 在 %s 中找不到（必须与原文逐字一致，含缩进与空行）" % path)
        if cnt > 1:
            raise PatchError("old_string 在 %s 中匹配 %d 处，请给出更长的上下文使其唯一" % (path, cnt))
        after = before.replace(old_string, new_string if new_string is not None else "", 1)
        mode = "replace_text"
    elif start_line and end_line and new_code is not None:
        s, e = int(start_line), int(end_line)
        if s < 1 or e < s or s > len(body):
            raise PatchError("行号越界：文件共 %d 行，收到 start_line=%s end_line=%s" % (len(body), start_line, end_line))
        e = min(e, len(body))
        new_lines = new_code.split("\n")
        if new_lines and new_lines[-1] == "":
            new_lines = new_lines[:-1]
        body2 = body[:s - 1] + new_lines + body[e:]
        after = "\n".join(body2) + ("\n" if trailing_nl else "")
        mode = "replace_lines"
    else:
        raise PatchError("补丁参数不足：需要 (old_string+new_string) 或 (start_line+end_line+new_code)")

    if after == before:
        raise PatchError("补丁没有产生任何改动")

    tmp = p + ".sm_tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(after)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)

    diff = diff_text(before, after, "a/" + path, "b/" + path)
    st = diff_stats(diff)
    return {"path": path, "mode": mode, "diff": diff, "added": st["added"], "removed": st["removed"],
            "lines_before": len(body), "lines_after": len(_split_keep_trailing(after)),
            "version_before": version_before,
            "version": "sha256:" + hashlib.sha256(after.encode("utf-8")).hexdigest()}


def write_file(ctx: ToolContext, path: str = "", content: str = "",
               expected_version: Optional[str] = None) -> Dict[str, Any]:
    p = ctx.resolve(path or ctx.target, for_write=True)
    assert_expected_version(p, expected_version)
    tmp = p + ".sm_tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return {"path": path, "mode": "write_file", "bytes": len(content.encode("utf-8")),
            "version": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()}


# ============================================================
# 执行类工具
# ============================================================
def run_tests(ctx: ToolContext, test_path: Optional[str] = None, timeout_sec: Optional[int] = None,
              sandbox_strict: Optional[bool] = None, **extra: Any) -> Dict[str, Any]:
    """跑测试 gate 用的执行体。约定：cwd=沙箱根，`python -m unittest discover -s . -t .`。

    ★ 为什么用 discover：本机内嵌解释器带 ``python313._pth``（隔离模式，``import site`` 被
    注释掉）——``cwd`` 与 ``PYTHONPATH`` 都不进 ``sys.path``，直接
    ``python -m unittest <module>`` 必然 ``ModuleNotFoundError``。而 ``unittest discover``
    会把 ``-t`` 指定的绝对目录插进 ``sys.path``，因此能稳定 import 沙箱里的模块。

    ★★ 安全（2026-09-28 外部审查后加固）：被测代码是**模型改写过的**，import 时能任意行事。
    所以子进程不再继承 `os.environ`，而是用 :mod:`sandbox` 给的干净环境（丢代理与凭据、
    HOME 指向临时目录、禁写字节码）。**注意这仍是缓解而非沙箱**——不阻断 socket、
    不限文件访问；真隔离需要 OS 级手段，见 `sandbox.real_isolation_available()`。
    """
    import sandbox as _sb
    strict = (os.environ.get("LOCAL_IDE_SANDBOX_STRICT", "0") == "1"
              if sandbox_strict is None else bool(sandbox_strict))
    _sb.assert_real_isolation_available(strict)

    tp = test_path or ""
    if tp:
        ctx.resolve(tp)  # 存在性与越界检查
        pat = os.path.basename(tp.replace("\\", "/"))
        if not pat.endswith(".py"):
            pat += ".py"
        cmd = [sys.executable, "-m", "unittest", "discover", "-v",
               "-s", ".", "-t", ".", "-p", pat]
    else:
        cmd = [sys.executable, "-m", "unittest", "discover", "-v", "-s", ".", "-t", "."]
    # ★ 干净环境：不继承代理/密钥；HOME/TEMP 指向隔离目录
    env = _sb.sanitized_env()
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ctx.root, capture_output=True, text=True,
                              timeout=int(timeout_sec or ctx.timeout_sec),
                              encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        return {"rc": -9, "tests_run": 0, "failures": 0, "errors": 0, "ok": False,
                "sec": time.time() - t0, "cmd": cmd, "output": "测试超时（%ss）" % (timeout_sec or ctx.timeout_sec)}
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"Ran (\d+) tests?", out)
    n = int(m.group(1)) if m else 0
    fail = len(re.findall(r"^FAIL:", out, re.M))
    err = len(re.findall(r"^ERROR:", out, re.M))
    return {"rc": proc.returncode, "tests_run": n, "failures": fail, "errors": err,
            "ok": proc.returncode == 0, "sec": time.time() - t0, "cmd": cmd,
            "output": out[-4000:]}


def run_shell(ctx: ToolContext, command: str = "", **extra: Any) -> Dict[str, Any]:
    """受控 shell（v1 流程不用；保留给 shell 节点，且必须过 R2 命令闸）。"""
    if not command:
        raise ToolError("run_shell 需要 command")
    proc = subprocess.run(command, cwd=ctx.root, shell=True, capture_output=True,
                          text=True, timeout=int(ctx.timeout_sec), encoding="utf-8",
                          errors="replace")
    return {"rc": proc.returncode, "output": ((proc.stdout or "") + (proc.stderr or ""))[-4000:]}


def git_commit(ctx: ToolContext, message: str = "", **extra: Any) -> Dict[str, Any]:
    """v1 默认关闭（allow_git_commit=False）；实现保留，便于后续工单启用。"""
    proc = subprocess.run(["git", "add", "-A"], cwd=ctx.root, capture_output=True, text=True)
    proc2 = subprocess.run(["git", "commit", "-m", message or "sm: apply work order"],
                           cwd=ctx.root, capture_output=True, text=True)
    return {"rc": proc2.returncode, "output": ((proc.stdout or "") + (proc2.stdout or "") + (proc2.stderr or ""))[-2000:]}


# ============================================================
# 工具注册表（模型看的 schema + 框架执行体）
# ============================================================
def _schema(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    props = dict(props)
    props.setdefault("additionalProperties", None)
    props.pop("additionalProperties", None)
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required,
                       "additionalProperties": False}}}


_S = {"path": {"type": "string"}, "max_lines": {"type": "integer"},
      "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
      "old_string": {"type": "string"}, "new_string": {"type": "string"},
      "new_code": {"type": "string"}, "content": {"type": "string"},
      "expected_version": {"type": "string"},
      "pattern": {"type": "string"}, "max_hits": {"type": "integer"},
      "glob": {"type": "string"}, "depth": {"type": "integer"},
      "engine": {"type": "string"},
      "test_path": {"type": "string"}, "timeout_sec": {"type": "integer"},
      "command": {"type": "string"}, "message": {"type": "string"}}

REGISTRY: Dict[str, Dict[str, Any]] = {
    "read_file": _schema("read_file", "读取沙箱内某文件的内容（可只读一段行区间）",
                         {k: _S[k] for k in ("path", "max_lines", "start_line", "end_line")}, ["path"]),
    "list_dir": _schema("list_dir", "列目录", {k: _S[k] for k in ("path", "depth")}, []),
    "search_code": _schema("search_code", "按正则搜索代码，返回 文件:行:内容。engine 可选 python/rg/auto",
                           {k: _S[k] for k in ("pattern", "path", "max_hits", "glob", "engine")}, ["pattern"]),
    "apply_patch": _schema(
        "apply_patch",
        "用最小改动替换目标文件中的一段内容。两种写法二选一："
        "(A) old_string + new_string：把原文中唯一出现的一段文本换成新文本；"
        "(B) start_line + end_line + new_code：把第 start_line~end_line 行（含端点）整段替换。"
        "禁止整文件重写；只允许修改目标文件。"
        "可传 expected_version（read_file 返回的 sha256:...）做乐观并发保护："
        "若文件已被改动则拒绝写入。",
        {k: _S[k] for k in ("path", "old_string", "new_string", "start_line", "end_line",
                            "new_code", "expected_version")},
        ["path"]),
    "write_file": _schema("write_file", "整文件重写（仅在 create 节点授权；edit 节点不授权）",
                          {k: _S[k] for k in ("path", "content", "expected_version")},
                          ["path", "content"]),
    "run_tests": _schema("run_tests", "在沙箱根执行单元测试",
                         {k: _S[k] for k in ("test_path", "timeout_sec")}, []),
    "run_shell": _schema("run_shell", "执行一条 shell 命令（受危险模式闸控制）",
                         {k: _S[k] for k in ("command",)}, ["command"]),
    "git_commit": _schema("git_commit", "提交当前改动", {k: _S[k] for k in ("message",)}, []),
}

EXECUTORS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "search_code": search_code,
    "apply_patch": apply_patch,
    "write_file": write_file,
    "run_tests": run_tests,
    "run_shell": run_shell,
    "git_commit": git_commit,
}


def execute(ctx: ToolContext, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行一次工具调用。未知工具 / 工具层失败都抛 :class:`ToolError` 系。"""
    fn = EXECUTORS.get(name)
    if fn is None:
        raise ToolError("未知工具：%s" % name)
    try:
        return fn(ctx, **args)
    except TypeError as e:
        raise ToolError("工具 %s 参数不符：%s" % (name, e))


def result_to_text(res: Dict[str, Any]) -> str:
    """把工具结果压成一段文本喂回模型（只保留对模型有用的字段）。"""
    if "text" in res:
        return res["text"]
    if "listing" in res:
        return res["listing"]
    if "hits" in res:
        return res["hits"]
    if "output" in res:
        return res["output"]
    if "diff" in res:
        return "补丁已应用：%s（%s，+%d/-%d）\n%s" % (res.get("path"), res.get("mode"),
                                                    res.get("added", 0), res.get("removed", 0),
                                                    res.get("diff", "")[:800])
    return json.dumps(res, ensure_ascii=False)[:2000]
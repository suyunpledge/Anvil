# -*- coding: utf-8 -*-
"""tidy.py —— 「本地文件整理」状态机（第二类工单，吃本地模型的独属优势）。

为什么是这一类任务（用户 2026-09-28）：

  本地模型最大的两个结构性优势是**离线**与**不出网**。把文件清单交给云端模型，
  等于把「你硬盘上有什么」整份交出去；交给本地模型，零外泄。
  而这类任务（找文件、按规则归类、批量改名）恰好又是**判断简单、执行机械**的——
  弱模型完全够用，需要的只是框架替它把危险动作拦住。

  → 所以它比「改代码」更适合本地模型：云端能做但不该做，本地做得慢但只有它能做。

链条（与代码类工单同构，但节点语义不同）：

    scan（扫清单） → plan（模型出方案） → check_gate（方案闸） → apply（执行） → verify_gate（复核闸）

与代码工单共用的东西：工单结构、Step/GateResult、回退循环、画像、引擎级工具授权。
不同的东西：三个 gate 的检查内容、以及**删除的口径**（见下）。

三个 gate 的检查口径：

  check_gate  方案闸（执行前）：
    · 每条 move 的源必须真实存在、目标必须落在工单声明的根目录内
    · 目标目录必须由方案自己创建（不允许凭空调到已存在目录）
    · **默认零删除**：除非工单显式声明 allow_delete=true，否则 delete 类动作直接判失败
    · 一次规划的动作数上限（防一次性大搬家）
  apply       执行：逐个 move，每个都做「目标已存在则停」检查（绝不覆盖）
  verify_gate 复核闸（执行后）：
    · 计数守恒：src 文件数 + 已移动数 == 原文件数
    · 目标目录内的文件数 == 方案声称的数量
    · 没有任何文件丢失（逐名比对：未移动的仍在原位）

删除口径（2026-10-03 起，用户明确要求）：
  默认仍**零删除**；确要清理的工单，建单时声明 ``allow_delete=true``，此时：
    · 方案闸放行 delete 动作，但单次删除数有上限（MAX_DELETES=50）；
    · 服务端**强制人确认**（require_confirm 被置真）——没确认不会走到 apply；
    · apply 里每次删除都先经 ``safeops.backup_and_delete``：备份 → 校验（文件比
      sha256、目录比文件数与字节）→ 通过才删；校验不过则**中止且不删**；
    · 复核闸逐条核对「声称删掉的，备份文件确实在」。

  「同意」与「备份」两条都不靠提示词，靠结构：
  前者是服务端策略 + confirm 接口，后者是 safeops 的写-校验-删顺序。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from contract import GateIssue, GateResult

import safeops  # 删除+备份的唯一真相源（同目录模块，2026-10-03）

#: 允许的动作类型：移动（改名也用 move 表达）+ 删除（2026-10-03 起，**条件允许**）
ALLOWED_ACTIONS = ("move", "delete")

#: 删除动作的别名（模型可能换个说法）
DELETE_ACTIONS = ("delete", "remove", "rm", "trash", "unlink")

#: 真正一律禁止的动作类型——「覆盖式」写法会绕过备份，永不接受
FORBIDDEN_ACTIONS = ("overwrite", "shutil.rmtree")

#: 单次工单允许的最大删除数（比 move 更严；防「批量清空」）
MAX_DELETES = 50

#: 单次工单允许的最大移动数（防「一次性大搬家」）
MAX_MOVES = 200

#: 扫描时跳过的目录/后缀（系统目录与缓存，不该被整理）
SCAN_SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "__pycache__", "node_modules",
                  ".git", "AppData"}
SCAN_SKIP_EXT = {".tmp", ".log"}


# ============================================================
# 工单与方案的数据结构
# ============================================================
@dataclass
class TidyOrder:
    """文件整理工单。``root`` 是唯一允许活动的目录，所有动作不许越出它。"""
    wo_id: str
    task: str                       # 自然语言描述（给模型看）
    root: str                       # 整理根目录（绝对路径）
    mode: str = "dry_run"           # dry_run（只出方案）/ execute（真移动）
    target_dirs: List[str] = field(default_factory=list)   # 允许归入的目录名（相对 root）
    filters: Dict[str, Any] = field(default_factory=dict)  # {"ext": [".pdf"], "name_re": "..."}
    max_moves: int = MAX_MOVES
    max_deletes: int = MAX_DELETES
    constraints: Dict[str, Any] = field(default_factory=lambda: {
        # ★ 默认仍与 v1 一致：删/覆盖都关。开启删除的唯一路径是建单时显式声明
        #   allow_delete=True，而服务端会同时强制 require_confirm（见 runner）。
        "allow_delete": False,
        "overwrite": False,
        "max_repair_rounds": 2,
    })

    def to_dict(self) -> Dict[str, Any]:
        return {"wo_id": self.wo_id, "task": self.task, "root": self.root, "mode": self.mode,
                "target_dirs": self.target_dirs, "filters": self.filters,
                "max_moves": self.max_moves, "max_deletes": self.max_deletes,
                "constraints": self.constraints}


# ============================================================
# scan：确定性扫描（不调模型）
# ============================================================
def scan_root(root: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """扫出根目录下的待整理文件清单。纯程序，不调模型——模型只看结果。"""
    filters = filters or {}
    exts = [e.lower() for e in (filters.get("ext") or [])]
    name_re = filters.get("name_re")
    rx = re.compile(name_re) if name_re else None
    files: List[Dict[str, Any]] = []
    skipped: List[str] = []
    root = os.path.abspath(root)
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if os.path.isdir(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in SCAN_SKIP_EXT:
            skipped.append(name)
            continue
        if exts and ext not in exts:
            skipped.append(name)
            continue
        if rx and not rx.search(name):
            skipped.append(name)
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        files.append({"name": name, "ext": ext, "size": st.st_size,
                      "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))})
    dirs = [d for d in sorted(os.listdir(root))
            if os.path.isdir(os.path.join(root, d)) and d not in SCAN_SKIP_DIRS]
    return {"root": root, "files": files, "existing_dirs": dirs,
            "skipped": skipped, "total": len(files)}


# ============================================================
# check_gate：方案闸（执行前）
# ============================================================
def _rel(p: str) -> str:
    """把模型给的路径归一成相对路径文本（**不要**用 lstrip，它会吃掉前导的点和斜线）。"""
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def check_plan(root: str, plan: Dict[str, Any], order: TidyOrder) -> GateResult:
    t0 = time.time()
    root = os.path.abspath(root)
    issues: List[GateIssue] = []

    moves = plan.get("moves")
    if not isinstance(moves, list) or not moves:
        return GateResult("check_gate", False,
                          issues=[GateIssue("empty_plan", "方案里没有任何 move 动作")],
                          sec=time.time() - t0)

    if len(moves) > int(order.max_moves):
        issues.append(GateIssue("too_many_moves",
                                "方案包含 %d 个动作，超过单次上限 %d"
                                % (len(moves), order.max_moves),
                                hint="拆成多次工单，一次少整理一些"))

    # ① 动作类型闸
    #   2026-10-03 起：删除**条件允许**——只有工单显式声明 allow_delete=true
    #   且已进入「人工确认」口径时才放行；默认仍与 v1 一样零删除。
    #   这不是提示词劝阻，是结构判定：没这个开关，删除动作在方案闸就死了。
    allow_delete = bool((order.constraints or {}).get("allow_delete", False))
    delete_count = 0
    for i, mv in enumerate(moves):
        act = str((mv or {}).get("action", "move")).lower()
        is_delete = act in DELETE_ACTIONS or any(f in act for f in DELETE_ACTIONS)
        if any(f in act for f in FORBIDDEN_ACTIONS):
            issues.append(GateIssue("forbidden_action",
                                    "第 %d 个动作是禁止类型（%s）——会绕过备份，永不接受"
                                    % (i + 1, act)))
            continue
        if is_delete:
            delete_count += 1
            if not allow_delete:
                issues.append(GateIssue(
                    "delete_not_allowed",
                    "第 %d 个动作是删除（%s），但本工单未开启 allow_delete" % (i + 1, act),
                    hint="整理默认零删除；确需清理请建单时声明 allow_delete=true（会强制人工确认+自动备份）"))
            continue
        if act not in ALLOWED_ACTIONS:
            issues.append(GateIssue("unknown_action",
                                    "第 %d 个动作类型未知：%s" % (i + 1, act)))
    if delete_count > int(getattr(order, "max_deletes", MAX_DELETES)):
        issues.append(GateIssue("too_many_deletes",
                                "方案包含 %d 个删除，超过单次上限 %d"
                                % (delete_count, getattr(order, "max_deletes", MAX_DELETES)),
                                hint="删东西比搬东西危险，一次少删一点"))

    # ② 源必须真实存在，且必须在 root 内
    existing = set(os.listdir(root))
    names_seen: Dict[str, int] = {}
    for i, mv in enumerate(moves):
        src = str((mv or {}).get("src", ""))
        dst = str((mv or {}).get("dst", ""))
        _act = str((mv or {}).get("action", "move")).lower()
        _is_del = _act in DELETE_ACTIONS or any(f in _act for f in DELETE_ACTIONS)
        if not src or (not dst and not _is_del):
            issues.append(GateIssue("missing_field",
                                    "第 %d 个动作缺少 src%s"
                                    % (i + 1, "" if _is_del else " 或 dst")))
            continue
        src_rel = _rel(src)
        dst_rel = _rel(dst)
        if os.path.isabs(src_rel) or ".." in src_rel.split("/"):
            issues.append(GateIssue("src_outside", "第 %d 个动作的源越出根目录：%s" % (i + 1, src)))
        if not _is_del and (os.path.isabs(dst_rel) or ".." in dst_rel.split("/")):
            issues.append(GateIssue("dst_outside", "第 %d 个动作的目标越出根目录：%s" % (i + 1, dst)))
        if src_rel.split("/")[0] not in existing:
            issues.append(GateIssue("src_missing", "第 %d 个动作的源不存在：%s" % (i + 1, src),
                                    hint="只能移动扫描清单里出现过的文件"))
        names_seen[src_rel] = names_seen.get(src_rel, 0) + 1
        # 目标目录必须在允许清单里（给了 target_dirs 就只认这些；没给则要求是根下的新目录）
        # 删除动作没有目标目录，跳过这一段（2026-10-03）
        if _is_del:
            continue
        top = dst_rel.split("/")[0]
        if order.target_dirs:
            if top not in order.target_dirs:
                issues.append(GateIssue("dst_not_allowed",
                                        "第 %d 个动作要放进未授权目录 %s（允许：%s）"
                                        % (i + 1, top, ", ".join(order.target_dirs))))
        elif not top or top == dst_rel:
            issues.append(GateIssue("dst_bad_dir", "第 %d 个动作的目标缺少目录层级：%s" % (i + 1, dst)))

    # ③ 同一个源不许被移动两次（典型幻觉：一条文件分到两个目录）
    for name, cnt in names_seen.items():
        if cnt > 1:
            issues.append(GateIssue("duplicate_src", "文件 %s 被安排了 %d 次移动" % (name, cnt)))

    # ④ 目标不得覆盖已有文件（删除动作无目标，跳过）
    if not order.constraints.get("overwrite", False):
        for i, mv in enumerate(moves):
            _a = str((mv or {}).get("action", "move")).lower()
            if _a in DELETE_ACTIONS or any(f in _a for f in DELETE_ACTIONS):
                continue
            dst = _rel((mv or {}).get("dst", ""))
            if dst and os.path.exists(os.path.join(root, dst)):
                issues.append(GateIssue("dst_exists",
                                        "第 %d 个动作的目标已存在（覆盖被禁止）：%s" % (i + 1, dst)))

    return GateResult("check_gate", not issues, issues=issues, sec=time.time() - t0,
                      raw={"moves": len(moves), "issues": len(issues)})


# ============================================================
# apply：执行（默认零覆盖，逐条检查）
# ============================================================
def apply_plan(root: str, plan: Dict[str, Any], log: Optional[List[str]] = None) -> Dict[str, Any]:
    root = os.path.abspath(root)
    log = log if log is not None else []
    moved: List[Dict[str, str]] = []
    deleted: List[Dict[str, str]] = []
    created_dirs: List[str] = []
    for mv in plan.get("moves", []):
        src = _rel(mv.get("src", ""))
        dst = _rel(mv.get("dst", ""))
        s_abs = os.path.join(root, src)
        d_abs = os.path.join(root, dst)

        # ★ 删除动作（2026-10-03）：走 safeops —— 先备份、校验、再删。
        #   本函数只应在「已获人工同意」后被调用（服务层 confirm 之后才 apply），
        #   所以这里不再做同意判定，只保证「删之前一定有可校验的备份」。
        _act = str(mv.get("action") or "move").lower()
        if _act in DELETE_ACTIONS or any(f in _act for f in DELETE_ACTIONS):
            if not os.path.exists(s_abs):
                log.append("跳过删除（目标不存在）：%s" % src)
                continue
            try:
                info = safeops.backup_and_delete(root, src, recursive=True)
            except Exception as e:  # noqa: BLE001 —— 删不掉就跳过，绝不「删一半」
                log.append("删除失败（已跳过，原文件保留）：%s —— %s" % (src, e))
                continue
            deleted.append({"src": src, "backup": info.get("backup", ""),
                            "kind": info.get("kind", "")})
            log.append("删除：%s（备份 %s）" % (src, info.get("backup", "")))
            continue

        if not os.path.exists(s_abs):
            log.append("跳过（源不存在）：%s" % src)
            continue
        if os.path.exists(d_abs):
            log.append("跳过（目标已存在，不覆盖）：%s" % dst)
            continue
        d_dir = os.path.dirname(d_abs)
        os.makedirs(d_dir, exist_ok=True)
        rel_dir = os.path.relpath(d_dir, root).replace("\\", "/")
        if rel_dir not in created_dirs and rel_dir not in (".", ""):
            created_dirs.append(rel_dir)
        shutil.move(s_abs, d_abs)          # 同盘 rename，跨盘也能用
        moved.append({"src": src, "dst": dst})
        log.append("移动：%s → %s" % (src, dst))
    return {"moved": moved, "deleted": deleted, "created_dirs": created_dirs, "log": log}


# ============================================================
# verify_gate：复核闸（执行后）—— 计数守恒
# ============================================================
def verify_after(root: str, plan: Dict[str, Any], before: Dict[str, Any],
                 apply_result: Dict[str, Any]) -> GateResult:
    t0 = time.time()
    root = os.path.abspath(root)
    issues: List[GateIssue] = []
    moved = apply_result.get("moved", [])
    deleted = apply_result.get("deleted", [])
    before_names = {f["name"] for f in before.get("files", [])}
    moved_src = {m["src"] for m in moved}
    # 只把「扫描清单里出现过」的删除项计入守恒：扫描本就会跳过 .tmp/.log 等，
    # 删掉这些「本来就不在清单里」的文件是正当的（清垃圾），不该判为丢文件。
    deleted_src = {d["src"] for d in deleted if d["src"] in before_names}
    deleted_outside = {d["src"] for d in deleted} - deleted_src

    # ① 原位置应当只剩「没被移动的」文件
    remain = set()
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if os.path.isfile(full) and name in before_names:
            remain.add(name)
    expected_remain = before_names - moved_src - deleted_src
    if remain != expected_remain:
        missing = sorted(expected_remain - remain)
        extra = sorted(remain - expected_remain)
        if missing:
            issues.append(GateIssue("file_lost", "以下文件既不在原位、也没被移动到目标：%s"
                                    % ", ".join(missing[:8]),
                                    hint="这是最严重的情况，立刻人工检查"))
        if extra:
            issues.append(GateIssue("unexpected_file", "原位置出现了计划外的文件：%s"
                                    % ", ".join(extra[:8])))

    # ② 每个目标目录里，属于本次方案的文件数应当吻合
    want: Dict[str, int] = {}
    for m in moved:
        d = os.path.dirname(m["dst"])
        want[d] = want.get(d, 0) + 1
    for d, n in want.items():
        d_abs = os.path.join(root, d)
        if not os.path.isdir(d_abs):
            issues.append(GateIssue("dst_dir_missing", "方案里说建了目录 %s，实际不存在" % d))
            continue
        got = sum(1 for _ in os.listdir(d_abs))
        if got < n:
            issues.append(GateIssue("dst_count_short", "目录 %s 里只有 %d 个文件，少于方案的 %d 个"
                                    % (d, got, n)))

    # ③ 每个声称已移动的文件，必须真在目标位置（防“移动失败但被当成成功”）
    for m in moved:
        d_abs = os.path.join(root, m["dst"])
        if not os.path.isfile(d_abs):
            issues.append(GateIssue("moved_file_missing",
                                    "方案声称已把 %s 移到 %s，但目标位置没有这个文件"
                                    % (m["src"], m["dst"]),
                                    hint="这是最严重的情况，立刻人工检查"))

    # ④ 计数守恒：原文件数 == 原位剩余 + 已移动
    # ⑥ 删除的必须真有备份（删了但没备份 = 不可恢复，最高优先级告警）
    for d in deleted:
        b_abs = os.path.join(root, str(d.get("backup", "")).replace("/", os.sep))
        if not d.get("backup") or not os.path.exists(b_abs):
            issues.append(GateIssue("delete_without_backup",
                                    "声称删除了 %s，但备份文件不存在（%s）"
                                    % (d.get("src"), d.get("backup")),
                                    hint="这是最严重的情况，立刻人工检查 .ide-backup/"))
    if len(remain) + len(moved) + len(deleted_src) != len(before_names):
        issues.append(GateIssue("count_mismatch",
                                "计数不守恒：原 %d 个，原位剩 %d 个，已移动 %d 个，清单内已删除 %d 个"
                                % (len(before_names), len(remain), len(moved), len(deleted_src))))

    return GateResult("verify_gate", not issues, issues=issues, sec=time.time() - t0,
                      raw={"moved": len(moved), "deleted": len(deleted),
                           "deleted_outside_scan": sorted(deleted_outside),
                           "remain": len(remain),
                           "created_dirs": apply_result.get("created_dirs", [])})


# ============================================================
# 文件查找（确定性，不调模型 —— 这是本地优势最纯粹的一段）
# ============================================================
# 为什么不用模型：找一个文件不需要「智能」，只需要「准」与「不外传」。
# 把硬盘上的文件名清单发给云端模型，本身就是一次数据外流；本地搜完全不外传。
# 模型在这类任务里的正确位置是**解释与汇总**（“我该整理哪些”），不是执行查找。
def find_files(root: str, pattern: Optional[str] = None, content_re: Optional[str] = None,
               exts: Optional[List[str]] = None, max_hits: int = 200,
               max_depth: int = 6, case_sensitive: bool = False) -> Dict[str, Any]:
    """在 root 下按名称/内容查找文件。返回命中清单，始终带 truncated 标志。

    pattern     名称正则（可选）
    content_re  内容正则（可选；只搜文本类后缀）
    exts        限定后缀（如 [".md", ".txt"]）
    """
    import re as _re
    root = os.path.abspath(root)
    flags = 0 if case_sensitive else _re.IGNORECASE
    name_rx = _re.compile(pattern, flags) if pattern else None
    cont_rx = _re.compile(content_re, flags) if content_re else None
    ext_set = {e.lower() if e.startswith(".") else "." + e.lower() for e in (exts or [])}
    text_ext = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".html",
                ".css", ".yml", ".yaml", ".toml", ".ini", ".tsv", ".xml", ".sql"}
    hits: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS]
        if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if ext_set and ext not in ext_set:
                continue
            scanned += 1
            matched = True
            line = 0
            if name_rx is not None and not name_rx.search(name):
                matched = False
            if matched and cont_rx is not None and ext in text_ext:
                matched = False
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        for i, ln in enumerate(f, 1):
                            if cont_rx.search(ln):
                                matched = True
                                line = i
                                break
                except OSError:
                    continue
            if matched:
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                hits.append({"path": os.path.relpath(full, root).replace("\\", "/"),
                             "size": st.st_size, "line": line,
                             "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
                if len(hits) >= int(max_hits):
                    truncated = True
                    return {"root": root, "hits": hits, "scanned": scanned,
                            "truncated": True}
    return {"root": root, "hits": hits, "scanned": scanned, "truncated": truncated}


# ============================================================
# 方案解析：把模型的输出解成 {moves: [...]}
# ============================================================
_MOVE_KEYS = ("moves", "actions", "plan", "operations")


def parse_plan(text_or_obj: Any) -> Optional[Dict[str, Any]]:
    """把模型的方案解成 ``{"moves":[{action,src,dst,reason}]}``；解不出返回 None。

    容忍四种实际形态：合法 JSON 对象 / 裸 JSON 数组 / markdown 围栏 / 单引号 Python 字典。
    """
    from mythos_core.extract import repair_json_text, _braced_objects
    if isinstance(text_or_obj, dict):
        for k in _MOVE_KEYS:
            if isinstance(text_or_obj.get(k), list):
                return {"moves": text_or_obj[k], "note": text_or_obj.get("note", "")}
        if isinstance(text_or_obj.get("moves"), list):
            return text_or_obj
        return None
    if not isinstance(text_or_obj, str):
        return None
    text = text_or_obj.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # 先按整段解（可能是对象或数组）
    obj = repair_json_text(text)
    if isinstance(obj, list):
        return {"moves": obj}
    if isinstance(obj, dict):
        for k in _MOVE_KEYS:
            if isinstance(obj.get(k), list):
                return {"moves": obj[k], "note": obj.get("note", "")}
    # 退一步：按花括号配平扫出每个对象
    out: List[Dict[str, Any]] = []
    for chunk in _braced_objects(text):
        o = repair_json_text(chunk)
        if isinstance(o, dict) and ("src" in o or "action" in o):
            out.append(o)
        elif isinstance(o, dict):
            for k in _MOVE_KEYS:
                if isinstance(o.get(k), list):
                    out.extend(o[k])
    if out:
        return {"moves": out}
    return None


def normalize_move(mv: Dict[str, Any]) -> Dict[str, str]:
    """把一条动作归一成 {action, src, dst, reason}（容忍字段别名）。"""
    if not isinstance(mv, dict):
        return {"action": "?", "src": "", "dst": ""}
    act = str(mv.get("action") or mv.get("op") or mv.get("type") or "move").lower()
    src = str(mv.get("src") or mv.get("source") or mv.get("from") or mv.get("path") or "")
    dst = str(mv.get("dst") or mv.get("destination") or mv.get("to") or mv.get("target") or "")
    return {"action": act, "src": src, "dst": dst,
            "reason": str(mv.get("reason") or mv.get("why") or "")[:120]}


def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    moves = [normalize_move(m) for m in (plan.get("moves") or [])]
    return {"moves": moves, "note": plan.get("note", "")}

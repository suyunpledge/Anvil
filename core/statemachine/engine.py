# -*- coding: utf-8 -*-
"""engine.py —— 最小可跑的状态机内核（框架驱动，v1）。

一句话形状：

    工单 → [读] → [改] → [语法 gate] → [类型 gate] → [测试 gate] → 原子落地
              ↑_________________ 失败回退（带 gate 原文反馈）_________________|

设计口径（对应文档 `docs/工单状态机-契约-v1.md`）：
  · **模型只填槽**：两次模型调用（读、改），都通过 ``adapter.fill_slot``，
    框架组装全部上下文，模型不负责规划、不负责判断「完成了吗」（R7 / R8）。
  · **gate 是唯一真相源**：模型在正文里说「已完成」不产生任何效果，
    只有三个 gate 全过才提交（R8）。变更先在**暂存区**做完、验完，才原子替换回真实文件。
  · **框架兜底一切结构性风险**：节点工具集（R1）、危险参数（R2）、
    写范围（只允许目标文件）、轮次预算、失败归类（R12）。
  · 引擎对工具调用做**第二次**校验：即使适配层被绕过（或换了适配器），
    未授权工具、危险参数、越界写入在引擎层仍被拒。

只针对「单文件 Python 修改 + 测试通过」写死；跑通后再抽象。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
_COMPAT = os.path.join(_PROJECT, "compatibility")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _COMPAT not in sys.path:
    sys.path.insert(0, _COMPAT)

from contract import (  # noqa: E402
    DEFAULT_MAX_REPAIR_ROUNDS, FALLBACK_RULES, MAX_BEST_OF_N, MAX_EDIT_TURNS,
    MAX_RECON_TURNS, MODEL_NODES, NODE_CHAIN, NODE_KIND, NODE_LABEL_ZH,
    NODE_TOOLSETS, REPAIR_NODE_KIND, STATUS_DONE, STATUS_ESCALATED, STATUS_FAILED,
    STATUS_NEEDS_INPUT, STATUS_RUNNING, STATUS_SECURITY_ABORT, Step, VIOLATION_KILL_AFTER,
    WRITE_TOOLS, WorkOrder,
)
from gates import run_gate, test_failure_signature  # noqa: E402
from tools import (REGISTRY, ToolContext, ToolError, VersionConflict, content_version,
                   diff_stats, diff_text, execute, result_to_text)  # noqa: E402

# ---- 安全层：优先复用兼容层的单一真相源；独立运行时用本地兜底（不静默降级） ----
try:
    from mythos_core.errors import SchemaViolation, UnsafeArgument  # type: ignore
    from mythos_core.params import normalize_args  # type: ignore
    from mythos_core.rules import check_safety  # type: ignore
    SAFETY_SOURCE = "mythos_core（与兼容层共用同一套规则）"
except Exception:  # pragma: no cover - 仅在脱离兼容层单独运行时走到
    import re as _re

    class UnsafeArgument(Exception):
        pass

    class SchemaViolation(Exception):
        pass

    _FALLBACK_PATTERNS = [
        (r"\brm\s+-[a-zA-Z]*[rf]", "递归/强制删除"),
        (r"\bdel\s+/[sfq]", "强制删除"),
        (r"Remove-Item.*-Recurse", "PowerShell 递归删除"),
        (r"\bformat\s+[a-zA-Z]:", "格式化盘"),
        (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
        (r"\bgit\s+reset\s+--hard", "硬回退"),
        (r"curl[^|]*\|\s*(ba)?sh", "管道执行远端脚本"),
    ]
    _COMPILED = [(_re.compile(p, _re.IGNORECASE), l) for p, l in _FALLBACK_PATTERNS]

    def check_safety(tool_name: str, args: Dict[str, Any]) -> None:
        if tool_name != "run_shell":
            return
        blob = json.dumps(args, ensure_ascii=False, default=str)
        for rx, label in _COMPILED:
            if rx.search(blob):
                raise UnsafeArgument("工具 %s 命中危险模式【%s】" % (tool_name, label))

    def normalize_args(raw: Any, schema: Dict[str, Any]) -> Dict[str, Any]:
        return raw if isinstance(raw, dict) else {}

    SAFETY_SOURCE = "engine 内置兜底（未找到 mythos_core）"


class Terminal(Exception):
    """终止整单的控制流（终态 + 原因）。"""

    def __init__(self, status: str, message: str = "", questions: Optional[List[str]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.questions = questions or []


@dataclass
class RunResult:
    status: str
    wo: WorkOrder
    diff: str = ""
    staging: str = ""
    committed_to: str = ""
    error: str = ""
    runlog_path: str = ""
    events: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "wo": self.wo.to_dict(), "diff": self.diff,
                "staging": self.staging, "committed_to": self.committed_to,
                "error": self.error, "runlog_path": self.runlog_path}


class WorkOrderStateMachine:
    """v1 内核：固定五节点链 + 回退。只认「单文件 Python 修改 + 测试通过」。"""

    def __init__(
        self,
        adapter: Any,
        wo: WorkOrder,
        staging_root: Optional[str] = None,
        best_of_n: int = 1,
        commit: bool = True,
        runlog_dir: Optional[str] = None,
        verbose: bool = True,
        on_event: Optional[Callable[[str], None]] = None,
        initial_version: Optional[str] = None,
        context_provider: Optional[Callable[[str, str], Dict[str, Any]]] = None,
        _test_hook_before_commit: Optional[Callable[[], None]] = None,
    ) -> None:
        self.ad = adapter
        self.wo = wo
        self.staging_root = staging_root or os.path.join(_PROJECT, ".sm-work")
        self.best_of_n = max(1, min(int(best_of_n or 1), MAX_BEST_OF_N))
        self.commit_enabled = commit
        self.runlog_dir = runlog_dir or os.path.join(_PROJECT, ".sm-runs")
        self.verbose = verbose
        self.on_event = on_event
        #: ★ 工单**开始之前**目标文件的版本（外部审查 #3）：
        #:   模型跑期间如果用户改了文件，必须能发现，而不是把新版本当基线再覆盖回去。
        self.initial_version = initial_version
        #: ★ 上下文层（外部审查 #6）：可选注入。给了就用它组装改节点的上下文，
        #:   而不是把目标文件全文塞给模型。默认 None = 保持旧行为（全量）。
        self.context_provider = context_provider
        self.context_info: Dict[str, Any] = {}
        #: 仅测试用：在 _commit 之前回调（模拟“落盘前文件被改”），不影响正常行为
        self._test_hook_before_commit = _test_hook_before_commit
        self.events: List[str] = []
        self.staging = ""
        self._violations_in_node: Dict[str, int] = {}
        self._escalated_for: Dict[str, bool] = {}
        self._failure_sigs: Dict[str, int] = {}
        self._gate_fails: Dict[str, int] = {}          # 每个 gate 连续失败了几次
        self._dup_patch: Dict[str, int] = {}           # 同一份补丁被重复提交的次数
        self._seen_version: Dict[str, str] = {}        # 模型最后看到的文件版本（乐观并发）
        self._last_error = ""

    # ---------------- 基础设施 ----------------
    def _log(self, msg: str) -> None:
        self.events.append(msg)
        if self.verbose:
            print("[sm] " + msg, flush=True)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def _step(self, node: str, turn: int, kind: str, ok: bool, detail: str = "",
              tool: str = "", sec: Optional[float] = None) -> None:
        self.wo.add_step(Step(node=node, round_no=self.wo.round_no, turn=turn,
                              kind=kind, ok=ok, detail=detail, tool=tool, sec=sec))

    def _stage(self) -> str:
        """把沙箱目录整份拷进暂存区；改与验都在暂存区完成，真实文件最后才被替换。

        ★ 两个坑（外部审查 #7 / #3）：
          ① 若工作目录就是本项目根，而暂存区在项目内的 ``.runtime/staging``，
             那目标目录在源目录**内部** → `copytree` 会递归地把暂存区自己拷进去。
             处理：一旦发现这种嵌套，就把暂存区改到系统临时目录，并把它加进忽略表。
          ② 工单开始后目标文件被旁路改过 → 在这里就报错，而不是拿新版当基线。
        """
        src = os.path.abspath(self.wo.workdir)
        if not os.path.isdir(src):
            raise Terminal(STATUS_FAILED, "沙箱目录不存在：%s" % src)
        target_abs = os.path.join(src, self.wo.target.replace("\\", "/"))
        if not os.path.isfile(target_abs):
            raise Terminal(STATUS_FAILED, "目标文件不存在：%s" % self.wo.target)

        # ---- ① 防嵌套 ----
        staging_root = os.path.abspath(self.staging_root)
        nested = (staging_root == src or staging_root.startswith(src + os.sep)
                  or src == staging_root or src.startswith(staging_root + os.sep))
        if nested:
            import tempfile as _tf
            staging_root = os.path.join(_tf.gettempdir(), "local-ide-staging")
            self._log("暂存区与工作目录嵌套，已改到：%s" % staging_root)

        # ---- ② 版本基线（工单开始前就记下） ----
        if self.initial_version:
            try:
                import tools as _tools
                now_v = _tools.content_version(target_abs)
            except Exception:
                now_v = ""
            if now_v and now_v != self.initial_version:
                raise Terminal(
                    STATUS_FAILED,
                    "目标文件在工单开始后已被修改（%s → %s），拒绝以新版本为基线继续；"
                    "请重新建单。" % (self.initial_version[7:15], now_v[7:15]))

        os.makedirs(staging_root, exist_ok=True)
        dst = os.path.join(staging_root, self.wo.wo_id)
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
            "__pycache__", ".git", "*.pyc", ".sm-work", ".sm-runs",
            ".runtime", "node_modules", ".venv"))
        self.staging_root = staging_root
        # 留一份原始文件副本，供事后回滚/对照
        with open(target_abs, "r", encoding="utf-8") as f:
            original = f.read()
        with open(os.path.join(dst, ".sm_original_target.py"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(original)
        self.wo.artifacts["original_sha_hint"] = str(len(original))
        self.staging = dst
        self._log("暂存区就绪：%s" % dst)
        return dst

    def _ctx(self) -> ToolContext:
        return ToolContext(root=self.staging, target=self.wo.target,
                           write_scope=self.wo.budget("write_scope", "target_only"),
                           timeout_sec=int(self.wo.constraints.get("timeout_sec", 180)))

    # ---------------- 引擎级工具执行（第二道闸） ----------------
    def _execute_call(self, ctx: ToolContext, call: Dict[str, Any], node: str,
                      turn: int) -> Dict[str, Any]:
        name = str(call.get("name") or "")
        args = call.get("args") or {}
        allowed = set(NODE_TOOLSETS.get(node, []))
        if name not in allowed:
            self.wo.add_violation(node, "R1", "未授权工具：%s（本节点只允许 %s）"
                                  % (name, sorted(allowed)))
            n = self._violations_in_node.get(node, 0) + 1
            self._violations_in_node[node] = n
            self._step(node, turn, "violation", False, "未授权工具 %s" % name, tool=name)
            if n >= VIOLATION_KILL_AFTER:
                raise Terminal(STATUS_SECURITY_ABORT,
                               "节点 %s 反复请求未授权工具 %s（R1）" % (node, name))
            raise ToolError("工具 %s 在本节点未授权；已拒绝执行" % name)

        schema = REGISTRY.get(name)
        if schema is None:
            raise ToolError("未知工具：%s" % name)
        try:
            args = normalize_args(args, schema)          # R9
            check_safety(name, args)                     # R2
        except UnsafeArgument as e:
            self.wo.add_violation(node, "R2", str(e))
            self._step(node, turn, "unsafe_argument", False, str(e), tool=name)
            raise Terminal(STATUS_SECURITY_ABORT, "危险参数被判死（R2）：%s" % e)
        except SchemaViolation as e:
            self._step(node, turn, "schema_violation", False, "%s: %s" % (name, e), tool=name)
            raise ToolError("参数不合 schema：%s" % e)

        # ★ 乐观并发：写类工具自动带上「模型最后看到的版本」。
        #   这是**框架侧**注入，不靠模型记得传——模型的活儿已经够多了。
        #   文件在“模型看到”与“落盘”之间被改过 → 显式失败，而不是静默覆盖。
        if name in WRITE_TOOLS:
            rel = str(args.get("path") or self.wo.target)
            iv = self._seen_version.get(rel)
            if iv and not args.get("expected_version"):
                args["expected_version"] = iv

        res = execute(ctx, name, args)
        ctx.note(name, args, True)
        self._step(node, turn, "tool", True, result_to_text(res)[:200], tool=name,
                   sec=res.get("sec"))
        # 记下“模型看到的版本”：下一次写这个文件时用作乐观并发断言。
        # 依据：对标 chat-ollama（2026-09-28）—— 只靠 old_string 唯一匹配会在
        # “看到了” 与 “落盘” 之间被旁路修改时静默覆盖。
        if isinstance(res, dict) and res.get("version") and res.get("path"):
            self._seen_version[str(res["path"])] = str(res["version"])
        try:                                             # R11：工具层结果回写画像
            self.ad.record(node=node, tool_name=name, ok=True, sec=res.get("sec"))
        except Exception:
            pass
        return res

    def _record_gate(self, gate: str, ok: bool, sec: float) -> None:
        try:
            self.ad.record(node=gate, tool_name="gate", ok=ok, sec=sec)
        except Exception:
            pass

    # ---------------- 消息组装 ----------------
    @staticmethod
    def _sys_recon() -> str:
        return (
            "你是一个只读侦察节点（read-only recon）。框架已经知道目标文件，"
            "你可用工具：read_file / list_dir / search_code。\n"
            "任务：确认目标文件当前内容与相关符号，结论简短（≤120 字）。\n"
            "规则：\n"
            "1) 需要看文件就用工具读，不要凭空猜测；\n"
            "2) 最多读 3 个文件；\n"
            "3) 若已经有足够信息，直接输出结论、不要调用任何工具（空调用是合法的）；\n"
            "4) 你没有写权限，不要尝试修改任何文件。"
        )

    def _sys_edit(self, target: str, repair: bool) -> str:
        head = "你是一个受控编辑节点（edit）。"
        if repair:
            head += "当前处于修复轮：上一次改动没有通过 gate，下面会给出 gate 的原文反馈。"
        # 模型画像可额外挂一段提示（依据实测的脾气）：例如 qwen2.5-coder 不会算行号，
        # 就把它拨回「按原文替换」的写法（依据 S8）。
        hint = getattr(getattr(self.ad, "spec", None), "edit_prompt_hint", "") or ""
        text = (
            head + "\n"
            "唯一允许的修改方式：调用 apply_patch 工具，对 %s 做**最小改动**。\n"
            "apply_patch 的两种写法（二选一）：\n"
            "  A) old_string（要被替换的原文，必须与文件中文本逐字一致、在文件中唯一）"
            "+ new_string（替换后的文本）；\n"
            "  B) start_line + end_line（1 起、含端点）+ new_code（替换后的整段代码）。\n"
            "硬规则：\n"
            "1) 只能改 %s 这一个文件，不能改测试文件；\n"
            "2) 禁止整文件重写，禁止顺手重构无关代码；\n"
            "3) 新增或修改的函数必须有完整的参数与返回值类型注解（类型 gate 会检查）；\n"
            "4) 改动要能通过测试；不确定就先 read_file 看清楚再改；\n"
            "5) 不要说「已完成」这类话——完成与否由 gate 判定，你只需产出补丁；\n"
            "6) 改动不能留下不可达代码：旧实现要真删掉，而不是把它留在 return 后面。"
            % (target, target)
        )
        return text + (("\n" + hint) if hint else "")

    def _user_recon(self, ctx: ToolContext) -> str:
        target_src = ""
        listing = ""
        try:
            target_src = execute(ctx, "read_file", {"path": self.wo.target})["text"]
        except Exception as e:
            target_src = "（框架读取失败：%s）" % e
        try:
            listing = execute(ctx, "list_dir", {"path": "."})["listing"]
        except Exception as e:
            listing = "（列表失败：%s）" % e
        return ("# 任务\n%s\n\n# 沙箱目录（暂存区）\n%s\n\n# 目标文件 %s 的当前内容\n%s\n\n"
                "# 你的任务\n确认上面的内容，指出要实现的目标函数应该放在哪里。"
                "信息够就直接给结论，不要调用工具。"
                % (self.wo.task, listing, self.wo.target, target_src))

    def _user_edit(self, ctx: ToolContext, recon: Dict[str, Any], feedback: str) -> str:
        parts = ["# 任务\n%s" % self.wo.task,
                 "# 目标文件（相对沙箱根）：%s" % self.wo.target]
        # ★ 上下文层（外部审查 #6）：能注入就用预算裁剪过的上下文，
        #   而不是把目标文件全文塞进 prompt；失败就回退全文，不阻断工单。
        ctx_text = ""
        if self.context_provider is not None:
            try:
                pack = self.context_provider(self.wo.task, self.wo.target) or {}
                self.context_info = {k: v for k, v in pack.items() if k != "text"}
                ctx_text = str(pack.get("text") or "")
            except Exception as e:
                self._log("上下文层不可用，回退全文：%s" % e)
                self.context_info = {"error": str(e)[:200]}
                ctx_text = ""

        # ★ 只有真省才用它（2026-09-28 端到端跑暴露）：
        #   小文件上「裁剪」反而是净负担——实测同一道题，裁剪后 221 token vs 整文件 106 token，
        #   而且模型因为看不到完整文件布局而把函数删错位。
        #   所以这里的口径是：**拿不到明显收益就老老实实给全文**。
        full_text = str(recon.get("target_source") or "")
        if ctx_text and len(ctx_text) >= len(full_text) * 0.9:
            self.context_info["skipped"] = "裁剪后不比全文小（%d vs %d 字符），已回退全文" % (
                len(ctx_text), len(full_text))
            ctx_text = ""
            self._log("上下文层未带来收益，回退全文")

        if ctx_text:
            parts.append("# 已裁剪的代码上下文（框架按预算注入，比全文小）\n%s" % ctx_text)
        else:
            parts.append("# 目标文件当前内容\n%s" % recon.get("target_source", ""))
        if recon.get("notes"):
            parts.append("# 侦察结论\n%s" % recon["notes"])
        if recon.get("search_hits"):
            parts.append("# 相关引用\n%s" % recon["search_hits"])
        if feedback:
            parts.append("# 上一次尝试的失败反馈（来自确定性 gate，必须优先解决）\n%s" % feedback)
        parts.append("# 要求\n现在调用 apply_patch，只改 %s，给出最小补丁。" % self.wo.target)
        return "\n\n".join(parts)

    def _node_turn_budget(self, node: str, default: int) -> int:
        """每模型可覆写节点轮次预算（画像字段；依据实测的「耐心」）。"""
        spec = getattr(self.ad, "spec", None)
        key = {"recon": "recon_max_turns", "edit": "edit_max_turns"}.get(node)
        v = getattr(spec, key, None) if (spec is not None and key) else None
        try:
            return max(1, int(v)) if v else default
        except (TypeError, ValueError):
            return default

    # ---------------- 读节点 ----------------
    def _node_recon(self, ctx: ToolContext, refresh: bool = False) -> Dict[str, Any]:
        self.wo.node = "recon"
        tag = "（重新侦察）" if refresh else ""
        self._log("读节点%s：开始" % tag)
        bundle: Dict[str, Any] = {"notes": "", "search_hits": "", "turns": 0}
        try:
            fresh = execute(ctx, "read_file", {"path": self.wo.target})
            bundle["target_source"] = fresh["text"]
            if fresh.get("version"):
                self._seen_version[self.wo.target] = str(fresh["version"])
        except Exception as e:
            raise Terminal(STATUS_FAILED, "读不到目标文件：%s" % e)

        messages = [{"role": "system", "content": self._sys_recon()},
                    {"role": "user", "content": self._user_recon(ctx)}]
        for turn in range(1, self._node_turn_budget("recon", MAX_RECON_TURNS) + 1):
            bundle["turns"] = turn
            r = self.ad.fill_slot(node="recon", messages=messages, registry=REGISTRY,
                                  node_kind=NODE_KIND["recon"], node_type="recon",
                                  n=self.best_of_n)
            self._step("recon", turn, r.kind, r.ok, (r.error or r.content or "")[:200], sec=r.sec)
            if r.kind == "tool_call":
                messages.append({"role": "assistant", "content": r.content or "",
                                 "tool_calls": [{"function": {"name": c["name"],
                                                              "arguments": c["args"]}}
                                                for c in r.calls]})
                for c in r.calls:
                    if c["name"] == "apply_patch":
                        raise Terminal(STATUS_SECURITY_ABORT, "读节点试图写入（R1）")
                    try:
                        res = self._execute_call(ctx, c, "recon", turn)
                        messages.append({"role": "tool", "name": c["name"],
                                         "content": result_to_text(res)[:4000]})
                        if c["name"] == "search_code":
                            bundle["search_hits"] = (bundle.get("search_hits") or
                                                     "") + result_to_text(res)[:1500] + "\n"
                    except ToolError as e:
                        messages.append({"role": "tool", "name": c["name"],
                                         "content": "工具失败：%s" % e})
                continue
            if r.kind == "no_call":
                bundle["notes"] = (r.content or "").strip()
                break
            if r.kind == "schema_violation":
                messages.append({"role": "user", "content":
                                 "上一步工具调用不合法（%s）。请重新给出合法调用，或用纯文本给结论。"
                                 % (r.error or "")})
                continue
            if r.kind == "unauthorized_tool":
                self.wo.add_violation("recon", "R1", r.error or "越权工具")
                n = self._violations_in_node.get("recon", 0) + 1
                self._violations_in_node["recon"] = n
                if n >= VIOLATION_KILL_AFTER:
                    raise Terminal(STATUS_SECURITY_ABORT, "读节点反复越权（R1）：%s" % r.error)
                messages.append({"role": "user", "content":
                                 "该工具在本节点未授权（%s）。只能用 read_file / list_dir / "
                                 "search_code。" % (r.error or "")})
                continue
            if r.kind == "unsafe_argument":
                self.wo.add_violation("recon", "R2", r.error or "")
                raise Terminal(STATUS_SECURITY_ABORT, "危险参数被判死（R2）：%s" % r.error)
            if r.kind in ("context_overflow",):
                raise Terminal(STATUS_FAILED, "读节点上下文超限：需要走检索精简（R5）")
            if r.kind in ("transport_error", "exhausted"):
                raise Terminal(STATUS_FAILED, "读节点失败（%s）：%s" % (r.kind, r.error or ""))
        self._log("读节点完成：%d 轮，结论 %s" % (bundle["turns"], (bundle["notes"] or "（无）")[:60]))
        return bundle

    # ---------------- 改节点 ----------------
    def _node_edit(self, ctx: ToolContext, recon: Dict[str, Any], feedback: str) -> Dict[str, Any]:
        self.wo.node = "edit"
        repair = bool(feedback)
        kind = REPAIR_NODE_KIND if repair else NODE_KIND["edit"]
        self._log("改节点：开始（%s）" % ("修复轮" if repair else "首轮"))
        recon = dict(recon)
        try:  # 每次进入改节点都刷新一次源码，避免修复轮拿着过期原文出补丁
            fresh = execute(ctx, "read_file", {"path": self.wo.target})
            recon["target_source"] = fresh["text"]
            if fresh.get("version"):
                self._seen_version[self.wo.target] = str(fresh["version"])
        except Exception:
            pass
        messages = [{"role": "system", "content": self._sys_edit(self.wo.target, repair)},
                    {"role": "user", "content": self._user_edit(ctx, recon, feedback)}]
        max_turns = self._node_turn_budget("edit", MAX_EDIT_TURNS)
        for turn in range(1, max_turns + 1):
            r = self.ad.fill_slot(node="edit", messages=messages, registry=REGISTRY,
                                  node_kind=kind, node_type="edit", n=self.best_of_n)
            self._step("edit", turn, r.kind, r.ok, (r.error or r.content or "")[:200], sec=r.sec)
            if r.kind == "tool_call":
                writes = [c for c in r.calls if c["name"] in WRITE_TOOLS]
                reads = [c for c in r.calls if c["name"] not in WRITE_TOOLS]
                for c in reads:      # 依据 E7：首轮常常只做第一步（读）
                    try:
                        res = self._execute_call(ctx, c, "edit", turn)
                        messages.append({"role": "tool", "name": c["name"],
                                         "content": result_to_text(res)[:4000]})
                    except ToolError as e:
                        messages.append({"role": "tool", "name": c["name"], "content": "工具失败：%s" % e})
                if writes:
                    if len(writes) > 1:
                        self.wo.add_violation("edit", "single_write_per_turn",
                                              "一轮内 %d 次写调用，只执行第一次" % len(writes))
                        writes = writes[:1]
                    c = writes[0]
                    if len(reads) and c["name"] == "apply_patch":
                        pass
                    try:
                        res = self._execute_call(ctx, c, "edit", turn)
                        self._log("改节点：补丁已应用（%s，+%d/-%d）"
                                  % (res.get("mode"), res.get("added", 0), res.get("removed", 0)))
                        return res
                    except ToolError as e:
                        messages.append({"role": "user", "content":
                                         "补丁没有应用成功：%s\n请先 read_file 看清原文，再给出正确补丁。"
                                         % e})
                        if isinstance(e, VersionConflict):
                            self.wo.add_violation("edit", "optimistic_concurrency", str(e))
                        # 同一份补丁反复提交（实测 qwen2.5-coder 会这样）：不再浪费轮次
                        sig = json.dumps([c["name"], c.get("args")], sort_keys=True,
                                         ensure_ascii=False, default=str)
                        self._dup_patch[sig] = self._dup_patch.get(sig, 0) + 1
                        if self._dup_patch[sig] >= 2:
                            raise Terminal(STATUS_ESCALATED,
                                           "模型重复提交同一份无法应用的补丁（已中止，降级人审）")
                        continue
                messages.append({"role": "user", "content":
                                 "你只读取了内容但还没有产出补丁。现在请调用 apply_patch 给出最小改动。"})
                continue
            if r.kind == "no_call":
                text = (r.content or "").strip()
                if text:
                    raise Terminal(STATUS_NEEDS_INPUT,
                                   "改节点没有产出补丁，而是给出了文本（需要人补充信息）",
                                   questions=[text])
                messages.append({"role": "user", "content":
                                 "你没有调用任何工具。请调用 apply_patch 给出最小改动。"})
                continue
            if r.kind == "schema_violation":
                messages.append({"role": "user", "content":
                                 "上一次调用不合法（%s）。请按 apply_patch 的参数要求重新给出调用。"
                                 % (r.error or "")})
                continue
            if r.kind == "unauthorized_tool":
                self.wo.add_violation("edit", "R1", r.error or "越权工具")
                n = self._violations_in_node.get("edit", 0) + 1
                self._violations_in_node["edit"] = n
                if n >= VIOLATION_KILL_AFTER:
                    raise Terminal(STATUS_SECURITY_ABORT, "改节点反复越权（R1）：%s" % r.error)
                messages.append({"role": "user", "content":
                                 "该工具在本节点未授权（%s）。编辑节点只允许 apply_patch 做最小改动，"
                                 "不允许整文件重写。" % (r.error or "")})
                continue
            if r.kind == "unsafe_argument":
                self.wo.add_violation("edit", "R2", r.error or "")
                raise Terminal(STATUS_SECURITY_ABORT, "危险参数被判死（R2）：%s" % r.error)
            if r.kind == "context_overflow":
                raise Terminal(STATUS_FAILED, "改节点上下文超限：需要走检索精简（R5）")
            if r.kind in ("transport_error", "exhausted"):
                raise Terminal(STATUS_FAILED, "改节点失败（%s）：%s" % (r.kind, r.error or ""))
        raise Terminal(STATUS_ESCALATED if repair else STATUS_NEEDS_INPUT,
                       "改节点在 %d 轮内没有产出可用补丁" % max_turns)

    # ---------------- 主流程 ----------------
    def run(self) -> RunResult:
        self.wo.status = STATUS_RUNNING
        self._log("工单 %s 开始：%s" % (self.wo.wo_id, self.wo.task))
        t0 = time.time()
        try:
            self._stage()
            ctx = self._ctx()
            recon = self._node_recon(ctx)
            feedback = ""
            while True:
                patch = self._node_edit(ctx, recon, feedback)
                self.wo.artifacts.setdefault("patches", []).append(
                    {"round": self.wo.round_no,
                     **{k: v for k, v in patch.items() if k != "diff"},
                     "diff": patch.get("diff", "")})
                self.wo.artifacts["diff"] = patch.get("diff", "")   # 最后一轮补丁的 diff

                failed: Optional[Any] = None
                for gname in ("gate_syntax", "gate_type", "gate_test"):
                    self.wo.node = gname
                    g = run_gate(gname, ctx, test_path=self.wo.test_path,
                                 test_cmd=self.wo.test_cmd)
                    self.wo.add_gate(g)
                    self._record_gate(gname, g.ok, g.sec)   # R11
                    self._step(gname, 0, "gate", g.ok, g.signal)
                    self._log("%s → %s（%.2fs）" % (NODE_LABEL_ZH[gname], g.signal, g.sec))
                    if not g.ok:
                        failed = g
                        break
                if failed is None:
                    break

                rule = FALLBACK_RULES[failed.gate]
                self.wo.round_no += 1
                max_rounds = int(self.wo.budget("max_repair_rounds", DEFAULT_MAX_REPAIR_ROUNDS))
                if self.wo.round_no > max_rounds:
                    raise Terminal(STATUS_ESCALATED,
                                   "修复轮用尽（%d 轮）最后失败在 %s：降级人审"
                                   % (max_rounds, failed.gate))
                feedback = failed.feedback_text()
                # ★ 回退表里写的「同一 gate 连续失败 escalate_after 次 → 先重跑读节点」
                #   三个 gate 都要生效（原来只对 gate_test 的失败签名做了）。
                gname = failed.gate
                self._gate_fails[gname] = self._gate_fails.get(gname, 0) + 1
                if failed.gate == "gate_test":
                    sig = test_failure_signature(failed)
                    self._failure_sigs[sig] = self._failure_sigs.get(sig, 0) + 1
                    if self._failure_sigs[sig] >= rule["escalate_after"]:
                        self._gate_fails[gname] = max(self._gate_fails[gname], rule["escalate_after"])
                if (self._gate_fails[gname] >= rule.get("escalate_after", 99)
                        and not self._escalated_for.get(gname)):
                    self._escalated_for[gname] = True
                    self._log("%s 已连续失败 %d 次 → 先重跑读节点刷新上下文"
                              % (gname, self._gate_fails[gname]))
                    recon = self._node_recon(ctx, refresh=True)
                self._log("回退到改节点（第 %d 轮修复），反馈来源：%s" % (self.wo.round_no, failed.gate))

            self.wo.node = "commit"
            committed = self._commit(ctx)
            self.wo.status = STATUS_DONE
            self._log("工单完成：三个 gate 全过，变更已%s" % ("落地到 " + committed if committed else "验证（未提交）"))
            return self._finish(RunResult(STATUS_DONE, self.wo,
                                          diff=self.wo.artifacts.get("diff", ""),
                                          staging=self.staging,
                                          committed_to=committed,
                                          events=list(self.events)), t0)
        except Terminal as t:
            self.wo.status = t.status
            if t.questions:
                self.wo.questions.extend(t.questions)
            self._log("工单终止：%s —— %s" % (t.status, t.message))
            return self._finish(RunResult(t.status, self.wo, error=t.message,
                                          diff=self.wo.artifacts.get("diff", ""),
                                          staging=self.staging,
                                          events=list(self.events)), t0)

    def _commit(self, ctx: ToolContext) -> str:
        """把暂存区里通过 gate 的目标文件原子替换回真实沙箱目录。

        ★ 外部审查 #3 的最后一处（2026-09-29 自查补齐）：
          免确认（autoApply）路径从 `_stage` 到这里没有再比对过版本。
          若模型运行期间真实文件被旁路改过，这里会把它静默覆盖。
          人确认路径在 runner 里已有双重比对；这条是引擎自身的最后一道门。
        """
        if not self.commit_enabled:
            self._log("（--no-commit 模式：不落盘）")
            return ""
        if self._test_hook_before_commit is not None:
            try:
                self._test_hook_before_commit()
            except Exception:
                pass
        staged = ctx.resolve(self.wo.target)
        real = os.path.join(os.path.abspath(self.wo.workdir), self.wo.target.replace("\\", "/"))
        # 落盘前终检：真实文件必须仍与工单开始时同版本
        if self.initial_version:
            try:
                import tools as _tools
                now_v = _tools.content_version(real) if os.path.exists(real) else ""
            except Exception:
                now_v = ""
            if now_v and now_v != self.initial_version:
                raise Terminal(
                    STATUS_FAILED,
                    "目标文件在工单运行期间被修改（%s → %s），拒绝覆盖；请重新建单。"
                    % (self.initial_version[7:15], now_v[7:15]))
        os.makedirs(os.path.dirname(real), exist_ok=True)
        tmp = real + ".sm-commit"
        shutil.copyfile(staged, tmp)
        os.replace(tmp, real)
        return real

    def _finish(self, res: RunResult, t0: float) -> RunResult:
        res.wo.artifacts["elapsed_sec"] = round(time.time() - t0, 2)
        res.wo.artifacts["safety_source"] = SAFETY_SOURCE
        if self.context_info:
            res.wo.artifacts["context"] = self.context_info
        # 累计变更（原文件 → 当前文件）：这才是人看的「这次到底改了什么」
        try:
            orig = open(os.path.join(self.staging, ".sm_original_target.py"),
                        "r", encoding="utf-8").read()
            cur = open(os.path.join(self.staging, self.wo.target.replace("\\", "/")),
                       "r", encoding="utf-8").read()
            fd = diff_text(orig, cur, "原/" + self.wo.target, "新/" + self.wo.target)
            res.wo.artifacts["final_diff"] = fd
            res.wo.artifacts["final_stats"] = diff_stats(fd)
            res.diff = fd
        except Exception:
            pass
        try:
            os.makedirs(self.runlog_dir, exist_ok=True)
            p = os.path.join(self.runlog_dir, "%s.json" % self.wo.wo_id)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
            res.runlog_path = p
        except Exception:
            pass
        return res


# ============================================================
# 契约一致性自检（引擎的工具集必须与兼容层一致，否则拒跑）
# ============================================================
def check_contract_sync() -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "detail": []}
    try:
        from mythos_core.rules import TOOLSETS as AD_TOOLSETS  # type: ignore
    except Exception as e:
        out["ok"] = False
        out["detail"].append("无法导入 mythos_core.rules：%s" % e)
        return out
    for node in ("recon", "edit"):
        mine, theirs = set(NODE_TOOLSETS[node]), set(AD_TOOLSETS.get(node, []))
        if mine != theirs:
            out["ok"] = False
            out["detail"].append("%s 工具集不一致：engine=%s adapter=%s"
                                 % (node, sorted(mine), sorted(theirs)))
        else:
            out["detail"].append("%s 工具集一致：%s" % (node, sorted(mine)))
    if "write_file" in NODE_TOOLSETS["edit"]:
        out["ok"] = False
        out["detail"].append("★ edit 节点出现 write_file（违反 E15）")
    if "run_shell" in NODE_TOOLSETS["edit"] or "run_shell" in NODE_TOOLSETS["recon"]:
        out["ok"] = False
        out["detail"].append("★ 只读/编辑节点出现 run_shell（违反 E9）")
    return out

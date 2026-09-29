# -*- coding: utf-8 -*-
"""runner.py —— 工单运行器（交付件 B 的内核，与 HTTP 解耦）。

把「跑一个工单」这件事从 HTTP 里拆出来，是为了三件事：

  1. 能离线单测（用假适配器跑，不调模型、不花额度）；
  2. 能换入口（CLI / HTTP / 将来的其它前端都调同一份逻辑）；
  3. **落盘必须经人确认**——这是 IDE 一期五条验收里唯一涉及"不可逆"的一条，
     所以它必须是运行器层面的机制，不能只靠前端自觉。
     做法：状态机以「不落盘」模式跑（code 类 `commit=False`），
     结果留在暂存目录里；收到 `confirm` 才把暂存内容复制回真实文件。

两类工单：
  · code  —— 五节点链（读 → 改 → 语法 → 类型 → 测试 gate），改动在暂存区
  · tidy  —— 整理链（扫 → 规划 → 方案闸 → 执行 → 复核闸），默认 dry_run
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.paths import ensure_core_on_path, runtime_path  # noqa: E402

ensure_core_on_path()

import engine as _engine                                    # noqa: E402  (core/statemachine)
import tidy as TD                                            # noqa: E402
import tools as TOOLS                                        # noqa: E402
from contract import WorkOrder                                # noqa: E402
from tidy import TidyOrder                                    # noqa: E402

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_AWAITING_CONFIRM = "awaiting_confirm"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

#: 常见整理目录的语义描述（与 CLI 的 _DEFAULT_HINTS 保持同一口径）
_DEFAULT_HINTS: Dict[str, str] = {
    "文档": "合同、报告、说明书、笔记、会议纪要等文字性文档",
    "票据": "发票、报销单、记账凭证、税额、开票与报销资料",
    "合同": "合同、协议、甲方乙方、租期、违约与保密条款",
    "照片": "照片、图像、摄影、光圈快门像素等拍摄参数",
    "图片": "照片、截图、图像、设计稿、插画",
    "压缩包": "压缩归档文件、备份包、资源包",
    "表格": "表格、数据表、清单、统计与预算数据",
    "视频": "视频、录像、影片、剪辑素材",
    "音频": "音频、录音、音乐、语音素材",
    "代码": "源代码、脚本、程序文件、项目源码",
    "安装包": "安装程序、可执行文件、软件安装包",
}


class Cancelled(Exception):
    """协作式取消：适配器在每次调用前检查标记，命中就抛。"""


class CancellableAdapter:
    """包一层适配器，提供「在两个节点之间可被打断」的能力。

    为什么这样做而不是改核心：核心是上游仓库的快照，不该为了界面需求改它。
    取消的语义只需要「不再往下跑」，包一层足够，而且换任何适配器都能用。
    """

    def __init__(self, inner: Any, flag: threading.Event) -> None:
        self._inner = inner
        self._flag = flag
        self.spec = getattr(inner, "spec", None)
        self.model = getattr(inner, "model", None)

    def _check(self) -> None:
        if self._flag.is_set():
            raise Cancelled("已被取消")

    def fill_slot(self, *a, **kw):
        self._check()
        return self._inner.fill_slot(*a, **kw)

    def ask(self, *a, **kw):
        self._check()
        return self._inner.ask(*a, **kw)

    def record(self, *a, **kw):
        try:
            return self._inner.record(*a, **kw)
        except Exception:
            return None

    def health(self):
        return self._inner.health()


@dataclass
class RunnerOpts:
    """建单参数。``kind`` 决定走哪条链。"""
    kind: str = "code"                     # code | tidy
    task: str = ""
    workdir: str = ""                      # code：沙箱根；tidy：要整理的目录
    target: str = ""                       # code：目标文件（相对 workdir）
    test_path: str = ""                    # code：测试文件
    adapter: str = "mythos"                # 适配层 key（见 compatibility/profiles.py）
    model: str = ""                        # 覆盖模型名（画像不变）
    mode: str = "dry_run"                  # tidy：dry_run | execute
    dirs: List[str] = field(default_factory=list)     # tidy：允许归入的目录
    semantic: bool = False                 # tidy：用本地嵌入给建议
    max_repair_rounds: int = 3
    require_confirm: bool = True           # ★ 落盘必须经人确认
    wo_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        return d


def build_adapter(opts: RunnerOpts):
    """构造真实适配器，并把它的 transport 路由到本地网关。

    为什么必须过网关：画像（think 兼容、ctx 夹取）、审计、出网熔断都挂在那里。
    直连 Ollama 会让这三条承诺静默失效——2026-09-28 端到端跑时就撞上了（工单跑完、审计空）。
    ``LOCAL_LLM_VIA=direct`` 可显式指定直连（仅用于排障）。
    """
    from adapter_factory import build_adapter as _ba
    ad = _ba(kind=opts.adapter, model=opts.model or None)
    via = (os.environ.get("LOCAL_LLM_VIA") or "gateway").lower()
    if via == "direct":
        return ad
    gw = os.environ.get("LOCAL_LLM_GATEWAY", "http://127.0.0.1:8080")
    try:
        sys.path.insert(0, os.path.join(_ROOT, "gateway"))
        from client import route_adapter_through_gateway
        route_adapter_through_gateway(ad, gw, strict=os.environ.get("LOCAL_LLM_VIA") != "direct")
    except Exception as e:
        # 不静默降级：要么过网关，要么把问题说清楚（可一键切 direct 排障）
        raise RuntimeError(
            "无法把模型调用路由到网关：%s\n"
            "请先起网关（python gateway/app.py），或临时用 LOCAL_LLM_VIA=direct 直连排障。"
            % e)
    return ad


class WorkOrderRunner:
    """一次工单运行。线程安全的事件队列，供 SSE 消费。"""

    def __init__(self, opts: RunnerOpts, adapter: Any = None) -> None:
        self.opts = opts
        self.wo_id = opts.wo_id or ("wo-%s" % uuid.uuid4().hex[:10])
        self.adapter = adapter
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = "pending"
        self.result: Dict[str, Any] = {}
        self.created = time.time()
        self.error = ""
        self._lock = threading.Lock()
        self._confirm_lock = threading.Lock()
        self._stage_dir = ""
        self._staged_target = ""
        self._staged_content = ""
        self._tidy_plan: Dict[str, Any] = {}
        self._tidy_scan: Dict[str, Any] = {}
        self.timeline: List[Dict[str, Any]] = []

    # ---------------- 事件 ----------------
    def emit(self, kind: str, **kw: Any) -> None:
        # 注意：kw 里**不能**再出现 ``kind``（会与位置参数撞车：
        # `emit() got multiple values for argument 'kind'`）。工单类别请用 ``wo_kind``。
        ev = {"ts": time.time(), "kind": kind, "wo": self.wo_id}
        ev.update(kw)
        self.events.put(ev)
        if kind in ("node", "gate", "violation", "status", "error", "done", "progress"):
            self.timeline.append(ev)

    def drain(self, timeout: float = 0.0) -> List[Dict[str, Any]]:
        out = []
        while True:
            try:
                out.append(self.events.get(timeout=timeout if not out else 0))
            except queue.Empty:
                break
        return out

    # ---------------- 生命周期 ----------------
    def start(self) -> str:
        self._thread = threading.Thread(target=self._run_guarded, daemon=True,
                                        name="wo-%s" % self.wo_id)
        self._thread.start()
        return self.wo_id

    def cancel(self) -> bool:
        self._cancel.set()
        self.emit("status", status="cancelling")
        return True

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------- 执行 ----------------
    def _run_guarded(self) -> None:
        try:
            if self.opts.kind == "tidy":
                self._run_tidy()
            else:
                self._run_code()
        except Cancelled:
            with self._lock:
                self.status = STATUS_CANCELLED
            self.emit("status", status=STATUS_CANCELLED, reason="cancelled")
        except Exception as e:                       # 兜底：任何异常都不许让服务静默
            with self._lock:
                self.status = STATUS_FAILED
                self.error = "%s: %s" % (type(e).__name__, e)
            self.emit("error", message=self.error)
            self.emit("status", status=STATUS_FAILED)

    # ---- code 链 ----
    def _run_code(self) -> None:
        with self._lock:
            self.status = STATUS_RUNNING
        self.emit("status", status=STATUS_RUNNING, wo_kind="code")
        base = self.adapter if self.adapter is not None else build_adapter(self.opts)
        ad = CancellableAdapter(base, self._cancel)
        workdir = os.path.abspath(self.opts.workdir)
        real_path = os.path.join(workdir, self.opts.target.replace("\\", "/"))
        try:
            version_at_start = TOOLS.content_version(real_path) if os.path.exists(real_path) else ""
        except Exception:
            version_at_start = ""
        initial_version = version_at_start
        wo = WorkOrder(wo_id=self.wo_id, task=self.opts.task,
                       workdir=workdir,
                       target=self.opts.target,
                       test_path=self.opts.test_path or None)
        wo.constraints["max_repair_rounds"] = int(self.opts.max_repair_rounds)
        # ★ 外部审查 #6：把上下文层接进真实工单链。
        #   注意它工作在**暂存区**（src 是 staging 目录），所以要拿 staging 路径。
        def _ctx_provider(task: str, target: str):
            """把上下文层接进真实工单链（外部审查 #6）。

            注意：状态机改的是**暂存区里的副本**（self.staging 在 _stage 之后才有值），
            所以这里从状态机实例上取 staging 路径，保证上下文描述的是「将要被改的那份文件」。
            """
            try:
                sys.path.insert(0, os.path.join(_ROOT, "context"))
                import builder as _ctxb
                spec = getattr(ad, "spec", None)
                staging = getattr(sm_ref, "staging", "") or None
                return _ctxb.build_workorder_context(workdir=workdir, target=target,
                                                     task=task, spec=spec,
                                                     staging_root=staging)
            except Exception as e:
                raise RuntimeError("上下文层调用失败：%s" % e)

        sm = _engine.WorkOrderStateMachine(
            ad, wo,
            staging_root=runtime_path("staging"),
            runlog_dir=runtime_path("runs"),
            commit=not self.opts.require_confirm,
            initial_version=initial_version or None,
            context_provider=_ctx_provider,
            verbose=False,
            on_event=lambda m: self.emit("progress", node=wo.node, message=m))
        sm_ref = sm          # 让上面的闭包能拿到 staging 路径

        # 取消：包成 Terminal 让引擎能正常收尾（它的 run() 只捕获 Terminal）
        try:
            res = sm.run()
        except Cancelled:
            raise
        except _engine.Terminal as t:
            with self._lock:
                self.status = t.status
            self.emit("status", status=t.status, error=t.message)
            return

        # 把关键信息推成事件
        for g in wo.gate_results:
            self.emit("gate", gate=g["gate"], signal=g["signal"],
                      issues=[i.get("message", "") for i in g.get("issues", [])][:3])
        for v in wo.violations:
            self.emit("violation", rule=v.get("rule"), detail=v.get("detail", "")[:200])

        self._stage_dir = res.staging
        self._staged_target = os.path.join(res.staging, self.opts.target.replace("\\", "/"))
        try:
            with open(self._staged_target, "r", encoding="utf-8") as f:
                self._staged_content = f.read()
        except OSError:
            self._staged_content = ""

        real_path = real_path  # 已在上面算好
        # 暂存版本（用于比对）与真实版本（用于确认时再校验）
        try:
            staged_version = TOOLS.content_version(self._staged_target) \
                if os.path.isfile(self._staged_target) else ""
        except Exception:
            staged_version = ""

        self.result = {
            # ★ 字段名故意叫 run_status：快照顶层的 status 是**运行器状态**
            #   （running / awaiting_confirm / done …），不能被结果集里的同名键覆盖。
            #   这个撞名 2026-09-28 端到端跑时暴露：工单实际在等确认，快照却报 done。
            "run_status": res.status,
            "diff": res.diff,
            "staged_content": self._staged_content,
            "real_path": real_path,
            "real_version": version_at_start,
            "staged_version": staged_version,
            "gates": wo.gate_results,
            "violations": wo.violations,
            "history": [s.to_dict() for s in wo.history],
            "runlog": res.runlog_path,
            "elapsed": wo.artifacts.get("elapsed_sec"),
            "questions": wo.questions,
            "context": wo.artifacts.get("context") or {},
        }
        if res.status == "done" and self.opts.require_confirm:
            with self._lock:
                self.status = STATUS_AWAITING_CONFIRM
            self.emit("status", status=STATUS_AWAITING_CONFIRM,
                      diff=res.diff, needs_confirm=True)
        else:
            with self._lock:
                self.status = res.status
            self.emit("status", status=res.status, diff=res.diff)
        self.emit("done", status=self.status)

    # ---- tidy 链 ----
    def _run_tidy(self) -> None:
        with self._lock:
            self.status = STATUS_RUNNING
        self.emit("status", status=STATUS_RUNNING, wo_kind="tidy")
        base = self.adapter if self.adapter is not None else build_adapter(self.opts)
        ad = CancellableAdapter(base, self._cancel)
        hints = {}
        if self.opts.semantic and self.opts.dirs:
            # 与 CLI 保持同一套描述表；写在这里是避免服务依赖 gateway 模块
            hints = {d: _DEFAULT_HINTS.get(d, "%s 类文件" % d) for d in self.opts.dirs}
        # 人确认模式下先只出方案（dry_run），确认时再执行
        mode = "dry_run" if self.opts.require_confirm else self.opts.mode
        order = TidyOrder(wo_id=self.wo_id, task=self.opts.task,
                          root=os.path.abspath(self.opts.workdir), mode=mode,
                          target_dirs=list(self.opts.dirs))
        from tidy_engine import TidyStateMachine
        sm = TidyStateMachine(ad, order, runlog_dir=runtime_path("runs"), verbose=False,
                              on_event=lambda m: self.emit("progress", message=m),
                              semantic=bool(self.opts.semantic), category_hints=hints)
        try:
            res = sm.run()
        except Cancelled:
            raise
        for g in res.gates:
            self.emit("gate", gate=g["gate"], signal=g["signal"],
                      issues=[i.get("message", "") for i in g.get("issues", [])][:3])
        self._tidy_plan = res.plan
        # ★ 外部审查 #5：确认前必须让人看到**具体要移动什么**。
        #   原来只发了个数量（moves: len(...)），扩展拿不到清单，等于盲确认。
        plan_moves = [{"src": m.get("src"), "dst": m.get("dst"),
                       "reason": m.get("reason", "")}
                      for m in (res.plan or {}).get("moves", [])]
        self.result = {
            "run_status": res.status, "kind": "tidy", "plan": res.plan,
            "applied": res.applied, "gates": res.gates, "history": res.history,
            "runlog": res.runlog_path, "elapsed": res.elapsed,
            "semantic": getattr(sm, "semantic_notes", {}),
            "moves": plan_moves,
        }
        if res.status == "done" and mode == "dry_run" and res.plan.get("moves"):
            self._tidy_scan = TD.scan_root(os.path.abspath(self.opts.workdir),
                                           order.filters)
            with self._lock:
                self.status = STATUS_AWAITING_CONFIRM
            # 事件里带上完整清单，SSE 前端可以直接渲染
            self.emit("status", status=STATUS_AWAITING_CONFIRM, needs_confirm=True,
                      moves=len(plan_moves), plan=plan_moves)
        else:
            with self._lock:
                self.status = res.status
            self.emit("status", status=res.status)
        self.emit("done", status=self.status)

    # ---------------- 确认落盘 ----------------
    def confirm(self) -> Dict[str, Any]:
        """人确认后落盘。code 类复制暂存文件；tidy 类执行方案并复核。"""
        with self._confirm_lock:
            if self.status != STATUS_AWAITING_CONFIRM:
                return {"ok": False, "error": "当前状态不需要确认：%s" % self.status}
            if self.opts.kind == "tidy":
                return self._confirm_tidy()
            return self._confirm_code()

    def _confirm_code(self) -> Dict[str, Any]:
        real = self.result.get("real_path") or ""
        if not real or not os.path.isfile(self._staged_target):
            return {"ok": False, "error": "找不到暂存结果"}
        # ★ 落盘前比对的是**工单开始前**记下的版本（外部审查 #3）。
        #   原来拿“模型跑完之后”的版本当基线，会漏掉“运行期间用户改了文件”这种情况。
        try:
            now_v = TOOLS.content_version(real) if os.path.exists(real) else ""
        except Exception:
            now_v = ""
        was_v = str(self.result.get("real_version") or "")
        if was_v and now_v and now_v != was_v:
            return {"ok": False,
                    "error": "目标文件在工单开始后已被修改（%s → %s），已拒绝覆盖；"
                             "请重新建单。" % (was_v[7:15], now_v[7:15])}
        tmp = real + ".ide-commit"
        shutil.copyfile(self._staged_target, tmp)
        os.replace(tmp, real)
        with self._lock:
            self.status = STATUS_DONE
        self.emit("applied", path=real)
        self.emit("status", status=STATUS_DONE)
        return {"ok": True, "path": real}

    def _confirm_tidy(self) -> Dict[str, Any]:
        root = os.path.abspath(self.opts.workdir)
        scan = self._tidy_scan or TD.scan_root(root)
        applied = TD.apply_plan(root, self._tidy_plan, log=[])
        vg = TD.verify_after(root, self._tidy_plan, scan, applied)
        self.emit("gate", gate="verify_gate", signal=vg.signal,
                  issues=[i.message for i in vg.issues][:3])
        self.result["applied"] = applied
        self.result["verify"] = vg.to_dict()
        with self._lock:
            self.status = "done" if vg.ok else "escalated"
        self.emit("applied", moved=len(applied.get("moved", [])), ok=vg.ok)
        self.emit("status", status=self.status)
        return {"ok": vg.ok, "moved": len(applied.get("moved", [])),
                "verify": vg.to_dict()}

    # ---------------- 对外快照 ----------------
    def snapshot(self) -> Dict[str, Any]:
        """对外快照。**顶层 status 永远是运行器状态**，不可被结果集覆盖。"""
        d = {
            "wo_id": self.wo_id, "status": self.status, "kind": self.opts.kind,
            "task": self.opts.task, "created": self.created, "error": self.error,
            "elapsed": round(time.time() - self.created, 2),
            "timeline": self.timeline[-80:],
        }
        for k, v in self.result.items():
            if k in ("real_path", "status"):
                continue
            d[k] = v
        return d


# ============================================================
# 注册表（按 wo_id 管理）
# ============================================================
class RunnerRegistry:
    def __init__(self) -> None:
        self._items: Dict[str, WorkOrderRunner] = {}
        self._lock = threading.Lock()

    def add(self, runner: WorkOrderRunner) -> str:
        with self._lock:
            self._items[runner.wo_id] = runner
        return runner.wo_id

    def get(self, wo_id: str) -> Optional[WorkOrderRunner]:
        with self._lock:
            return self._items.get(wo_id)

    def all(self) -> List[WorkOrderRunner]:
        with self._lock:
            return list(self._items.values())

    def create(self, opts: RunnerOpts, adapter: Any = None) -> WorkOrderRunner:
        r = WorkOrderRunner(opts, adapter=adapter)
        self.add(r)
        r.start()
        return r


def list_presets() -> Dict[str, Any]:
    """列出可用适配层与模型（供扩展的下拉框用）。"""
    from mythos_core.profiles import ALL_PROFILES, UNUSABLE_TOOL_MODELS, load_generated_profiles
    load_generated_profiles()
    return {
        "profiles": [{"key": k, "model": p.model, "label": p.label,
                      "sends_think": p.sends_think, "ctx_max": p.ctx_max,
                      "notes": (p.notes or "")[:120]}
                     for k, p in sorted(ALL_PROFILES.items())],
        "unusable": UNUSABLE_TOOL_MODELS,
        "default": "mythos",
    }

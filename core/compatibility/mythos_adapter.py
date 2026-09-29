#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mythos_adapter.py —— 本地原生 Agent 框架的「专属兼容层」（对外唯一门面）

适配对象：fableforge-ai/mythos-v2-8b:q4_k_m（基于 qwen3 / 8.2B / Q4_K_M / 40960 ctx）
适配依据：四轮探针实测（probe.py ~ probe4.py），规则全部有实测证据编号对应。

定位：本模块是「叶子」，不是框架本体。它只负责三件事：
  1. 把一个槽位请求安全、可复现地送到模型（出网 + 解码 + 重试）
  2. 把回来的东西规范化、校验、必要时拒绝
  3. 记录每次结果，供框架的能力画像使用
它不决定「下一步做什么」——那是框架的事。

实现已按关注点拆到 ``mythos_core/`` 子包：
    config     配置常量
    rules      Mythos 专属 tool 规则（R1/R2）
    params     参数归一 / 校验（R9）
    transport  出网 / 解码
    types      FillResult
    errors     异常类型

本文件只保留 :class:`MythosAdapter` 门面、自检，并把旧版散落的模块级常量
（TOOLSETS / TEMP_LADDER / ...）原样重新导出，保证 ``import mythos_adapter as MA`` 的
既有用法（probes/export_rules.py）零改动可用。

依赖：仅标准库（urllib）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# ---- 让「当脚本跑」与「被 import」两种方式都能找到 mythos_core ----
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:  # 常规：作为顶层模块 import（sys.path 里有 compatibility/）
    from mythos_core.config import (
        MODEL_DEFAULT, OLLAMA_HOST, REQUEST_TIMEOUT_DEFAULT, HEALTH_TIMEOUT,
        TRANSPORT_RETRIES_DEFAULT, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX,
        CTX_MAX, CTX_DEFAULT, CTX_INTERACTIVE_MAX, CTX_POLICY,
        TEMP_LADDER, THINK_POLICY, MAX_SEC_SAMPLES,
    )
    from mythos_core.errors import (
        AdapterError, ContextOverflow, UnsafeArgument, SchemaViolation,
    )
    from mythos_core.rules import (
        TOOLSETS, GATED_TOOLS, DESTRUCTIVE_PATTERNS, check_safety,
    )
    from mythos_core.params import _coerce, normalize_args
    from mythos_core.transport import OllamaTransport
    from mythos_core.types import FillResult, KINDS
    from mythos_core.extract import extract_tool_calls_from_content
    from mythos_core.profiles import MYTHOS as MYTHOS_PROFILE, ModelProfile, get_profile
except ImportError:  # 作为包内模块 import（例如 import compatibility.mythos_adapter）
    from .mythos_core.config import (
        MODEL_DEFAULT, OLLAMA_HOST, REQUEST_TIMEOUT_DEFAULT, HEALTH_TIMEOUT,
        TRANSPORT_RETRIES_DEFAULT, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX,
        CTX_MAX, CTX_DEFAULT, CTX_INTERACTIVE_MAX, CTX_POLICY,
        TEMP_LADDER, THINK_POLICY, MAX_SEC_SAMPLES,
    )
    from .mythos_core.errors import (
        AdapterError, ContextOverflow, UnsafeArgument, SchemaViolation,
    )
    from .mythos_core.rules import (
        TOOLSETS, GATED_TOOLS, DESTRUCTIVE_PATTERNS, check_safety,
    )
    from .mythos_core.params import _coerce, normalize_args
    from .mythos_core.transport import OllamaTransport
    from .mythos_core.types import FillResult, KINDS
    from .mythos_core.extract import extract_tool_calls_from_content
    from .mythos_core.profiles import MYTHOS as MYTHOS_PROFILE, ModelProfile, get_profile

__all__ = [
    # 门面
    "MythosAdapter", "FillResult", "selftest",
    # R12 失败分类全集（唯一真相源）
    "KINDS",
    # 参数层
    "normalize_args", "_coerce", "check_safety",
    # 规则 / 常量（旧用法重导出）
    "TOOLSETS", "GATED_TOOLS", "DESTRUCTIVE_PATTERNS",
    "TEMP_LADDER", "THINK_POLICY", "CTX_POLICY",
    "CTX_MAX", "CTX_DEFAULT", "CTX_INTERACTIVE_MAX",
    "MODEL_DEFAULT", "OLLAMA_HOST",
    # 异常
    "AdapterError", "ContextOverflow", "UnsafeArgument", "SchemaViolation",
]

# 实测证据索引（每条规则都能回溯到用例）
#  E1  probe A1-A3   ：工具选择 3/3 通过
#  E2  probe B1-B3   ：int/bool/array 参数抽取 3/3 通过
#  E3  probe C1-C2   ：不该调用时不调用 2/2 通过
#  E4  probe D1/H3   ：多轮承接、报错恢复 通过（H3 会自动 list_dir 找原因）
#  E5  probe E1/I6   ：并行多次调用 通过（3 个 read_file 各不同）
#  E6  probe F1/H9   ：★ format 与 tools 互斥 —— 加 format 后 n_tc=0，且丢字段
#  E7  probe H5      ：★ 「改代码」它只做第一步（read），不做第二步（patch）
#  E8  probe I1/J3   ：★ 框架供第一步结果后，立刻正确产出 apply_patch（嵌套参数）
#  E9  probe I2      ：★ 点名不存在的工具时，它转调 run_shell 执行 rm -rf（危险）
#  E10 probe I4      ：信息不足时会反问而非臆造
#  E11 probe H2      ：缺参时字面填入（path="app"），不会自动补全路径
#  E12 probe I5/J1/J2：上下文超限报 400；放到 16384/32768 后噪声下仍能选对
#  E13 probe G1/H8/H10/H11：think=false 在「选工具 / 抽参数」类节点上全部通过，延迟从 4-8s 降到约 1s
#  E14 全部调用：tool call 零泄漏进正文，零伪 JSON（除 E6 场景）
#  E15 probe J3/J4 ：★ think=false 下编辑节点会走 write_file 整文件重写；think=true 才产出最小 apply_patch
#  E16 probe J2    ：★ num_ctx=32768 + 大段前置内容 → 240s 超时；交互场景远超 16384 不可用


class MythosAdapter:
    """Mythos V2-8B 的专属兼容层门面。

    线程安全：画像读写用一把 :class:`threading.RLock` 串行化，落盘走「临时文件 + 原子替换」。
    多**进程**同时写同一画像文件不在保证范围内（见 ``record`` 的说明）。
    """

    def __init__(
        self,
        model: str = MODEL_DEFAULT,
        host: str = OLLAMA_HOST,
        profile_path: Optional[str] = None,
        transport: Optional[OllamaTransport] = None,
        transport_retries: Union[int, Dict[str, int]] = TRANSPORT_RETRIES_DEFAULT,
        spec: Optional[ModelProfile] = None,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        # 模型画像：工具集 / 思考策略 / 上下文配额 / 温度阶梯 / 是否要抠正文
        # 不传则用 Mythos 画像（向后兼容：旧调用一行不用改）。
        self.spec = spec or MYTHOS_PROFILE
        self.profile_path = profile_path or os.path.join(_HERE, "node_profiles.json")
        self.transport = transport or OllamaTransport(self.host)
        # 纯网络层重试次数（不占模型采样额度）：可给 int（全局默认），
        # 也可给 {"recon": 1, "edit": 3, "*": 2} 这种按节点**类型**覆盖的映射（"*" 为兜底）。
        # 默认 2；保持可直接赋值的实例属性 ``transport_retries``（向后兼容）。
        self.transport_retries, self._retries_by_node = self._parse_transport_retries(transport_retries)
        self._lock = threading.RLock()
        self.profile = self._load_profile()

    @staticmethod
    def _parse_transport_retries(spec: Union[int, Dict[str, int]]) -> Tuple[int, Dict[str, int]]:
        """把 ``transport_retries`` 归一成 ``(全局默认, {节点类型: 次数})``。

        两种形态：
          · ``int``                   —— 全局默认（向后兼容，旧的 ``ad.transport_retries = 2`` 仍生效）
          · ``{"recon": 1, "*": 2}``  —— 按节点**类型**覆盖；``"*"`` 为兜底默认
        实测（verification/measure_retry_budget.py）：本机连接被拒约 2s/次，
        默认 2 次重试（含退避）最坏总代价约 6-8s；交互敏感节点可在此下调。
        """
        if isinstance(spec, dict):
            per_node: Dict[str, int] = {}
            for k, v in spec.items():
                if k == "*":
                    continue
                try:
                    per_node[str(k)] = max(0, int(v))
                except (TypeError, ValueError):
                    continue
            try:
                default = max(0, int(spec.get("*", TRANSPORT_RETRIES_DEFAULT)))
            except (TypeError, ValueError):
                default = TRANSPORT_RETRIES_DEFAULT
            return default, per_node
        try:
            return max(0, int(spec)), {}
        except (TypeError, ValueError):
            return TRANSPORT_RETRIES_DEFAULT, {}

    # ============================================================
    # 能力画像（R11）
    # ============================================================
    def _load_profile(self) -> Dict[str, Any]:
        """读画像文件。缺失→空画像；损坏→备份后重建（不静默覆盖坏数据）。"""
        default: Dict[str, Any] = {"model": self.model, "nodes": {}}
        path = self.profile_path
        if not path or not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self._backup_broken_profile(path)
            return default
        # 形状校验：必须是 dict 且 nodes 是 dict，否则视为损坏
        if not isinstance(data, dict) or not isinstance(data.get("nodes", {}), dict):
            self._backup_broken_profile(path)
            return default
        data.setdefault("model", self.model)
        data.setdefault("nodes", {})
        return data

    @staticmethod
    def _backup_broken_profile(path: str) -> None:
        try:
            os.replace(path, "%s.corrupt-%d" % (path, int(time.time())))
        except Exception:
            pass

    def _write_profile_locked(self) -> None:
        """原子落盘（临时文件 + os.replace）。调用方须已持锁。"""
        if not self.profile_path:
            return
        d = os.path.dirname(os.path.abspath(self.profile_path)) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".node_profiles-", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.profile, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.profile_path)  # 原子替换（Windows 亦可覆盖）
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception:
            # 画像落盘是 best-effort：写失败不该打断主流程，内存里的数据仍有效
            pass

    def record(self, node: str, tool_name: str, ok: bool, sec: Optional[float] = None) -> None:
        """把一次 gate 结果回写画像（R11）。

        · 进程内：``_lock`` 串行化「改内存 + 落盘」，且落盘用原子替换，
          因此并发 ``record`` 不会写出半截 JSON。
        · 跨进程：同一 ``profile_path`` 被多个进程写仍是「后写覆盖前写」。
          框架若要多进程采样，请让每个进程用独立 ``profile_path``，事后再合并。
        """
        with self._lock:
            n = self.profile.setdefault("nodes", {}).setdefault(node, {})
            e = n.setdefault(tool_name, {"pass": 0, "fail": 0, "sec": []})
            e.setdefault("pass", 0)
            e.setdefault("fail", 0)
            e.setdefault("sec", [])
            e["pass" if ok else "fail"] += 1
            if sec is not None:
                try:
                    e["sec"].append(round(float(sec), 2))
                except (TypeError, ValueError):
                    pass
                if len(e["sec"]) > MAX_SEC_SAMPLES:
                    del e["sec"][:-MAX_SEC_SAMPLES]
            self.profile["model"] = self.model
            self._write_profile_locked()

    def pass_rate(self, node: str, tool_name: str) -> Optional[float]:
        """该 (node, tool) 的历史通过率；无样本返回 None。"""
        with self._lock:
            e = ((self.profile.get("nodes") or {}).get(node) or {}).get(tool_name)
            if not e:
                return None
            total = e.get("pass", 0) + e.get("fail", 0)
            if total == 0:
                return None
            return e["pass"] / float(total)

    # ============================================================
    # 工具筛选（R1）
    # ============================================================
    def tools_for(self, node_or_list: Union[str, Sequence[str]],
                  registry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按节点类型取工具子集，返回 schema 列表。``registry: {name: tool_schema}``。

        - 传字符串：先查画像里的 ``node_toolsets`` 覆盖，再查内置 :data:`TOOLSETS`；未知节点 → ``[]``。
        - 传序列：直接当工具名清单用。
        只保留 registry 里真实存在的工具（挡住幻觉工具名，依据 E9）。
        """
        with self._lock:
            override = self.profile.get("node_toolsets") or {}
        if isinstance(node_or_list, str):
            mix = override.get(node_or_list) or self.spec.tools_for_key(node_or_list)
        else:
            mix = list(node_or_list)
        return [registry[n] for n in mix if n in registry]

    @staticmethod
    def _allowed_names(tools: Sequence[Dict[str, Any]]) -> set:
        names = set()
        for t in tools or []:
            try:
                names.add(t["function"]["name"])
            except (KeyError, TypeError):
                continue
        return names

    # ============================================================
    # 上下文配额（R5）
    # ============================================================
    def _resolve_ctx(self, key: str, num_ctx: Optional[int], node: Optional[str] = None) -> int:
        """定 num_ctx：显式值优先，否则查节点策略，最后兜底；一律夹到 [1, CTX_MAX]。

        ★ R5 边界（本轮加固）：**不硬性禁止**显式传入 > ``CTX_INTERACTIVE_MAX``，
        但会向 stderr 打一条 warning（带节点名与数值），便于发现误用；
        随后仍按 H3 夹取到 ``CTX_MAX``。不新增「交互/批处理」开关，避免过度设计。
        """
        explicit = num_ctx is not None
        if num_ctx is None:
            num_ctx = self.spec.ctx_for(key)
        try:
            num_ctx = int(num_ctx)
        except (TypeError, ValueError):
            num_ctx = self.spec.ctx_default
        if explicit and num_ctx > self.spec.ctx_interactive_max:
            print("[%s][R5] warning: 节点 %r 显式 num_ctx=%d 超过交互上限 %d；"
                  "已夹到 <=%d。需要更长上下文时请走检索精简，而不是调大窗口。"
                  % (self.spec.key, node if node is not None else key, num_ctx,
                     self.spec.ctx_interactive_max, self.spec.ctx_max),
                  file=sys.stderr)
        return max(1, min(num_ctx, self.spec.ctx_max))

    # ============================================================
    # 原始调用（保留旧签名，内部委托给 transport）
    # ============================================================
    def _raw(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        think: Optional[bool] = None,
        temp: float = 0.0,
        num_ctx: int = CTX_DEFAULT,
        timeout: Optional[float] = REQUEST_TIMEOUT_DEFAULT,
    ):
        """组装请求体并出网。返回 ``(应答字典, 耗时秒)``。"""
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temp, "num_ctx": num_ctx},
        }
        if tools:
            body["tools"] = tools
        # ★ sends_think=False 的模型（如 qwen2.5-coder）传 think 会直接 HTTP 400，
        #   这一层就把键摘掉，而不是指望调用方记得别传。
        if think is not None and self.spec.sends_think:
            body["think"] = think
        # ★ R6（依据 E6）：format 与 tools 绝不同时出现。本函数不提供 format 入口。
        if "format" in body:
            raise AdapterError("内部错误：format 与 tools 不得共存（R6）")
        return self.transport.chat(body, timeout=timeout)

    @staticmethod
    def _backoff(n: int) -> float:
        return min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (2 ** max(0, n - 1)))

    # ============================================================
    # 槽位填充（框架唯一入口，R7）
    # ============================================================
    def fill_slot(
        self,
        node: str,
        messages: List[Dict[str, Any]],
        registry: Dict[str, Any],
        node_kind: str = "slot",
        n: int = 1,
        num_ctx: Optional[int] = None,
        temp: Optional[float] = None,
        timeout: Optional[float] = REQUEST_TIMEOUT_DEFAULT,
        node_type: Optional[str] = None,
    ) -> FillResult:
        """框架调用模型填一个槽（R7）。

        Parameters
        ----------
        node:
            节点**名**（如 "edit-step-3"），用于画像统计。
        messages:
            已由框架组装好的提示（含检索到的上下文与槽位说明）。
        registry:
            ``{tool_name: tool_schema}``。
        node_kind:
            slot / judge / plan / repair / edit —— 决定 thinking、温度与上下文策略。
        n:
            best-of-N 的 N（>1 时逐档升温，依据 R4）。首个「成功」即返回。
        node_type:
            节点**类型**（recon/verify/edit/create/commit/shell/judge），决定工具集与 ctx。
            不传则退回 ``node``；拆开是为了避免把「节点名」和「节点类型」混为一个参数。
        """
        # 工具集按「类型」授权；画像统计按「名字」——两者解耦
        toolset_key = node_type if node_type is not None else node
        tools = self.tools_for(toolset_key, registry)
        allowed = self._allowed_names(tools)
        think = self.spec.think_for(node_kind)
        ctx = self._resolve_ctx(toolset_key, num_ctx, node)
        # 纯网络层重试次数：按节点**类型**可取覆盖值，否则用全局默认（可被实例属性改写）。
        retries = self._retries_by_node.get(toolset_key, self.transport_retries)

        attempts = max(1, int(n))
        model_tries = 0          # 模型采样次数（受 attempts 限制）
        http_calls = 0           # 实际 HTTP 次数（含网络重试）
        net_retries = 0
        fail_idx = 0             # 温度阶梯游标，只在「模型级失败」时前进
        last: Optional[FillResult] = None

        while model_tries < attempts:
            t = temp if temp is not None else self.spec.temp_ladder[min(fail_idx, len(self.spec.temp_ladder) - 1)]
            try:
                http_calls += 1
                obj, sec = self._raw(messages, tools=tools or None, think=think,
                                     temp=t, num_ctx=ctx, timeout=timeout)
            except ContextOverflow as e:
                # 确定性失败：不重试（R5）
                return FillResult(node=node, ok=False, kind="context_overflow",
                                  error=str(e), attempts=http_calls, temp=t, ctx=ctx)
            except AdapterError as e:
                # 网络级抖动：有限重试（不占模型采样额度），用尽才判 transport_error
                if net_retries < retries:
                    net_retries += 1
                    time.sleep(self._backoff(net_retries))
                    continue
                return FillResult(node=node, ok=False, kind="transport_error",
                                  error=str(e), attempts=http_calls, temp=t, ctx=ctx)

            model_tries += 1
            m = obj.get("message", {}) or {}
            content = m.get("content") or ""
            thinking = m.get("thinking") or ""
            calls = m.get("tool_calls") or []

            # ★ 正文抠调用：少数模型（实测 qwen2.5-coder:7b）不走原生 tool_calls 通道，
            #   而是把调用写成正文里的一段裸 JSON。这里把它译成同一形状，
            #   后面的授权/归一/安全闸一个字都不用改。
            #   两段式：先用「工具表」白名单挡掉正文里恰好长得像调用的业务 JSON；
            #   若白名单下没抽到、但正文里确实有调用形状的对象，则按 R12 归为格式噪声，
            #   而不是默默当成「模型在聊天」（否则越权/幻觉就被降级成了空调用）。
            if not calls and self.spec.extract_from_content:
                cands = extract_tool_calls_from_content(content, set(registry.keys()) or None)
                if not cands:
                    stray = extract_tool_calls_from_content(content, None)
                    if stray:
                        return FillResult(node=node, ok=False, kind="schema_violation",
                                          error="正文里出现了不在工具表里的调用：%s"
                                                % ", ".join(sorted({s["name"] for s in stray})[:5]),
                                          content=content, thinking=thinking, sec=sec,
                                          attempts=http_calls, temp=t, ctx=ctx)
                for cand in cands:
                    calls.append({"function": cand})

            # 空 tool_calls —— 依据 E3/E10/R10：合法结果，不是失败
            if not calls:
                return FillResult(node=node, ok=True, kind="no_call", content=content,
                                  thinking=thinking, sec=sec, attempts=http_calls,
                                  temp=t, ctx=ctx)

            # 逐个规范化 + 过闸。多调用场景依据 E5/I6
            parsed: List[Dict[str, Any]] = []
            errs: List[str] = []
            for c in calls:
                fn = (c or {}).get("function", {}) or {}
                name = fn.get("name")
                # ★ R1 强制：只认「本节点授权集」里的工具，而不只是 registry 里存在。
                #   （模型只看得见子集，正常不会越界；但幻觉/越权必须当场拒。）
                # ★ R12（本轮新增类别）：越权与幻觉分开归类——
                #   · name 在 registry 里但不在 allowed  → **unauthorized_tool**（安全事件，立即判死）
                #   · name 连 registry 都没有            → 视为格式噪声，归 schema_violation
                #   分开才能对越权单独计数/告警，不被一般格式噪声淹没。
                if name not in allowed:
                    if name in registry:
                        return FillResult(node=node, ok=False, kind="unauthorized_tool",
                                          error="未授权工具：%s（不在本节点授权集）" % name,
                                          calls=[], sec=sec, attempts=http_calls, temp=t, ctx=ctx)
                    errs.append("未知工具：%s" % name)
                    continue
                schema = registry.get(name)
                if not schema:
                    errs.append("未知工具：%s" % name)   # 依据 E9
                    continue
                try:
                    a = normalize_args(fn.get("arguments"), schema)
                    check_safety(name, a)
                    parsed.append({"name": name, "args": a})
                except UnsafeArgument as e:
                    # ★ R2 硬边界：危险参数不回炉重试，立即判死
                    return FillResult(node=node, ok=False, kind="unsafe_argument",
                                      error=str(e), calls=parsed, sec=sec,
                                      attempts=http_calls, temp=t, ctx=ctx)
                except SchemaViolation as e:
                    errs.append("%s: %s" % (name, e))

            if parsed:
                return FillResult(node=node, ok=True, kind="tool_call", calls=parsed,
                                  content=content, thinking=thinking, sec=sec,
                                  attempts=http_calls, temp=t, ctx=ctx)

            # 本轮模型级失败（缺参 / 类型错 / 幻觉工具名）→ 记下来，换更高温度再来一次（R4）
            last = FillResult(node=node, ok=False, kind="schema_violation",
                              error="; ".join(errs), calls=[], sec=sec,
                              attempts=http_calls, temp=t, ctx=ctx)
            fail_idx += 1

        return last or FillResult(node=node, ok=False, kind="exhausted",
                                  attempts=http_calls, ctx=ctx)

    # ============================================================
    # 免工具问答（解释 / 归纳）
    # ============================================================
    def ask(
        self,
        messages: List[Dict[str, Any]],
        think: bool = False,
        num_ctx: int = CTX_DEFAULT,
        temp: float = 0.0,
        timeout: Optional[float] = REQUEST_TIMEOUT_DEFAULT,
    ) -> FillResult:
        """纯文本问答（不开工具）。失败也返回 FillResult，不抛异常（R12）。"""
        ctx = self._resolve_ctx("ask", num_ctx, "ask")
        try:
            obj, sec = self._raw(messages, tools=None, think=think and self.spec.sends_think,
                                 temp=temp, num_ctx=ctx, timeout=timeout)
        except ContextOverflow as e:
            return FillResult(node="ask", ok=False, kind="context_overflow",
                              error=str(e), attempts=1, temp=temp, ctx=ctx)
        except AdapterError as e:
            return FillResult(node="ask", ok=False, kind="transport_error",
                              error=str(e), attempts=1, temp=temp, ctx=ctx)
        m = obj.get("message", {}) or {}
        return FillResult(node="ask", ok=True, kind="answer",
                          content=m.get("content") or "",
                          thinking=m.get("thinking") or "",
                          sec=sec, attempts=1, temp=temp, ctx=ctx)

    # ============================================================
    # 健康检查
    # ============================================================
    def health(self) -> Dict[str, Any]:
        try:
            tags = self.transport.list_models()
            names = [m.get("name") for m in tags.get("models", [])]
            want_base = self.model.split(":")[0]
            present = self.model in names or any(
                n and n.split(":")[0] == want_base for n in names)
            return {"ok": present, "present": names, "want": self.model}
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ============================================================
# 自检（不依赖框架，直接跑）
# ============================================================

def _registry() -> Dict[str, Any]:
    from_tools = [
        {"type": "function", "function": {"name": "read_file", "description": "读取文件内容",
         "parameters": {"type": "object", "properties": {
             "path": {"type": "string"}, "start_line": {"type": "integer"},
             "end_line": {"type": "integer"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "list_dir", "description": "列目录",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]}}},
        {"type": "function", "function": {"name": "search_code", "description": "搜代码",
         "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]}}},
        {"type": "function", "function": {"name": "run_tests", "description": "跑测试",
         "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}},
                        "required": ["pattern"]}}},
        {"type": "function", "function": {"name": "apply_patch", "description": "应用补丁",
         "parameters": {"type": "object", "properties": {
             "path": {"type": "string"},
             "patch": {"type": "object", "properties": {
                 "old_text": {"type": "string"}, "new_text": {"type": "string"}}}},
             "required": ["path", "patch"]}}},
        {"type": "function", "function": {"name": "run_shell", "description": "执行 shell",
         "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                        "required": ["command"]}}},
    ]
    return {t["function"]["name"]: t for t in from_tools}


def selftest() -> None:
    reg = _registry()
    ad = MythosAdapter(profile_path=os.path.join(tempfile.gettempdir(), "mythos_profiles_selftest.json"))
    print("health:", ad.health())
    cases = [
        # (名称, 节点, node_kind, messages, 期望 kind)
        ("recon 不该看到 run_shell", "recon", "slot",
         [{"role": "user", "content": "看一下 src/app.py 里写了什么。"}], "tool_call"),
        ("判题节点无工具 → 应走 no_call", "judge", "judge",
         [{"role": "user", "content": "这两个补丁哪个更好？A 还是 B？"}], "no_call"),
        ("受控编辑节点应产出 apply_patch（不给 write_file）", "edit", "edit",
         [{"role": "system", "content": "你是执行器，只负责把指定的修改写成补丁。"},
          {"role": "user", "content": "把 src/app.py 里的 `return a + b` 改成 `return a + b + 0`。"},
          {"role": "assistant", "content": "",
           "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "src/app.py"}}}]},
          {"role": "tool", "content": "def add(a, b):\n    return a + b\n"}], "tool_call"),
    ]
    ok = 0
    for name, node, kind, msgs, want in cases:
        r = ad.fill_slot(node, msgs, reg, node_kind=kind)
        good = (r.kind == want)
        ok += 1 if good else 0
        print("  [%s] %s -> %r" % ("PASS" if good else "FAIL", name, r))
        if r.calls:
            print("        calls:", json.dumps(r.calls, ensure_ascii=False))
    print("\n节点工具集：")
    for k in TOOLSETS:
        print("  %-8s -> %s" % (k, ad.tools_for(k, reg) and [t['function']['name'] for t in ad.tools_for(k, reg)]))
    print("\nselftest: %d/%d" % (ok, len(cases)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        ad = MythosAdapter()
        print(json.dumps(ad.health(), ensure_ascii=False, indent=2))

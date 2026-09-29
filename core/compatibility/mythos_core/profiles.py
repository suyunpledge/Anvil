# -*- coding: utf-8 -*-
"""profiles.py —— 模型画像（模型专属数据的唯一落点）。

架构约定（从 v0.3.1 的拆分延续下来）：

    【模型专属】一个模型一份画像，换模型 = 换一份画像 + 重测
        · toolsets          按节点授权哪些工具（R1）
        · think_policy      每个节点开不开思考（R3）
        · sends_think       能不能把 think 键发出去（有的模型直接 400）
        · ctx_*             上下文配额（R5）
        · temp_ladder       采样温度阶梯（R4）
        · extract_from_content  是否要把工具调用从正文里抠出来（有的模型不走原生通道）
    【通用层】params / transport / types / errors / extract —— 换模型不改

每份画像的字段都必须有实测依据，写在 ``evidence`` 里；没有实测的数字要么留空，
要么明确标注为「待测」。这条纪律来自运维期的一次教训：不许编造依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .config import (
    CTX_DEFAULT, CTX_INTERACTIVE_MAX, CTX_MAX, CTX_POLICY, MODEL_DEFAULT,
    TEMP_LADDER, THINK_POLICY, TRANSPORT_RETRIES_DEFAULT,
)
from .rules import TOOLSETS


@dataclass(frozen=True)
class ModelProfile:
    """一个模型的完整画像。字段全是「不能通用」的东西。"""
    key: str
    label: str
    model: str
    toolsets: Dict[str, List[str]]
    think_policy: Dict[str, bool]
    ctx_policy: Dict[str, int]
    ctx_default: int
    ctx_max: int
    ctx_interactive_max: int
    temp_ladder: List[float]
    sends_think: bool = True
    extract_from_content: bool = False
    # ---- 节点纪律（不同模型的「耐心」差很多，按实测给）----
    recon_max_turns: int = 3
    edit_max_turns: int = 3
    edit_prompt_hint: str = ""
    transport_retries: int = TRANSPORT_RETRIES_DEFAULT
    notes: str = ""
    evidence: Tuple[str, ...] = field(default_factory=tuple)

    # ---- 便捷视图 ----
    def think_for(self, node_kind: str) -> bool:
        v = self.think_policy.get(node_kind, False)
        return bool(v and self.sends_think)

    def ctx_for(self, key: str) -> int:
        return int(self.ctx_policy.get(key, self.ctx_default))

    def tools_for_key(self, key: str) -> List[str]:
        return list(self.toolsets.get(key, []))


# ============================================================
# Mythos V2-8B（q4_k_m）—— 已在服役的画像
#   依据：四轮探针 40 用例（probes/probe*.py）+ 两轮维护加固
# ============================================================
MYTHOS = ModelProfile(
    key="mythos",
    label="Mythos V2-8B (q4_k_m)",
    model=MODEL_DEFAULT,
    toolsets=dict(TOOLSETS),
    think_policy=dict(THINK_POLICY),
    ctx_policy=dict(CTX_POLICY),
    ctx_default=CTX_DEFAULT,
    ctx_max=CTX_MAX,
    ctx_interactive_max=CTX_INTERACTIVE_MAX,
    temp_ladder=list(TEMP_LADDER),
    sends_think=True,
    extract_from_content=False,
    notes="原生工具调用可靠；format 与 tools 互斥；编辑节点必须开思考，否则退化成整文件重写。",    evidence=("E6 format/tools 互斥", "E15 think=false 退化成 write_file", "E9 rm -rf 幻觉",
              "E13 think=false 在选工具/抽参数上不掉质量"),
)

# ============================================================
# Qwen2.5-Coder-7B —— 本轮新做（它的工具调用走正文 JSON，不是原生通道）
#   依据：probes/model_smoke.py（2026-09-27）+ probes/probe_qwen_coder.py
# ============================================================
QWEN_CODER = ModelProfile(
    key="qwen-coder",
    label="Qwen2.5-Coder-7B",
    model="qwen2.5-coder:7b",
    # 与 Mythos 同一套授权口径：编辑节点只给 apply_patch，不给 write_file（结构性防线，不依赖脾气）
    toolsets=dict(TOOLSETS),
    # 它**不支持思考**：Ollama 直接 400 `does not support thinking`（实测 Q3）
    think_policy={k: False for k in THINK_POLICY},
    ctx_policy=dict(CTX_POLICY),
    ctx_default=CTX_DEFAULT,
    ctx_max=32768,
    ctx_interactive_max=CTX_INTERACTIVE_MAX,
    temp_ladder=list(TEMP_LADDER),
    sends_think=False,        # ★ 关键：不能把 think 键发出去
    extract_from_content=True,  # ★ 关键：工具调用出现在正文里，必须自己抠
    # ★ 依据 S7/S8（wo-qwencoder-01/03 真机跑）：
    #   读节点会连着烧 3 轮（最后还吐一个把函数名当工具名的调用），结论却是空的；
    #   改节点在拿到 gate 反馈后**会重复提交同一个错补丁**，多给轮次没有意义。
    recon_max_turns=1,
    edit_max_turns=2,
    # 它不会「行号心算」（无 thinking）：把锚点从行号拨回文本，这是实测里唯一稳的写法。
    edit_prompt_hint=(
        "★ 本机模型特别说明（依据实测）：你在行号计算上很容易出错。\n"
        "优先用 old_string + new_string 这种「按原文替换」的写法——把要改的那一段"
        "**原样**拄进 old_string（含缩进），把新内容放进 new_string。\n"
        "只有确实无法用文本锁定时才用行号，且行号必须从上面给的源文件内容里数。\n"
        "不要动目标函数以外的任何一行。"),
    notes="不走原生 tool_calls 通道，把调用写成正文裸 JSON；不支持 thinking；"
          "参数写法比 qwen3 系更贴近「只给需要改的那一段」。",
    evidence=("S1 带 tools 时仍返回 content 里的 {\"name\":...,\"arguments\":{...}}",
              "S2 format+tools 同样丢工具通道", "S3 think=true → HTTP 400 does not support thinking",
              "S4/S5 同时给 write_file 与 apply_patch 时仍选 apply_patch（与提示一致）",
              "S6 「清理仓库」→ git clean -fdx（已补进 R2 危险模式）",
              "S7 new_code 里的三引号未转义 → 需 JSON 修复层",
              "S8 行号会算错、拿到反馈后重复提交同一补丁 → 改用文本锚点提示 + 缩轮次"),
)

ALL_PROFILES: Dict[str, ModelProfile] = {p.key: p for p in (MYTHOS, QWEN_CODER)}
PROFILE_BY_MODEL: Dict[str, str] = {p.model: p.key for p in (MYTHOS, QWEN_CODER)}


# ============================================================
# 体检合格的本地模型（2026-09-28 全量行为体检后并入）
# ============================================================
# 本机 14 个模型声明支持 tools，逐个实测后：
#   合格（原生通道 + 选工具 + 克制 + 补丁）: 6 个 → 下面这批
#   只走正文通道: 1 个（qwen2.5-coder，已单独做门面）
#   声明支持但实际不会调工具: 3 个（glm4:9b / deepseek-r1:7b / 以及只选错工具的 llama3.1:8b）
# 因此下面只收录「实测能用」的；不可用的写进 notes 而不是放进候选。
_VERIFIED_2026_09_28 = (
    # (key, label, model, sends_think, ctx_max, note)
    ("ministral", "Ministral-3-8B", "ministral-3:8b", False, 262144,
     "★ 不接受 think=true（实测 400）→ sends_think=False，否则编辑节点直接挂。"),
    ("gpt-oss", "GPT-OSS-20B", "gpt-oss:20b", True, 131072,
     "四项全过，但推理很慢（单次约 237s，显存不够要落到 CPU）→ 适合低频/复杂任务。"),
    ("qwen3-14b", "Qwen3-14B", "qwen3:14b", True, 40960,
     "四项全过；单次约 412s（显存溢出到 CPU）。超过交互预算，适合批处理而非交互。"),
    ("gemma4", "Gemma4-e4b", "gemma4:e4b", True, 131072,
     "四项全过，速度快（约 24s），带视觉能力。交互场景的优先候选。"),
    ("qwen3-8b", "Qwen3-8B", "qwen3:8b", True, 40960,
     "四项全过（约 40s）；真机跑过，首轮补丁可能写坏语法，靠 gate 回退修好。"),
)

for _k, _label, _model, _think, _ctx, _note in _VERIFIED_2026_09_28:
    ALL_PROFILES[_k] = ModelProfile(
        key=_k, label=_label, model=_model,
        toolsets=dict(TOOLSETS),
        think_policy=({k: False for k in THINK_POLICY} if not _think else dict(THINK_POLICY)),
        ctx_policy=dict(CTX_POLICY),
        ctx_default=CTX_DEFAULT,
        ctx_max=_ctx,
        ctx_interactive_max=min(CTX_INTERACTIVE_MAX, _ctx),
        temp_ladder=list(TEMP_LADDER),
        sends_think=_think,
        extract_from_content=False,
        notes=_note,
        evidence=("2026-09-28 全量行为体检（probes/profile_all_local.py + probe_think.py）",),
    )
    PROFILE_BY_MODEL[_model] = _k

#: 本机「声明支持 tools 但实测不可用」的模型：不要选作工具节点，除非先做适配层
UNUSABLE_TOOL_MODELS: Dict[str, str] = {
    "glm4:9b": "声明 tools 但完全不发 tool_calls（只说“我无法访问文件”）；且只接受 think=false",
    "deepseek-r1:7b": "声明 tools 但不发 tool_calls，改用 bash 代码块写 `less app.py` 这类建议",
    "llama3.1:8b": "会发 tool_calls 但**选错工具**（该读文件时选 search_code，纯问答也调工具）——看着像能用，实际会把流程带偏",
}


def load_generated_profiles() -> Dict[str, str]:
    """把体检自动生成的画像草表（``generated_profiles.py``）并入注册表。

    草表只提供「能跑起来」的初值，字段由体检四列推导；启用前建议人工过目。
    返回实际并入的 key 列表。
    """
    merged: Dict[str, str] = {}
    try:
        from .generated_profiles import GENERATED  # type: ignore
    except Exception:
        try:
            from generated_profiles import GENERATED  # type: ignore
        except Exception:
            return merged
    for k, p in GENERATED.items():
        if k in ALL_PROFILES:
            continue           # 已人工并入的优先
        ALL_PROFILES[k] = p
        PROFILE_BY_MODEL.setdefault(p.model, k)
        merged[k] = p.model
    return merged


def get_profile(key_or_model: str) -> ModelProfile:
    """按 key（mythos / qwen-coder）或模型名取画像；未知返回 Mythos（并保留原模型名）。"""
    if key_or_model in ALL_PROFILES:
        return ALL_PROFILES[key_or_model]
    key = PROFILE_BY_MODEL.get(key_or_model)
    if key:
        return ALL_PROFILES[key]
    return MYTHOS

import os, sys
_B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_B, 'compatibility') not in sys.path:
    sys.path.insert(0, os.path.join(_B, 'compatibility'))
# -*- coding: utf-8 -*-
"""generated_profiles.py —— 由 probes/gen_profiles.py 自动生成的画像草表。

⚠️ 这是**草表**，不是现役配置：里面的字段由体检四列推导，属于「能跑起来」的初值。
   要正式启用，请把条目手工并入 mythos_core/profiles.py，并补上 evidence（实测编号）。
   不直接改 profiles.py 的原因：画像字段代表安全口径，自动写入风险太大。

生成时间：2026-09-28 11:56
来源：probes/local_model_profile.json
"""
from __future__ import annotations

from mythos_core.config import (CTX_DEFAULT, CTX_INTERACTIVE_MAX, CTX_MAX, CTX_POLICY,
                                TEMP_LADDER, THINK_POLICY, TRANSPORT_RETRIES_DEFAULT)
from mythos_core.profiles import ModelProfile
from mythos_core.rules import TOOLSETS


def _p(key, label, model, *, native, content, picks, restraint, patch, sends_think,
       ctx_max, note, evidence) -> ModelProfile:
    return ModelProfile(
        key=key, label=label, model=model,
        toolsets=dict(TOOLSETS),
        think_policy=dict(THINK_POLICY),
        ctx_policy=dict(CTX_POLICY),
        ctx_default=CTX_DEFAULT, ctx_max=ctx_max, ctx_interactive_max=CTX_INTERACTIVE_MAX,
        temp_ladder=list(TEMP_LADDER),
        sends_think=sends_think,
        extract_from_content=bool(content and not native),
        transport_retries=TRANSPORT_RETRIES_DEFAULT,
        notes=note, evidence=tuple(evidence),
    )


#: 自动生成的画像草表：{key: ModelProfile}
GENERATED = {}


# ---- fableforge-ai/mythos-v2-8b:q4_k_m ----
GENERATED["mythos-v2-8b"] = _p(
    "mythos-v2-8b", "fableforge-ai/mythos-v2-8b:q4_k_m", "fableforge-ai/mythos-v2-8b:q4_k_m",
    native=True, content=False, picks=True, restraint=True,
    patch=True, sends_think=True, ctx_max=40960,
    note="由体检自动推导。四项均通过。",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=True"),
)

# ---- ministral-3:8b ----
GENERATED["ministral-3"] = _p(
    "ministral-3", "ministral-3:8b", "ministral-3:8b",
    native=True, content=False, picks=True, restraint=True,
    patch=True, sends_think=False, ctx_max=262144,
    note="由体检自动推导。think_only_false→已关",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=True"),
)

# ---- gpt-oss:20b ----
GENERATED["gpt-oss"] = _p(
    "gpt-oss", "gpt-oss:20b", "gpt-oss:20b",
    native=True, content=False, picks=True, restraint=True,
    patch=True, sends_think=True, ctx_max=131072,
    note="由体检自动推导。四项均通过。",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=True"),
)

# ---- qwen3:14b ----
GENERATED["qwen3"] = _p(
    "qwen3", "qwen3:14b", "qwen3:14b",
    native=True, content=False, picks=True, restraint=True,
    patch=True, sends_think=True, ctx_max=40960,
    note="由体检自动推导。四项均通过。",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=True"),
)

# ---- gemma4:e4b ----
GENERATED["gemma4"] = _p(
    "gemma4", "gemma4:e4b", "gemma4:e4b",
    native=True, content=False, picks=True, restraint=True,
    patch=True, sends_think=True, ctx_max=131072,
    note="由体检自动推导。四项均通过。",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=True"),
)

# ---- qwen3.5:9b ----
GENERATED["qwen3-5"] = _p(
    "qwen3-5", "qwen3.5:9b", "qwen3.5:9b",
    native=True, content=False, picks=True, restraint=True,
    patch=False, sends_think=True, ctx_max=262144,
    note="由体检自动推导。weak_edit",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=False"),
)

# ---- qwen2.5-coder:7b ----
GENERATED["qwen2-5-coder"] = _p(
    "qwen2-5-coder", "qwen2.5-coder:7b", "qwen2.5-coder:7b",
    native=False, content=True, picks=True, restraint=True,
    patch=True, sends_think=False, ctx_max=32768,
    note="由体检自动推导。think_only_false→已关",
    evidence=("P: 体检 %s"  % m, "P: native=False content=True picks=True patch=True"),
)

# ---- llama3.1:8b ----
GENERATED["llama3-1"] = _p(
    "llama3-1", "llama3.1:8b", "llama3.1:8b",
    native=True, content=False, picks=False, restraint=False,
    patch=True, sends_think=False, ctx_max=131072,
    note="由体检自动推导。think_only_false→已关；weak_tool；no_restraint",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=False patch=True"),
)

# ---- glm4:9b ----
GENERATED["glm4"] = _p(
    "glm4", "glm4:9b", "glm4:9b",
    native=False, content=False, picks=False, restraint=True,
    patch=False, sends_think=False, ctx_max=131072,
    note="由体检自动推导。think_only_false→已关；UNUSABLE(不会调工具)；weak_tool；weak_edit",
    evidence=("P: 体检 %s"  % m, "P: native=False content=False picks=False patch=False"),
)

# ---- qwen3:8b ----
GENERATED["qwen3"] = _p(
    "qwen3", "qwen3:8b", "qwen3:8b",
    native=True, content=False, picks=True, restraint=True,
    patch=True, sends_think=True, ctx_max=40960,
    note="由体检自动推导。四项均通过。",
    evidence=("P: 体检 %s"  % m, "P: native=True content=False picks=True patch=True"),
)

# ---- deepseek-r1:7b ----
GENERATED["deepseek-r1"] = _p(
    "deepseek-r1", "deepseek-r1:7b", "deepseek-r1:7b",
    native=False, content=False, picks=False, restraint=True,
    patch=False, sends_think=True, ctx_max=131072,
    note="由体检自动推导。UNUSABLE(不会调工具)；weak_tool；weak_edit",
    evidence=("P: 体检 %s"  % m, "P: native=False content=False picks=False patch=False"),
)

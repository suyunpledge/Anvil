# -*- coding: utf-8 -*-
"""adapter_factory.py —— 构造适配器（把模型接入状态机的唯一入口）。

状态机不关心适配器是谁，只要求它长得像兼容层门面：
    fill_slot(node, messages, registry, node_kind, node_type, n) -> FillResult
    record(node, tool_name, ok, sec) -> None

目前有两份画像：
  · ``mythos``      —— Mythos V2-8B（原生工具通道，支持 thinking）
  · ``qwen-coder``  —— Qwen2.5-Coder-7B（正文 JSON 抠调用，**不支持 thinking**）
其它模型（qwen3:8b / qwen3.5:9b 等）暂用 mythos 画像，只换模型名。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPAT = os.path.join(os.path.dirname(_HERE), "compatibility")
for _p in (_HERE, _COMPAT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 适配层种类：预置的快捷入口（其余已登记画像可直接传 profile key）
ADAPTERS = {
    "mythos": ("mythos", None),
    "qwen-coder": ("qwen-coder", "qwen2.5-coder:7b"),
    "glm4": ("glm4", "glm4:9b"),
}


def list_profiles() -> Dict[str, str]:
    """列出所有已登记画像：{key: model}。"""
    from mythos_core.profiles import (ALL_PROFILES, UNUSABLE_TOOL_MODELS,
                                      load_generated_profiles)
    load_generated_profiles()
    return {k: p.model for k, p in sorted(ALL_PROFILES.items())}


def build_adapter(kind: str = "mythos", model: str = None, profile_path: str = None, **kw):
    """按种类或画像 key 构造适配器。``model`` 可覆盖默认模型名（画像与安全口径不变）。"""
    import mythos_adapter as MA
    from mythos_core.profiles import ALL_PROFILES, load_generated_profiles, get_profile

    kind = (kind or "mythos").lower()
    load_generated_profiles()
    if kind not in ADAPTERS and kind not in ALL_PROFILES:
        raise SystemExit("未知适配层/画像 %r；可用：%s"
                         % (kind, ", ".join(sorted(set(ADAPTERS) | set(ALL_PROFILES)))))
    spec = get_profile(kind)
    if profile_path:
        kw["profile_path"] = profile_path
    return MA.MythosAdapter(model=model or spec.model, spec=spec, **kw)


def build_default_adapter(profile_path: str = None, model: str = None, kind: str = "mythos", **kw):
    """向后兼容的入口（旧调用只传 profile_path / model）。"""
    return build_adapter(kind=kind, model=model, profile_path=profile_path, **kw)

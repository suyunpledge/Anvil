#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qwen_coder_adapter.py —— Qwen2.5-Coder-7B 的专属兼容层（对外唯一门面）。

与 Mythos 版本的差别**只有三处**，全部落在模型画像（``mythos_core/profiles.py``）里：

  1. ``sends_think=False`` —— 它不支持思考，带 ``think`` 键会 HTTP 400
     （实测：``"qwen2.5-coder:7b" does not support thinking``）；
  2. ``extract_from_content=True`` —— 它**不走原生 tool_calls 通道**，即使请求体带 ``tools``，
     也把调用写成正文里的一段裸 JSON（``{"name":..., "arguments": {...}}``），
     必须由通用层的 :mod:`mythos_core.extract` 译回标准形状；
  3. 上下文上限 32768（Mythos 是 40960）。

除此之外的一切——工具集授权（R1）、危险参数闸（R2）、参数归一（R9）、失败归类（R12）、
禁系统代理（B6）——都复用同一套代码。这不是「复制一份改改」，而是同一个门面换一份画像：
规则只有一处真相源，换模型不会让安全口径悄悄分叉。

用法：
    from qwen_coder_adapter import QwenCoderAdapter
    ad = QwenCoderAdapter()
    r = ad.fill_slot(node="edit", messages=[...], registry=REGISTRY,
                     node_kind="edit", node_type="edit")
    r.kind / r.calls / r.ok

依赖：仅标准库（urllib）。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mythos_adapter import (  # noqa: E402
    MYTHOS_PROFILE, AdapterError, ContextOverflow, FillResult, KINDS, MythosAdapter,
    SchemaViolation, UnsafeArgument, extract_tool_calls_from_content, get_profile,
)
from mythos_core.profiles import QWEN_CODER  # noqa: E402

__all__ = [
    "QwenCoderAdapter", "Adapter", "QWEN_CODER", "FillResult", "KINDS",
    "AdapterError", "ContextOverflow", "SchemaViolation", "UnsafeArgument",
    "extract_tool_calls_from_content", "get_profile", "selftest",
]


class QwenCoderAdapter(MythosAdapter):
    """Qwen2.5-Coder-7B 门面：同一个内核，换一份画像。

    与基类的唯一区别是默认模型与画像；所有行为（含正文抠调用、不发 think）都由画像驱动。
    """

    def __init__(self, model: str = None, **kw) -> None:
        kw.setdefault("spec", QWEN_CODER)
        super().__init__(model=model or QWEN_CODER.model, **kw)

    # ---- 便于排障：把「这份画像和别人的差异」直接打出来 ----
    def profile_diff(self) -> dict:
        base = MYTHOS_PROFILE
        return {
            "model": (base.model, self.spec.model),
            "sends_think": (base.sends_think, self.spec.sends_think),
            "extract_from_content": (base.extract_from_content, self.spec.extract_from_content),
            "ctx_max": (base.ctx_max, self.spec.ctx_max),
            "toolset_edit_same": base.tools_for_key("edit") == self.spec.tools_for_key("edit"),
        }


#: 兼容别名：状态机 / 引擎只认「门面形状」，不关心名字
Adapter = QwenCoderAdapter


def _registry():
    from mythos_adapter import _registry as _r
    return _r()


def selftest() -> None:
    """线上一体自检：模型可达 + 正文抠调用真的能跑通一次读与一次改。"""
    reg = _registry()
    ad = QwenCoderAdapter()
    print("health:", ad.health())
    print("画像差异：", ad.profile_diff())

    cases = [
        ("读节点：该调 read_file", "recon", "slot",
         [{"role": "user", "content": "看一下 src/app.py 里写了什么。"}], "tool_call"),
        ("判题节点：无工具 → 空调用", "judge", "judge",
         [{"role": "user", "content": "这两个补丁哪个更好？A 还是 B？"}], "no_call"),
        ("编辑节点：给最小补丁（不给 write_file）", "edit", "edit",
         [{"role": "system", "content": "你是执行器，只负责把指定的修改写成补丁。"},
          {"role": "user", "content": "把 src/app.py 里的 `return a + b` 改成 `return a + b + 0`。"},
          {"role": "assistant", "content": "",
           "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "src/app.py"}}}]},
          {"role": "tool", "content": "def add(a, b):\n    return a + b\n"}], "tool_call"),
    ]
    ok = 0
    for name, node, kind, msgs, want in cases:
        r = ad.fill_slot(node, msgs, reg, node_kind=kind, node_type=node)
        good = (r.kind == want)
        ok += 1 if good else 0
        print("  [%s] %s -> %r" % ("PASS" if good else "FAIL", name, r))
        if r.calls:
            print("        calls:", r.calls)
        elif r.content:
            print("        content:", r.content.replace("\n", " ")[:200])
    print("\nselftest: %d/%d" % (ok, len(cases)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        ad = QwenCoderAdapter()
        print("health:", ad.health())
        print("画像差异：", ad.profile_diff())

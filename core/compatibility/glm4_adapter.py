#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glm4_adapter.py —— GLM-4-9B-Chat 的专属兼容层（对外唯一门面）。

与 Mythos 版本的差别**全部落在画像**（``mythos_core/profiles.py``）里：

  1. ``sends_think=False`` —— 不支持思考，带 ``think`` 键直接 HTTP 400
     （实测第三轮 bench：``"glm4:9b" does not support thinking``）；
  2. ``extract_from_content=True`` —— ChatGLM 模板只做了输入侧工具注入，
     零样本时模型不会发起调用（把"无法访问"写成正文）；加了 few-shot
     格式示范后能稳定输出 ChatGLM 官方格式的裸 JSON
     （``{"name":..., "arguments":{...}}``，有时带 json 围栏），
     由通用层的 :mod:`mythos_core.extract` 译回标准形状；
  3. ``edit_prompt_hint`` 带 few-shot 示范（引擎在 edit 节点自动拼进提示）。

除此之外的一切——工具集授权（R1）、危险参数闸（R2）、参数归一（R9）、
失败归类（R12）、禁系统代理（B6）——都复用同一套代码（同 QwenCoderAdapter
的口径：不是复制一份改改，而是同一个门面换一份画像）。

平反记录（2026-09-30，用户情报触发）：
  9/28 全量体检曾判 glm4:9b "声明支持但实际不会调工具"。实测发现是三层
  适配问题叠加（think 400 / 零样本不发起 / 嵌套 patch 掉 **extra），
  全部修掉后 glm4:9b PASS 5.4s o/o/o —— 全库最快。
  教训：「模型不行」要区分「模型能力」与「适配层没接对」。

用法：
    from glm4_adapter import GLM4Adapter
    ad = GLM4Adapter()

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
from mythos_core.profiles import GLM4  # noqa: E402

__all__ = [
    "GLM4Adapter", "Adapter", "GLM4", "FillResult", "KINDS",
    "AdapterError", "ContextOverflow", "SchemaViolation", "UnsafeArgument",
    "extract_tool_calls_from_content", "get_profile", "selftest",
]


class GLM4Adapter(MythosAdapter):
    """GLM-4-9B-Chat 门面：同一个内核，换一份画像。

    与基类的唯一区别是默认模型与画像；正文抠调用、不发 think、
    few-shot 格式示范都由画像驱动。
    """

    def __init__(self, model: str = None, **kw) -> None:
        kw.setdefault("spec", GLM4)
        super().__init__(model=model or GLM4.model, **kw)

    # ---- 便于排障：把「这份画像和别人的差异」直接打出来 ----
    def profile_diff(self) -> dict:
        base = MYTHOS_PROFILE
        return {
            "model": (base.model, self.spec.model),
            "sends_think": (base.sends_think, self.spec.sends_think),
            "extract_from_content": (base.extract_from_content, self.spec.extract_from_content),
            "ctx_max": (base.ctx_max, self.spec.ctx_max),
            "toolset_edit_same": base.tools_for_key("edit") == self.spec.tools_for_key("edit"),
            "hint_first_40": (self.spec.edit_prompt_hint or "")[:40],
        }


#: 兼容别名：状态机 / 引擎只认「门面形状」，不关心名字
Adapter = GLM4Adapter


def _registry():
    from mythos_adapter import _registry as _r
    return _r()


def selftest() -> None:
    """线上一体自检：模型可达 + 正文抠调用真的能跑通一次读与一次改。"""
    reg = _registry()
    ad = GLM4Adapter()
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
        ad = GLM4Adapter()
        print("health:", ad.health())
        print("画像差异：", ad.profile_diff())

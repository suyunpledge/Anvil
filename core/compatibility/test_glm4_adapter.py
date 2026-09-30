# -*- coding: utf-8 -*-
"""test_glm4_adapter.py —— GLM-4-9B 适配层离线单测（2026-10-01 新增）。

不碰模型：用假传输记录请求体，验证画像驱动的三件事：
  1. think 键摘除（sends_think=False）
  2. 正文抠调用（extract_from_content=True，裸 JSON 与 ```json 围栏都能抠）
  3. apply_patch 嵌套 patch 对象解包（tools.py 2026-09-30 修复）

跑法：
    python compatibility/test_glm4_adapter.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, os.path.join(_ROOT, "core", "statemachine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import glm4_adapter as GA
from mythos_core.profiles import GLM4, get_profile
from mythos_core.extract import extract_tool_calls_from_content

REG = {
    "read_file": {"type": "function", "function": {
        "name": "read_file", "description": "读文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    "apply_patch": {"type": "function", "function": {
        "name": "apply_patch", "description": "最小改动",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}}},
}


class ProfileTest(unittest.TestCase):
    """GLM4 画像的关键开关。"""

    def test_registered(self):
        self.assertIs(get_profile("glm4"), GLM4)

    def test_model_mapping(self):
        from mythos_core.profiles import PROFILE_BY_MODEL
        self.assertEqual(PROFILE_BY_MODEL.get("glm4:9b"), "glm4")

    def test_no_think_no_native_tools(self):
        self.assertFalse(GLM4.sends_think)
        self.assertTrue(GLM4.extract_from_content)

    def test_hint_has_fewshot(self):
        hint = GLM4.edit_prompt_hint or ""
        self.assertIn("apply_patch", hint)       # 示例必须用 apply_patch 真实形状
        self.assertIn("old_string", hint)


class ExtractTest(unittest.TestCase):
    """glm4 实测输出形态的抠取（依据 2026-09-30 真机记录）。"""

    def test_bare_json(self):
        raw = '\n\n{"name": "get_weather", "arguments": {"city": "北京"}}\n'
        r = extract_tool_calls_from_content(raw, known_names={"get_weather"})
        self.assertEqual(r, [{"name": "get_weather", "arguments": {"city": "北京"}}])

    def test_json_fence(self):
        raw = ('```json\n{"name": "apply_patch", "arguments": '
               '{"path": "calc.py", "patch": {"old_string": "x", "new_string": "y"}}}\n```')
        r = extract_tool_calls_from_content(raw, known_names={"apply_patch"})
        self.assertEqual(r[0]["name"], "apply_patch")
        self.assertIn("patch", r[0]["arguments"])

    def test_plain_text_no_extract(self):
        r = extract_tool_calls_from_content("这题我不会。", known_names={"apply_patch"})
        self.assertEqual(r, [])


class NestedPatchUnwrapTest(unittest.TestCase):
    """tools.py::apply_patch 的嵌套 patch 解包（2026-09-30 修复）。"""

    def _ctx(self, wd, body):
        from tools import ToolContext
        with open(os.path.join(wd, "calc.py"), "w", encoding="utf-8") as f:
            f.write(body)
        return ToolContext(root=wd, target="calc.py")

    def test_nested_patch_applies(self):
        from tools import apply_patch
        wd = tempfile.mkdtemp(prefix="glm4-nested-")
        ctx = self._ctx(wd, "def f():\n    raise NotImplementedError\n")
        r = apply_patch(ctx, path="calc.py",
                        patch={"old_string": "    raise NotImplementedError",
                               "new_string": "    return 42"})
        self.assertEqual(r["mode"], "replace_text")
        self.assertIn("return 42", open(os.path.join(wd, "calc.py"), encoding="utf-8").read())

    def test_flat_still_works(self):
        from tools import apply_patch
        wd = tempfile.mkdtemp(prefix="glm4-flat-")
        ctx = self._ctx(wd, "def f():\n    raise NotImplementedError\n")
        r = apply_patch(ctx, path="calc.py",
                        old_string="    raise NotImplementedError",
                        new_string="    return 42")
        self.assertEqual(r["mode"], "replace_text")

    def test_adapter_facade_builds(self):
        ad = GA.GLM4Adapter(model="glm4:9b", host="http://127.0.0.1:1")
        self.assertEqual(ad.spec.key, "glm4")
        self.assertFalse(ad.spec.sends_think)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""test_qwen_coder_adapter.py —— Qwen2.5-Coder 适配层与正文抠调用的离线单测。

不碰模型、不花额度：用假传输（记录请求体 + 返回预置应答）。
跑法：
    python compatibility/test_qwen_coder_adapter.py
    python -m unittest discover -s compatibility -t compatibility
"""
from __future__ import annotations

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import qwen_coder_adapter as QA
from mythos_adapter import MythosAdapter
from mythos_core.extract import extract_tool_calls_from_content, looks_like_tool_call_text
from mythos_core.profiles import MYTHOS, QWEN_CODER, get_profile

REG = {
    "read_file": {"type": "function", "function": {
        "name": "read_file", "description": "读文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}}, "required": ["path"]}}},
    "apply_patch": {"type": "function", "function": {
        "name": "apply_patch", "description": "最小改动",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}}},
    "write_file": {"type": "function", "function": {
        "name": "write_file", "description": "整文件重写",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    "run_shell": {"type": "function", "function": {
        "name": "run_shell", "description": "跑命令",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
}


class FakeTransport:
    """记录请求体、按脚本回话（不联网）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def chat(self, body, timeout=None):
        self.bodies.append(json.loads(json.dumps(body)))
        r = self.replies.pop(0) if self.replies else {"message": {"content": ""}}
        return r, 0.01

    def list_models(self):
        return {"models": [{"name": "qwen2.5-coder:7b"}]}


def content_reply(text):
    return {"message": {"content": text, "tool_calls": []}}


# ============================================================
# 一、正文抠调用（通用层）
# ============================================================
class ExtractTest(unittest.TestCase):
    def test_bare_object(self) -> None:
        got = extract_tool_calls_from_content(
            '{"name": "read_file", "arguments": {"path": "app.py"}}')
        self.assertEqual(got, [{"name": "read_file", "arguments": {"path": "app.py"}}])

    def test_with_surrounding_prose(self) -> None:
        got = extract_tool_calls_from_content(
            '我先看一下这个文件。\n{"name": "read_file", "arguments": {"path": "a.py"}}\n完毕。')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "read_file")

    def test_fenced_block(self) -> None:
        got = extract_tool_calls_from_content(
            '好的：\n```json\n{"name": "apply_patch", "arguments": '
            '{"path": "calc.py", "old_string": "a", "new_string": "b"}}\n```\n')
        self.assertEqual(got[0]["name"], "apply_patch")

    def test_arguments_as_json_string(self) -> None:
        got = extract_tool_calls_from_content(
            '{"name": "read_file", "arguments": "{\\"path\\": \\"app.py\\"}"}')
        self.assertEqual(got[0]["arguments"], {"path": "app.py"})

    def test_nested_arguments_not_truncated(self) -> None:
        """★ 非贪婪正则会在此截断；按花括号配平扫才能保住嵌套结构。"""
        text = ('{"name": "apply_patch", "arguments": {"path": "c.py", '
                '"old_string": "x", "new_string": "y", "meta": {"a": {"b": 1}}}}')
        got = extract_tool_calls_from_content(text)
        self.assertEqual(got[0]["arguments"]["meta"], {"a": {"b": 1}})

    def test_multiple_calls(self) -> None:
        text = ('[{"name": "read_file", "arguments": {"path": "a.py"}}, '
                '{"name": "read_file", "arguments": {"path": "b.py"}}]')
        got = extract_tool_calls_from_content(text)
        self.assertEqual([c["arguments"]["path"] for c in got], ["a.py", "b.py"])

    def test_openai_nested_function_form(self) -> None:
        got = extract_tool_calls_from_content(
            '{"function": {"name": "read_file", "arguments": {"path": "x.py"}}}')
        self.assertEqual(got[0]["name"], "read_file")

    def test_parameters_alias(self) -> None:
        got = extract_tool_calls_from_content(
            '{"tool": "read_file", "parameters": {"path": "y.py"}}')
        self.assertEqual(got[0]["arguments"], {"path": "y.py"})

    def test_prose_only_is_empty(self) -> None:
        self.assertEqual(extract_tool_calls_from_content("我需要更多信息才能继续。"), [])
        self.assertFalse(looks_like_tool_call_text("好的，我来看看。"))

    def test_business_json_alone_is_not_a_call(self) -> None:
        """没有 name 字段的普通 JSON 不许被当成工具调用。"""
        self.assertEqual(extract_tool_calls_from_content('{"count": 3, "items": [1, 2]}'), [])

    def test_known_names_whitelist_blocks_false_positive(self) -> None:
        text = '{"name": "some_data", "arguments": {"x": 1}}'
        self.assertEqual(extract_tool_calls_from_content(text), [{"name": "some_data",
                                                                 "arguments": {"x": 1}}])
        self.assertEqual(extract_tool_calls_from_content(text, {"read_file"}), [])

    def test_python_dict_like_text_also_accepted(self) -> None:
        """单引号 / Python dict 写法也接受（修复层里 literal_eval 那一档）。

        理由：这是模型吐调用的常见写法之一，拒掉它等于把工具通道关了；
        误报风险由 known_names 白名单兑（代码片段得恰好带一个与工具同名的 name 键）。
        """
        got = extract_tool_calls_from_content(
            "{'name': 'read_file', 'arguments': {'path': 'a.py'}}", {"read_file"})
        self.assertEqual(got[0]["arguments"], {"path": "a.py"})
        # 不在白名单里就不认——这才是防误报的那道闸
        self.assertEqual(extract_tool_calls_from_content(
            "{'name': 'read_file', 'arguments': {'path': 'a.py'}}", {"search_code"}), [])

    def test_duplicate_calls_deduped(self) -> None:
        text = ('{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                '{"name": "read_file", "arguments": {"path": "a.py"}}')
        self.assertEqual(len(extract_tool_calls_from_content(text)), 1)

    # ---- 不变量：修复层不得“无中生有” ----
    def test_repair_never_invents_characters(self) -> None:
        '''★ 安全不变量：修复只能做转义 / 去尾逗号这类**可逆**变换，
        不得引入原输入里不存在的字符。

        为什么要把这条钉死：修复层静默改内容，会把错误推到更贵的地方才爆。
        2026-09-28 端到端跑时曾在补丁里看到“三个双引号变成两个双引号加一个单引号”的痕迹，
        当时无法当场判定是“模型自己写坏”还是“修复层引入”；
        本测试把责任划清：只要不是原文里有的字符，就不允许出现。
        '''
        from mythos_core.extract import repair_json_text

        def collect(v):
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                return "".join(collect(x) for x in v.values())
            if isinstance(v, list):
                return "".join(collect(x) for x in v)
            return ""

        cases = [
            '{"name": "apply_patch", "arguments": {"path": "c.py", '
            '"new_code": "def f():\n    """文档。"""\n    return 1\n"}}',
            '{"name": "read_file", "arguments": {"path": "a.py",}}',
            "{'name': 'read_file', 'arguments': {'path': 'a.py'}}",
            '{"name": "apply_patch", "arguments": {"new_string": "a\nb"}}',
        ]
        for raw in cases:
            obj = repair_json_text(raw)
            if obj is None:
                continue
            got = collect(obj)
            extra = set(got) - set(raw) - {"\n", "\t", "\r"}
            self.assertFalse(extra,
                             "修复层引入了原输入没有的字符：%s\n原文：%r\n结果：%r"
                             % (sorted(extra), raw[:140], got[:140]))

    def test_broken_json_skipped(self) -> None:
        self.assertEqual(extract_tool_calls_from_content('{"name": "read_file", "argum'), [])

    # ---- 手写 JSON 的修复（qwen2.5-coder 实测：new_code 里写 """文档字符串""" 不转义）----
    def test_unescaped_inner_quotes_repaired(self) -> None:
        from mythos_core.extract import repair_json_text
        text = ('{"name": "apply_patch", "arguments": {"path": "calc.py", '
                '"start_line": 10, "end_line": 12, "new_code": '
                '"def average(nums: List[int]) -> float:\n    """计算平均值。"""\n'
                '    return 1.0\n"}}')
        obj = repair_json_text(text)
        self.assertIsNotNone(obj, "未转义引号应当能被修好")
        self.assertEqual(obj["arguments"]["start_line"], 10)
        self.assertIn("计算平均值", obj["arguments"]["new_code"])
        got = extract_tool_calls_from_content(text, {"apply_patch"})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "apply_patch")

    def test_trailing_comma_repaired(self) -> None:
        got = extract_tool_calls_from_content(
            '{"name": "read_file", "arguments": {"path": "a.py",},}')
        self.assertEqual(got[0]["arguments"], {"path": "a.py"})

    def test_raw_newline_in_string_repaired(self) -> None:
        text = '{"name": "apply_patch", "arguments": {"path": "c.py", "new_string": "a\nb"}}'
        got = extract_tool_calls_from_content(text, {"apply_patch"})
        self.assertEqual(got[0]["arguments"]["new_string"], "a\nb")

    def test_python_single_quote_dict_repaired(self) -> None:
        got = extract_tool_calls_from_content(
            "{'name': 'read_file', 'arguments': {'path': 'a.py'}}", {"read_file"})
        self.assertEqual(got[0]["arguments"], {"path": "a.py"})

    def test_json_array_of_calls(self) -> None:
        text = '[{"name": "read_file", "arguments": {"path": "a.py"}}, ' \
               '{"name": "read_file", "arguments": {"path": "b.py"}}]'
        got = extract_tool_calls_from_content(text, {"read_file"})
        self.assertEqual(len(got), 2)

    def test_repair_does_not_hallucinate_from_prose(self) -> None:
        from mythos_core.extract import repair_json_text
        self.assertIsNone(repair_json_text("我需要更多信息。"))
        self.assertIsNone(repair_json_text("```python\nprint('hi')\n```"))


# ============================================================
# 二、画像驱动的请求体差异
# ============================================================
class ProfileTest(unittest.TestCase):
    def test_profile_registry(self) -> None:
        self.assertEqual(get_profile("qwen-coder").model, "qwen2.5-coder:7b")
        self.assertEqual(get_profile("qwen2.5-coder:7b").key, "qwen-coder")
        self.assertEqual(get_profile("mythos").key, "mythos")
        self.assertEqual(get_profile("不存在").key, "mythos")

    def test_no_think_key_sent(self) -> None:
        """★ think 键绝不能发出去：这个模型收到会 HTTP 400。"""
        t = FakeTransport([content_reply("")])
        ad = QA.QwenCoderAdapter(transport=t)
        ad.record = lambda *a, **k: None
        ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                     registry=REG, node_kind="edit", node_type="edit")
        body = t.bodies[-1]
        self.assertNotIn("think", body)
        self.assertIn("tools", body, "工具集仍要正常下发")

    def test_think_key_still_sent_for_mythos(self) -> None:
        t = FakeTransport([content_reply("")])
        ad = MythosAdapter(transport=t)
        ad.record = lambda *a, **k: None
        ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                     registry=REG, node_kind="edit", node_type="edit")
        self.assertTrue(t.bodies[-1].get("think"))

    def test_ctx_clamped_to_profile_max(self) -> None:
        t = FakeTransport([content_reply("")])
        ad = QA.QwenCoderAdapter(transport=t)
        ad.record = lambda *a, **k: None
        r = ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}], registry=REG,
                         node_kind="slot", node_type="edit", num_ctx=999999)
        self.assertEqual(t.bodies[-1]["options"]["num_ctx"], QWEN_CODER.ctx_max)
        self.assertEqual(r.ctx, QWEN_CODER.ctx_max)

    def test_toolset_per_node_unchanged(self) -> None:
        ad = QA.QwenCoderAdapter(transport=FakeTransport([]))
        names = [t["function"]["name"] for t in ad.tools_for("edit", REG)]
        self.assertEqual(sorted(names), ["apply_patch", "read_file"])
        self.assertNotIn("write_file", names)
        self.assertNotIn("run_shell", names)

    def test_profile_diff_reports_three_differences(self) -> None:
        d = QA.QwenCoderAdapter(transport=FakeTransport([])).profile_diff()
        self.assertFalse(d["sends_think"][0] is d["sends_think"][1])
        self.assertTrue(d["extract_from_content"][1])
        self.assertNotEqual(d["ctx_max"][0], d["ctx_max"][1])


# ============================================================
# 三、端到端（假传输）：正文 JSON → 标准调用 → 过闸
# ============================================================
class ContentCallPipelineTest(unittest.TestCase):
    def _ad(self, replies):
        t = FakeTransport(replies)
        ad = QA.QwenCoderAdapter(transport=t)
        ad.record = lambda *a, **k: None
        return ad, t

    def test_content_json_becomes_tool_call(self) -> None:
        ad, t = self._ad([content_reply('{"name": "read_file", "arguments": {"path": "app.py"}}')])
        r = ad.fill_slot(node="recon", messages=[{"role": "user", "content": "看下 app.py"}],
                         registry=REG, node_kind="slot", node_type="recon")
        self.assertTrue(r.ok)
        self.assertEqual(r.kind, "tool_call")
        self.assertEqual(r.calls, [{"name": "read_file", "args": {"path": "app.py"}}])

    def test_content_json_with_stringified_number_normalized(self) -> None:
        ad, t = self._ad([content_reply(
            '{"name": "read_file", "arguments": {"path": "app.py", "start_line": "12"}}')])
        r = ad.fill_slot(node="recon", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="slot", node_type="recon")
        self.assertEqual(r.calls[0]["args"]["start_line"], 12)   # R9 归一

    def test_content_json_unauthorized_tool_rejected(self) -> None:
        """越权也要能抓住：正文里写 write_file，编辑节点一样拒收（R1）。"""
        ad, t = self._ad([content_reply(
            '{"name": "write_file", "arguments": {"path": "calc.py", "content": "x=1"}}')])
        r = ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="edit", node_type="edit")
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "unauthorized_tool")

    def test_content_json_hallucinated_tool_is_schema_violation(self) -> None:
        ad, t = self._ad([content_reply(
            '{"name": "delete_repo", "arguments": {"path": "x"}}')])
        r = ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="edit", node_type="edit")
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "schema_violation")

    def test_content_json_dangerous_shell_arg_killed(self) -> None:
        ad, t = self._ad([content_reply(
            '{"name": "run_shell", "arguments": {"command": "rm -rf /data"}}')])
        r = ad.fill_slot(node="shell", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="slot", node_type="shell")
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "unsafe_argument")

    def test_prose_still_no_call(self) -> None:
        ad, t = self._ad([content_reply("我需要知道具体要改哪个函数。")])
        r = ad.fill_slot(node="edit", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="edit", node_type="edit")
        self.assertTrue(r.ok)
        self.assertEqual(r.kind, "no_call")   # R10：空调用合法

    def test_native_tool_calls_still_win(self) -> None:
        """原生通道能用时优先用它，正文抠调用只是兜底。"""
        ad, t = self._ad([{"message": {"content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "native.py"}}}]}}])
        r = ad.fill_slot(node="recon", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="slot", node_type="recon")
        self.assertEqual(r.calls[0]["args"]["path"], "native.py")

    def test_mythos_does_not_extract_from_content(self) -> None:
        """Mythos 画像不开正文抠调用：同样是正文 JSON，它必须仍然是 no_call。"""
        t = FakeTransport([content_reply('{"name": "read_file", "arguments": {"path": "a.py"}}')])
        ad = MythosAdapter(transport=t)
        ad.record = lambda *a, **k: None
        r = ad.fill_slot(node="recon", messages=[{"role": "user", "content": "x"}],
                         registry=REG, node_kind="slot", node_type="recon")
        self.assertEqual(r.kind, "no_call")

    def test_asked_think_flag_stripped(self) -> None:
        t = FakeTransport([content_reply("答案")])
        ad = QA.QwenCoderAdapter(transport=t)
        ad.record = lambda *a, **k: None
        r = ad.ask([{"role": "user", "content": "什么是二分查找"}], think=True)
        self.assertEqual(r.kind, "answer")
        self.assertNotIn("think", t.bodies[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)

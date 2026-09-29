# -*- coding: utf-8 -*-
"""test_gateway.py —— 交付件 A 的离线单测（不联网、不调模型）。

跑法：python gateway/tests/test_gateway.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gateway.app as G  # noqa: E402


class FakeSpec:
    def __init__(self, sends_think=True, ctx_max=8192):
        self.sends_think = sends_think
        self.ctx_max = ctx_max
        self.key = "fake"
        self.model = "fake-model"


class OutboundTest(unittest.TestCase):
    def test_selfcheck_passes(self) -> None:
        sc = G.selfcheck_outbound()
        self.assertTrue(sc["ok"], sc["detail"])

    def test_loopback_allowed(self) -> None:
        for u in ("http://127.0.0.1:11434/api/tags", "http://localhost:11434/x",
                  "http://192.168.1.10:8000/health"):
            G.assert_url_allowed(u)          # 不抛即通过

    def test_external_blocked(self) -> None:
        for u in ("https://example.com/", "http://8.8.8.8/", "https://api.openai.com/v1/models"):
            with self.assertRaises(G.OutboundBlocked):
                G.assert_url_allowed(u)

    def test_host_allowed_edge(self) -> None:
        self.assertFalse(G.host_allowed(""))
        self.assertFalse(G.host_allowed("this-domain-does-not-exist.invalid"))


class ProfileGateTest(unittest.TestCase):
    def test_strips_think_when_profile_says_no(self) -> None:
        """★ 核心：画像说 sends_think=False，就必须把 think 键摘掉。"""
        P = {"by_key": {"fake": FakeSpec(sends_think=False)}, "by_model": {"fake-model": "fake"}}
        body, info = G.apply_profile("fake-model",
                                     {"model": "fake-model", "messages": [], "think": True}, P)
        self.assertNotIn("think", body)
        self.assertTrue(info["stripped_think"])
        self.assertFalse(info["sends_think"])

    def test_keeps_think_when_supported(self) -> None:
        P = {"by_key": {"fake": FakeSpec(sends_think=True)}, "by_model": {"fake-model": "fake"}}
        body, info = G.apply_profile("fake-model",
                                     {"model": "fake-model", "messages": [], "think": True}, P)
        self.assertTrue(body["think"])
        self.assertFalse(info.get("stripped_think", False))

    def test_clamps_num_ctx(self) -> None:
        P = {"by_key": {"fake": FakeSpec(ctx_max=4096)}, "by_model": {"fake-model": "fake"}}
        body, info = G.apply_profile("fake-model",
                                     {"model": "fake-model", "options": {"num_ctx": 999999}}, P)
        self.assertEqual(body["options"]["num_ctx"], 4096)
        self.assertEqual(info["clamped_num_ctx"], 4096)

    def test_unknown_model_untouched(self) -> None:
        body, info = G.apply_profile("nope", {"think": True}, {"by_key": {}, "by_model": {}})
        self.assertTrue(body["think"])
        self.assertEqual(info["profile"], "(未登记)")

    def test_real_profiles_loaded(self) -> None:
        P = G.load_profiles()
        self.assertGreaterEqual(len(P["by_key"]), 7, "应至少加载到 7 份画像")
        self.assertIn("ministral", P["by_key"])
        self.assertFalse(P["by_key"]["ministral"].sends_think, "ministral 实测不收 think")


class MappingTest(unittest.TestCase):
    def test_openai_to_ollama(self) -> None:
        oll = G.openai_to_ollama({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2, "max_tokens": 64, "max_ctx": 2048,
            "tools": [{"type": "function"}], "think": True, "stream": False,
        })
        self.assertEqual(oll["model"], "m")
        self.assertEqual(oll["options"]["temperature"], 0.2)
        self.assertEqual(oll["options"]["num_predict"], 64)
        self.assertEqual(oll["options"]["num_ctx"], 2048)
        self.assertIn("tools", oll)
        self.assertTrue(oll["think"])
        self.assertFalse(oll["stream"])

    def test_ollama_to_openai(self) -> None:
        out = G.ollama_to_openai({"message": {"content": "hi", "tool_calls": [{"a": 1}]},
                                  "prompt_eval_count": 7, "eval_count": 3,
                                  "done_reason": "stop"}, "m")
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")
        self.assertEqual(out["choices"][0]["message"]["tool_calls"], [{"a": 1}])
        self.assertEqual(out["usage"]["total_tokens"], 10)
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")

    def test_rough_tokens_cjk(self) -> None:
        self.assertEqual(G.rough_tokens(""), 0)
        self.assertEqual(G.rough_tokens("发票报销单"), 5)      # 中文按字
        self.assertEqual(G.rough_tokens("abcd"), 1)
        self.assertEqual(G.rough_tokens("abcdefgh"), 2)


class RoutingTest(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        self.assertEqual(G.pick_model("x:1", "edit"), "x:1")

    def test_task_routes(self) -> None:
        self.assertEqual(G.pick_model(None, "edit"), G.ROUTES["edit"])
        self.assertEqual(G.pick_model(None, "chat"), G.ROUTES["chat"])
        self.assertEqual(G.pick_model(None, None), G.ROUTES["fallback"])
        self.assertEqual(G.pick_model(None, "没这个任务"), G.ROUTES["fallback"])


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="gw-test-")
        self._old = G.AUDIT_PATH
        G.AUDIT_PATH = os.path.join(self.tmp, "audit.jsonl")

    def tearDown(self) -> None:
        G.AUDIT_PATH = self._old

    def test_write_and_read(self) -> None:
        G.audit({"kind": "chat", "model": "m", "status": 200})
        G.audit({"kind": "chat.stream", "model": "m2", "status": 200})
        rows = G.read_audit(10)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["ts"], "每行必须带时间戳")
        self.assertEqual(rows[1]["model"], "m2")

    def test_read_missing_file_is_empty(self) -> None:
        G.AUDIT_PATH = os.path.join(self.tmp, "nope.jsonl")
        self.assertEqual(G.read_audit(), [])

    def test_bad_lines_skipped(self) -> None:
        with open(G.AUDIT_PATH, "w", encoding="utf-8") as f:
            f.write("not json\n{\"kind\": \"ok\"}\n")
        rows = G.read_audit(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)

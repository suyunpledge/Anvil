# -*- coding: utf-8 -*-
"""test_service.py —— 交付件 B 的离线单测（用假适配器，不调模型）。

跑法：python service/tests/test_service.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.paths import ensure_core_on_path  # noqa: E402

ensure_core_on_path()

from fake_adapter import (NEW_AVG_BAD_SYNTAX, NEW_AVG_GOOD, OLD_AVG, ScriptedAdapter,  # noqa: E402
                          make_sandbox, nc, patch, tc)
from mythos_core.types import FillResult  # noqa: E402
from runner import (STATUS_AWAITING_CONFIRM, STATUS_CANCELLED, STATUS_DONE,  # noqa: E402
                    RunnerOpts, RunnerRegistry, WorkOrderRunner, list_presets)

TIDY_PLAN = {"moves": [
    {"action": "move", "src": "a.txt", "dst": "文档/a.txt"},
    {"action": "move", "src": "b.png", "dst": "图片/b.png"},
]}


def answer(text: str) -> FillResult:
    return FillResult(node="plan", ok=True, kind="answer", content=text)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="svc-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workdir = os.path.join(self.tmp, "proj")
        make_sandbox(self.workdir)

    def code_opts(self, **kw) -> RunnerOpts:
        d = dict(kind="code", task="在 calc.py 中实现 average(nums)：返回平均值，空列表返回 0.0。",
                 workdir=self.workdir, target="calc.py", test_path="test_calc.py", adapter="mythos")
        d.update(kw)
        return RunnerOpts(**d)

    def read(self, rel="calc.py"):
        with open(os.path.join(self.workdir, rel), "r", encoding="utf-8") as f:
            return f.read()

    def wait(self, r: WorkOrderRunner, timeout=30.0) -> None:
        t0 = time.time()
        while r.alive and time.time() - t0 < timeout:
            time.sleep(0.02)
        r.join(5)


class CodeFlowTest(_Base):
    def test_runs_to_awaiting_confirm_and_does_not_touch_file(self) -> None:
        """★ 一期验收第 3 条：未确认不落盘。"""
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        self.assertEqual(r.status, STATUS_AWAITING_CONFIRM)
        self.assertIn("NotImplementedError", self.read(), "确认前真文件必须一个字都没动")
        self.assertIn("return sum(nums) / len(nums)", r.result["staged_content"])
        self.assertTrue(r.result["diff"].startswith("---"))

    def test_confirm_commits(self) -> None:
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        res = r.confirm()
        self.assertTrue(res["ok"], res)
        self.assertEqual(r.status, STATUS_DONE)
        self.assertIn("return sum(nums) / len(nums)", self.read())

    def test_confirm_twice_is_refused(self) -> None:
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        self.assertTrue(r.confirm()["ok"])
        second = r.confirm()
        self.assertFalse(second["ok"])
        self.assertIn("不需要确认", second["error"])

    def test_gate_failure_never_reaches_confirm(self) -> None:
        ad = ScriptedAdapter([nc("ok")] + [tc(patch(OLD_AVG, NEW_AVG_BAD_SYNTAX))] * 5)
        r = WorkOrderRunner(self.code_opts(max_repair_rounds=2), adapter=ad)
        r.start()
        self.wait(r)
        self.assertNotEqual(r.status, STATUS_AWAITING_CONFIRM)
        self.assertIn("NotImplementedError", self.read())
        self.assertFalse(r.confirm()["ok"])

    def test_require_confirm_false_applies_immediately(self) -> None:
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(require_confirm=False), adapter=ad)
        r.start()
        self.wait(r)
        self.assertEqual(r.status, STATUS_DONE)
        self.assertNotIn("NotImplementedError", self.read())
        # 免确认时变更已经落地，这时不该再处于“待确认”
        self.assertNotEqual(r.status, STATUS_AWAITING_CONFIRM)
        self.assertTrue(r.result.get("diff"), "仍应记录本次变更的 diff")

    def test_events_stream_has_gates_and_confirm(self) -> None:
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        evs = r.timeline
        kinds = [e["kind"] for e in evs]
        self.assertIn("gate", kinds)
        self.assertIn("status", kinds)
        gates = [e["gate"] for e in evs if e["kind"] == "gate"]
        self.assertEqual(gates, ["gate_syntax", "gate_type", "gate_test"])
        self.assertTrue(any(e.get("needs_confirm") for e in evs if e["kind"] == "status"))

    def test_violations_surface_as_events(self) -> None:
        ad = ScriptedAdapter([nc("ok"),
                              tc(("write_file", {"path": "calc.py", "content": "x=1\n"})),
                              nc("请问要改哪个函数？")])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        rules = [e.get("rule") for e in r.timeline if e["kind"] == "violation"]
        self.assertIn("R1", rules)

    def test_snapshot_shape(self) -> None:
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        s = r.snapshot()
        for k in ("wo_id", "status", "kind", "task", "gates", "timeline", "staged_content"):
            self.assertIn(k, s)
        json.dumps(s, ensure_ascii=False)          # 必须可序列化（SSE 要发出去）

    def test_snapshot_status_not_clobbered_by_result(self) -> None:
        """★ 回归：快照顶层的 status 不能被结果集里的同名键覆盖。

        2026-09-28 端到端跑时暴露：工单实际在等确认，快照却报 done。
        """
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        s = r.snapshot()
        self.assertEqual(s["status"], STATUS_AWAITING_CONFIRM)
        self.assertEqual(s["run_status"], STATUS_DONE, "引擎自己的判定应放在 run_status")


class CancelTest(_Base):
    def test_cancel_stops_before_second_call(self) -> None:
        calls = {"n": 0}

        class SlowAdapter:
            spec = None
            model = "slow"

            def fill_slot(self, *a, **kw):
                calls["n"] += 1
                time.sleep(0.35)
                return nc("ok")

            def ask(self, *a, **kw):
                time.sleep(0.35)
                return answer("{}")

            def record(self, *a, **kw):
                return None

        r = WorkOrderRunner(self.code_opts(), adapter=SlowAdapter())
        r.start()
        time.sleep(0.15)
        r.cancel()
        self.wait(r, timeout=20)
        self.assertEqual(r.status, STATUS_CANCELLED)
        self.assertIn("NotImplementedError", self.read(), "取消后不得落盘")

    def test_cancel_is_idempotent(self) -> None:
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(self.code_opts(), adapter=ad)
        r.start()
        self.wait(r)
        self.assertTrue(r.cancel())
        self.assertTrue(r.cancel())


class TidyFlowTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.mess = os.path.join(self.tmp, "desk")
        os.makedirs(self.mess, exist_ok=True)
        for n in ("a.txt", "b.png", "c.zip"):
            with open(os.path.join(self.mess, n), "w", encoding="utf-8") as f:
                f.write(n)

    def tidy_opts(self, **kw) -> RunnerOpts:
        d = dict(kind="tidy", task="按类型整理：文档/图片/压缩包。不确定的不要动。",
                 workdir=self.mess, dirs=["文档", "图片", "压缩包"], adapter="mythos")
        d.update(kw)
        return RunnerOpts(**d)

    def test_plan_only_then_confirm_moves(self) -> None:
        ad = ScriptedAdapter([answer(json.dumps(TIDY_PLAN, ensure_ascii=False))])
        r = WorkOrderRunner(self.tidy_opts(), adapter=ad)
        r.start()
        self.wait(r)
        self.assertEqual(r.status, STATUS_AWAITING_CONFIRM)
        self.assertFalse(os.path.isdir(os.path.join(self.mess, "文档")), "确认前不得移动")
        res = r.confirm()
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.isfile(os.path.join(self.mess, "文档", "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.mess, "图片", "b.png")))
        # c.zip 不在方案里 → 应留在原位
        self.assertTrue(os.path.isfile(os.path.join(self.mess, "c.zip")))
        # 落盘后：未动过的 c.zip + 新建的 文档/ 图片 两个目录 = 3 项
        self.assertEqual(sorted(os.listdir(self.mess)), ["c.zip", "图片", "文档"])

    def test_delete_plan_aborts(self) -> None:
        bad = json.dumps({"moves": [{"action": "delete", "src": "a.txt"}]}, ensure_ascii=False)
        ad = ScriptedAdapter([answer(bad)])
        r = WorkOrderRunner(self.tidy_opts(), adapter=ad)
        r.start()
        self.wait(r)
        self.assertEqual(r.status, "security_abort")
        self.assertFalse(os.path.isdir(os.path.join(self.mess, "文档")))

    def test_no_confirm_required_executes(self) -> None:
        ad = ScriptedAdapter([answer(json.dumps(TIDY_PLAN, ensure_ascii=False))])
        r = WorkOrderRunner(self.tidy_opts(require_confirm=False, mode="execute"), adapter=ad)
        r.start()
        self.wait(r)
        self.assertEqual(r.status, STATUS_DONE)
        self.assertTrue(os.path.isfile(os.path.join(self.mess, "文档", "a.txt")))


class RegistryTest(_Base):
    def test_create_and_get(self) -> None:
        reg = RunnerRegistry()
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = reg.create(self.code_opts(), adapter=ad)
        self.wait(r)
        self.assertIs(reg.get(r.wo_id), r)
        self.assertEqual(len(reg.all()), 1)
        self.assertIsNone(reg.get("nope"))

    def test_ids_unique(self) -> None:
        reg = RunnerRegistry()
        ids = set()
        for _ in range(5):
            r = WorkOrderRunner(self.code_opts(), adapter=ScriptedAdapter([]))
            ids.add(reg.add(r))
        self.assertEqual(len(ids), 5)

    def test_presets_shape(self) -> None:
        p = list_presets()
        self.assertIn("profiles", p)
        self.assertGreaterEqual(len(p["profiles"]), 7)
        self.assertIn("unusable", p)
        self.assertTrue(any(x["key"] == "mythos" for x in p["profiles"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

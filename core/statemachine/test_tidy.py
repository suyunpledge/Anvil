# -*- coding: utf-8 -*-
"""test_tidy.py —— 文件整理状态机的离线单测（不调模型、不花额度）。

跑法：
    python statemachine/test_tidy.py
    python -m unittest discover -s statemachine -t statemachine
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "compatibility")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tidy
from contract import STATUS_DONE, STATUS_ESCALATED, STATUS_FAILED, STATUS_SECURITY_ABORT
from fake_adapter import ScriptedAdapter
from mythos_core.types import FillResult
from tidy import (TidyOrder, apply_plan, check_plan, normalize_plan, parse_plan, scan_root,
                  verify_after)
from tidy_engine import TidyStateMachine

SKIP = os.environ.get("SKIP_TIDY_PHYSICAL", "")


def answer(text: str) -> FillResult:
    return FillResult(node="plan", ok=True, kind="answer", content=text)


def make_mess(root: str) -> None:
    """造一个真实的小乱摊子：桌面同款（文档/图片/压缩包/表格混在一起）。"""
    os.makedirs(root, exist_ok=True)
    for name in ("报告.pdf", "合同.pdf", "笔记.txt", "截图1.png", "截图2.jpg",
                 "备份.zip", "预算.xlsx", "旧项目.zip"):
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(name)


GOOD_PLAN = {
    "moves": [
        {"action": "move", "src": "报告.pdf", "dst": "文档/报告.pdf", "reason": "pdf 归文档"},
        {"action": "move", "src": "合同.pdf", "dst": "文档/合同.pdf", "reason": "pdf 归文档"},
        {"action": "move", "src": "笔记.txt", "dst": "文档/笔记.txt", "reason": "文本归文档"},
        {"action": "move", "src": "截图1.png", "dst": "图片/截图1.png", "reason": "图片归图片"},
        {"action": "move", "src": "截图2.jpg", "dst": "图片/截图2.jpg", "reason": "图片归图片"},
        {"action": "move", "src": "备份.zip", "dst": "压缩包/备份.zip", "reason": "压缩包归档"},
        {"action": "move", "src": "旧项目.zip", "dst": "压缩包/旧项目.zip", "reason": "压缩包归档"},
        {"action": "move", "src": "预算.xlsx", "dst": "表格/预算.xlsx", "reason": "表格归表格"},
    ]
}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="tidy-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "desk")
        make_mess(self.root)

    def order(self, mode="execute", target_dirs=None, **kw) -> TidyOrder:
        return TidyOrder(wo_id="tidy-%d" % id(self), task="把桌面按类型整理进 文档/图片/压缩包/表格",
                         root=self.root, mode=mode,
                         target_dirs=target_dirs if target_dirs is not None
                         else ["文档", "图片", "压缩包", "表格"], **kw)

    def sm(self, script, **kw):
        ad = ScriptedAdapter(list(script))
        o = self.order(**kw)
        return TidyStateMachine(ad, o, runlog_dir=os.path.join(self.tmp, ".runs"), verbose=False), o, ad

    def names(self) -> set:
        out = set()
        for dp, dn, fn in os.walk(self.root):
            for f in fn:
                out.add(os.path.relpath(os.path.join(dp, f), self.root).replace("\\", "/"))
        return out


# ============================================================
# 一、扫描（确定性，不调模型）
# ============================================================
class ScanTest(_Base):
    def test_scan_finds_all(self) -> None:
        s = scan_root(self.root)
        self.assertEqual(s["total"], 8)
        self.assertEqual({f["ext"] for f in s["files"]}, {".pdf", ".txt", ".png", ".jpg", ".zip", ".xlsx"})

    def test_scan_filters_by_ext(self) -> None:
        s = scan_root(self.root, {"ext": [".pdf"]})
        self.assertEqual(s["total"], 2)
        self.assertEqual(len(s["skipped"]), 6)

    def test_scan_filter_by_name_regex(self) -> None:
        s = scan_root(self.root, {"name_re": "截图"})
        self.assertEqual(s["total"], 2)

    def test_scan_skips_temp_and_log(self) -> None:
        open(os.path.join(self.root, "x.tmp"), "w").close()
        open(os.path.join(self.root, "y.log"), "w").close()
        s = scan_root(self.root)
        self.assertEqual(s["total"], 8)
        self.assertIn("x.tmp", s["skipped"])


# ============================================================
# 二、方案闸（执行前）
# ============================================================
class CheckGateTest(_Base):
    def test_good_plan_passes(self) -> None:
        g = check_plan(self.root, normalize_plan(GOOD_PLAN), self.order())
        self.assertTrue(g.ok, [i.message for i in g.issues])

    def test_empty_plan_fails(self) -> None:
        g = check_plan(self.root, {"moves": []}, self.order())
        self.assertFalse(g.ok)
        self.assertEqual(g.issues[0].type, "empty_plan")

    def test_delete_action_rejected(self) -> None:
        """★ v1 默认零删除：出现删除动作直接判失败（结构上不给商量）。"""
        plan = {"moves": [{"action": "delete", "src": "报告.pdf", "dst": ""}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertFalse(g.ok)
        types = {i.type for i in g.issues}
        self.assertIn("forbidden_delete", types)

    def test_remove_alias_also_rejected(self) -> None:
        plan = {"moves": [{"action": "remove", "src": "报告.pdf"}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertIn("forbidden_delete", {i.type for i in g.issues})

    def test_src_must_exist(self) -> None:
        plan = {"moves": [{"action": "move", "src": "不存在的.pdf", "dst": "文档/不存在的.pdf"}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertIn("src_missing", {i.type for i in g.issues})

    def test_src_escape_rejected(self) -> None:
        plan = {"moves": [{"action": "move", "src": "../../etc/passwd", "dst": "文档/x"}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertIn("src_outside", {i.type for i in g.issues})

    def test_dst_escape_rejected(self) -> None:
        plan = {"moves": [{"action": "move", "src": "报告.pdf", "dst": "../外面/报告.pdf"}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertIn("dst_outside", {i.type for i in g.issues})

    def test_dst_dir_must_be_allowed(self) -> None:
        plan = {"moves": [{"action": "move", "src": "报告.pdf", "dst": "随便建的/报告.pdf"}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertIn("dst_not_allowed", {i.type for i in g.issues})

    def test_duplicate_src_rejected(self) -> None:
        plan = {"moves": [
            {"action": "move", "src": "报告.pdf", "dst": "文档/报告.pdf"},
            {"action": "move", "src": "报告.pdf", "dst": "压缩包/报告.pdf"}]}
        g = check_plan(self.root, normalize_plan(plan), self.order())
        self.assertIn("duplicate_src", {i.type for i in g.issues})

    def test_dst_exists_rejected(self) -> None:
        os.makedirs(os.path.join(self.root, "文档"), exist_ok=True)
        with open(os.path.join(self.root, "文档", "报告.pdf"), "w") as f:
            f.write("已存在")
        g = check_plan(self.root, normalize_plan(GOOD_PLAN), self.order())
        self.assertIn("dst_exists", {i.type for i in g.issues})

    def test_too_many_moves_rejected(self) -> None:
        o = self.order(max_moves=3)
        g = check_plan(self.root, normalize_plan(GOOD_PLAN), o)
        self.assertIn("too_many_moves", {i.type for i in g.issues})


# ============================================================
# 三、执行 + 复核闸
# ============================================================
class ApplyAndVerifyTest(_Base):
    def test_apply_moves_files(self) -> None:
        before = scan_root(self.root)
        res = apply_plan(self.root, normalize_plan(GOOD_PLAN))
        self.assertEqual(len(res["moved"]), 8)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "文档", "报告.pdf")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "报告.pdf")))
        g = verify_after(self.root, normalize_plan(GOOD_PLAN), before, res)
        self.assertTrue(g.ok, [i.message for i in g.issues])
        self.assertEqual(len(self.names()), 8, "总文件数必须守恒")

    def test_apply_never_overwrites(self) -> None:
        os.makedirs(os.path.join(self.root, "文档"), exist_ok=True)
        with open(os.path.join(self.root, "文档", "报告.pdf"), "w", encoding="utf-8") as f:
            f.write("原有的重要内容")
        res = apply_plan(self.root, normalize_plan(GOOD_PLAN))
        with open(os.path.join(self.root, "文档", "报告.pdf"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "原有的重要内容")
        self.assertTrue(any("跳过（目标已存在" in l for l in res["log"]))

    def test_verify_catches_lost_file(self) -> None:
        before = scan_root(self.root)
        plan = normalize_plan(GOOD_PLAN)
        res = apply_plan(self.root, plan)
        os.remove(os.path.join(self.root, "文档", "笔记.txt"))   # 模拟“别处弄丢了”
        g = verify_after(self.root, plan, before, res)
        self.assertFalse(g.ok)
        # 精确症状：声称已移过去的文件不在目标位置
        self.assertIn("moved_file_missing", {i.type for i in g.issues})

    def test_verify_catches_file_lost_from_source(self) -> None:
        """文件既不在原位、也不在目标 → 报 count_mismatch / file_lost。"""
        before = scan_root(self.root)
        plan = normalize_plan(GOOD_PLAN)
        # 谎报移动成功但实际什么都没做：原位文件还在，但计数对不上
        res = {"moved": plan["moves"][:3], "created_dirs": [], "log": []}
        g = verify_after(self.root, plan, before, res)
        self.assertFalse(g.ok)
        self.assertTrue({"file_lost", "moved_file_missing", "count_mismatch"}
                        & {i.type for i in g.issues})

    def test_verify_catches_count_mismatch(self) -> None:
        before = scan_root(self.root)
        plan = normalize_plan(GOOD_PLAN)
        res = apply_plan(self.root, plan)
        res["moved"] = res["moved"][:-1]                   # 谎报少移动一个
        g = verify_after(self.root, plan, before, res)
        self.assertFalse(g.ok)


# ============================================================
# 四、方案解析（模型输出的实际形态）
# ============================================================
class ParseTest(unittest.TestCase):
    def test_plain_json_object(self) -> None:
        p = parse_plan(json.dumps(GOOD_PLAN, ensure_ascii=False))
        self.assertEqual(len(p["moves"]), 8)

    def test_fenced_json(self) -> None:
        p = parse_plan("```json\n" + json.dumps(GOOD_PLAN, ensure_ascii=False) + "\n```")
        self.assertEqual(len(p["moves"]), 8)

    def test_bare_array(self) -> None:
        p = parse_plan(json.dumps(GOOD_PLAN["moves"], ensure_ascii=False))
        self.assertEqual(len(p["moves"]), 8)

    def test_prose_around_json(self) -> None:
        p = parse_plan("好的，我来整理：\n" + json.dumps(GOOD_PLAN, ensure_ascii=False) + "\n完毕。")
        self.assertIsNotNone(p)
        self.assertEqual(len(p["moves"]), 8)

    def test_alias_fields(self) -> None:
        p = normalize_plan(parse_plan('{"moves":[{"op":"move","from":"a.pdf","to":"文档/a.pdf"}]}'))
        self.assertEqual(p["moves"][0]["src"], "a.pdf")
        self.assertEqual(p["moves"][0]["dst"], "文档/a.pdf")

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_plan("我建议你手动整理这些文件。"))
        self.assertIsNone(parse_plan(""))


# ============================================================
# 五、整机（脚本化适配器）
# ============================================================
class EngineTest(_Base):
    def test_happy_path_executes_and_verifies(self) -> None:
        sm, o, ad = self.sm([answer(json.dumps(GOOD_PLAN, ensure_ascii=False))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE, res.error)
        self.assertEqual([g["signal"] for g in res.gates], ["ok", "ok"])
        self.assertEqual(len(self.names()), 8)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "图片", "截图1.png")))

    def test_dry_run_does_not_touch_files(self) -> None:
        sm, o, ad = self.sm([answer(json.dumps(GOOD_PLAN, ensure_ascii=False))], mode="dry_run")
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "文档")))
        self.assertEqual(len(self.names()), 8)

    def test_delete_plan_aborts_without_touching_files(self) -> None:
        bad = json.dumps({"moves": [{"action": "delete", "src": "报告.pdf", "dst": ""}]},
                         ensure_ascii=False)
        sm, o, ad = self.sm([answer(bad)])
        res = sm.run()
        self.assertEqual(res.status, STATUS_SECURITY_ABORT)
        self.assertEqual(len(self.names()), 8, "安全终止时不得动任何文件")

    def test_bad_plan_then_good_plan_repairs(self) -> None:
        bad = {"moves": [{"action": "move", "src": "报告.pdf", "dst": "随便建的/报告.pdf"}]}
        sm, o, ad = self.sm([answer(json.dumps(bad, ensure_ascii=False)),
                             answer(json.dumps(GOOD_PLAN, ensure_ascii=False))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE, res.error)
        self.assertEqual([g["signal"] for g in res.gates], ["fail", "ok", "ok"])

    def test_unparseable_output_repairs(self) -> None:
        sm, o, ad = self.sm([answer("这些文件建议你手动分类。"),
                             answer(json.dumps(GOOD_PLAN, ensure_ascii=False))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE, res.error)
        kinds = [h["kind"] for h in res.history]
        self.assertIn("parse_failed", kinds)

    def test_repair_budget_exhausted_escalates(self) -> None:
        bad = json.dumps({"moves": [{"action": "move", "src": "报告.pdf", "dst": "随便/报告.pdf"}]},
                         ensure_ascii=False)
        sm, o, ad = self.sm([answer(bad), answer(bad), answer(bad)])
        res = sm.run()
        self.assertEqual(res.status, STATUS_ESCALATED)
        self.assertEqual(len(self.names()), 8, "未通过方案闸时不得动任何文件")

    def test_empty_dir_is_done_immediately(self) -> None:
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        o = TidyOrder(wo_id="t-empty", task="整理", root=empty, mode="execute")
        ad = ScriptedAdapter([])
        sm = TidyStateMachine(ad, o, runlog_dir=os.path.join(self.tmp, ".runs"), verbose=False)
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertEqual(ad.calls, [], "空目录不该调模型")

    def test_model_is_never_given_file_tools(self) -> None:
        """★ 结构约束：整理节点根本没有工具——模型只能出文本方案。"""
        sm, o, ad = self.sm([answer(json.dumps(GOOD_PLAN, ensure_ascii=False))])
        sm.run()
        self.assertTrue(ad.calls)
        for c in ad.calls:
            self.assertNotIn("registry", c)     # ask() 不带工具
        src = open(os.path.join(_HERE, "tidy_engine.py"), encoding="utf-8").read()
        self.assertNotIn("REGISTRY", src, "整理流程不得把工具表交给模型")
        self.assertIn("self.ad.ask(", src)

    def test_runlog_written(self) -> None:
        sm, o, ad = self.sm([answer(json.dumps(GOOD_PLAN, ensure_ascii=False))])
        res = sm.run()
        self.assertTrue(os.path.isfile(res.runlog_path))
        d = json.load(open(res.runlog_path, encoding="utf-8"))
        self.assertEqual(d["status"], STATUS_DONE)
        self.assertEqual(len(d["plan"]["moves"]), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)

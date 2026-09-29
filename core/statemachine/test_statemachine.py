# -*- coding: utf-8 -*-
"""test_statemachine.py —— 状态机内核的离线单测（不调模型、不花额度）。

跑法（内嵌解释器带 safe-path，按模块名加载会 ModuleNotFoundError）：
    python statemachine/test_statemachine.py
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

import typecheck
from contract import (NODE_CHAIN, STATUS_DONE, STATUS_ESCALATED, STATUS_FAILED,
                      STATUS_NEEDS_INPUT, STATUS_SECURITY_ABORT, WorkOrder)
from engine import Terminal, WorkOrderStateMachine, check_contract_sync
from fake_adapter import (NEW_AVG_BAD_SYNTAX, NEW_AVG_GOOD, NEW_AVG_UNDEFINED, OLD_AVG,
                          ScriptedAdapter, make_sandbox, nc, patch, sv, tc, unauth, unsafe)
from gates import gate_syntax, gate_test, gate_type, run_gate
from tools import ToolContext, ToolError, execute


class _Base(unittest.TestCase):
    counter = 0

    def setUp(self) -> None:
        _Base.counter += 1
        self.tmp = tempfile.mkdtemp(prefix="sm-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workdir = os.path.join(self.tmp, "proj")
        make_sandbox(self.workdir)

    def make_sm(self, script, best_of_n: int = 1, task: str = None, test_path: str = "test_calc.py",
                max_repair_rounds: int = 3, commit: bool = True):
        ad = ScriptedAdapter(script)
        wo = WorkOrder(wo_id="wo-test-%d" % _Base.counter,
                       task=task or "在 calc.py 里实现 average(nums)：返回平均值，空列表返回 0.0，"
                                    "并确保 test_calc.py 全部通过。",
                       workdir=self.workdir, target="calc.py", test_path=test_path)
        wo.constraints["max_repair_rounds"] = max_repair_rounds
        wo.constraints["best_of_n"] = best_of_n
        sm = WorkOrderStateMachine(ad, wo, staging_root=os.path.join(self.tmp, ".sm-work"),
                                   runlog_dir=os.path.join(self.tmp, ".sm-runs"),
                                   best_of_n=best_of_n, commit=commit, verbose=False)
        return sm, wo, ad

    def read(self, rel: str = "calc.py", root: str = None) -> str:
        with open(os.path.join(root or self.workdir, rel), "r", encoding="utf-8") as f:
            return f.read()

    def ctx(self) -> ToolContext:
        return ToolContext(root=self.workdir, target="calc.py")


# ============================================================
# 一、五节点链路
# ============================================================
class HappyPathTest(_Base):
    def test_five_nodes_pass_and_commit(self) -> None:
        sm, wo, ad = self.make_sm([nc("average 需要实现，位置在 calc.py 第 12 行附近。"),
                                  tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        order = []
        for s in wo.history:
            if not order or order[-1] != s.node:
                order.append(s.node)
        self.assertEqual(order[:5], ["recon", "edit", "gate_syntax", "gate_type", "gate_test"])
        self.assertEqual([g["signal"] for g in wo.gate_results],
                         ["ok", "ok", "ok"])
        self.assertIn("return sum(nums) / len(nums)", self.read())
        self.assertTrue(res.diff.startswith("---"))
        self.assertTrue(os.path.exists(res.runlog_path))

    def test_gate_signal_shape(self) -> None:
        sm, wo, ad = self.make_sm([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        sm.run()
        for g in wo.gate_results:
            self.assertEqual(set(g) >= {"gate", "ok", "signal", "issues", "sec", "round"}, True)
            self.assertIn(g["signal"], ("ok", "fail"))
            self.assertEqual(g["signal"], "ok" if g["ok"] else "fail")

    def test_records_written_for_gates_and_tools(self) -> None:
        """R11：gate 结果与工具结果都要回写画像。"""
        sm, wo, ad = self.make_sm([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        sm.run()
        gated = {r["node"] for r in ad.records if r["node"].startswith("gate_")}
        self.assertEqual(gated, {"gate_syntax", "gate_type", "gate_test"})
        self.assertIn("apply_patch", {r["tool"] for r in ad.records})

    def test_best_of_n_passed_through(self) -> None:
        sm, wo, ad = self.make_sm([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))], best_of_n=3)
        sm.run()
        self.assertTrue(ad.calls)
        self.assertTrue(all(c["n"] == 3 for c in ad.calls))

    def test_contract_sync_ok(self) -> None:
        self.assertTrue(check_contract_sync()["ok"])


# ============================================================
# 二、回退规则
# ============================================================
class FallbackTest(_Base):
    def test_syntax_gate_falls_back_to_edit(self) -> None:
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(patch(OLD_AVG, NEW_AVG_BAD_SYNTAX)),
                                   tc(patch(NEW_AVG_BAD_SYNTAX, NEW_AVG_GOOD))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertEqual(wo.round_no, 1)
        sigs = [g["signal"] for g in wo.gate_results]
        self.assertEqual(sigs, ["fail", "ok", "ok", "ok"])
        # 第二次改节点的提示里必须带 gate 原文反馈
        second_edit = ad.calls_for("edit")[1]["messages"]
        self.assertTrue(any("未通过" in (m.get("content") or "") for m in second_edit))
        self.assertTrue(any("第 1 轮修复" in e for e in sm.events), sm.events)

    def test_repeated_syntax_failure_refreshes_recon(self) -> None:
        """回退表里写了「同一 gate 连续失败 2 次 → 先重跑读节点」，它必须真发生。"""
        bad2 = NEW_AVG_BAD_SYNTAX.replace("return 1.0", "return 1.0 +")
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(patch(OLD_AVG, NEW_AVG_BAD_SYNTAX)),
                                   tc(patch(NEW_AVG_BAD_SYNTAX, bad2)),
                                   nc("重新侦察"),
                                   tc(patch(bad2, NEW_AVG_GOOD))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertEqual(ad.nodes().count("recon"), 2, "语法 gate 连续失败 2 次后应先重跑读节点")
        self.assertTrue(any("先重跑读节点" in e for e in sm.events), sm.events)

    def test_type_gate_falls_back_to_edit(self) -> None:
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(patch(OLD_AVG, NEW_AVG_UNDEFINED)),
                                   tc(patch(NEW_AVG_UNDEFINED, NEW_AVG_GOOD))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertEqual([g["gate"] for g in wo.gate_results][:2], ["gate_syntax", "gate_type"])
        self.assertEqual(wo.gate_results[1]["signal"], "fail")
        self.assertIn("undefined_name", json.dumps(wo.gate_results[1], ensure_ascii=False))

    def test_test_gate_repeat_signature_escalates_to_recon(self) -> None:
        bad1 = patch(OLD_AVG, NEW_AVG_GOOD.replace("    if not nums:\n        return 0.0\n", "")
                     .replace("return sum(nums) / len(nums)", "return 1.0"))
        bad2 = patch("    return 1.0", "    return 0.0")
        good3 = patch("    return 0.0", "    if not nums:\n        return 0.0\n    return sum(nums) / len(nums)")
        sm, wo, ad = self.make_sm([nc("ok"), tc(bad1), tc(bad2), nc("重新侦察"), tc(good3)])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertEqual(wo.round_no, 2)
        self.assertEqual(ad.nodes().count("recon"), 2, "测试失败签名重复后应重新走读节点")
        self.assertIn("return sum(nums) / len(nums)", self.read())

    def test_repair_budget_exhausted_escalates(self) -> None:
        # 每轮都让补丁能应用、但始终留着语法错（逐级加符号），把修复轮用尽
        chain = [("    return a + b", "    return a +\n"),
                 ("    return a +\n", "    return a + *\n"),
                 ("    return a + *\n", "    return a + **\n"),
                 ("    return a + **\n", "    return a + ***\n")]
        script = [nc("ok")]
        for i, (o, n) in enumerate(chain):
            script.append(tc(patch(o, n)))
            if i == 1:
                script.append(nc("重新侦察"))      # 第二次语法失败 → 先刷上下文
        sm, wo, ad = self.make_sm(script, max_repair_rounds=3)
        res = sm.run()
        self.assertEqual(res.status, STATUS_ESCALATED)
        self.assertEqual(wo.round_no, 4)
        self.assertEqual(sum(1 for g in wo.gate_results if not g["ok"]), 4)
        self.assertIn("NotImplementedError", self.read(), "未通过时不得落盘")


# ============================================================
# 三、模型层不该被信任（R8 / R10 / R1 / R2）
# ============================================================
class TrustBoundaryTest(_Base):
    def test_model_claiming_done_does_not_finish(self) -> None:
        """R8：模型在正文里说「已完成」不产生任何效果。"""
        sm, wo, ad = self.make_sm([nc("ok"), nc("已完成修改，average 已经实现好了。")])
        res = sm.run()
        self.assertEqual(res.status, STATUS_NEEDS_INPUT)
        self.assertNotEqual(res.status, STATUS_DONE)
        self.assertIn("NotImplementedError", self.read())
        self.assertTrue(wo.questions)

    def test_no_call_in_recon_is_legal(self) -> None:
        """R10：空调用是合法结果，不是失败。"""
        sm, wo, ad = self.make_sm([nc("信息已足够。"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        recon_steps = [s for s in wo.history if s.node == "recon"]
        self.assertTrue(any(s.kind == "no_call" and s.ok for s in recon_steps))

    def test_unauthorized_write_tool_refused_by_engine(self) -> None:
        """R1：改节点调 write_file —— 引擎必须拒绝执行（适配层被绕过的场景）。"""
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(("write_file", {"path": "calc.py", "content": "x = 1\n"})),
                                   nc("请问要改哪个函数？")])
        res = sm.run()
        self.assertEqual(res.status, STATUS_NEEDS_INPUT)
        self.assertTrue(any(v["rule"] == "R1" for v in wo.violations))
        self.assertIn("NotImplementedError", self.read(), "被拒的 write_file 绝不能落盘")

    def test_repeated_unauthorized_kills_run(self) -> None:
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(("write_file", {"path": "calc.py", "content": "x = 1\n"})),
                                   tc(("write_file", {"path": "calc.py", "content": "x = 1\n"}))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_SECURITY_ABORT)
        self.assertIn("NotImplementedError", self.read())

    def test_unsafe_argument_kills_run(self) -> None:
        """R2：危险参数（rm -rf）判死、不回炉。"""
        sm, wo, ad = self.make_sm([])
        sm._stage()
        ctx = sm._ctx()
        with self.assertRaises(Terminal) as cm:
            sm._execute_call(ctx, {"name": "run_shell", "args": {"command": "rm -rf /data"}},
                             "shell", 1)
        self.assertEqual(cm.exception.status, STATUS_SECURITY_ABORT)
        self.assertTrue(any(v["rule"] == "R2" for v in wo.violations))

    def test_unsafe_argument_covers_git_clean(self) -> None:
        """★ 依据 S6（qwen2.5-coder 探针 E1）：它给的是 `git clean -fdx`。"""
        sm, wo, ad = self.make_sm([])
        sm._stage()
        ctx = sm._ctx()
        for cmd in ("git clean -fdx", "git clean -fd", "git reset --hard HEAD", "shutdown /s"):
            with self.assertRaises(Terminal) as cm:
                sm._execute_call(ctx, {"name": "run_shell", "args": {"command": cmd}}, "shell", 1)
            self.assertEqual(cm.exception.status, STATUS_SECURITY_ABORT, cmd)

    def test_safe_shell_commands_not_blocked(self) -> None:
        from mythos_core.rules import check_safety
        for cmd in ("git status", "python -V", "ls -la", "git log --oneline -5",
                    "pytest -q", "git clean --dry-run -n"):
            check_safety("run_shell", {"command": cmd})   # 不应抛异常

    def test_removing_unrelated_code_is_caught_and_repaired(self) -> None:
        """★ 首次真机跑暴露的缺口：模型把 main() 与入口守卫一起删掉，三个 gate 全过。

        加上保留性检查后，这一步必须被类型 gate 拦住并退回改节点，由模型自己补回来。
        """
        tail = ('\n\n\ndef main() -> None:\n    print(add(1, 2))\n'
                '    print(average([1, 2, 3]))\n\n\nif __name__ == "__main__":\n    main()\n')
        after_avg = OLD_AVG + tail
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(patch(after_avg, NEW_AVG_GOOD)),
                                   tc(patch(NEW_AVG_GOOD, NEW_AVG_GOOD + tail))])
        res = sm.run()
        self.assertEqual(res.status, STATUS_DONE)
        self.assertEqual(wo.round_no, 1)
        types = set()
        for g in wo.gate_results:
            for i in g.get("issues", []):
                types.add(i["type"])
        self.assertIn("removed_symbol", types)
        self.assertIn("removed_main_guard", types)
        self.assertIn("def main()", self.read(), "被删的无关代码必须被补回来")

    def test_write_scope_blocks_test_file(self) -> None:
        """★ 模型不能改测试文件把测试「改绿」。"""
        sm, wo, ad = self.make_sm([nc("ok"),
                                   tc(patch(OLD_AVG, NEW_AVG_GOOD, path="test_calc.py")),
                                   tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        res = sm.run()
        self.assertIn("test_add", self.read("test_calc.py"), "测试文件必须保持原样")
        self.assertIn("return sum(nums) / len(nums)", self.read())
        self.assertEqual(res.status, STATUS_DONE)

    def test_adapter_transport_error_surfaces(self) -> None:
        sm, wo, ad = self.make_sm([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        ad.script = [nc("ok"),
                     type(nc(""))(node="x", ok=False, kind="transport_error", error="连接被拒")]
        res = sm.run()
        self.assertEqual(res.status, STATUS_FAILED)
        self.assertIn("transport_error", res.error)


# ============================================================
# 四、工具与 gate 的单元行为
# ============================================================
class ToolAndGateTest(_Base):
    def test_sandbox_escape_rejected(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(ToolError):
            ctx.resolve("../../evil.py", for_write=True)
        with self.assertRaises(ToolError):
            ctx.resolve("C:/Windows/system32/x.py", for_write=True)
        with self.assertRaises(ToolError):
            execute(ctx, "apply_patch", {"path": "../../evil.py", "old_string": "a",
                                         "new_string": "b"})

    def test_symlink_escape_rejected(self) -> None:
        """★ 符号链接越界（2026-09-28 对标后加固）：字符串前缀判断看不出来。"""
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("机密\n")
        link = os.path.join(self.workdir, "link")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("本机不支持创建符号链接：%s" % e)
        ctx = self.ctx()
        with self.assertRaises(ToolError) as cm:
            ctx.resolve("link/secret.txt")
        self.assertIn("符号链接", str(cm.exception))

    def test_sibling_prefix_not_confused(self) -> None:
        """``/work-other`` 不能被 ``/work`` 前缀误判为沙箱内。"""
        from tools import ToolContext
        root = os.path.join(self.tmp, "work")
        os.makedirs(root, exist_ok=True)
        ctx = ToolContext(root=root, target="calc.py")
        self.assertIsNotNone(ctx.resolve("calc.py"))
        with self.assertRaises(ToolError):
            ctx.resolve("../work-other/x.py")

    def test_apply_patch_forms_and_errors(self) -> None:
        ctx = self.ctx()
        # 行区间形态
        res = execute(ctx, "apply_patch", {"path": "calc.py", "start_line": 1, "end_line": 1,
                                           "new_code": "# -*- coding: utf-8 -*-  (touched)"})
        self.assertEqual(res["mode"], "replace_lines")
        self.assertIn(res["version"].startswith("sha256:"), (True,))
        # old_string 找不到 → PatchError
        with self.assertRaises(ToolError):
            execute(ctx, "apply_patch", {"path": "calc.py", "old_string": "不存在的文本",
                                         "new_string": "x"})
        # 参数不足
        with self.assertRaises(ToolError):
            execute(ctx, "apply_patch", {"path": "calc.py"})

    # ---- 乐观并发（借自 chat-ollama，2026-09-28）----
    def test_version_conflict_blocks_write(self) -> None:
        from tools import VersionConflict, content_version
        ctx = self.ctx()
        v1 = content_version(os.path.join(self.workdir, "calc.py"))
        # 先让文件被“另一条路径”改掉
        execute(ctx, "apply_patch", {"path": "calc.py", "old_string": "    return a + b",
                                     "new_string": "    return a + b + 0"})
        with self.assertRaises(VersionConflict):
            execute(ctx, "apply_patch", {"path": "calc.py", "old_string": "    return a + b + 0",
                                         "new_string": "    return a + b + 1",
                                         "expected_version": v1})

    def test_version_match_allows_write(self) -> None:
        from tools import content_version
        ctx = self.ctx()
        v = content_version(os.path.join(self.workdir, "calc.py"))
        res = execute(ctx, "apply_patch", {"path": "calc.py", "old_string": "    return a + b",
                                            "new_string": "    return a + b + 0",
                                            "expected_version": v})
        self.assertEqual(res["version_before"], v)
        self.assertNotEqual(res["version"], v)

    def test_read_file_reports_version_and_truncated(self) -> None:
        ctx = self.ctx()
        r = execute(ctx, "read_file", {"path": "calc.py"})
        self.assertTrue(r["version"].startswith("sha256:"))
        self.assertFalse(r["truncated"])
        r2 = execute(ctx, "read_file", {"path": "calc.py", "max_lines": 20})
        self.assertTrue(r2["truncated"], "文件长于 20 行时必须显式标 truncated")

    def test_engine_injects_expected_version(self) -> None:
        """框架侧自动带版本：模型不用记得传，旁路修改仍会被拦。"""
        import tools as T
        import engine as E
        sm, wo, ad = self.make_sm([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        original = E.execute
        seen = {}

        def spy(ctx, name, args):
            if name in T.WRITE_TOOLS:
                seen["expected_version"] = args.get("expected_version")
            return original(ctx, name, args)

        E.execute = spy
        try:
            res = sm.run()
        finally:
            E.execute = original
        self.assertEqual(res.status, STATUS_DONE)
        self.assertTrue(str(seen.get("expected_version", "")).startswith("sha256:"), seen)

    def test_gate_syntax_ok_and_fail(self) -> None:
        ctx = self.ctx()
        self.assertTrue(gate_syntax(ctx).ok)
        execute(ctx, "apply_patch", {"path": "calc.py", "old_string": "    return a + b",
                                     "new_string": "    return a + b\n\n\ndef broken(: pass"})
        g = gate_syntax(ctx)
        self.assertFalse(g.ok)
        self.assertEqual(g.issues[0].type, "syntax_error")
        self.assertTrue(g.issues[0].line > 0)

    def test_gate_type_ok_and_fail(self) -> None:
        ctx = self.ctx()
        self.assertTrue(gate_type(ctx).ok, "样板文件本身应通过类型 gate")
        execute(ctx, "apply_patch", {"path": "calc.py", "old_string": OLD_AVG,
                                     "new_string": NEW_AVG_UNDEFINED})
        g = gate_type(ctx)
        self.assertFalse(g.ok)
        self.assertIn("undefined_name", {i.type for i in g.issues})

    def test_gate_test_requires_declared_test(self) -> None:
        ctx = self.ctx()
        g = run_gate("gate_test", ctx, test_path=None)
        self.assertFalse(g.ok)
        self.assertEqual(g.issues[0].type, "no_test_declared")

    def test_gate_test_runs_real_tests(self) -> None:
        ctx = self.ctx()
        self.assertFalse(gate_test(ctx, test_path="test_calc.py").ok, "average 未实现时测试应当是红的")
        execute(ctx, "apply_patch", {"path": "calc.py", "old_string": OLD_AVG,
                                     "new_string": NEW_AVG_GOOD})
        g = gate_test(ctx, test_path="test_calc.py")
        self.assertTrue(g.ok, g.raw)
        self.assertEqual(g.raw["tests_run"], 3)

    def test_gate_test_signature_stable(self) -> None:
        from gates import test_failure_signature
        ctx = self.ctx()
        g1 = gate_test(ctx, test_path="test_calc.py")
        g2 = gate_test(ctx, test_path="test_calc.py")
        self.assertEqual(test_failure_signature(g1), test_failure_signature(g2))


# ============================================================
# 五、类型检查器单元（v1 近似）
# ============================================================
class TypeCheckTest(unittest.TestCase):
    CLEAN = '''# -*- coding: utf-8 -*-
from typing import List


def f(x: int, y: int = 0) -> int:
    return x + y


class A:
    def m(self, n: List[int]) -> None:
        self.data = [i for i in n if i > 0]
        for k in self.data:
            print(k)
'''

    def test_clean_source_passes(self) -> None:
        self.assertEqual(typecheck.check_source(self.CLEAN), [])

    def test_undefined_name_detected(self) -> None:
        src = "def f(x: int) -> int:\n    return x + y\n"
        issues = typecheck.check_source(src)
        self.assertEqual(issues[0]["type"], "undefined_name")
        self.assertEqual(issues[0]["line"], 2)

    def test_missing_annotation_detected(self) -> None:
        src = "def f(x):\n    return x\n"
        kinds = {i["type"] for i in typecheck.check_source(src)}
        self.assertIn("missing_annotation", kinds)

    def test_nested_function_annotations_not_required(self) -> None:
        src = "def f(x: int) -> int:\n    def inner(y):\n        return y\n    return inner(x)\n"
        self.assertEqual(typecheck.check_source(src), [])

    def test_duplicate_def_detected(self) -> None:
        src = "def f(x: int) -> int:\n    return x\n\n\ndef f(x: int) -> int:\n    return x + 1\n"
        kinds = {i["type"] for i in typecheck.check_source(src)}
        self.assertIn("duplicate_def", kinds)

    def test_comprehension_and_match_no_false_positive(self) -> None:
        src = ('from typing import List\n\n\ndef f(items: List[int]) -> int:\n'
               '    evens = [i * 2 for i in items if i % 2 == 0]\n'
               '    total = 0\n'
               '    for e in evens:\n'
               '        total += e\n'
               '    return total\n')
        self.assertEqual(typecheck.check_source(src), [])

    def test_string_annotation_skipped(self) -> None:
        src = "def f(x: 'Weird') -> 'Weird':\n    return x\n"
        self.assertEqual(typecheck.check_source(src), [])

    def test_preservation_check_detects_removed_symbol(self) -> None:
        old = "def a() -> None:\n    pass\n\n\ndef b() -> None:\n    a()\n"
        new = "def a() -> None:\n    pass\n"
        issues = typecheck.preservation_issues(old, new)
        self.assertEqual([i["type"] for i in issues], ["removed_symbol"])
        self.assertIn("b", issues[0]["message"])

    def test_preservation_check_detects_removed_main_guard(self) -> None:
        old = "def a() -> None:\n    pass\n\n\nif __name__ == '__main__':\n    a()\n"
        new = "def a() -> None:\n    pass\n"
        kinds = {i["type"] for i in typecheck.preservation_issues(old, new)}
        self.assertIn("removed_main_guard", kinds)

    def test_preservation_check_allows_additions(self) -> None:
        old = "def a() -> None:\n    pass\n"
        new = "def a() -> None:\n    pass\n\n\ndef b() -> None:\n    a()\n"
        self.assertEqual(typecheck.preservation_issues(old, new), [])

    # ---- 死代码 / 不可达代码（qwen3:8b 真机跑暴露）----
    def test_unreachable_code_after_return(self) -> None:
        src = ('from typing import List\n\n\n'
               'def f(nums: List[int]) -> float:\n'
               '    """doc"""\n'
               '    if not nums:\n        return 0.0\n'
               '    return sum(nums) / len(nums)\n'
               '    """doc"""\n'
               '    raise NotImplementedError\n')
        kinds = {i["type"] for i in typecheck.check_source(src)}
        self.assertIn("unreachable_code", kinds)

    def test_early_return_inside_if_is_not_unreachable(self) -> None:
        src = ('from typing import List\n\n\n'
               'def f(nums: List[int]) -> float:\n'
               '    """doc"""\n'
               '    if not nums:\n        return 0.0\n'
               '    return sum(nums) / len(nums)\n')
        self.assertEqual(typecheck.check_source(src), [])

    def test_raise_then_code_is_unreachable(self) -> None:
        src = "def f(x: int) -> int:\n    raise ValueError(x)\n    return x\n"
        kinds = {i["type"] for i in typecheck.check_source(src)}
        self.assertIn("unreachable_code", kinds)

    def test_dead_string_after_statement(self) -> None:
        src = 'def f(x: int) -> int:\n    """doc"""\n    y = x\n    """stray"""\n    return y\n'
        kinds = {i["type"] for i in typecheck.check_source(src)}
        self.assertIn("dead_string", kinds)

    def test_normal_docstrings_not_flagged(self) -> None:
        src = ('"""module doc"""\n\n\n'
               'class A:\n    """class doc"""\n\n'
               '    def m(self) -> int:\n        """method doc"""\n        return 1\n')
        self.assertEqual(typecheck.check_source(src), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

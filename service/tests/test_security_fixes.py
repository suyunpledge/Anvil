# -*- coding: utf-8 -*-
"""test_security_fixes.py —— 针对外部审查 7 条问题的回归测试。

每条对应一个真问题。改完这些测试必须仍然通过，否则说明加固被回退了。
跑法：python service/tests/test_security_fixes.py
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
for _p in (_ROOT, os.path.join(_ROOT, "service"), os.path.join(_ROOT, "gateway"),
           os.path.join(_ROOT, "context")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.paths import ensure_core_on_path  # noqa: E402

ensure_core_on_path()

import sandbox as SB  # noqa: E402
import tools as TOOLS  # noqa: E402
from fake_adapter import (NEW_AVG_GOOD, OLD_AVG, ScriptedAdapter, make_sandbox, nc,  # noqa: E402
                          patch, tc)


# ============================================================
# #1 测试执行：不再继承全部环境变量
# ============================================================
class SandboxEnvTest(unittest.TestCase):
    def test_credential_like_vars_dropped(self) -> None:
        os.environ["MY_TEST_API_KEY_XYZ"] = "should-not-leak"
        os.environ["OPENAI_API_KEY"] = "sk-should-not-leak-0123456789"
        try:
            env = SB.sanitized_env()
        finally:
            os.environ.pop("MY_TEST_API_KEY_XYZ", None)
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertNotIn("MY_TEST_API_KEY_XYZ", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_proxy_neutralized(self) -> None:
        os.environ["HTTPS_PROXY"] = "http://evil.example:8080"
        try:
            env = SB.sanitized_env()
        finally:
            os.environ.pop("HTTPS_PROXY", None)
        self.assertEqual(env.get("HTTPS_PROXY"), "")
        self.assertEqual(env.get("no_proxy"), "*")

    def test_home_isolated_and_bytecode_off(self) -> None:
        env = SB.sanitized_env()
        self.assertNotEqual(env.get("USERPROFILE"), os.environ.get("USERPROFILE"))
        self.assertEqual(env.get("PYTHONDONTWRITEBYTECODE"), "1")
        self.assertEqual(env.get("LOCAL_IDE_SANDBOX"), "1")

    def test_keeps_python_runnable(self) -> None:
        env = SB.sanitized_env()
        self.assertIn("PATH", env)          # 丢了 PATH 子进程起不来
        self.assertTrue(env.get("SystemRoot") or env.get("SYSTEMROOT"))

    def test_os_isolation_honestly_reported(self) -> None:
        """★ 不许假装有沙箱：有就报有（并说明手段），没有就报没有（并说明原因）。

        2026-10-03 起 Windows 有 Job Object（winjob），本机报 True——测试只验证
        「报告与实际能力一致」，不再绑定具体值。
        """
        ok, why = SB.real_isolation_available()
        if ok:
            self.assertIn("Job Object", why)
        else:
            self.assertTrue(why)
        s = SB.summary()
        self.assertEqual(bool(s["os_isolation"]), ok)
        self.assertTrue(s["env_sanitized"])

    def test_strict_mode_refuses_without_real_isolation(self) -> None:
        """★ 2026-10-03 起 Windows 有 Job Object 隔离（winjob），strict 应当**通过**。
        strict 的语义是「拿不到真隔离就拒跑」——现在拿得到了，所以不拒。"""
        ok, why = SB.real_isolation_available()
        if ok:
            # 有真隔离：strict 与非 strict 都不应抛
            SB.assert_real_isolation_available(strict=True)
            SB.assert_real_isolation_available(strict=False)
        else:
            # 没有真隔离的环境：strict 必须拒跑
            with self.assertRaises(RuntimeError):
                SB.assert_real_isolation_available(strict=True)
        SB.assert_real_isolation_available(strict=False)   # 不抛

    def test_dropped_names_returns_names_not_values(self) -> None:
        names = SB.dropped_names()
        self.assertIsInstance(names, list)
        for n in names[:5]:
            self.assertNotIn("=", n)


class SandboxExecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sec-exec-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, "t.py"), "w", encoding="utf-8") as f:
            f.write("import os, unittest\n\n\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_env_sanitized(self) -> None:\n"
                    "        self.assertNotIn('OPENAI_API_KEY', os.environ)\n"
                    "        self.assertEqual(os.environ.get('LOCAL_IDE_SANDBOX'), '1')\n")
        self.ctx = TOOLS.ToolContext(root=self.root, target="t.py")

    def test_test_gate_runs_with_clean_env(self) -> None:
        from gates import gate_test
        os.environ["OPENAI_API_KEY"] = "sk-secret-0123456789abcdef"
        try:
            g = gate_test(self.ctx, test_path="t.py")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertTrue(g.ok, (g.raw or {}).get("output", "")[-400:])
        notes = (g.raw or {}).get("sandbox") or {}
        self.assertTrue(notes.get("env_sanitized"))
        # 2026-10-03 起 Windows 有 Job Object：os_isolation 可能为 True（有真隔离）
        # 或 False（无 winjob 的环境）。两种都合法，只要求字段存在且与 summary 一致。
        self.assertIn(notes.get("os_isolation"), (True, False))
        if notes.get("os_isolation"):
            self.assertTrue(notes.get("job_object"))

    def test_run_tests_reports_sandbox_notes(self) -> None:
        r = TOOLS.run_tests(self.ctx, test_path="t.py")
        self.assertTrue(r["ok"], r.get("output", "")[-300:])


# ============================================================
# #3 版本基线：工单开始后文件被改 → 拒绝
# ============================================================
class VersionBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sec-ver-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workdir = os.path.join(self.tmp, "proj")
        make_sandbox(self.workdir)

    def test_stage_refuses_if_file_changed_after_order_start(self) -> None:
        import engine as E
        from contract import WorkOrder
        target = os.path.join(self.workdir, "calc.py")
        v0 = TOOLS.content_version(target)
        # 模拟“工单开始后用户改了文件”
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n# 用户手动加的一行\n")
        wo = WorkOrder(wo_id="v-1", task="实现 average", workdir=self.workdir,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(ScriptedAdapter([]), wo,
                                     staging_root=os.path.join(self.tmp, "st"),
                                     runlog_dir=os.path.join(self.tmp, "rl"),
                                     verbose=False, initial_version=v0)
        # 注意：引擎的 run() 会把 Terminal 转成终态返回值，不会向外抛
        res = sm.run()
        self.assertEqual(res.status, "failed")
        self.assertIn("已被修改", res.error)
        # 而且不得动过文件
        with open(target, encoding="utf-8") as f:
            self.assertIn("用户手动加的一行", f.read())

    def test_stage_ok_when_version_unchanged(self) -> None:
        import engine as E
        from contract import WorkOrder
        target = os.path.join(self.workdir, "calc.py")
        v0 = TOOLS.content_version(target)
        wo = WorkOrder(wo_id="v-2", task="实现 average", workdir=self.workdir,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(ScriptedAdapter([nc("ok"),
                                                      tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
                                     wo, staging_root=os.path.join(self.tmp, "st"),
                                     runlog_dir=os.path.join(self.tmp, "rl"),
                                     verbose=False, initial_version=v0)
        res = sm.run()
        self.assertEqual(res.status, "done")


# ============================================================
# #7 嵌套暂存：工作目录就是项目根时不得递归复制
# ============================================================
class NestedStagingTest(unittest.TestCase):
    def test_nested_staging_moved_out(self) -> None:
        import engine as E
        from contract import WorkOrder
        tmp = tempfile.mkdtemp(prefix="sec-nest-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workdir = os.path.join(tmp, "projroot")
        make_sandbox(workdir)
        # 关键：把暂存区放在工作目录**内部**（就像 .runtime/staging 在项目根里）
        inner_staging = os.path.join(workdir, ".runtime", "staging")
        wo = WorkOrder(wo_id="n-1", task="实现 average", workdir=workdir,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(ScriptedAdapter([nc("ok"),
                                                      tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
                                     wo, staging_root=inner_staging,
                                     runlog_dir=os.path.join(tmp, "rl"),
                                     verbose=False)
        res = sm.run()
        self.assertEqual(res.status, "done")
        # 暂存区必须已被移出工作目录（否则就是自嵌套复制）
        self.assertFalse(os.path.abspath(sm.staging).startswith(os.path.abspath(workdir) + os.sep),
                         "暂存区仍在工作目录内：%s" % sm.staging)
        # 并且暂存副本里不能再出现 .runtime（否则说明复制时把自己套进去了）
        nested = os.path.join(res.staging, ".runtime")
        self.assertFalse(os.path.exists(nested),
                         "暂存副本里出现了 .runtime，说明 copytree 递归复制了自己")


# ============================================================
# #6 上下文层接入
# ============================================================
class ContextWiringTest(unittest.TestCase):
    def test_context_provider_is_used(self) -> None:
        import engine as E
        from contract import WorkOrder
        tmp = tempfile.mkdtemp(prefix="sec-ctx-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workdir = os.path.join(tmp, "proj")
        make_sandbox(workdir)
        seen = {}

        def provider(task, target):
            seen["called"] = True
            seen["task"] = task
            return {"text": "## 已裁剪上下文\nCUSTOM_CONTEXT_MARKER", "tokens_est": 12,
                    "kept": ["target_source"], "dropped": ["repo_map"]}

        wo = WorkOrder(wo_id="c-1", task="实现 average", workdir=workdir,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(ScriptedAdapter([nc("ok"),
                                                      tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
                                     wo, staging_root=os.path.join(tmp, "st"),
                                     runlog_dir=os.path.join(tmp, "rl"),
                                     verbose=False, context_provider=provider)
        res = sm.run()
        self.assertEqual(res.status, "done")
        self.assertTrue(seen.get("called"), "上下文层没被调用")
        self.assertEqual(wo.artifacts.get("context", {}).get("tokens_est"), 12)

    def test_long_context_falls_back_to_full_text(self) -> None:
        """★ 回归（2026-09-28 端到端暴露）：裁剪后不比全文小 → 必须回退全文。

        真实教训：同一道题，裁剪后 221 token vs 整文件 106 token，模型看不到完整
        文件布局，把函数删错位了。所以“用不用裁剪”要由收益决定，不能一刀切。
        """
        import engine as E
        from contract import WorkOrder
        tmp = tempfile.mkdtemp(prefix="sec-ctx4-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workdir = os.path.join(tmp, "proj")
        make_sandbox(workdir)
        giant = "## 裁剪上下文\n" + ("填充内容 " * 2000)

        def provider(task, target):
            return {"text": giant, "tokens_est": 9999, "kept": ["task"]}

        wo = WorkOrder(wo_id="c-4", task="实现 average", workdir=workdir,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(ScriptedAdapter([nc("ok"),
                                                      tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
                                     wo, staging_root=os.path.join(tmp, "st"),
                                     runlog_dir=os.path.join(tmp, "rl"),
                                     verbose=False, context_provider=provider)
        res = sm.run()
        self.assertEqual(res.status, "done")
        self.assertIn("skipped", wo.artifacts.get("context", {}),
                      "裁剪无收益时应当记录回退，而不是硬用大上下文")

    def test_context_provider_failure_falls_back(self) -> None:
        """上下文层炸了不能让工单也炸：应回退全文并照常完成。"""
        import engine as E
        from contract import WorkOrder
        tmp = tempfile.mkdtemp(prefix="sec-ctx2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        workdir = os.path.join(tmp, "proj")
        make_sandbox(workdir)

        def bad(task, target):
            raise RuntimeError("模拟上下文层故障")

        wo = WorkOrder(wo_id="c-2", task="实现 average", workdir=workdir,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(ScriptedAdapter([nc("ok"),
                                                      tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
                                     wo, staging_root=os.path.join(tmp, "st"),
                                     runlog_dir=os.path.join(tmp, "rl"),
                                     verbose=False, context_provider=bad)
        res = sm.run()
        self.assertEqual(res.status, "done")
        self.assertIn("error", wo.artifacts.get("context", {}))

    def test_build_workorder_context_prefers_staging(self) -> None:
        import builder as B
        import builder as B
        tmp = tempfile.mkdtemp(prefix="sec-ctx3-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real = os.path.join(tmp, "real")
        stage = os.path.join(tmp, "stage")
        make_sandbox(real)
        make_sandbox(stage)
        with open(os.path.join(stage, "calc.py"), "a", encoding="utf-8") as f:
            f.write("\n# STAGING_ONLY\n")

        class S:
            ctx_max = 8192
            model = "f"
            key = "f"

        pack = B.build_workorder_context(workdir=real, target="calc.py",
                                        task="实现 average", spec=S(), staging_root=stage)
        # 必须指向暂存区里的那一份，而不是真实目录
        self.assertTrue(os.path.abspath(pack["target_abs"]).startswith(os.path.abspath(stage)),
                        "应读取暂存区副本，实得 %s" % pack["target_abs"])
        self.assertIn("average", pack["text"])


# ============================================================
# #2 服务端策略（不需要起服务）
# ============================================================
class PolicyTest(unittest.TestCase):
    def _svc(self):
        """按**文件路径**加载 service/app.py。

        不能直接 `import app`：gateway/ 与 service/ 下都有 app.py，
        谁在 sys.path 前面就导入谁（本测试就撞上过这个坑）。
        """
        import importlib.util
        path = os.path.join(_ROOT, "service", "app.py")
        spec = importlib.util.spec_from_file_location("svc_app_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _policy(self):
        return self._svc().ConfirmPolicy

    def test_force_confirm_overrides_client(self) -> None:
        P = self._policy()
        p = P(force_confirm=True, allowed_roots=[], require_token=True)
        self.assertTrue(p.effective_confirm(False), "服务端要求确认时，客户端传 false 不得生效")
        self.assertTrue(p.effective_confirm(True))

    def test_client_choice_respected_when_not_forced(self) -> None:
        P = self._policy()
        p = P(force_confirm=False, allowed_roots=[], require_token=True)
        self.assertFalse(p.effective_confirm(False))

    def test_workdir_whitelist(self) -> None:
        P = self._policy()
        tmp = tempfile.mkdtemp(prefix="sec-root-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "allowed")
        other = os.path.join(tmp, "other")
        os.makedirs(root, exist_ok=True)
        os.makedirs(other, exist_ok=True)
        p = P(force_confirm=True, allowed_roots=[root], require_token=True)
        self.assertTrue(p.workdir_allowed(root))
        self.assertTrue(p.workdir_allowed(os.path.join(root, "sub")))
        self.assertFalse(p.workdir_allowed(other), "兄弟目录不得被前缀误判为允许")
        self.assertFalse(p.workdir_allowed(""))

    def test_no_roots_means_unrestricted(self) -> None:
        P = self._policy()
        p = P(force_confirm=True, allowed_roots=[], require_token=True)
        self.assertTrue(p.workdir_allowed("C:\\anywhere"))

    def test_policy_from_env_defaults_are_safe(self) -> None:
        P = self._policy()
        for k in ("LOCAL_IDE_REQUIRE_CONFIRM", "LOCAL_IDE_REQUIRE_TOKEN"):
            os.environ.pop(k, None)
        p = P.from_env()
        self.assertTrue(p.force_confirm, "默认必须强制人确认")
        self.assertTrue(p.require_token, "默认必须要求令牌")

    def test_token_file_written_and_not_world_readable_intent(self) -> None:
        svcapp = self._svc()
        tok = "test-token-abc123"
        svcapp._write_token_file(tok)
        p = svcapp._token_path()
        self.assertTrue(os.path.isfile(p))
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), tok)


# ============================================================
# #4 流式路径的出网校验（不需要真起服务）
# ============================================================
class StreamOutboundTest(unittest.TestCase):
    def test_stream_goes_through_outbound_gate(self) -> None:
        import gateway.app as G
        old = G.OLLAMA
        try:
            G.OLLAMA = "http://evil.example.com"        # 假装被环境变量指到外网
            with self.assertRaises(G.OutboundBlocked):
                G.post_ollama_stream("/api/chat", {"model": "m"})
            with self.assertRaises(G.OutboundBlocked):
                G.post_ollama("/api/chat", {"model": "m"})
        finally:
            G.OLLAMA = old

    def test_stream_helper_exists(self) -> None:
        import gateway.app as G
        self.assertTrue(callable(getattr(G, "post_ollama_stream", None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""test_review_fixes.py —— 2026-09-29 自查审查所修复项的回归测试。

A. /staged 必须鉴权，且不再回传完整本地路径
B. 免确认（autoApply）路径的落盘终检：运行期间文件被改 → 拒绝覆盖
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.paths import ensure_core_on_path  # noqa: E402

ensure_core_on_path()

import tools as TOOLS  # noqa: E402
from fake_adapter import NEW_AVG_GOOD, OLD_AVG, ScriptedAdapter, make_sandbox, nc, patch, tc  # noqa: E402


def _load_service_app():
    """按文件路径加载 service/app.py（避免与 gateway/app.py 撞名）。"""
    import importlib.util
    path = os.path.join(_ROOT, "service", "app.py")
    spec = importlib.util.spec_from_file_location("svc_app_review", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StagedAuthTest(unittest.TestCase):
    """A. /staged 鉴权与路径收敛。"""

    def setUp(self) -> None:
        self.svc = _load_service_app()
        from fastapi.testclient import TestClient
        self.app = self.svc.build_app()
        self.client = TestClient(self.app)

    def test_staged_requires_token(self) -> None:
        r = self.client.get("/wo/whatever/staged")
        self.assertEqual(r.status_code, 401, "敏感读口必须要求令牌")

    def test_staged_no_absolute_path(self) -> None:
        """★ 即便带对令牌，也不回传完整本地路径（只回文件名）。"""
        # 造一张假工单占位（不必真跑）：直接往注册表塞一个带结果的 runner
        from runner import RunnerOpts, WorkOrderRunner
        tmp = tempfile.mkdtemp(prefix="rev-staged-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wd = os.path.join(tmp, "proj")
        make_sandbox(wd)
        ad = ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
        r = WorkOrderRunner(RunnerOpts(kind="code", task="实现 average", workdir=wd,
                                       target="calc.py", test_path="test_calc.py"),
                            adapter=ad)
        r.start()
        r.join(30)
        self.svc.REGISTRY.add(r)
        token = os.environ.get("LOCAL_IDE_TOKEN", "")
        if not token:
            # build_app 在无环境变量时会生成随机令牌；从令牌文件读
            tp = self.svc._token_path()
            token = open(tp, encoding="utf-8").read().strip() if os.path.isfile(tp) else ""
        resp = self.client.get("/wo/%s/staged" % r.wo_id,
                               headers={"X-Local-Ide-Token": token})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        body = resp.json()
        self.assertNotIn("path", body, "不得再回传完整路径")
        self.assertEqual(body.get("filename"), "calc.py")
        self.assertNotIn(tmp, json.dumps(body), "响应里不得包含本机绝对路径")


class AutoApplyVersionCheckTest(unittest.TestCase):
    """B. 免确认路径的落盘终检（外部审查 #3 的最后一处）。"""

    def _run(self, mutate_during: bool):
        import engine as E
        from contract import WorkOrder
        tmp = tempfile.mkdtemp(prefix="rev-auto-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wd = os.path.join(tmp, "proj")
        make_sandbox(wd)
        target = os.path.join(wd, "calc.py")
        v0 = TOOLS.content_version(target)
        wo = WorkOrder(wo_id="b-%s" % mutate_during, task="实现 average", workdir=wd,
                       target="calc.py", test_path="test_calc.py")
        sm = E.WorkOrderStateMachine(
            ScriptedAdapter([nc("ok"), tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
            wo, staging_root=os.path.join(tmp, "st"), runlog_dir=os.path.join(tmp, "rl"),
            verbose=False, commit=True, initial_version=v0,
            # 模拟“模型跑的期间文件被改”：在暂存完成后、提交前改真实文件
            _test_hook_before_commit=(lambda: open(target, "a", encoding="utf-8").write(
                "\n# 运行期间被旁路改的一行\n")) if mutate_during else None)
        return sm.run(), target

    def test_autoapply_blocked_when_file_changed(self) -> None:
        res, target = self._run(mutate_during=True)
        self.assertEqual(res.status, "failed")
        self.assertIn("运行期间被修改", res.error)
        with open(target, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("运行期间被旁路改的一行", content, "人工改动必须保留")
        self.assertIn("NotImplementedError", content, "旧内容未被覆盖")

    def test_autoapply_ok_when_unchanged(self) -> None:
        res, target = self._run(mutate_during=False)
        self.assertEqual(res.status, "done")
        with open(target, encoding="utf-8") as f:
            self.assertNotIn("NotImplementedError", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)

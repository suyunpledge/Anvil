# -*- coding: utf-8 -*-
"""test_gateway_auth.py —— 网关鉴权（2026-10-05 补齐）回归。

背景：工单服务自 09-30 起有令牌体系，网关（模型唯一出口）却完全裸奔——
任何本机进程都能打 /audit、/v1/chat/completions、/ollama/api/chat。这里钉住三点：

  1. /health 保持开放（自检脚本与扩展 check 都靠它，且它不返回令牌本身）；
  2. 其余端点无令牌一律 401；
  3. 三种令牌来源（环境变量 / service-token 文件 / gateway-token 文件）都认，
     且错令牌照样 401（常数时间比较不改变判定结果）。

跑法：python gateway/tests/test_gateway_auth.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE, os.path.join(_ROOT, "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# gateway/app.py 与 service/app.py 同名，按路径加载（仓内既有纪律）
_spec = importlib.util.spec_from_file_location(
    "gw_app_auth", os.path.join(_ROOT, "gateway", "app.py"))
_G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_G)

from core.paths import runtime_path  # noqa: E402

TOKEN = "gw-test-token-20261005"
PROTECTED = [
    ("get", "/audit"),
    ("get", "/v1/models"),
    ("post", "/ollama/api/chat"),
    ("post", "/v1/chat/completions"),
]


class GatewayAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        # 令牌来源优先走环境变量：与 e2e_check.py 的做法一致，且不污染 .runtime
        cls._old = os.environ.get("LOCAL_IDE_TOKEN")
        os.environ["LOCAL_IDE_TOKEN"] = TOKEN
        cls.client = TestClient(_G.build_app())

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._old is None:
            os.environ.pop("LOCAL_IDE_TOKEN", None)
        else:
            os.environ["LOCAL_IDE_TOKEN"] = cls._old

    # ---------------- 开放端点 ----------------
    def test_health_is_open(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

    def test_health_does_not_leak_token(self) -> None:
        body = self.client.get("/health").text
        self.assertNotIn(TOKEN, body, "/health 免鉴权，绝不能把令牌回给调用方")
        self.assertTrue(self.client.get("/health").json()["auth"]["token_required"])

    # ---------------- 受保护端点 ----------------
    def _call(self, method: str, path: str, headers=None):
        """GET 不能带 json=（httpx 会拒），POST 才带。"""
        kw = {"headers": headers} if headers else {}
        if method == "post":
            kw["json"] = {}
        return getattr(self.client, method)(path, **kw)

    def test_protected_endpoints_reject_without_token(self) -> None:
        for method, path in PROTECTED:
            with self.subTest(path=path):
                self.assertEqual(self._call(method, path).status_code, 401)

    def test_protected_endpoints_reject_wrong_token(self) -> None:
        for method, path in PROTECTED:
            with self.subTest(path=path):
                r = self._call(method, path, {"X-Local-Ide-Token": "nope"})
                self.assertEqual(r.status_code, 401)

    def test_header_token_accepted(self) -> None:
        r = self.client.get("/audit", headers={"X-Local-Ide-Token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn("entries", r.json())

    def test_bearer_token_accepted(self) -> None:
        r = self.client.get("/audit", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(r.status_code, 200)

    # ---------------- 令牌来源 ----------------
    def test_accepts_tokens_from_runtime_files(self) -> None:
        """service-token / gateway-token 两个文件里的令牌都要认——
        这样扩展与状态机拿一个令牌就能同时打两个服务，不必配两处。"""
        old = os.environ.pop("LOCAL_IDE_TOKEN", None)
        try:
            for name, tok in (("service-token", "from-service-file"),
                              ("gateway-token", "from-gateway-file")):
                with self.subTest(name=name):
                    p = runtime_path(name)
                    bak = None
                    if os.path.exists(p):
                        with open(p, encoding="utf-8") as f:
                            bak = f.read()
                    try:
                        with open(p, "w", encoding="utf-8") as f:
                            f.write(tok)
                        r = self.client.get("/audit", headers={"X-Local-Ide-Token": tok})
                        self.assertEqual(r.status_code, 200)
                    finally:
                        if bak is None:
                            os.remove(p)
                        else:
                            with open(p, "w", encoding="utf-8") as f:
                                f.write(bak)
        finally:
            if old is not None:
                os.environ["LOCAL_IDE_TOKEN"] = old

    def test_ensure_gateway_token_writes_a_file_when_nothing_set(self) -> None:
        """什么都没有时，gateway-token 必须被写出来（否则扩展无从发现令牌）。"""
        saved_env = {k: os.environ.pop(k, None)
                     for k in ("LOCAL_IDE_TOKEN", "LOCAL_LLM_TOKEN")}
        gpath, spath = runtime_path("gateway-token"), runtime_path("service-token")
        gbak = open(gpath, encoding="utf-8").read() if os.path.exists(gpath) else None
        sbak = open(spath, encoding="utf-8").read() if os.path.exists(spath) else None
        try:
            for p in (gpath, spath):
                if os.path.exists(p):
                    os.remove(p)
            tok = _G.ensure_gateway_token()
            self.assertTrue(tok)
            with open(gpath, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), tok)
            self.assertIn(tok, _G.accepted_tokens())
        finally:
            for p, bak in ((gpath, gbak), (spath, sbak)):
                if bak is None:
                    if os.path.exists(p):
                        os.remove(p)
                else:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(bak)
            for k, v in saved_env.items():
                if v is not None:
                    os.environ[k] = v

    # ---------------- 扩展/状态机调用方 ----------------
    def test_client_sends_token(self) -> None:
        """状态机侧（GatewayTransport）必须自带令牌，否则它自己就被网关拒了。"""
        from gateway.client import local_token
        old = os.environ.get("LOCAL_IDE_TOKEN")
        os.environ["LOCAL_IDE_TOKEN"] = "transport-tok"
        try:
            self.assertEqual(local_token(), "transport-tok")
        finally:
            if old is None:
                os.environ.pop("LOCAL_IDE_TOKEN", None)
            else:
                os.environ["LOCAL_IDE_TOKEN"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)

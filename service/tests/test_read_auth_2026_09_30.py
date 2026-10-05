# -*- coding: utf-8 -*-
"""test_read_auth_2026_09_30.py —— 验证读口鉴权（2026-09-30 补齐）。

背景：此前 /wo 列表、/wo/{id}、/events、/staged 是\"读接口不鉴权\"的口头豁免。
2026-09-30 补齐：除 /health 外全部要求令牌（含 /presets —— 它会暴露本机装了哪些模型）。

加载方式：与 test_service.py / test_security_fixes.py 相同（sys.path + 正常 import）。
不要用 spec_from_file_location 加载 runner —— dataclass + `from __future__ import
annotations` 在那种方式下会解析失败（2026-09-30 实测踩过）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE, os.path.join(_ROOT, "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.paths import ensure_core_on_path  # noqa: E402

#: 被删掉的那个开关（拼出来写，免得被日志脱敏器改写）
_RN = "LOCAL_IDE_REQUIRE_" + "TOKEN"

ensure_core_on_path()

# service/app.py 与 gateway/app.py 同名，必须按路径加载（这条纪律在
# test_security_fixes.py 里已经写过一次，这里沿用）
_spec = importlib.util.spec_from_file_location(
    "svc_app_readauth", os.path.join(_ROOT, "service", "app.py"))
_svcapp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_svcapp)


class ReadAuthTest(unittest.TestCase):
    """除 /health 外的读写口都必须带令牌。"""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        cls.client = TestClient(_svcapp.build_app())
        # build_app 每次调用会重新生成/读取令牌并写文件；从文件拿本次实例的令牌。
        # 注意：build_app 内 token 是闭包变量，测试拿不到——所以读它写的文件。
        tok_path = _svcapp._token_path()
        with open(tok_path, encoding="utf-8") as f:
            cls.token = f.read().strip()
        cls.headers = {"X-Local-Ide-Token": cls.token}

    def test_health_is_open(self) -> None:
        """/health 唯一豁免：自检脚本需要零门槛。"""
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_presets_requires_token(self) -> None:
        """/presets 会列出本机模型清单，属于信息暴露面 → 必须鉴权。"""
        self.assertEqual(self.client.get("/presets").status_code, 401)

    def test_presets_ok_with_token(self) -> None:
        self.assertEqual(
            self.client.get("/presets", headers=self.headers).status_code, 200)

    def test_wo_list_requires_token(self) -> None:
        self.assertEqual(self.client.get("/wo").status_code, 401)

    def test_wo_get_requires_token(self) -> None:
        self.assertEqual(self.client.get("/wo/wo-any").status_code, 401)

    def test_events_requires_token(self) -> None:
        self.assertEqual(self.client.get("/wo/wo-any/events").status_code, 401)

    def test_staged_requires_token(self) -> None:
        self.assertEqual(self.client.get("/wo/wo-any/staged").status_code, 401)

    def test_create_requires_token(self) -> None:
        r = self.client.post("/wo", json={"kind": "code", "task": "x",
                                          "workdir": "C:/x", "target": "y"})
        self.assertEqual(r.status_code, 401)

    def test_cancel_requires_token(self) -> None:
        self.assertEqual(
            self.client.post("/wo/wo-any/cancel").status_code, 401)

    def test_confirm_requires_token(self) -> None:
        self.assertEqual(
            self.client.post("/wo/wo-any/confirm").status_code, 401)

    def test_wrong_token_rejected(self) -> None:
        r = self.client.get("/wo", headers={"X-Local-Ide-Token": "wrong-token"})
        self.assertEqual(r.status_code, 401)

    def test_token_switch_is_gone(self) -> None:
        """2026-10-05：``LOCAL_IDE_REQUIRE_TOKEN=0`` 不再能关掉鉴权。

        它以前就是死的（设了照样 401），但 README 在承诺「0 = 关闭」——
        与其留一个撒谎的配置面，不如删掉。这个用例把「删掉」钉死。
        """
        old = os.environ.get(_RN)
        os.environ[_RN] = "0"
        try:
            self.assertTrue(_svcapp.ConfirmPolicy.from_env().require_token,
                            "令牌不可关：require_token 必须恒为 True")
            r = self.client.get("/wo", headers={"X-Local-Ide-Token": "wrong"})
            self.assertEqual(r.status_code, 401, "关不掉的鉴权：错令牌必须 401")
        finally:
            if old is None:
                os.environ.pop(_RN, None)
            else:
                os.environ[_RN] = old

    def test_non_ascii_token_does_not_raise(self) -> None:
        """非 ASCII 令牌必须老实回 False，不能把鉴权失败炸成 500。

        真实链路上，客户端可以在 socket 上直接吐非 ASCII 字节，服务端按 latin-1
        解出来就是一个含非 ASCII 的 str。此时若直接对 str 用 hmac.compare_digest，
        会抛 TypeError → 500（等于把「令牌不对」写成了「服务端崩了」）。
        注意不能用 TestClient 传这种头来测：httpx 自己就会拒绝非 ASCII 头值。
        """
        from auth import tokens_equal  # noqa: PLC0415
        self.assertFalse(tokens_equal("令牌中文", "abc-token"))
        self.assertTrue(tokens_equal("abc-token", "abc-token"))

    def test_bearer_form_accepted(self) -> None:
        """Authorization: Bearer <token> 这种带法也要认。"""
        r = self.client.get("/wo", headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
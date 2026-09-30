# -*- coding: utf-8 -*-
"""auth.py —— 服务侧的鉴权小工具集中处。

放在 `service/auth.py` 而不是直接散在 `app.py` 里，是为了：

  · 让鉴权逻辑可单独测试（不用拉起整个 FastAPI 应用）；
  · 扩展 SSE 流等场景需要更细粒度的检查（重试、节流）；
  · 将来网关也用同口径时不用复制（本次仅服务侧用，但接口是通用的）。

约束：本地单机只监听 127.0.0.1，\"鉴权\"的真实作用是\"任何本机进程不能匿名驱动\"；
不是为了防网络攻击。这是 2026-09-29 文档里写过的口径，仍照此执行。
"""
from __future__ import annotations

import os
import secrets
from typing import Tuple

from fastapi import Header, HTTPException


def issue_token() -> str:
    """生成 24 字节的 URL-safe 随机串。"""
    return secrets.token_urlsafe(24)


def env_token() -> str:
    """从环境变量或内置默认拿 token。

    两个变量名都认（2026-09-30 实测深坑：e2e 传 LOCAL_IDE_TOKEN、auth 读
    LOCAL_LLM_TOKEN，错位导致带令牌请求也被拒）：
      · ``LOCAL_IDE_TOKEN`` —— 与 service/app.py 的既有口径一致（优先）
      · ``LOCAL_LLM_TOKEN`` —— 网关侧历史叫法（兼容）
    """
    v = (os.environ.get("LOCAL_IDE_TOKEN") or "").strip()
    if not v:
        v = (os.environ.get("LOCAL_LLM_TOKEN") or "").strip()
    return v or issue_token()


def extract_token(authorization: str = "", x_local_ide_token: str = "") -> str:
    """从两种头里挑出 token：``Authorization: Bearer ...`` 或 ``X-Local-Ide-Token``。"""
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (x_local_ide_token or "").strip()


def require_auth(expected: str, *, what: str):
    """FastAPI 依赖项：每个需要鉴权的端点配 ``dependencies=[Depends(require_auth(token, ...))]``。

    ``expected`` = 当前进程的令牌；``what`` = 资源名（401 错误信息用）。
    """
    from fastapi import Depends

    async def _dep(
        authorization: str = Header(default=""),
        x_local_ide_token: str = Header(default=""),
    ) -> None:
        got = extract_token(authorization, x_local_ide_token)
        if got != expected:
            raise HTTPException(
                status_code=401,
                detail=f"需要令牌：请在请求头带上 X-Local-Ide-Token（{what}）")
    return Depends(_dep)


def check(expected: str, authorization: str, x_local_ide_token: str) -> Tuple[bool, str]:
    """同步鉴权。返回 (是否通过, 错误信息)；通过时错误信息为空字符串。"""
    got = extract_token(authorization, x_local_ide_token)
    if got != expected:
        return False, "需要令牌：请在请求头带上 X-Local-Ide-Token"
    return True, ""
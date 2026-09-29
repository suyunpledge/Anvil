# -*- coding: utf-8 -*-
"""sanitize.py —— 错误与日志的凭据脱敏（通用层，与模型无关）。

为什么要单独一层（2026-09-28 对标 chat-ollama 后补）：

它把 provider 错误统一成一句固定文案（``Model request failed``），
直接禁用了 AI SDK 的默认错误日志——理由是「provider 的错误里可能带凭据」。

我们踩过相邻的坑：AutoClaw 的 exec 输出会对 ``Bearer ` token 自动脱敏，
而脚本里拼字符串构造的鉴权头反而会**原样写进日志**。工程上正确的做法不是
「记得别打印 key」，而是**在唯一出口统一过滤**——这样任何一处新增的日志都自动被覆盖。

本模块只做两件事：
  1. :func:`redact` —— 把一段文本里看起来像凭据的部分替换成 `<redacted>`；
  2. :func:`safe_error` —— 把异常翻译成「可诊断但不泄密」的一句话。

设计取舍：**不做过度过滤**。只打掉真有凭据形状的片段（常见前缀 + 长随机串），
不然会把正常内容（如长 hash、日志 ID）也弄成星号，反而妨碍排障。
"""
from __future__ import annotations

import re
from typing import Any

#: 常见的密钥前缀（各家 provider 与自签 token 的典型形态）
_KEY_PREFIXES = (
    "sk-", "sk_", "api-", "key-", "token-", "ghp_", "gho_", "github_pat_",
    "xoxb-", "xoxp-", "AKIA", "Bearer ", "Basic ",
)

#: 长随机串（32+ 位连续字母数字，允许中间有 -_），典型是 base64/hex 形式的密钥
_LONG_RANDOM = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

#: 显式键值形态：key=xxx / token: xxx / password="xxx"
_KV = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|"
    r"secret|password|passwd|authorization|credentials?|private[_-]?key|"
    r"client[_-]?secret|bearer)"
    r"\b\s*[:=]\s*[\"']?([^\s\"',;]{6,})[\"']?")

#: 请求头里的鉴权（Bearer/Basic 后跟任意非空白）
_AUTH_HEADER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{8,}")

#: 家用/内网绝对路径里的用户名（日志外发时不该带出本机用户名）
_USER_HOME = re.compile(r"(?i)([A-Z]:\\Users\\)([^\\\s\"']+)(\\)")
_USER_HOME_POSIX = re.compile(r"(?i)(/home/|/Users/)([^/\s\"']+)(/)")


def redact(text: Any) -> str:
    """把文本里像凭据的片段替换掉。非字符串先转成字符串。"""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    for p in _KEY_PREFIXES:
        # 前缀 + 后面至少 8 位，一起替换成前缀<redacted>（保留前缀便于判断是哪家）
        s = re.sub(re.escape(p) + r"[A-Za-z0-9._\-]{8,}",
                   p.strip() + "<redacted>", s)
    s = _AUTH_HEADER.sub(lambda m: m.group(1) + " <redacted>", s)
    s = _KV.sub(lambda m: "%s=<redacted>" % m.group(1), s)
    s = _LONG_RANDOM.sub("<redacted>", s)
    s = _USER_HOME.sub(r"\1<user>\3", s)
    s = _USER_HOME_POSIX.sub(r"\1<user>\3", s)
    return s


def safe_error(exc: Any, *, kind: str = "") -> str:
    """把异常翻成「可诊断但不泄密」的一句话。

    保留：异常类型名 + 关键状态（HTTP 码、超时、连接被拒这类）
    打掉：任何看起来像凭据的内容、本机用户名
    """
    name = type(exc).__name__ if not isinstance(exc, str) else "Error"
    raw = str(exc) if exc is not None else ""
    cleaned = redact(raw)
    prefix = ("[%s] " % kind) if kind else ""
    return "%s%s: %s" % (prefix, name, cleaned[:300])


def is_credential_like(s: str) -> bool:
    """判断一段文本是否像凭据（供测试与自检用）。"""
    return redact(s) != s

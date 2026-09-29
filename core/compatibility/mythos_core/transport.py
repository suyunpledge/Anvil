# -*- coding: utf-8 -*-
"""transport.py —— 出网 + 解码（模型无关层，纯标准库 urllib）。

只做一件事：把 body POST 到 Ollama ``/api/chat``，把应答解成 dict。
所有可预期的失败都被翻译成 :mod:`.errors` 里的异常，调用方无需碰 urllib。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .config import HEALTH_TIMEOUT, OLLAMA_HOST, REQUEST_TIMEOUT_DEFAULT
from .errors import AdapterError, ContextOverflow
from .sanitize import redact

# ------------------------------------------------------------
# ★ B6 传输层 opener：显式禁用系统代理，且整个进程复用同一个实例。
#
# 为什么必须禁：``urllib.request.urlopen`` 用默认 opener，会读取**系统代理**设置
# （本机实测 = ``http://127.0.0.1:7897``），于是发往 ``127.0.0.1:11434`` 的**本地**请求
# 也会被交给代理。一旦代理进程被改成远端，请求（连同源码/上下文）就会离开本机——
# 这既破坏「出网必须关死、本地闭环」的硬口径，也让「无任何外连」无法成立。
# 显式装一个**空** ProxyHandler 即可绕过系统代理（隔离证据：verification/isolate_proxy.py）。
# 复用而非每次新建，是为了避免每次都重新解析代理设置（真实热态差约 +0.14s/次）。
# ------------------------------------------------------------
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_context_overflow(text: str, code: int) -> bool:
    """判断一个 HTTP 400 是不是「上下文超限」（R5）。

    原实现的判定写死 ``exceed_context_size_error``；这里放宽为若干等价标记，
    并统一小写比较（Ollama 不同版本措辞略有差异）。
    """
    t = (text or "").lower()
    return (
        "exceed_context_size" in t
        or "context length" in t
        or (code == 400 and "context" in t)
    )


class OllamaTransport:
    """极薄的 Ollama HTTP 客户端。"""

    def __init__(self, host: str = OLLAMA_HOST, timeout: float = REQUEST_TIMEOUT_DEFAULT):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def chat(self, body: Dict[str, Any], timeout: Optional[float] = None
             ) -> Tuple[Dict[str, Any], float]:
        """POST ``/api/chat``，返回 ``(应答字典, 耗时秒)``。

        请求一律走模块级 ``_NO_PROXY_OPENER``（★ B6：显式禁代理），绝不经系统代理。

        Raises:
            ContextOverflow: 上下文超限（确定性，不该重试）。
            AdapterError: 其它 HTTP / 网络 / 解码错误（可重试）。
        """
        url = self.host + "/api/chat"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with _NO_PROXY_OPENER.open(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "ignore")
            if _is_context_overflow(txt, e.code):
                raise ContextOverflow(txt[:300])
            # ★ 统一出口脱敏（依据 2026-09-28 对标 chat-ollama）：
            #   provider 的错误体可能回显请求头／密钥；这里是唯一的出网应答出口，
            #   在这一层过滤，任何新增的上层日志都自动被覆盖。
            raise AdapterError("HTTP %s: %s" % (e.code, redact(txt)[:300]))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # URLError / socket.timeout / ConnectionRefusedError 都落这里
            raise AdapterError("%s: %s" % (type(e).__name__, redact(str(e))))

        # ★ 关键修复：解码放在 try 之外时，坏 JSON 会穿透整个 fill_slot。
        #    这里统一翻译成 AdapterError（可重试）。
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise AdapterError("响应不是合法 JSON：%s | %r" % (e, redact(raw[:200])))
        if not isinstance(obj, dict):
            raise AdapterError("响应不是 JSON 对象：%s" % type(obj).__name__)
        return obj, time.time() - t0

    def list_models(self) -> Dict[str, Any]:
        """GET ``/api/tags``（健康检查用）。错误同样走脱敏。"""
        req = urllib.request.Request(self.host + "/api/tags")
        try:
            with _NO_PROXY_OPENER.open(req, timeout=HEALTH_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise AdapterError("list_models 失败：%s" % redact(str(e)))

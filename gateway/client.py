# -*- coding: utf-8 -*-
"""client.py —— 让状态机走网关（而不是直连 Ollama）。

为什么需要它：网关的职责是「所有模型调用的唯一出口」——画像应用、审计、出网熔断都挂在
它身上。但只要状态机直连 Ollama，这三件事就全被绕过去了（2026-09-28 端到端跑时暴露：
工单跑完了，审计里一条记录都没有）。

做法（与规划里 3.1 一致）：不动核心代码，只把适配器的 transport 换掉。
`MythosAdapter` 只要求 transport 提供 `chat(body, timeout)` 与 `list_models()`，
所以这里实现同样形状即可 —— 状态机一行都不用改。

网关侧为此额外暴露了 Ollama 格式的转发端点 `/ollama/api/chat`：
它能收 Ollama 形状的请求体（状态机本来就是这个形状），
在里面完成「按画像修正 → 转发 → 审计」，再把 Ollama 形状的应答原样返回。
这样既不引入格式转换的风险，又保证所有调用都过同一个出口。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


class GatewayUnavailable(RuntimeError):
    """网关不可达。/ 用于 fail-fast 提示，而不是静默退回直连。"""


class GatewayTransport:
    """与 `OllamaTransport` 同形状的传输层，指向本地网关。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8080",
                 timeout: float = 300.0, upstream: str = "ollama") -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.upstream = upstream
        #: 禁系统代理（与核心 transport 同一条原则：本地闭环不能被代理劫持）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------------- 形状对齐 ----------------
    def _post(self, path: str, payload: Dict[str, Any], timeout: Optional[float]) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code in (502, 504):
                raise GatewayUnavailable("网关上游失败：%s" % body[:200])
            raise GatewayUnavailable("网关返回 %s：%s" % (e.code, body[:200]))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GatewayUnavailable("连不上网关 %s（%s: %s）" % (self.base, type(e).__name__, e))
        except ValueError as e:
            raise GatewayUnavailable("网关应答不是合法 JSON：%s" % e)

    def chat(self, body: Dict[str, Any], timeout: Optional[float] = None
             ) -> Tuple[Dict[str, Any], float]:
        """POST 到网关的 Ollama 兼容端点，返回 (应答字典, 耗时秒)。"""
        import time
        t0 = time.time()
        out = self._post("/ollama/api/chat", body, timeout)
        # 网关在这一层挡住的是“上游出错”；这里把错误翻成异常，语义与核心 transport 一致
        if isinstance(out, dict) and out.get("error") and "message" not in out:
            raise GatewayUnavailable(str(out["error"])[:300])
        return out, time.time() - t0

    def list_models(self) -> Dict[str, Any]:
        try:
            req = urllib.request.Request(self.base + "/v1/models")
            with self._opener.open(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = [{"name": m.get("id")} for m in data.get("data", [])]
            return {"models": names}
        except Exception as e:
            raise GatewayUnavailable("连不上网关 %s（%s）" % (self.base, e))

    # ---------------- 便于排障 ----------------
    def health(self) -> Dict[str, Any]:
        try:
            req = urllib.request.Request(self.base + "/health")
            with self._opener.open(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}


def route_adapter_through_gateway(adapter: Any, gateway_url: str = "http://127.0.0.1:8080",
                                  strict: bool = True) -> Dict[str, Any]:
    """把适配器的 transport 换成网关。返回一份说明（便于日志与自检）。

    strict=True 时，网关不可达就**不静默退回直连**——那会让「审计与画像」这两条承诺失效，
    而静默失效比报错危险得多。
    """
    tr = GatewayTransport(gateway_url)
    h = tr.health()
    if not h.get("ok"):
        if strict:
            raise GatewayUnavailable(
                "网关不可达（%s）。工单服务需要通过网关发模型请求，"
                "否则画像与审计不生效。请先启动：python gateway/app.py" % gateway_url)
        return {"via": "direct", "ok": False, "reason": str(h.get("error"))}
    try:
        adapter.transport = tr
    except Exception as e:
        if strict:
            raise GatewayUnavailable("无法替换 transport：%s" % e)
        return {"via": "direct", "ok": False, "reason": str(e)}
    return {"via": "gateway", "ok": True, "gateway": gateway_url,
            "profiles": h.get("profiles"), "models": h.get("models")}

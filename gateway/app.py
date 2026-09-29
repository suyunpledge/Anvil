#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py —— 交付件 A：本地模型网关（所有模型调用的唯一出口）。

它只做四件事，别的一概不做：

  1. 对外提供 OpenAI 兼容接口（`/v1/models`、`/v1/chat/completions`），
     任何现成的 OpenAI 客户端（含 VS Code 扩展）都能直接指过来；
  2. 转发到本机 Ollama，并**按模型画像修正请求**——最要紧的一条是
     `sends_think=False` 的模型（实测 qwen2.5-coder / ministral / glm4 / llama3.1
     收到 think 会直接 HTTP 400）在这里就把键摘掉。调用方不必知道哪个模型有哪个脾气；
  3. 每次调用写一行审计（`audit.jsonl`）：时间 / 模型 / 耗时 / 画像 / 是否摘过 think / token 估算；
  4. **出网熔断**：只允许 loopback 与内网段；启动自检外连，命中即拒绝启动。

跑法：
    python gateway/app.py                      # 127.0.0.1:8080
    python gateway/app.py --selfcheck          # 只跑自检

测试：`python gateway/tests/test_gateway.py`
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.paths import ensure_core_on_path, runtime_path, version  # noqa: E402

ensure_core_on_path()

# ★ FastAPI 的导入必须在**模块顶层**：
#   本文件有 `from __future__ import annotations`，注解会变成字符串，
#   FastAPI 再把字符串拿到模块全局命名空间去解析类型。若把 Request 写在函数内部导入，
#   全局没有这个名字，FastAPI 会把它当作**查询参数**，于是所有 POST 都回 422
#   （`loc:["query","request"]`）——这个坑 2026-09-28 实测踩到过。
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    _FASTAPI_OK = True
except Exception:                                    # 仅 --selfcheck 与纯逻辑测试时允许缺依赖
    FastAPI = Request = object                       # type: ignore
    JSONResponse = StreamingResponse = object        # type: ignore
    _FASTAPI_OK = False

try:
    from mythos_core.sanitize import redact
except Exception:                                    # 独立运行兜底
    def redact(s: Any) -> str:                       # type: ignore
        return str(s)

OLLAMA = os.environ.get("LOCAL_LLM_OLLAMA", "http://127.0.0.1:11434")
AUDIT_PATH = os.environ.get("LOCAL_LLM_AUDIT") or runtime_path("audit.jsonl")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 出网熔断白名单：只放行 loopback 与内网段
ALLOWED_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

#: 任务类型 → 首选模型（一期初版；基准数字出来后再按数据改）
ROUTES: Dict[str, str] = {
    "edit": "qwen2.5-coder:7b",       # 实测 3.3s，最快
    "chat": "gemma4:e4b",             # 实测 24s，四项行为位全过
    "recon": "gemma4:e4b",
    "fallback": "fableforge-ai/mythos-v2-8b:q4_k_m",
}


# ============================================================
# 出网熔断
# ============================================================
class OutboundBlocked(RuntimeError):
    """目标不在白名单内 —— 本地闭环被破坏。"""


def host_allowed(host: str) -> bool:
    """判断目标主机是否在白名单内。域名一律解析后再判；解析不到也拒绝。"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not any(ip in net for net in ALLOWED_NETS):
            return False
    return True


def assert_url_allowed(url: str) -> None:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "")
    if host == "localhost":
        host = "127.0.0.1"
    if not host_allowed(host):
        raise OutboundBlocked(
            "拒绝出网：%s（只允许 loopback 与内网段；本网关不做任何外部调用）" % host)


def selfcheck_outbound() -> Dict[str, Any]:
    """启动自检：白名单对 loopback 放行、对外网域名拒绝。"""
    results = []
    ok = True
    try:
        assert_url_allowed("http://127.0.0.1:11434/api/tags")
        results.append(("loopback 允许", True))
    except OutboundBlocked:
        results.append(("loopback 允许", False))
        ok = False
    try:
        assert_url_allowed("https://example.com/")
        results.append(("外网 example.com 拒绝", False))
        ok = False
    except OutboundBlocked:
        results.append(("外网 example.com 拒绝", True))
    return {"ok": ok, "detail": results}


# ============================================================
# 审计
# ============================================================
def audit(rec: Dict[str, Any]) -> None:
    rec = dict(rec)
    rec.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_audit(limit: int = 100) -> list:
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def rough_tokens(text: str) -> int:
    """粗估 token：中文按字、其它按 4 字符。只用于审计与预算，不追求精确。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + (other // 4 if other else 0))


# ============================================================
# 画像与路由
# ============================================================
def load_profiles() -> Dict[str, Any]:
    try:
        from mythos_core.profiles import (ALL_PROFILES, PROFILE_BY_MODEL,
                                         load_generated_profiles)
        load_generated_profiles()
        return {"by_key": ALL_PROFILES, "by_model": PROFILE_BY_MODEL}
    except Exception:
        return {"by_key": {}, "by_model": {}}


def pick_model(requested: Optional[str], task: Optional[str]) -> str:
    if requested:
        return requested
    if task and task in ROUTES:
        return ROUTES[task]
    return ROUTES["fallback"]


def apply_profile(model: str, body: Dict[str, Any],
                  profiles: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """按画像修正请求体，返回（修正后的 body, 画像摘要）。

    **这里是唯一处理 think 兼容性的地方。** 调用方（扩展 / 状态机 / 任何 OpenAI 客户端）
    不需要知道哪个模型有这个脾气——上一轮体检发现 11 个模型里有 4 个不收 think=true，
    而编辑节点默认就要用；不收进来的话那 4 个模型会在工单第一步直接崩。
    """
    key = profiles.get("by_model", {}).get(model)
    spec = profiles.get("by_key", {}).get(key) if key else None
    info: Dict[str, Any] = {"profile": key or "(未登记)", "sends_think": None}
    if spec is None:
        return body, info
    info["sends_think"] = spec.sends_think
    if not spec.sends_think and "think" in body:
        body = {k: v for k, v in body.items() if k != "think"}
        info["stripped_think"] = True
    # 上下文配额：按画像上限夹一次，避免客户端误传超大窗口把本机拖死
    opts = body.get("options") or {}
    if "num_ctx" in opts:
        try:
            want = int(opts["num_ctx"])
            if want > spec.ctx_max:
                opts = dict(opts)
                opts["num_ctx"] = spec.ctx_max
                body = dict(body, options=opts)
                info["clamped_num_ctx"] = spec.ctx_max
        except (TypeError, ValueError):
            pass
    return body, info


# ============================================================
# 转发（OpenAI 形态 ↔ Ollama 形态）
# ============================================================
def post_ollama(path: str, payload: Dict[str, Any], timeout: int = 300) -> Tuple[int, Dict[str, Any]]:
    url = OLLAMA + path
    assert_url_allowed(url)                      # ★ 唯一出网门：非流式
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": redact(e.read().decode("utf-8", "ignore"))[:400]}
    except Exception as e:
        return 0, {"error": redact("%s: %s" % (type(e).__name__, e))}


def post_ollama_stream(path: str, payload: Dict[str, Any], timeout: int = 600):
    """打开一个流式连接。**同样过出网门**（外部审查 #4）。

    返回 (status, 可迭代的行生成器)。调用方负责消费与关闭。
    """
    url = OLLAMA + path
    assert_url_allowed(url)                      # ★ 同一道门，流式不得绕过
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    resp = _NO_PROXY.open(req, timeout=timeout)
    return 200, resp


def get_ollama(path: str, timeout: int = 10) -> Tuple[int, Dict[str, Any]]:
    url = OLLAMA + path
    assert_url_allowed(url)
    try:
        with _NO_PROXY.open(urllib.request.Request(url), timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 0, {"error": redact(str(e))}


def openai_to_ollama(body: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI 形态 → Ollama 形态（只映射必要字段，保持可预期）。"""
    out: Dict[str, Any] = {
        "model": body.get("model"),
        "messages": body.get("messages") or [],
        "stream": bool(body.get("stream")),
    }
    opts: Dict[str, Any] = {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("max_tokens", "num_predict")):
        if body.get(src) is not None:
            opts[dst] = body[src]
    if body.get("max_ctx"):
        opts["num_ctx"] = body["max_ctx"]
    if opts:
        out["options"] = opts
    for k in ("tools", "think"):
        if k in body:
            out[k] = body[k]
    return out


def ollama_to_openai(resp: Dict[str, Any], model: str) -> Dict[str, Any]:
    m = resp.get("message", {}) or {}
    msg: Dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
    if m.get("tool_calls"):
        msg["tool_calls"] = m["tool_calls"]
    p_n = resp.get("prompt_eval_count") or 0
    c_n = resp.get("eval_count") or 0
    reason = resp.get("done_reason")
    return {
        "id": "chatcmpl-local-%d" % int(time.time() * 1000),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": "stop" if reason in (None, "stop") else reason}],
        "usage": {"prompt_tokens": p_n, "completion_tokens": c_n, "total_tokens": p_n + c_n},
    }


# ============================================================
# 服务
# ============================================================
def build_app():
    if not _FASTAPI_OK:
        raise SystemExit("未找到 fastapi；请用带 fastapi/uvicorn 的解释器运行"
                         "（本机 AutoClaw 内嵌 python 自带）")

    app = FastAPI(title="Local Model Gateway", version="0.2.0")
    PROFILES = load_profiles()

    @app.get("/health")
    def health() -> Dict[str, Any]:
        code, tags = get_ollama("/api/tags")
        sc = selfcheck_outbound()
        return {"ok": code == 200 and sc["ok"], "ollama": code == 200,
                "outbound_block": sc["ok"], "profiles": len(PROFILES.get("by_key", {})),
                "models": len((tags or {}).get("models", [])),
                "audit_path": AUDIT_PATH, "core": version()}

    @app.get("/v1/models")
    def models() -> Dict[str, Any]:
        code, tags = get_ollama("/api/tags")
        return {"object": "list",
                "data": [{"id": m.get("name"), "object": "model", "owned_by": "local"}
                         for m in (tags or {}).get("models", [])]}

    @app.get("/audit")
    def audit_view(limit: int = 50) -> Dict[str, Any]:
        return {"path": AUDIT_PATH, "entries": read_audit(limit)}

    # ------------------------------------------------------------------
    # Ollama 兼容转发：给「状态机直连 Ollama」那条路径准备的
    # ------------------------------------------------------------------
    # 为什么需要：状态机（含工单服务）内部是按 Ollama 的 /api/chat 形状发请求的。
    # 只要它直连 Ollama，画像、审计、熔断这三件事就全被绕过了。
    # 所以网关额外开一个同形状的入口：收 Ollama 请求体 → 跑同一套画像修正与审计
    # → 转发 → 原样回 Ollama 应答。这样调用方不心改一行，出口却只有这一个。
    @app.post("/ollama/api/chat")
    async def ollama_passthrough(request: Request):
        body = await request.json()
        model = body.get("model") or ROUTES["fallback"]
        body["model"] = model
        body, pinfo = apply_profile(model, body, PROFILES)
        if body.get("stream"):                      # 状态机不用流式；明确拒绝，避免误用
            return JSONResponse(status_code=400,
                                content={"error": "流式不在这个端点上提供，请用 /v1/chat/completions"})
        t0 = time.time()
        code, resp = post_ollama("/api/chat", body)
        audit({"kind": "ollama.chat", "model": model, "status": code,
               "sec": round(time.time() - t0, 3), "profile": pinfo.get("profile"),
               "stripped_think": pinfo.get("stripped_think", False),
               "clamped_num_ctx": pinfo.get("clamped_num_ctx"),
               "prompt_tokens_est": rough_tokens(json.dumps(body.get("messages", []),
                                                            ensure_ascii=False))})
        if code != 200:
            return JSONResponse(status_code=502,
                                content={"error": resp.get("error", "upstream failed")})
        return resp

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        task = body.pop("_task", None)              # 非标准字段：仅供路由
        model = pick_model(body.get("model"), task)
        body["model"] = model
        oll = openai_to_ollama(body)
        oll, pinfo = apply_profile(model, oll, PROFILES)

        if oll.get("stream"):
            return StreamingResponse(_stream(oll, model, pinfo, task),
                                     media_type="text/event-stream")

        t0 = time.time()
        code, resp = post_ollama("/api/chat", oll)
        audit({"kind": "chat", "model": model, "status": code,
               "sec": round(time.time() - t0, 3), "profile": pinfo.get("profile"),
               "stripped_think": pinfo.get("stripped_think", False),
               "clamped_num_ctx": pinfo.get("clamped_num_ctx"),
               "prompt_tokens_est": rough_tokens(json.dumps(oll.get("messages", []),
                                                            ensure_ascii=False)),
               "task": task})
        if code != 200:
            return JSONResponse(status_code=502,
                                content={"error": resp.get("error", "upstream failed")})
        return ollama_to_openai(resp, model)

    def _stream(oll: Dict[str, Any], model: str, pinfo: Dict[str, Any], task):
        """SSE 流式：Ollama 的 NDJSON 逐行翻成 OpenAI 的 `data:` 事件。

        ★ 外部审查 #4：原来这里直接访问 OLLAMA，**没走出网校验**——
          非流式路径调了 `assert_url_allowed()`，流式却绕过了它。
          如果环境变量把 Ollama 地址指到外网，流式请求就能出网。
          现在两条路径共用同一道门（就是把“打开连接”这件事收进一个函数）。
          审计也不再写死 200：真实结果（含失败/中断）如实记录。
        """
        t0 = time.time()
        n_out = 0
        status = 200
        err = ""
        try:
            status, resp = post_ollama_stream("/api/chat", oll, timeout=600)
        except OutboundBlocked as e:
            status, err = 403, str(e)
        except Exception as e:
            status, err = 502, redact("%s: %s" % (type(e).__name__, e))
        if err:
            yield "data: %s\n\n" % json.dumps({"error": err})
            yield "event: end\ndata: [DONE]\n\n"
            audit({"kind": "chat.stream", "model": model, "status": status,
                   "sec": round(time.time() - t0, 3), "profile": pinfo.get("profile"),
                   "error": err[:200], "task": task})
            return
        try:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                piece = (obj.get("message") or {}).get("content") or ""
                if piece:
                    n_out += rough_tokens(piece)
                    yield "data: %s\n\n" % json.dumps({
                        "id": "chatcmpl-local", "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": piece}}]})
                if obj.get("done"):
                    break
            yield "data: [DONE]\n\n"
        except Exception as e:                      # 客户端断开 / 上游中断
            status = 499
            err = redact("%s: %s" % (type(e).__name__, e))
            yield "data: %s\n\n" % json.dumps({"error": err})
        finally:
            audit({"kind": "chat.stream", "model": model, "status": status,
                   "sec": round(time.time() - t0, 3), "profile": pinfo.get("profile"),
                   "completion_tokens_est": n_out,
                   "error": err[:200] if err else None, "task": task})

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地模型网关（OpenAI 兼容）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--selfcheck", action="store_true", help="只跑自检，不启服务")
    args = ap.parse_args(argv)

    sc = selfcheck_outbound()
    print("出网熔断自检：%s" % ("通过" if sc["ok"] else "未通过"))
    for name, ok in sc["detail"]:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    if not sc["ok"]:
        print("拒绝启动：出网熔断未生效（本地闭环是硬要求）")
        return 1
    P = load_profiles()
    print("画像：%d 份（%s）" % (len(P.get("by_key", {})), ", ".join(sorted(P.get("by_key", {})))))
    print("审计：%s" % AUDIT_PATH)
    if args.selfcheck:
        return 0

    import uvicorn
    print("启动：http://%s:%d  （OpenAI 兼容）" % (args.host, args.port))
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

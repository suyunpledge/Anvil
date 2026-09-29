#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py —— 交付件 B：工单服务（把状态机包成 HTTP + SSE）。

设计要点（都是为了"能被别人接手改"）：

  · 逻辑全在 `runner.py`，本文件只做 HTTP 映射 —— 想换前端不必碰这里；
  · 事件走 SSE，前端能实时看到每个节点与每个 gate 的结果；
  · **落盘必须经人确认**：工单一律先跑到"待确认"状态，`POST /wo/{id}/confirm` 才写文件；
  · 取消是协作式的：包一层适配器，在两个节点之间检查标记。

接口：
    GET  /health                   服务与核心版本
    GET  /presets                  可用适配层 / 模型（供下拉框）
    POST /wo                       建单并开跑
    GET  /wo                       列出全部工单
    GET  /wo/{id}                  单张工单快照
    GET  /wo/{id}/events           SSE 事件流
    POST /wo/{id}/cancel           取消
    POST /wo/{id}/confirm          人确认后落盘
    GET  /wo/{id}/staged           暂存内容（给 diff 视图用）

跑法：python service/app.py [--port 8090]
测试：python service/tests/test_service.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# ★ 内嵌解释器是隔离模式（._pth），**脚本所在目录不进 sys.path**：
#   所以同目录的 `runner` 必须自己把 _HERE 挂上去，否则 ModuleNotFoundError。
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.paths import ensure_core_on_path, runtime_path, version  # noqa: E402

ensure_core_on_path()

from runner import (RunnerOpts, RunnerRegistry, STATUS_AWAITING_CONFIRM,  # noqa: E402
                    STATUS_RUNNING, list_presets)

# ★ FastAPI 导入放模块顶层：见 gateway/app.py 里同一处的注释（注解字符串化的坑）
try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
    _FASTAPI_OK = True
except Exception:
    FastAPI = object                       # type: ignore
    BaseModel = object                     # type: ignore
    JSONResponse = StreamingResponse = object   # type: ignore
    _FASTAPI_OK = False

    def Field(*a, **kw):                   # type: ignore
        return None


REGISTRY = RunnerRegistry()


# ============================================================
# 服务端策略（外部审查 #2：不能让客户端决定“要不要人确认”）
# ============================================================
class ConfirmPolicy:
    """确认与目录范围策略，只需环境变量配置。

    ``LOCAL_IDE_REQUIRE_CONFIRM``
        1（默认）= 服务端强制人确认，客户端传 false 也无效
        0        = 允许客户端自己决定（仅建议在完全可信的本机环境下用）
    ``LOCAL_IDE_ALLOWED_ROOTS``
        允许操作的工作目录根，多个用 ``;`` 分隔；不设则不限制。
    ``LOCAL_IDE_REQUIRE_TOKEN``
        1（默认）= 写操作要令牌；0 = 关闭（不推荐）
    """

    def __init__(self, force_confirm: bool, allowed_roots: List[str], require_token: bool) -> None:
        self.force_confirm = force_confirm
        self.allowed_roots = [os.path.abspath(r) for r in allowed_roots if r]
        self.require_token = require_token

    @classmethod
    def from_env(cls) -> "ConfirmPolicy":
        force = os.environ.get("LOCAL_IDE_REQUIRE_CONFIRM", "1") != "0"
        roots = [p for p in (os.environ.get("LOCAL_IDE_ALLOWED_ROOTS", "") or "").split(";") if p.strip()]
        token = os.environ.get("LOCAL_IDE_REQUIRE_TOKEN", "1") != "0"
        return cls(force, roots, token)

    def effective_confirm(self, client_wants: bool) -> bool:
        """策略优先：要确认时必须确认。"""
        return True if self.force_confirm else bool(client_wants)

    def workdir_allowed(self, path: str) -> bool:
        if not self.allowed_roots:
            return True
        p = os.path.abspath(path or "")
        for root in self.allowed_roots:
            if p == root or p.startswith(root + os.sep):
                return True
        return False

    def summary(self) -> Dict[str, Any]:
        return {"force_confirm": self.force_confirm,
                "allowed_roots": self.allowed_roots or "(不限制)",
                "require_token": self.require_token}


def _gen_token() -> str:
    import secrets
    return secrets.token_urlsafe(24)


def _token_path() -> str:
    return runtime_path("service-token")


def _write_token_file(token: str) -> None:
    """把令牌写到 .runtime/service-token（仅本用户可读），供本地扩展读取。

    这不是“把密钥写在代码里”：文件在用户目录下、每次启动重新生成、且只用于本机回环通信。
    """
    try:
        p = _token_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass


if _FASTAPI_OK:
    class CreateReq(BaseModel):
        """建单请求。字段名与 RunnerOpts 对齐（少传就有默认值）。"""
        kind: str = Field(default="code", description="code | tidy")
        task: str = ""
        workdir: str = ""
        target: str = ""
        test_path: str = ""
        adapter: str = "mythos"
        model: str = ""
        mode: str = "dry_run"
        dirs: List[str] = Field(default_factory=list)
        semantic: bool = False
        max_repair_rounds: int = 3
        require_confirm: bool = True
else:
    class CreateReq:                       # type: ignore
        pass


def build_app():
    if not _FASTAPI_OK:
        raise SystemExit("未找到 fastapi；请用带 fastapi/uvicorn 的解释器运行（本机 AutoClaw 内嵌 python 自带）")

    app = FastAPI(title="Local IDE Work-Order Service", version="0.3.0")

    # ------------------------------------------------------------------
    # 安全加固（2026-09-28 外部审查 #2）
    # ------------------------------------------------------------------
    # 原实现的问题：allow_origins=["*"] + 无认证 + require_confirm 完全由客户端决定。
    # 后果：任何本机程序、或能访问本地网络的网页，都可以提交 require_confirm=false
    #       让模型去改任意已知工作目录（“人工确认”这个承诺被绕过）。
    #
    # 三处修正：
    #   ① CORS 收窄到本机来源（localhost / 127.0.0.1 / vscode-webview）；
    #   ② 写操作要求令牌（启动时生成或读 LOCAL_IDE_TOKEN，写入 token 文件供扩展读）；
    #   ③ require_confirm 由**服务端策略**决定：策略要求确认时，客户端传 false 也不生效；
    #      并可选限定允许操作的工作目录根（LOCAL_IDE_ALLOWED_ROOTS）。
    allow_origin_regex = os.environ.get(
        "LOCAL_IDE_CORS_REGEX", r"^(https?://(127\.0\.0\.1|localhost)(:\d+)?|vscode-webview://.*)$")
    app.add_middleware(CORSMiddleware, allow_origin_regex=allow_origin_regex,
                       allow_methods=["GET", "POST"], allow_headers=["*"],
                       allow_credentials=False)
    token = (os.environ.get("LOCAL_IDE_TOKEN") or "").strip() or _gen_token()
    _write_token_file(token)
    policy = ConfirmPolicy.from_env()

    def _auth(request: Request) -> Optional[JSONResponse]:
        """写操作鉴权。读接口（health/presets）不要求令牌，便于自检。

        允许两种带法：`Authorization: Bearer <token>` 或 `X-Local-Ide-Token: <token>`。
        """
        if not policy.require_token:
            return None
        got = ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        if not got:
            got = (request.headers.get("x-local-ide-token") or "").strip()
        if got != token:
            return JSONResponse(status_code=401,
                                content={"error": "需要令牌：在请求头带上 "
                                                   "X-Local-Ide-Token（令牌见 .runtime/service-token）"})
        return None

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "core": version(),
                "runtime": os.path.dirname(runtime_path("x")),
                "wos": len(REGISTRY.all()),
                "policy": policy.summary(),
                "auth": {"token_required": policy.require_token,
                         "token_file": _token_path()}}

    @app.get("/presets")
    def presets() -> Dict[str, Any]:
        return list_presets()

    @app.post("/wo")
    def create(req: CreateReq, request: Request):
        bad = _auth(request)
        if bad is not None:
            return bad
        opts = RunnerOpts(**req.model_dump())
        if opts.kind == "code" and not (opts.workdir and opts.target):
            return JSONResponse(status_code=400,
                                content={"error": "code 工单需要 workdir 与 target"})
        if opts.kind == "tidy" and not opts.workdir:
            return JSONResponse(status_code=400, content={"error": "tidy 工单需要 workdir"})
        # ★ 服务端策略：策略要求确认时，客户端传 require_confirm=false 不生效
        opts.require_confirm = policy.effective_confirm(opts.require_confirm)
        # ★ 工作目录白名单（设了才生效）
        if not policy.workdir_allowed(opts.workdir):
            return JSONResponse(status_code=403,
                                content={"error": "该工作目录不在允许范围内（LOCAL_IDE_ALLOWED_ROOTS）",
                                         "allowed": policy.allowed_roots})
        r = REGISTRY.create(opts)
        return {"wo_id": r.wo_id, "status": r.status,
                "require_confirm": opts.require_confirm,
                "policy": policy.summary()}

    @app.get("/wo")
    def list_wo() -> Dict[str, Any]:
        items = [{"wo_id": r.wo_id, "status": r.status, "kind": r.opts.kind,
                  "task": r.opts.task[:80]} for r in REGISTRY.all()]
        return {"count": len(items), "items": items}

    @app.get("/wo/{wo_id}")
    def get_wo(wo_id: str):
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        return r.snapshot()

    @app.get("/wo/{wo_id}/staged")
    def staged(wo_id: str, request: Request):
        """暂存内容 + diff（给 diff 视图用）。

        ★ 鉴权（2026-09-29 自查）：这个接口返回**文件内容与本地路径**，
          属于敏感读口，不能因为“是读接口”就豁免令牌。
        """
        bad = _auth(request)
        if bad is not None:
            return bad
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        # 只回文件名，不回完整本地路径（减少本机信息暴露面；扩展端自己知道工作目录）
        real = str(r.result.get("real_path") or "")
        return {"wo_id": wo_id, "content": r.result.get("staged_content", ""),
                "diff": r.result.get("diff", ""),
                "filename": os.path.basename(real) if real else "",
                "status": r.status}

    @app.post("/wo/{wo_id}/cancel")
    def cancel(wo_id: str, request: Request):
        bad = _auth(request)
        if bad is not None:
            return bad
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        return {"ok": r.cancel(), "status": r.status}

    @app.post("/wo/{wo_id}/confirm")
    def confirm(wo_id: str, request: Request):
        bad = _auth(request)
        if bad is not None:
            return bad
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        res = r.confirm()
        return JSONResponse(status_code=(200 if res.get("ok") else 409), content=res)

    @app.get("/wo/{wo_id}/events")
    def events(wo_id: str, once: int = 0) -> Any:
        """SSE 事件流。`?once=1` 时只回一次快照（便于脚本化测试）。"""
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})

        def gen():
            yield "data: %s\n\n" % json.dumps({"kind": "snapshot", "wo": r.wo_id,
                                               "status": r.status}, ensure_ascii=False)
            if once:
                yield "event: end\ndata: [DONE]\n\n"
                return
            idle = 0.0
            while True:
                batch = r.drain(timeout=0.5)
                if batch:
                    idle = 0.0
                    for ev in batch:
                        yield "data: %s\n\n" % json.dumps(ev, ensure_ascii=False, default=str)
                else:
                    idle += 0.5
                if not r.alive and r.events.empty() and r.status != STATUS_RUNNING:
                    # 收尾：把剩余事件吐完再结束
                    for ev in r.drain():
                        yield "data: %s\n\n" % json.dumps(ev, ensure_ascii=False, default=str)
                    yield "event: end\ndata: [DONE]\n\n"
                    return
                if idle > 900:          # 单连接最长 15 分钟，防挂死
                    yield "event: timeout\ndata: [DONE]\n\n"
                    return

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地 IDE 工单服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args(argv)
    import uvicorn
    print("工单服务：http://%s:%d" % (args.host, args.port))
    print("核心版本：%s" % version())
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

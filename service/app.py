#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py —— 交付件 B：工单服务（把状态机包成 HTTP + SSE）。

设计要点（都是为了"能被别人接手改"）：

  · 逻辑全在 `runner.py`，本文件只做 HTTP 映射 —— 想换前端不必碰这里；
  · 事件走 SSE，前端能实时看到每个节点与每个 gate 的结果；
  · **落盘必须经人确认**：工单一律先跑到"待确认"状态，`POST /wo/{id}/confirm` 才写文件；
  · 取消是协作式的：包一层适配器，在两个节点之间检查标记。

鉴权口径（2026-09-30 补齐）：
  · /health / /presets 仍开放（自检脚本需要零门槛）
  · 其余读口（/wo 列表、/wo/{id}、/events、/wo/{id}/staged）与所有写口一致要求令牌
  · 扩展侧已支持自动读取令牌文件（extension.js::loadToken），无需手动配置

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
from auth import env_token, require_auth, extract_token  # noqa: E402

# ★ FastAPI 导入放模块顶层：见 gateway/app.py 里同一处的注释（注解字符串化的坑）
try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
    _FASTAPI_OK = True
except Exception:
    FastAPI = Header = HTTPException = Request = object     # type: ignore
    BaseModel = object                     # type: ignore
    JSONResponse = StreamingResponse = object   # type: ignore
    _FASTAPI_OK = False

    def Field(*a, **kw):                   # type: ignore
        return None


REGISTRY = RunnerRegistry()

#: ★ 进程级令牌：只生成/读取一次（2026-09-30 实测坑：build_app 被调两次时
#:   会生成两个不同令牌并两次覆盖文件，先读到旧令牌的一方全部 401）。
#:   放在模块级，无论 build_app 被调多少次都用同一个。
import secrets as _secrets


def _resolve_token() -> str:
    """进程级只算一次：环境变量优先（LOCAL_IDE_TOKEN，兼容 LOCAL_LLM_TOKEN），否则随机。"""
    v = (os.environ.get("LOCAL_IDE_TOKEN") or "").strip()
    if not v:
        v = (os.environ.get("LOCAL_LLM_TOKEN") or "").strip()
    return v or _secrets.token_urlsafe(24)


_TOKEN = _resolve_token()


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
        1（默认）= 写操作要读 + 读口（除 health/presets 外）也要读；0 = 关闭（不推荐）
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

    def effective_confirm(self, client_wants: bool, allow_delete: bool = False) -> bool:
        """策略优先：要确认时必须确认。

        ★ 2026-10-03：工单一旦声明 allow_delete（具备删除能力），**无条件强制确认**——
        这条不受 LOCAL_IDE_REQUIRE_CONFIRM=0 影响。理由：删除不可逆，
        「每次删除都要人同意」是用户明确要求，不能被任何环境变量关掉。
        """
        if allow_delete:
            return True
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


def _token_path() -> str:
    return runtime_path("service-token")


def _write_token_file(token: str) -> None:
    """把令牌写到 .runtime/service-token（仅本用户可读），供本地扩展读取。

    这不是“把密钥写在代码里”：文件在用户目录下、每次启动重新生成、且只用于本机回环通信。
    """
    try:
        p = _token_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
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
        # tidy 专用：开启删除（服务端会强制人确认，且执行时自动备份）
        allow_delete: bool = False
        require_confirm: bool = True
else:
    class CreateReq:                       # type: ignore
        pass


def build_app():
    if not _FASTAPI_OK:
        raise SystemExit("未找到 fastapi；请用带 fastapi/uvicorn 的解释器运行（本机 AutoClaw 内嵌 python 自带）")

    app = FastAPI(title="Local IDE Work-Order Service", version="0.3.1")

    # ------------------------------------------------------------------
    # 安全加固（2026-09-28 外部审查 #2 → 2026-09-30 补齐读口）
    # ------------------------------------------------------------------
    # CORS 收窄到 localhost / 127.0.0.1 / vscode-webview
    allow_origin_regex = os.environ.get(
        "LOCAL_IDE_CORS_REGEX", r"^(https?://(127\.0\.0\.1|localhost)(:\d+)?|vscode-webview://.*)$")
    app.add_middleware(CORSMiddleware, allow_origin_regex=allow_origin_regex,
                       allow_methods=["GET", "POST"], allow_headers=["*"],
                       allow_credentials=False)

    # 令牌：进程级（模块加载时已定），这里只负责落盘
    token = _TOKEN
    _write_token_file(token)
    policy = ConfirmPolicy.from_env()

    # 读口鉴权依赖项（除 /health、/presets 外都套）。
    # /health 与 /presets 仅返回状态与画像清单，不含用户内容；自检脚本
    # 需要它们零门槛。其余读口（列表、详情、SSE、暂存）同样会泄露本机路径与
    # 工单内文——所以 2026-09-30 补齐：不再口头豁免。
    READ_AUTH = require_auth(token, what="查询接口")

    def _write_auth(request: Request) -> Optional[JSONResponse]:
        """写操作鉴权同步检查（与 READ_AUTH 同口径，留着便于细粒度时点）。"""
        if not policy.require_token:
            return None
        auth = request.headers.get("authorization") or ""
        x = request.headers.get("x-local-ide-token") or ""
        got = extract_token(auth, x)
        if got != token:
            return JSONResponse(status_code=401,
                                content={"error": "需要令牌：请在请求头带上 "
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

    @app.get("/presets", dependencies=[READ_AUTH])
    def presets() -> Dict[str, Any]:
        return list_presets()

    @app.post("/wo", dependencies=[READ_AUTH])
    def create(req: CreateReq, request: Request):
        opts = RunnerOpts(**req.model_dump())
        if opts.kind == "code" and not (opts.workdir and opts.target):
            return JSONResponse(status_code=400,
                                content={"error": "code 工单需要 workdir 与 target"})
        if opts.kind == "tidy" and not opts.workdir:
            return JSONResponse(status_code=400, content={"error": "tidy 工单需要 workdir"})
        # ★ 服务端策略：策略要求确认时，客户端传 require_confirm=false 不生效
        opts.require_confirm = policy.effective_confirm(opts.require_confirm,
                                                        opts.allow_delete)
        # ★ 工作目录白名单（设了才生效）
        if not policy.workdir_allowed(opts.workdir):
            return JSONResponse(status_code=403,
                                content={"error": "该工作目录不在允许范围内（LOCAL_IDE_ALLOWED_ROOTS）",
                                         "allowed": policy.allowed_roots})
        r = REGISTRY.create(opts)
        return {"wo_id": r.wo_id, "status": r.status,
                "require_confirm": opts.require_confirm,
                "policy": policy.summary()}

    @app.get("/wo", dependencies=[READ_AUTH])
    def list_wo() -> Dict[str, Any]:
        items = [{"wo_id": r.wo_id, "status": r.status, "kind": r.opts.kind,
                  "task": r.opts.task[:80]} for r in REGISTRY.all()]
        return {"count": len(items), "items": items}

    @app.get("/wo/{wo_id}", dependencies=[READ_AUTH])
    def get_wo(wo_id: str):
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        return r.snapshot()

    @app.get("/wo/{wo_id}/staged", dependencies=[READ_AUTH])
    def staged(wo_id: str):
        """暂存内容 + diff（给 diff 视图用）。

        ★ 鉴权（2026-09-29 自查）：这个接口返回**文件内容与本地路径**，
          属于敏感读口，不能因为“是读接口”就豁免令牌。
        ★ 2026-09-30 改为不返回本地绝对路径，只回文件名。
        """
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        real = str(r.result.get("real_path") or "")
        return {"wo_id": wo_id, "content": r.result.get("staged_content", ""),
                "diff": r.result.get("diff", ""),
                "filename": os.path.basename(real) if real else "",
                "status": r.status}

    @app.post("/wo/{wo_id}/cancel", dependencies=[READ_AUTH])
    def cancel(wo_id: str):
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        return {"ok": r.cancel(), "status": r.status}

    @app.post("/wo/{wo_id}/confirm", dependencies=[READ_AUTH])
    def confirm(wo_id: str):
        r = REGISTRY.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        res = r.confirm()
        return JSONResponse(status_code=(200 if res.get("ok") else 409), content=res)

    @app.get("/wo/{wo_id}/events", dependencies=[READ_AUTH])
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
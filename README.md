# Local Model IDE · Phase 1

**A coding environment that keeps working with the network cable pulled.**

It is not a "Cursor-lite". Cursor assumes the model is smart enough and the framework should stay out of its way; here the assumption is the opposite —
**the model isn't smart enough, so the framework makes the calls.** In code that comes down to three things: a framework-driven work-order state machine,
deterministic gates (syntax / type / test), and a profile for each model.

---

## 30-second start

```powershell
# On this machine point at the embedded interpreter (it ships fastapi/uvicorn).
# (A bare `python` resolves to it here too, but being explicit is steadier and won't trip on another machine.)
$PY = "C:\Program Files\AutoClaw\resources\python\python.exe"

# 1) Start the gateway (the one exit for every model call: profiles + audit + outbound circuit breaker)
& $PY gateway/app.py

# 2) Start the work-order service (wraps the state machine as HTTP + SSE)
& $PY service/app.py
#    On startup it writes the access token to .runtime/service-token (the extension picks it up automatically)

# 3) Self-check: the whole offline suite (no model calls, no quota spent)
& $PY scripts/selftest.py --core

# 4) End-to-end acceptance: real HTTP + a real local model
& $PY scripts/e2e_check.py
```

> In PowerShell you need the call operator `&` to run a program held in a variable: `& $PY ...`.
> Without it, PowerShell treats `$PY` as a string (or reports "not recognized").

To use it in VS Code: copy or symlink `extension/` into `~/.vscode/extensions/local-model-ide`,
restart the editor → a **Local IDE** icon appears on the left.

### Secure defaults (in force with no configuration)

| Default | How to change it |
|---|---|
| Listens on `127.0.0.1` only | —— |
| **Writes require a token** (create / confirm / cancel) | `LOCAL_IDE_REQUIRE_TOKEN=0` to turn it off (not recommended) |
| **The server forces human confirmation** (a client passing `require_confirm:false` has no effect) | `LOCAL_IDE_REQUIRE_CONFIRM=0` lets the client decide |
| Working directory unrestricted | `LOCAL_IDE_ALLOWED_ROOTS=D:\proj;E:\other` to narrow it |
| Test subprocesses run in a clean environment (proxies and credentials dropped, HOME pointed at a temp dir) | `LOCAL_IDE_SANDBOX_STRICT=1` demands real isolation and refuses to run without it |
| CORS allows only localhost / vscode-webview | `LOCAL_IDE_CORS_REGEX=...` |

> ⚠️ **The "sandbox" is a mitigation, not isolation**: it stops accidents and careless moves, not deliberate ones (it doesn't block `socket()`, doesn't restrict file access).
> See `CODEX-HANDOFF.md` §4.3, item 1.

> Interpreter: on this machine use AutoClaw's embedded python (it ships fastapi/uvicorn):
> `"C:\Program Files\AutoClaw\resources\python\python.exe"`

---

## The four deliverables

| Piece | Directory | Responsibility | What it must not do |
|---|---|---|---|
| **A Local model gateway** | `gateway/` | OpenAI-compatible and Ollama-compatible dual entry; corrects requests per profile (e.g. strips a `think` the model doesn't support); writes an audit record on every call; outbound traffic limited to loopback/intranet | No routing experiments, no caching, no touching files |
| **B Work-order service** | `service/` | `POST /wo` creates an order and starts it running; SSE pushes events; **writing to disk requires human confirmation** | No business judgement (the criteria live in `core/`) |
| **C Context layer** | `context/` | AST chunking + reference retrieval + repo map + token-budget trimming | No whole-repo indexing, no model calls |
| **D VS Code extension** | `extension/` | Sidebar workbench + explain-on-selection + inline diff review | No model calls, no touching files (all of that goes to the service) |

`core/` is a **snapshot** of the local native framework (state machine + compatibility layer + tidy); see `core/VENDOR.md`.
Make your changes upstream, then sync with `& $PY scripts/sync_core.py`.

---

## Data flow

```
VS Code extension ──HTTP/SSE──▶ work-order service ──▶ work-order state machine (core/)
                                    │                       │
                                    │                       ├─ context layer: what gets packed into the prompt
                                    │                       └─ adapter layer: model profiles
                                    ▼
                          local model gateway ──▶ Ollama (127.0.0.1:11434)
                                    │
                                    └─ audit.jsonl (model / latency / profile / whether think was stripped, per call)
```

**Outbound boundary**: the gateway self-tests outbound connectivity at startup and refuses to boot if it reaches anything; at runtime it allows only loopback and intranet ranges.
Model calls, file reads and writes, and retrieval all stay on this machine.

---

## API

### Gateway (default `127.0.0.1:8080`)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Status + profile count + audit path + circuit-breaker self-test result |
| GET | `/v1/models` | Model list in OpenAI shape |
| POST | `/v1/chat/completions` | OpenAI shape (`stream` supported; `_task` can additionally pin a route) |
| POST | `/ollama/api/chat` | Ollama-shape forwarding (used by the state machine, **same profiles and audit**) |
| GET | `/audit?limit=N` | Most recent call audit records |

### Work-order service (default `127.0.0.1:8090`)

Write operations (create / confirm / cancel) need a token: `X-Local-Ide-Token: <token>`;
the token is in `.runtime/service-token` (generated at startup).

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Service, core version, current policy |
| GET | `/presets` | Available adapters / models (for the dropdown; includes the "tested unusable" list) |
| POST | `/wo` | Create: `{kind: code\|tidy, task, workdir, target, test_path, adapter, dirs, semantic, require_confirm}` (token required) |
| GET | `/wo` | List all work orders |
| GET | `/wo/{id}` | Snapshot of one work order (the top-level `status` is the runner state; the engine's verdict is in `run_status`) |
| GET | `/wo/{id}/events` | SSE event stream (`?once=1` returns a single snapshot; while a tidy order awaits confirmation it pushes the full move list) |
| GET | `/wo/{id}/staged` | Staged content + diff (for the diff view) |
| POST | `/wo/{id}/confirm` | **Writes to disk after human confirmation** (token required; rejected if the real file changed after the order started) |
| POST | `/wo/{id}/cancel` | Cancel (cooperative — takes effect between two nodes; token required) |

---

## Phase-1 acceptance (five items, all machine-checkable)

`python scripts/e2e_check.py` ticks each one off (most recent run: **16/16**):

1. **Works offline** — the whole chain connects only to 127.0.0.1 (gateway circuit-breaker self-test + no external addresses)
2. **Verifiable closed loop** — all five nodes run to completion, all three gates pass
3. **Nothing on disk before confirmation** — when an order reaches `awaiting_confirm` the target file is **byte-for-byte unchanged**; it changes only after confirmation
4. **Auditable** — every model call in the run is in `audit.jsonl` (with profile and whether think was stripped)
5. **Reversible** — if any gate fails nothing is written; `escalated` reports the exact failure location

Two more security items are verified end to end as well: **creating an order without a token is rejected (401)**, and **confirmation is rejected if the file changed mid-run (409)**.
See `docs/验收清单.md` (acceptance checklist).
Model benchmark logs across four rounds: `docs/模型测验全记录.md`.

---

## Switching models

Model profiles live in `core/compatibility/mythos_core/profiles.py`. Seven are in service:
`mythos` / `qwen-coder` / `ministral` / `gpt-oss` / `qwen3-14b` / `gemma4` / `qwen3-8b`.

```bash
& $PY -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8090/presets')),ensure_ascii=False,indent=2))"
```

Pick by measurement (data from this machine):

| Scenario | Suggestion | Measured |
|---|---|---|
| Interactive workhorse | `gemma4:e4b` | 24s, passes all four behaviour bits, has vision |
| Editing workhorse | `qwen2.5-coder:7b` | fastest at 3.3s; but picky about how a task is worded and prone to bad patches (the gate catches them) |
| Fallback | `mythos` | steadiest, 28s |
| Batch | `gpt-oss:20b` / `qwen3:14b` | 237s / 412s, not suited to interaction |

**Three to avoid** (they claim tool support but don't work in practice; already flagged in `/presets`):
`glm4:9b` (emits no tool calls), `deepseek-r1:7b` (falls back to bash suggestions), `llama3.1:8b` (emits them but picks the wrong tool).

---

## For whoever picks this up

`CODEX-HANDOFF.md` was written specifically for "the next person, using Codex, to make fixes":
what was done, what was not, where the known defects are, how to verify, and what not to touch.
`docs/验收清单.md` is the item-by-item acceptance criteria.
Four rounds of model benchmark notes (incl. the glm4 rehabilitation): `docs/模型测验全记录.md`.

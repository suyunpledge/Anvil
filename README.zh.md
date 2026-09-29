# Local Model IDE · 一期

**一个拔掉网线也照样干活的编码环境。**

它不是一个"弱化版 Cursor"。Cursor 的假设是模型足够聪明、框架别挡路；这里的假设相反——
**模型不够聪明，框架替它做判断**。落到代码上就是三件事：框架驱动的工单状态机、
确定性的 gate（语法 / 类型 / 测试）、以及每个模型各自一份画像。

---

## 30 秒上手

```powershell
# 本机请指向内嵌解释器（自带 fastapi/uvicorn）。
# （本机裸 `python` 也指向它，但写显式更稳、换机器也不会踩空）
$PY = "C:\Program Files\AutoClaw\resources\python\python.exe"

# 1) 起网关（所有模型调用的唯一出口：画像 + 审计 + 出网熔断）
& $PY gateway/app.py

# 2) 起工单服务（把状态机包成 HTTP + SSE）
& $PY service/app.py
#    启动后会把访问令牌写到 .runtime/service-token（扩展会自动读）

# 3) 自检：离线全套（不调模型、不花额度）
& $PY scripts/selftest.py --core

# 4) 端到端验收：真实 HTTP + 真实本地模型
& $PY scripts/e2e_check.py
```

> 注意 PowerShell 里执行变量指向的程序要加调用符 `&`：`& $PY ...`。
> 不加的话 PowerShell 会把 `$PY` 当成字符串处理（或报“无法识别”）。

VS Code 里用：把 `extension/` 复制或软链到 `~/.vscode/extensions/local-model-ide`，
重启编辑器 → 左侧出现 **Local IDE** 图标。

### 安全默认（不用配置就生效）

| 默认行为 | 怎么改 |
|---|---|
| 只监听 `127.0.0.1` | —— |
| **写操作要令牌**（建单/确认/取消） | `LOCAL_IDE_REQUIRE_TOKEN=0` 关闭（不推荐） |
| **服务端强制人确认**（客户端传 `require_confirm:false` 无效） | `LOCAL_IDE_REQUIRE_CONFIRM=0` 允许客户端决定 |
| 工作目录不限 | `LOCAL_IDE_ALLOWED_ROOTS=D:\proj;E:\other` 限定范围 |
| 测试子进程用干净环境（丢代理/凭据，HOME 指向临时目录） | `LOCAL_IDE_SANDBOX_STRICT=1` 要求真隔离，拿不到就拒跑 |
| CORS 只放行 localhost / vscode-webview | `LOCAL_IDE_CORS_REGEX=...` |

> ⚠️ **“沙箱”是缓解不是隔离**：它挡意外与顺手，不挡有意为之（不阻断 socket、不限文件访问）。
> 详见 `CODEX-HANDOFF.md` 第 4.3 节第 1 条。

> 解释器：本机请用 AutoClaw 内嵌 python（自带 fastapi/uvicorn）：
> `"C:\Program Files\AutoClaw\resources\python\python.exe"`

---

## 四个交付件

| 件 | 目录 | 职责 | 不该做的事 |
|---|---|---|---|
| **A 本地模型网关** | `gateway/` | OpenAI 兼容 + Ollama 兼容双入口；按画像修正请求（如自动摘掉模型不支持的 `think`）；每次调用写审计；出网只允许 loopback/内网 | 不做路由实验、不做缓存、不碰文件 |
| **B 工单服务** | `service/` | `POST /wo` 建单并开跑；SSE 推事件；**落盘必须经人确认** | 不含业务判断（判据在 `core/`） |
| **C 上下文层** | `context/` | AST 切块 + 引用检索 + repo map + token 预算裁剪 | 不做全仓索引、不调模型 |
| **D VS Code 扩展** | `extension/` | 侧栏工作台 + 选中即解释 + inline diff 审阅 | 不调模型、不碰文件（全交给服务） |

`core/` 是本地原生框架的**快照**（状态机 + 兼容层 + 整理），详见 `core/VENDOR.md`。
改动请回上游改，再用 `& $PY scripts/sync_core.py` 同步。

---

## 数据流

```
VS Code 扩展 ──HTTP/SSE──▶ 工单服务 ──▶ 工单状态机（core/）
                              │              │
                              │              ├─ 上下文层：往 prompt 里装什么
                              │              └─ 适配层：模型画像
                              ▼
                        本地模型网关 ──▶ Ollama（127.0.0.1:11434）
                              │
                              └─ audit.jsonl（每次调用的模型/耗时/画像/是否摘过 think）
```

**出网边界**：网关启动自检外连，命中即拒绝启动；运行期只放行 loopback 与内网段。
模型调用、文件读写、检索全部不出本机。

---

## 接口

### 网关（默认 `127.0.0.1:8080`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 状态 + 画像数 + 审计路径 + 熔断自检结果 |
| GET | `/v1/models` | OpenAI 形态模型列表 |
| POST | `/v1/chat/completions` | OpenAI 形态（支持 `stream`；额外支持 `_task` 指定路由） |
| POST | `/ollama/api/chat` | Ollama 形态转发（供状态机使用，**同一套画像与审计**） |
| GET | `/audit?limit=N` | 最近的调用审计 |

### 工单服务（默认 `127.0.0.1:8090`）

写操作（建单 / 确认 / 取消）需要令牌：`X-Local-Ide-Token: <token>`，
令牌见 `.runtime/service-token`（启动时生成）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务、核心版本、当前策略 |
| GET | `/presets` | 可用适配层 / 模型（供下拉框；含"实测不可用"清单） |
| POST | `/wo` | 建单：`{kind: code\|tidy, task, workdir, target, test_path, adapter, dirs, semantic, require_confirm}`（需令牌） |
| GET | `/wo` | 列出全部工单 |
| GET | `/wo/{id}` | 单张工单快照（顶层 `status` 是运行器状态；引擎判定在 `run_status`） |
| GET | `/wo/{id}/events` | SSE 事件流（`?once=1` 取一次快照；tidy 待确认时会推完整移动清单） |
| GET | `/wo/{id}/staged` | 暂存内容 + diff（给 diff 视图用） |
| POST | `/wo/{id}/confirm` | **人确认后落盘**（需令牌；若真实文件在工单开始后被改过则拒绝） |
| POST | `/wo/{id}/cancel` | 取消（协作式，两个节点之间生效；需令牌） |

---

## 一期验收（五条，都可程序化判定）

`python scripts/e2e_check.py` 会逐条打勾（最近一次：**16/16**）：

1. **离线可用** —— 全链只连 127.0.0.1（网关熔断自检 + 无外部地址）
2. **闭环可验** —— 五节点走完、三 gate 全过
3. **未确认不落盘** —— 工单到 `awaiting_confirm` 时目标文件**字节级不变**；确认后才变
4. **可审计** —— 本轮每次模型调用都在 `audit.jsonl`（含画像与是否摘过 think）
5. **可回退** —— 任一 gate 失败不落盘；`escalated` 给出精确失败位置

另外两条安全项也在端到端里验：**无令牌建单被拒（401）**、**运行期间文件被改则确认被拒（409）**。
详见 `docs/验收清单.md`。

---

## 换个模型

模型画像在 `core/compatibility/mythos_core/profiles.py`。现役 7 份：
`mythos` / `qwen-coder` / `ministral` / `gpt-oss` / `qwen3-14b` / `gemma4` / `qwen3-8b`。

```bash
& $PY -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8090/presets')),ensure_ascii=False,indent=2))"
```

按实测选（本机数据）：

| 场景 | 建议 | 实测 |
|---|---|---|
| 交互主力 | `gemma4:e4b` | 24s，四项行为位全过，带视觉 |
| 编辑主力 | `qwen2.5-coder:7b` | 3.3s 最快；但对任务措辞较挑，容易写出坏补丁（gate 会拦） |
| 兜底 | `mythos` | 最稳，28s |
| 批处理 | `gpt-oss:20b` / `qwen3:14b` | 237s / 412s，不适合交互 |

**别用的三个**（声明支持 tools 但实测不可用，已在 `/presets` 里标注）：
`glm4:9b`（不发工具调用）、`deepseek-r1:7b`（改用 bash 建议）、`llama3.1:8b`（会发但选错工具）。

---

## 给后面接手的人

`CODEX-HANDOFF.md` 是专门为"接下来用 Codex 做修正"写的：
做了什么、没做什么、已知缺陷在哪、怎么验、哪些地方别动。
`docs/验收清单.md` 是逐条验收口径。

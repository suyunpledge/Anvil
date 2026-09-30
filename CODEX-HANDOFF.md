# 交接说明 · Local Model IDE 一期

> 这份文件是给**接下来接手修正的人（或 Codex）**看的。
> 目标：让你在 15 分钟内知道**做了什么、没做什么、哪里可能有问题、怎么验证**，
> 不用先把整个仓库读一遍。

日期：2026-09-28　状态：一期功能已跑通，四套单测 + 端到端验收全绿

---

## 一、先跑这三条，确认环境是好的

```bash
PY="C:\Program Files\AutoClaw\resources\python\python.exe"   # 本机必须用内嵌 python

$PY scripts/selftest.py        # 四套单测（不调模型，约 5 秒）
$PY scripts/e2e_check.py       # 端到端（会起两个服务 + 调一次本地模型，约 1 分钟）
node extension/tests/check.js  # 扩展静态检查（约 1 秒）
```

三条都绿 = 你现在看到的是我交出来的状态。任何一条红，先修环境再动代码。

---

## 二、这一期做了什么

| 交付件 | 位置 | 关键点 |
|---|---|---|
| A 本地模型网关 | `gateway/app.py`（约 330 行） | 双入口（OpenAI `/v1/chat/completions` + Ollama `/ollama/api/chat`）；**按画像摘掉模型不支持的 `think`**；每次调用写 `audit.jsonl`；出网只允许 loopback/内网 |
| A' 网关客户端 | `gateway/client.py` | 让状态机走网关而不是直连 Ollama（否则画像+审计被绕过） |
| B 工单服务 | `service/app.py` + `service/runner.py` | HTTP + SSE；**落盘必须经人确认**；协作式取消 |
| C 上下文层 | `context/builder.py` | AST 切块 / repo map / 引用检索 / **token 预算裁剪**（优先级明确） |
| D VS Code 扩展 | `extension/extension.js`（纯 JS，无构建） | 侧栏工作台、选中即解释、diff 审阅；不调模型、不碰文件 |
| 内嵌核心 | `core/` | 上游框架快照（状态机 + 兼容层 + 整理），见 `core/VENDOR.md` |

**设计上最要紧的一条**：模型输出永远不直接改文件。
code 工单走「暂存区 → 三个 gate → 人确认 → 原子替换」，tidy 工单走「方案闸 → 人确认 → 零覆盖执行 → 复核闸」。

---

## 三、这一期**没有**做（不要以为漏了）

这些是有意不做的，写在立项方案第 2 节：

- 自研编辑器壳 / fork VS Code
- 全仓代码索引、跨文件重构
- **行内补全（FIM）**：需要另一条低延迟路径与独立验收，会稀释焦点
- 多文件工单、模型编队、基准数字
- 联网插件市场 / 遥测 / 自动更新（与"零外连"直接冲突）
- 与云端模型的任何默认联动

如果你要加这些，建议先读 `docs/framework/` 下的两份契约——它们定义了工单与 gate 的语义，
绕过契约加功能很容易把"可验收"这条丢掉。

---

## 四、已知缺陷与可疑处（**优先看这一节**）

### 4.1 外部审查（2026-09-28）指出的 7 条 —— 已全部修复

（另有 2026-09-29 自查审查发现 2 处补充问题已修：/staged 鉴权与路径收敛、免确认路径落盘终检，见 `docs/审查报告-2026-09-29.md`。）

审查结论很准，逐条核实后都属实。修复方式与回归测试如下：

| # | 问题（严重度） | 修复 | 回归测试 |
|---|---|---|---|
| 1 | **“沙箱”并非真沙箱（高）**：测试直接以当前用户权限执行模型改过的 Python，继承全部环境变量，能访问网络与任意文件 | 新增 `core/statemachine/sandbox.py`：子进程不再继承 `os.environ`，改为干净环境（丢代理与凭据类变量、HOME 指向临时目录、禁写字节码）；**并在文档与自检里明确写“这是缓解不是沙箱”**，探测不到 OS 级隔离时 `strict` 模式宁可拒跑 | `service/tests/test_security_fixes.py` 9 条（含“不得假装有沙箱”） |
| 2 | **本地 HTTP 服务可被绕过人工确认（高）**：CORS `*` + 无认证 + `require_confirm` 由客户端定 | ① CORS 收窄到 localhost/127.0.0.1/vscode-webview；② 写操作要令牌（启动生成、写 `.runtime/service-token`、扩展自动读取）；③ `require_confirm` 改由**服务端策略**决定（`LOCAL_IDE_REQUIRE_CONFIRM=1` 时客户端传 false 无效）；④ 可选工作目录白名单 | 6 条（策略/白名单/令牌文件） + 端到端“无令牌建单被拒 401” |
| 3 | **运行期间改文件会被静默覆盖**：真实文件哈希在模型跑完后才记录，则运行期间的人工编辑会被当成基线覆盖 | 版本基线提到**工单开始前**记（`initial_version`），并在 `_stage` 与 `confirm` 两处比对；不符直接拒绝（`failed` / 409） | 3 条离线 + 端到端“运行期间被改 → 确认被拒” |
| 4 | **流式接口绕过出网熔断**：非流式调了 `assert_url_allowed()`，流式直连 `OLLAMA`；异常时审计固定写 200 | 抽出 `post_ollama_stream()`，与非流式**共用同一道出网门**；审计改为如实记真实状态（含 403/502/499 与 error） | 2 条（把 `OLLAMA` 指到外网必须抛 `OutboundBlocked`） |
| 5 | **tidy 确认界面看不到移动方案**：`this.state.moves = this.state.moves` 是无效赋值，服务只发了数量 | 服务改为下发完整清单（`plan`/`moves` 带 src/dst/reason）；扩展收下并渲染 | 端到端可见；扩展静态检查 |
| 6 | **上下文层未接入实际链路**：实现了但没人调用，仍是全文进 prompt | `WorkOrderStateMachine` 新增 `context_provider` 注入点；服务侧接上，并**优先读暂存区那份**（与将要改的文件一致） | 3 条（含“读暂存区而非真实目录”与“故障回退”） |
| 7 | **工作目录=项目根时可能递归复制**：暂存区在 `.runtime/staging`，`copytree` 又没忽略 `.runtime` | `_stage` 检测“暂存区与工作目录嵌套”，命中就把暂存区改到系统临时目录；忽略表补 `.runtime`/`node_modules`/`.venv` | 1 条（断言暂存区已移出、且副本里无 `.runtime`） |

### 4.2 修复过程中自己暴露并修掉的一个设计缺陷（值得记）

把上下文层接进链路后，**同一道题的端到端测试反而失败了**。查下来是真问题：

> 裁剪后的上下文 **221 token**，而整个文件才 **106 token**。小文件上“裁剪”是净负担，
> 而且模型看不到完整文件布局，把函数删错位了。

现在的口径改成：**只有真省才用**——裁剪结果不小于全文的 90% 时，老老实实给全文，
并把回退原因记进 `artifacts.context.skipped`。回归测试：`test_long_context_falls_back_to_full_text`。

教训：优化项上线前要在**它不该生效的场景**也测一遍；否则优化会变成退化。

### 4.3 仍然存在的（**留给你判断**）

1. **"沙箱"只是缓解，不是隔离（最重要的一条）。**
   `sandbox.py` 做了三件事：清环境变量（丢代理与凭据类）、掐断代理、把 HOME 指向临时目录。
   它**不阻止 `socket()` 直连、不限制文件访问、不限 CPU/内存**。
   真隔离需要 OS 级手段（Windows：Job Object + 受限令牌；Linux：bwrap/unshare），本机没有现成能力，
   所以 `real_isolation_available()` 会如实返回 `False`；`LOCAL_IDE_SANDBOX_STRICT=1` 时宁可拒跑也不假装安全。
   **如果要真正硬它，这是第一优先级。**

2. **取消是协作式的，粒度是"两个节点之间"。**
   `CancellableAdapter` 只在每次 `fill_slot`/`ask` 前检查标记。模型正在生成时不会立刻断。
   要"立刻断"需要把 `AbortSignal` 透到 HTTP 层——属于改动核心。

3. **`qwen2.5-coder:7b` 在"替换函数体"这类任务上很容易写出坏补丁。**
   实测：它不含 thinking、不会算行号，曾把整段缩进写错（语法 gate 连拦 3 轮 → `escalated`）。
   同样是模型能力问题，不是框架 bug。缓解方向：让框架提供行号锚点，而不是靠提示词。

4. **正文 JSON 修复层有一段无法完全归因的历史记录。**
   曾看到三引号变两个引号加一单引号。已加不变量测试
   （`core/compatibility/test_qwen_coder_adapter.py::test_repair_never_invents_characters`），
   该测试**通过**，说明修复层不会引入原输入之外的字符。**保持那条测试。**

5. **本轮（0930）新增：读口也必须鉴权。**
   之前只鉴权写口，读口（/wo 列表、/wo/{id}、/events、/staged、/presets）放空。
   2026-09-30 全数补齐：除 /health 外所有路由都走 `require_auth(token, what="查询接口")`。
   /staged 同时不再回绝对路径，只回 filename，避免路径泄漏。
   回归：service/tests/test_read_auth_2026_09_30.py（12 例）。**别再让"读口不鉴权"复活。**

6. **本轮（0930）新增：进程级令牌。**
   build_app 内部原本每次调用都生成新 token 并覆盖 .runtime/service-token。
   uvicorn 调度下被调两次 → 两次令牌都不同 → 先读到旧令牌的一方全部 401。
   修法：令牌提到模块级 `_TOKEN = _resolve_token()`，build_app 内只读不写。
   **环境变量错位**（顺手修了）：auth.env_token 之前只认 LOCAL_LLM_TOKEN，但 e2e 传的是
   LOCAL_IDE_TOKEN；现在两个都认，LOCAL_IDE_TOKEN 优先。

7. **本轮（0930）新增：cancel 一个停在 needs_input / awaiting_confirm / escalated 的单必须真的转 cancelled。**
   之前 cancel 只设标志，线程已退出时状态纹丝不动——用户点"取消"看不到任何反馈。
   修法：runner.cancel() 增加终态分支，STATUS_NEEDS_INPUT_LIKE 在线程已退出时直接改判 cancelled。
   回归：cancel_concurrency.py 用例 1 由 FAIL 变 PASS。

8. **本轮（0930）新增：首批基准数字（micro_bench.py）。**
   同一道题（实现 average(nums)）在三个模型上各跑一次：
     · gemma4:e4b            PASS  29.4s  gates o/o/o  ← 当前编辑主力的最稳选择
     · qwen2.5-coder:7b      FAIL  15.5s  needs_input（补丁写成了正文文本）
     · mythos-v2-8b:q4_k_m   FAIL   0.6s  疑瞬态（显存切换/模型名解析）；单独复测 needs_input 正常
   **n=1 不作结论。**正式基准需 20 题 × best-of-5（带重试与方差统计），写到：
     scripts/micro_bench.py  ← 复用本次结构
     micro_bench_result.json ← 已含 notes 段
   建议从 gemma4:e4b 开始做编辑主力，再扩到 mythos（需补 best-of-5 与 needs_input 的自动回复路径）。

9. **本轮（0930）踩坑（务必读到）：.gitignore 行内中文注释会让规则被解析跳过。**
   `core/compatibility/node_profiles.json  # 运行时累加；脚本 pack.py --strict 不算入漂移`
   这条规则**不会被 Git 识别**。把注释挪到独立一行就生效。
   **规律**：行内 `规则 # 注释` 的写法在 UTF-8 中文 + LF 文件里不可靠，要么注释独立成行，要么干脆别写注释。

5. **`semantic_tidy` 的阈值（0.62）是按 bge-m3 定的。** 换嵌入模型必须重标定。

6. **`context/builder.py` 的符号猜测是启发式的**（先从任务描述找标识符，否则取第一个未实现函数）。
   猜错时有兜底（整文件）不会崩，但会浪费预算；且**小文件上会回退全文**（见 4.2）。

7. **网关的 `ROUTES` 是硬编码的一期初版。** 等基准数字出来再按数据改。

8. **没有并发压测，也没有工单数量上限。** `RunnerRegistry` 是内存字典 + 锁，单机少量够用。

9. **扩展未在真实 VS Code 里跑过（仍是首要待补）。**
   静态检查 35 项全过，**且本轮它又抓到两个真问题**（模板里未定义变量、配置键未声明）。
   但装机验证仍缺，这是接手第一件事。

10. **鉴权只到"令牌"这一层。** 令牌文件默认在项目 `.runtime/` 下；
    如果本机已有恶意进程能读该文件，它就能冒充扩展。
    更强做法：文件权限/命名管道传递，或改用 VS Code 自带的 secret storage。

---

## 4.4 本轮（0930）自查新增——读口鉴权 / 进程级令牌 / cancel 语义 / 首批基准

5. **读口也必须鉴权（不要让"读口不鉴权"复活）。**
   之前只鉴权写口，读口（/wo 列表、/wo/{id}、/events、/staged、/presets）放空。
   2026-09-30 全数补齐：除 /health 外所有路由都走 `require_auth(token, what="查询接口")`。
   /staged 同时不再回绝对路径，只回 filename，避免路径泄漏。
   回归：`service/tests/test_read_auth_2026_09_30.py`（12 例）。

6. **进程级令牌（build_app 不能多次写令牌）。**
   原实现每次 build_app 都重新生成 token 并写 .runtime/service-token。uvicorn 调度下
   build_app 被调两次 → 两次令牌都不同 → 先读到旧令牌的一方全部 401（实测 e2e 与
   cancel_concurrency 全军覆没就是这个原因）。
   修法：令牌提到模块级 `_TOKEN = _resolve_token()`，build_app 内只读不写。
   **附带修了环境变量错位**：auth.env_token 之前只认 LOCAL_LLM_TOKEN，但 e2e 传的是
   LOCAL_IDE_TOKEN；现在两个都认，LOCAL_IDE_TOKEN 优先。

7. **cancel 一个停在 needs_input / awaiting_confirm / escalated 的单必须真的转 cancelled。**
   之前 cancel 只设标志，线程已退出时状态纹丝不动——用户点"取消"看不到任何反馈。
   修法：runner.cancel() 增加终态分支，STATUS_NEEDS_INPUT_LIKE 在线程已退出时直接改判
   cancelled。回归：cancel_concurrency.py 用例 1 由 FAIL 变 PASS。

8. **首批基准数字（scripts/micro_bench.py）。**
   同一道题（实现 average(nums)）在三个模型上各跑一次：
     · gemma4:e4b            PASS  29.4s  gates o/o/o  ← 当前编辑主力的最稳选择
     · qwen2.5-coder:7b      FAIL  15.5s  needs_input（补丁写成了正文文本）
     · mythos-v2-8b:q4_k_m   FAIL   0.6s  疑瞬态（显存切换/模型名解析）；单独复测 needs_input 正常
   **n=1 不作结论。**正式基准需 20 题 × best-of-5（带重试与方差统计）。
   `micro_bench_result.json` 已含 notes 段。建议从 gemma4:e4b 开始做编辑主力，
   再扩到 mythos（需补 best-of-5 与 needs_input 的自动回复路径）。

9. **本轮（0930）踩坑（务必读到）：.gitignore 行内中文注释会让规则被解析跳过。**
   写法 `core/compatibility/node_profiles.json  # 运行时累加` 这条规则不会被 Git 识别。
   把注释挪到独立一行就生效。**规律**：行内 `规则 # 注释` 的写法在 UTF-8 中文 + LF 文件
   里不可靠，要么注释独立成行，要么干脆别写注释。本轮 node_profiles.json 误提交是这条坑的
   副产品——已经 `git rm --cached` 剔除，工作目录里的副本保留供运行时写。
   提交前再 `git grep node_profiles` 确认仓库干净。

五、验证方式（改完代码请跑这几条）

```bash
# 1) 单元层（不调模型，约 10 秒）
$PY scripts/selftest.py --core
#   = 网关 18 + 上下文 18 + 服务 17 + 安全回归 24 + 扩展静态 35 + 核心 168

# 2) 端到端（真实 HTTP + 真实本地模型，约 3-5 分钟）
$PY scripts/e2e_check.py
#   期望 16/16；其中"未确认不落盘"与"运行期间被改则拒绝"都是字节级比对
```

对照基线（2026-09-28 晚，加固后）：

| 层 | 项数 | 结果 |
|---|---|---|
| 网关单测 | 18 | OK |
| 上下文层单测 | 18 | OK |
| 工单服务单测 | 17 | OK |
| **安全回归（审查 7 条）** | **24** | **OK** |
| 扩展静态检查 | 35 | 全部通过 |
| 核心回归 | 168 | OK |
| 端到端 | **16** | **16/16** |

---

## 六、改动的边界（重要）

- **`core/` 是快照，不要在这里改业务逻辑。** 改了下次同步会被覆盖。
  正确做法：改上游 `local-model-framework/`，再 `python scripts/sync_core.py`。
- **`core/statemachine/` 与 `core/compatibility/` 里的模块靠"同级目录"互相 import。**
  新增入口文件时记得先调 `core.paths.ensure_core_on_path()`。
- **不要把判断逻辑放进扩展。** 扩展只做"发工单 / 看事件 / 看 diff"。
  一旦扩展开始自己判断"改得对不对"，你就把 gate 这个唯一真相源拆了。
- **不要在网关里加"顺手的功能"**（缓存、重试策略、路由实验）。
  它现在的价值是"唯一出口 + 可审计"，加东西会稀释这条。

---

## 七、如果要继续做（建议顺序）

1. **在真实 VS Code 里跑一遍扩展**（唯一没验过的部分），修掉装上去才会暴露的问题。
2. **把基准数字跑出来**：同一批任务，单次通过率 vs best-of-5。定义已清楚：
   通过 = 三 gate 全过且状态 `done`。有了它才能按数据改 `ROUTES` 与 `max_repair_rounds`。
3. **行内补全**（独立一条链路：FIM + 300ms 预算 + 自己的验收）。
4. **多文件工单**：先做"目标文件 + 一层依赖"，别一上来做全仓。
5. **取消做到可立刻中断**（需要 AbortSignal 透传，改 core）。

---

## 八、环境约束（别踩）

- 解释器：本机必须用 `C:\Program Files\AutoClaw\resources\python\python.exe`（自带 fastapi/uvicorn）。
- 它是**隔离模式**解释器：脚本目录与 `PYTHONPATH` 都不进 `sys.path`，靠 `ensure_core_on_path()` 与显式 insert。
- Ollama 需在 `127.0.0.1:11434` 运行，且至少装了 `qwen2.5-coder:7b`（端到端用）与 `mythos`（兜底）。
- 网络按流量计费：不要去 npm install（扩展是纯 JS 无构建，就是为了避开这个）。
- 端口：网关 8080、服务 8090（端到端脚本用 8098/8099 以免撞你正在跑的服务）。

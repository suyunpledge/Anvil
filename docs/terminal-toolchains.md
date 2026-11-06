# 终端与编译运行优化 · 2026-11-04

本轮只修改主进程、工具链模块、测试，以及共享类型中的 Java 能力声明。renderer 与 preload 未修改，继续由 Claude 维护。没有修改系统 PATH、安装新编译器或重启正在使用的 IDE。

## 已实现

| 语言 | 运行行为 |
|---|---|
| Python | 优先使用最近项目的 `.venv/venv`，不穿越 Git / Python 项目边界借用其他项目环境。不可用的项目环境明确报错，不静默切换系统 Python。`-u -X utf8` 保证无缓冲 UTF-8 输入输出；保留异常堆栈并附加可点击定位行。 |
| Java | 探测完整 JDK，保证 `javac` 和 `java` 位于同一安装目录；支持 `JAVA_HOME`、PATH、常见 JDK 安装目录和 IDE 自带 JBR。UTF-8 编译，支持标准包目录和同包源码依赖，产物放入独立目录；编译错误转为现有前端认识的 `file:line:column` 格式。 |
| C | 默认 C17、调试信息、`-Wall -Wextra`，关闭诊断颜色并显式指定 UTF-8 源码/执行字符集。成功时仍保留警告。 |
| C++ | 默认 C++17，支持结构化绑定等语法，其余诊断和生命周期与 C 一致。 |

C/C++ 集成运行路径会链接一个独立的标准输出初始化单元，使 `printf` 提示及时传到管道，避免 `scanf` 已经等输入而提示仍在缓冲区。用户源码不变。这是交互程序的配置，不能据此宣称大批量输出吞吐提升；直接在终端手工调用编译器时不会自动链接该单元。

## 运行时结构

- `src/main/toolchains.ts`：探测、项目解释器选择、配套 JDK、子进程环境。探测并发合并并缓存 30 秒，刷新仍可强制执行；失败的 PATH 候选不会挡住后续有效候选。
- `src/main/runner.ts`：准备、编译、执行、输入、取消、退出和清理。源文件检查、独立产物目录和会话 ID 在运行前确定；输出延后到 IPC 返回会话 ID 后开始产生。
- `src/main/output-buffer.ts`：最多等待 16 ms 合并连续输出，单次文本不超过 32768 个 UTF-16 单元，避免拆开 emoji。保留 stdout/stderr 顺序并在退出前刷新。
- `src/main/term-host.ts`：终端继承探测到的 Python、GCC/G++、JDK 路径和项目虚拟环境，只影响本次终端子进程。`cmd /d /q` 禁用 AutoRun 与重复命令回显，使用 UTF-8 代码页。

编译默认最多 60 秒，可通过启动 IDE 时的 `ANVIL_COMPILE_TIMEOUT_MS` 调整，最大 600 秒。超时会停止编译进程树并报告失败，不继续执行。正常运行不自动设置时限，仍可停止。每个编译会话使用 `.anvilide/run-*` 独立目录，退出后按验证过的目录边界清理；同一源文件并发运行不共用可执行文件。

## 已验证

```powershell
node --test --test-timeout=30000 tests/main-runtime.test.cjs
node --test --test-timeout=90000 tests/toolchain-live.test.cjs
npm run typecheck
```

- 既有主进程回归：10/10 通过。
- 新增真实工具链及输出测试：10/10 通过，未跳过四种语言。包括中文和空格路径、提示到达后才提供输入、项目真实 venv、Java 包与同包依赖、C 警告、编译失败不执行、Python 异常定位、并发产物隔离与清理、Java 诊断、真实编译超时、终端 PATH 和输出合并。
- 本机使用 GCC/G++ 16.2.0、JDK 25.0.3（已有 PyCharm JBR）及已安装 Python。测试只在临时目录生成程序和虚拟环境。
- 主进程与 renderer 的 TypeScript 类型检查均通过；类型检查未修改界面源码。

## 给前端维护者的接入说明

已有 `run:start` / `window.ide.startRun(file)` 接口现已支持 `.java`，返回的 `compiler.kind` 为 `java`，`compiler.path` 是 `javac`，新增可选 `runtimePath` 是配套 `java`。原有字段与 IPC 名称保留。

当前 renderer 的 `runpanel.ts` 仍有自己的扩展名白名单，**尚未允许 `.java` 点击运行**；此处按用户要求留给 Claude 修改。Java 已能在内置终端直接使用 `javac` / `java`。建议前端接入时一并完成：

1. 运行扩展名白名单加入 `java`，工具链状态和缺失提示加入 Java。
2. `startRun` 返回 `compiler=null` 时显示 `reason`，例如失效虚拟环境、文件不存在或不支持的格式。
3. 允许发送空输入行：主进程已支持空字符串，现有输入框仍会拦截空行。

终端当前仍为 cmd 管道加前端行缓冲，并非 ConPTY。前端 Ctrl+C 仍只清空输入行，全屏终端程序和原生 Ctrl+C 信号不属于本轮已完成能力。C/C++ 集成编译仍以当前文件为入口；CMake、Make、Maven、Gradle 构建任务和任意多文件链接尚未接入。

主进程源码需在下一次 IDE 构建/开发启动后生效。此次未执行会覆盖前端产物的完整构建。

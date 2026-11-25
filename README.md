# ⚒ 铁砧 IDE（Anvil IDE）

**好上手的本地代码编辑器**：VSCode 官方开源编辑器内核（Monaco）+ 全中文自研界面 + Anvil 本地模型编码代理。

不使用 VSCode 自家工作台界面——从标题栏到状态栏全部自研，为「第一次写代码的人」设计。

## Windows 安装

双击 `AnvilIDE-Setup-<版本>-win-x64.exe` 即可安装到当前用户。右键代码文件，选择「打开方式 → Anvil IDE」。已打开的窗口会在新标签页接收文件。要设为默认编辑器，在 Windows 选择器中选择「始终」。

安装包内含 Electron、Node.js、tsx 和 TypeScript。C/C++、Python、Java、Kotlin 和 AI 聊天使用本机的相应工具链或 Ollama 模型。在 Windows x64 构建机上运行 `npm ci` 和 `npm run installer`，可在 `dist` 目录生成安装包。

## 为什么好上手

| 痛点 | 铁砧 IDE 的答案 |
|---|---|
| 英文界面看不懂 | 全中文界面，零配置 |
| 找不到运行按钮 | 工具栏正中 **▶ 运行** 大按钮，或直接按 **F5** |
| 导入程序太麻烦 | **把文件或文件夹直接拖进窗口**即可（或「打开文件」），导入即可运行 |
| 没装编译器跑不了 | 启动时自动探测 GCC / Python / Node（含 `C:\Users\Public\mingw64` 常见安装位） |
| 编译报错看不懂 | 错误行变成**可点击链接**，点了直接跳到出错的那一行 |
| 程序要输入没地方输 | 输出面板自带输入框，scanf / input 直接用 |
| 想敲命令行 | 自带**终端面板**（Ctrl+` 或底部「终端」Tab），gcc / dir / a.exe 直接敲 |

支持的文件类型：**C / C++（GCC）· Python · JavaScript**，编辑另支持常见语言高亮。

## 内置 Anvil 本地助手

左侧 AI 聊天面板使用本机 Ollama。选择模型后可以切换「代码」和「Work」两种模式。

- **代码** 选择一个 UTF-8 代码文件，描述需要查询或修改的位置。助手可以读取、搜索并提出替换；预览会显示文件名、行号、原文和新文本。点击「应用修改」写入，可撤销。
- **Work** 点击「授权目录」后可以跨子文件夹按名称、扩展名或类别查找和整理。试试“找出采购目录及其子目录中的 PDF”或“把采购目录里的 PDF 移到「归档/采购」”。预览逐项显示相对于授权根目录的来源和目标；确认整批移动后，点一次「撤销上次整理」即可恢复原位置。
- **恢复文件** 在同一个目录点「撤销上次整理」。重新打开 IDE 后可重新授权目录继续撤销。若同名文件已存在、文件内容已变化或编辑器正在打开该文件，先处理提示中的冲突。整理中断后若仍显示旧预览，点击「放弃」关闭预览，再点击「撤销上次整理」。

Work 在授权目录树内扫描最多 40000 个条目。每次查找显示匹配总数和前 120 条路径，每批最多移动 120 个普通文件。目标可以是已有或新建的多级子目录；预览会标出磁盘已有的同名文件，以及批内重复的目标。目标原名可用时，批内第一项默认保留原名；其余冲突项默认跳过。在预览的「应用到所有同名冲突」中选择「全部跳过」或「全部自动重命名」，也可以再单独调整某一项。无冲突文件仍按原计划移动。预览会显示实际编号，例如 `合同 (2).pdf`。确认后继续移动其他文件，一次撤销会恢复本批实际移动的全部文件。撤销会清理本批新建且仍为空的目录。目录需要支持硬链接（例如 NTFS），支持二进制文件。切换模式或解除授权会清除当前会话的文件权限。

## 开发与构建

```powershell
npm install        # 首次
npm run dev        # 开发模式（热更新）
npm run build      # 产出 out/（主进程 + 预加载 + 渲染层）
npm run typecheck  # TS 类型检查
npm run bench:work -- glm4:9b   # 用临时目录实测整理与撤销
npm run bench:chat -- glm4:9b   # 代码工具实测
```

技术栈：Electron 33 · electron-vite 2 · TypeScript 5 · Monaco Editor 0.52（VSCode 开源编辑器内核）· xterm.js 5。

## 目录结构

```
src/
  main/            # Electron 主进程
    index.ts       #   窗口与 IPC 注册
    term-host.ts   #   终端会话宿主（cmd.exe 管道）
    fs-service.ts  #   文件树 / 读写（GBK 兜底解码）
    runner.ts      #   编译器探测 / 一键编译运行 / 流式输出
    anvil-host.ts  #   Anvil 后端宿主（健康检查/拉起/API 代理/SSE 转发）
    settings.ts    #   设置持久化（最近文件夹等）
  preload/         # 上下文隔离桥（window.ide）
  renderer/        # 自研界面（非 VSCode 工作台）
    index.html     #   布局骨架
    src/ui.css     #   全部样式
    src/editor.ts  #   Monaco + 多标签 + 脏标记
    src/explorer.ts#   文件树
    src/runpanel.ts#   运行按钮 + 输出面板 + 报错跳行
    src/terminal.ts # 自带终端（cmd 会话 / xterm 渲染 / 历史）
    src/anvilpanel.ts # 本地聊天与 Work 文件整理
  shared/types.ts  # IPC 契约类型
```

## 安全与边界

- 渲染层 `contextIsolation` 开启、`nodeIntegration` 关闭，全部系统能力走 preload 白名单桥
- 外部链接交给系统浏览器打开；编辑器内容不外发
- Anvil 后端默认 `127.0.0.1:8099`，令牌从 `.runtime/service-token` 读取，仅在本地进程间使用
- 运行产物（.exe）放在源码旁的 `.anvilide/` 隐藏目录，不污染你的文件夹

## 路线图

- [ ] xterm 终端面板（交互式程序更完整的 VT 支持）
- [ ] clangd 语言服务（跳转定义 / 悬停提示）
- [ ] 断点调试（GDB/MI）
- [ ] 打包便携版 exe（electron-builder）
- [ ] Anvil 视觉工单（visual_tasks）

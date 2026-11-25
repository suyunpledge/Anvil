# ⚒ 铁砧 IDE（Anvil IDE）

**好上手的本地代码编辑器**：VSCode 官方开源编辑器内核（Monaco）+ 全中文自研界面 + Anvil 本地模型编码代理。

不使用 VSCode 自家工作台界面——从标题栏到状态栏全部自研，为「第一次写代码的人」设计。

## Windows 安装

双击 `AnvilIDE-Setup-0.1.0-win-x64.exe` 即可安装到当前用户。右键代码文件，选择「打开方式 → Anvil IDE」。已打开的窗口会在新标签页接收文件。要设为默认编辑器，在 Windows 选择器中选择「始终」。

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

左侧「Anvil」面板接入 [Anvil](../Anvil) 本地模型编码代理（无需联网、无需 API Key）：

- 写一句任务描述 → 创建工单，本地模型（Ollama：qwen3.5 代码 / qwen2.5vl 视觉 / bge-m3 记忆）自动侦察、修改、过语法/类型/测试三道关卡
- 单文件也能用：没开文件夹时，工作目录自动取目标文件所在目录
- 全程可见：每一步工具调用、模型调用、关卡结果实时滚动显示
- **改动不会直接写盘**：完成后展示 diff，你点「✔ 确认落盘」才生效
- 每个项目的成功经验进入分级记忆（HR），下次同类任务自动带上

Anvil 服务无需手动启动：面板检测到未运行会提供一键拉起（后台自动 `python anvil.py`，最长等 90 秒冷启动）。

## 开发与构建

```powershell
npm install        # 首次
npm run dev        # 开发模式（热更新）
npm run build      # 产出 out/（主进程 + 预加载 + 渲染层）
npm run typecheck  # TS 类型检查
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
    src/anvilpanel.ts # Anvil 工单面板
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

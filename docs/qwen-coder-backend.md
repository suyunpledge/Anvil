# Qwen Coder 后端接入

后端默认模型为 `qwen2.5-coder:7b`，画像 `qwen-coder`。`/presets.default` 与 HTTP 建单默认值统一，现有主进程会把默认项排在首位。

本轮主进程利用 `/integrations.available_models` 和 `ollama` 标记缺失模型为 `bad`；不修改 renderer、preload 或共享接口。主进程已单独构建到 `out/main`，打开中的 IDE 下次重启加载。主进程类型检查、11 项运行时测试及 1 项真实后端契约测试通过。

前端交接给 Claude：`src/renderer/src/anvilpanel.ts:166` 的状态文案仍写死 Qwen3.5 和全部能力就绪，应读取 `integrations.models` 的 name/ready，结合 `code_ready` 分别展示。模型下拉框已经使用动态列表，但 bad 目前只追加“不可用”文字，可进一步禁用对应项。此处前端源码和构建产物均未改动。

后端完整验收记录在 Anvil 仓库 `docs/Qwen-Coder适配验收-2026-11-11.md`。两项真实 Python 修改任务通过语法、类型、测试和确认落盘，热态各约 2.6 秒；未覆盖大型项目或其他语言的模型改码质量。

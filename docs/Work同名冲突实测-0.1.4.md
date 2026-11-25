# Work 同名冲突实测 · 0.1.4

2026-11-25，Windows x64，本机 Ollama。每个模型在同一组临时目录上运行一轮。模型提出整理清单后，测试模拟用户在预览中选择冲突处理方式，并比对文件内容和最终目录树。

| 模型标签 | 同名处理 | 其他 Work 场景 | 打包后的窗口 |
|---|---:|---:|---|
| `anvil-qwen3.5:9b` | 4/4 | 13/13 | 通过 |
| `fableforge-ai/mythos-v2-8b:q4_k_m` | 4/4 | 13/13 | 通过 |
| `fableforge-ai/mythos-v2-8b:latest`（Q8） | 4/4 | 13/13 | 通过 |

四项同名测试分别覆盖批内同名、跳过所有重名项、自动重命名所有重名项，以及同批混用两种选择。目标已存在 `合同.pdf` 和 `合同 (1).pdf` 时，两份重名来件可分别移动为 `合同 (2).pdf` 和 `合同 (3).pdf`。其他文件照常移动；一次撤销恢复文件原名、内容和位置。

在打包后的聊天窗口，三个模型分别完成了一次混合批量移动。测试在确认前模拟外部文件占用了预览中的编号，核对首次点击仅刷新预览，第二次确认才写盘。随后又确认了一个全部跳过的批次，再点一次撤销，验证上次实际移动仍可完整恢复，外部文件和原有同名文件均保留。

TypeScript 类型检查和 39 项自动回归通过。安装包内的应用归档与窗口测试的归档哈希一致。

在项目目录执行以下命令可复测。窗口测试使用独立的临时配置和文件夹。

```powershell
npm run bench:work -- anvil-qwen3.5:9b fableforge-ai/mythos-v2-8b:q4_k_m fableforge-ai/mythos-v2-8b:latest
npm run installer
.\node_modules\electron\dist\electron.exe tests/work-conflicts.ui.cjs
```

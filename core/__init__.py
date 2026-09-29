# -*- coding: utf-8 -*-
"""core —— 内嵌的本地原生框架快照（状态机 + 兼容层 + 整理）。

为什么内嵌而不是外部依赖：这个项目要能**独立跑、独立测、独立交给别人修**。
把核心放进来之后，`python service/app.py` 在任意目录都能起，不需要先装别的仓库。

⚠️ 请勿就地修改本目录下的代码后再期待上游同步。改动请回上游
   （`local-model-framework/`）改，再用 `scripts/sync_core.py` 重新同步过来。
   快照版本记录在 `core/VENDOR.md`。
"""

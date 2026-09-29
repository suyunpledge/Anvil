# -*- coding: utf-8 -*-
"""mythos_core —— 「本地原生 Agent 框架 · 专属兼容层」的分层内核。

分层：

    errors     异常类型（无依赖）
    config     配置常量（数据，无逻辑）
    rules      Mythos 专属 tool 规则（R1/R2）      ← 换模型必须重测重写
    params     参数归一与校验（R9，通用）
    transport  出网 + 解码（通用，纯 urllib）
    types      结果载体 FillResult（通用）

对外唯一入口仍是 ``compatibility/mythos_adapter.py`` 里的 :class:`MythosAdapter`；
本包不面向框架直接使用，只服务于那一个门面。
"""
from __future__ import annotations

__all__ = [
    "errors", "config", "rules", "params", "transport", "types",
]

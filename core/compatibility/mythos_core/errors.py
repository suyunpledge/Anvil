# -*- coding: utf-8 -*-
"""errors.py —— 兼容层异常类型（模型无关层）。

异常层级：
    AdapterError            出网 / 解码 / 传输层错误（可重试）
      ├─ ContextOverflow    上下文超限（确定性失败，不重试）
      └─ UnsafeArgument     危险参数命中（判死，不重试）
    SchemaViolation         参数不符合 schema（模型采样失败，可换温度重试）

把这些放成独立模块，是为了让 params / rules / transport 三个子层都能引用它们，
又不至于互相 import 形成环。
"""
from __future__ import annotations


class AdapterError(Exception):
    """兼容层基础异常：出网、解码、传输层的问题。"""


class ContextOverflow(AdapterError):
    """请求超出上下文配额（R5）。实测：超限直接 400，不静默截断。"""


class UnsafeArgument(AdapterError):
    """受控工具的参数命中危险模式（R2）。命中即判死，不回炉重试。"""


class SchemaViolation(AdapterError):
    """arguments 不合法或缺少必填项（R9）。属模型采样失败，可换温度重试。"""

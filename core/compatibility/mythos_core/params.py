# -*- coding: utf-8 -*-
"""params.py —— 参数规范化与校验（R9，模型无关层）。

职责：把模型吐出的 ``arguments`` 按 JSON Schema 递归归一类型、校验必填。
这是通用能力（任何模型都会吐 "10" 而不是 10），所以和 Mythos 专属规则分开。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict

from .errors import SchemaViolation

# 归一 boolean 时认可的「真 / 假」字面量（覆盖中文，实践里模型会用）
_TRUTHY = {"true", "yes", "y", "on", "1", "是", "真"}
_FALSY = {"false", "no", "n", "off", "0", "否", "假"}


def _coerce(value: Any, spec: Dict[str, Any]) -> Any:
    """按单个 JSON Schema 片段把值归一到正确类型（依据 E2 / E11）。

    与原实现的差别（都是加固，不改规则语义）：
      · 归一**失败**时抛 :class:`SchemaViolation`，不再把错误类型静默透传给工具
        （原实现里 ``"abc"`` 会被当作 integer 原样放行）。
      · object 类型做**真正的递归**归一（原实现只递归一层）。
      · array 支持 ``items`` 子 schema，且能解析形如 ``'["a","b"]'`` 的字符串。
      · string 收到 dict/list 时用 ``json.dumps``（合法 JSON），而不是 Python ``repr``。

    没有声明 ``type`` 的项原样返回。
    """
    if not isinstance(spec, dict):
        return value
    t = spec.get("type")
    if not t:
        return value

    if t == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            m = re.search(r"-?\d+", value)
            if m:
                return int(m.group())
        raise SchemaViolation("无法把 %r 归一为 integer" % (value,))

    if t == "number":
        if isinstance(value, bool):
            raise SchemaViolation("无法把 bool 归一为 number：%r" % (value,))
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        raise SchemaViolation("无法把 %r 归一为 number" % (value,))

    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in _TRUTHY:
                return True
            if s in _FALSY:
                return False
        raise SchemaViolation("无法把 %r 归一为 boolean" % (value,))

    if t == "array":
        item_spec = spec.get("items") or {}
        if isinstance(value, list):
            return [_coerce(v, item_spec) for v in value]
        if value is None or value == "":
            return []
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [_coerce(v, item_spec) for v in parsed]
                except Exception:
                    pass
            return [value]
        return [value]

    if t == "string":
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    if t == "object":
        if value is None:
            return {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                raise SchemaViolation("无法把字符串归一到 object：%r" % (value[:120],))
        if isinstance(value, dict):
            sub = spec.get("properties") or {}
            return {k: _coerce(v, sub.get(k, {})) for k, v in value.items()}
        raise SchemaViolation("无法把 %r 归一到 object" % (value,))

    return value


def normalize_args(raw_args: Any, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
    """把模型给的 ``arguments`` 规范成 dict，并按 schema 递归归一类型（R9）。

    - ``raw_args`` 可以是 dict、JSON 字符串，或 None（视作空参）。
    - 缺必填项直接抛 :class:`SchemaViolation`（依据 E11：模型会字面填入，但不会补全语义）。
    """
    if raw_args is None or (isinstance(raw_args, str) and raw_args.strip() == ""):
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            raise SchemaViolation("arguments 不是合法 JSON: %r" % (raw_args[:200],))
    if not isinstance(raw_args, dict):
        raise SchemaViolation("arguments 不是对象：%s" % type(raw_args).__name__)

    fn = (tool_schema or {}).get("function") or {}
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = params.get("required") or []

    out = {k: _coerce(v, props.get(k, {})) for k, v in raw_args.items()}

    missing = [r for r in required if r not in out or out[r] in ("", None)]
    if missing:
        raise SchemaViolation("缺少必填参数：%s" % ", ".join(missing))
    return out

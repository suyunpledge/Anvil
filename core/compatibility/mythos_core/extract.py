# -*- coding: utf-8 -*-
"""extract.py —— 从**正文**里抠工具调用（通用层，纯标准库）。

为什么需要它：不是所有本地模型都会走 Ollama 的原生 ``tool_calls`` 通道。
实测（probes/model_smoke.py，2026-09-27）：

  · mythos-v2-8b / qwen3:8b / qwen3.5:9b —— 原生通道正常，content 里不出现工具调用；
  · **qwen2.5-coder:7b** —— 即使请求体带 ``tools``，它也不产出 ``tool_calls``，
    而是把调用写成一段裸 JSON 放进 content：
        {"name": "read_file", "arguments": {"path": "app.py"}}
    这类模型没有修复层就完全用不了（框架拿到的是「一段看起来像 JSON 的文本」）。

本模块只做「文本 → 候选调用」这一步，不做授权判断（那是门面与引擎的事）：
返回的每个候选都要再经 R1（节点授权集）与 R9（schema 归一）才能执行。

解析要处理的实际脏数据：
  · 前后有解释性文字；被 ```json 围栏包着；一次给多个对象；
  · ``arguments`` 是 dict，**也可能是 JSON 字符串**（qwen2.5-coder 常见）；
  · 嵌套结构里还有 dict（``.*?`` 非贪婪正则会在这里截断，必须按花括号配平扫）；
  · 键名不统一：name / tool / function.name；arguments / parameters / args。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

#: 认得的工具名字段（按优先级）
_NAME_KEYS = ("name", "tool", "tool_name", "function")
#: 认得的参数字段（按优先级）
_ARG_KEYS = ("arguments", "parameters", "args", "input", "params")

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")


def _fix_unescaped_quotes(s: str) -> str:
    '''修常见的手写 JSON 毛病：字符串里没转义的引号 / 裸换行 / 裸制表符。

    为什么必需：qwen2.5-coder 在 ``new_code`` 里写文档字符串时，会把三个双引号原样敲进去
    而不转义（实测 wo-qwencoder-01），于是整段 JSON 解不开——模型其实调对了工具，
    是**格式**把结果卡住了。

    判定规则：在字符串内遇到双引号时，看它后面第一个非空白字符；
    如果是 ``, : } ]``（或已到末尾）就是字符串结束，否则当内层引号转义掉。
    裸换行/制表符在字符串内一律转义（JSON 不允许字面控制字符）。
    '''
    out = []
    i = 0
    n = len(s)
    in_str = False
    while i < n:
        ch = s[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            if i + 1 < n:
                out.append(s[i + 1])
            i += 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ',:}]':
                out.append('"')
                in_str = False
            else:
                out.append('\\"')
            i += 1
            continue
        if ch == "\n":
            out.append("\\n")
            i += 1
            continue
        if ch == "\r":
            i += 1
            continue
        if ch == "\t":
            out.append("\\t")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def repair_json_text(text: str) -> Optional[Any]:
    """尽力把一个「大概是 JSON」的文本解成对象；不行就返回 None（不猜）。

    依次尝试：原样 → 去尾逗号 → 修引号/裸换行 → Python 字面量（单引号 dict）。
    每一步都真解一次，解不开就下一档；全部失败返回 None。
    """
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    no_trailing_comma = re.sub(r",(\s*[}\]])", r"\1", s)
    tries = [s, no_trailing_comma, _fix_unescaped_quotes(s),
             _fix_unescaped_quotes(no_trailing_comma)]
    for cand in tries:
        try:
            return json.loads(cand)
        except Exception:
            continue
    try:
        import ast as _ast
        v = _ast.literal_eval(s)
        if isinstance(v, (dict, list)):
            return v
    except Exception:
        pass
    return None


def _braced_objects(text: str) -> List[str]:
    """按花括号配平扫描，取出所有顶层 JSON 对象文本（正确处理嵌套、字符串转义）。"""
    out: List[str] = []
    depth = 0
    start = -1
    in_str = False
    quote = ""
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def _unwrap_arguments(value: Any) -> Any:
    """``arguments`` 允许是 dict，也允许是 JSON 字符串（后者是实测里最常见的形态）。"""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s)
            except Exception:
                return value
        return value
    return value


def _normalize_obj(obj: Any) -> Optional[Dict[str, Any]]:
    """把一个候选对象规范化成 ``{"name": str, "arguments": dict}``；不是工具调用返回 None。"""
    if not isinstance(obj, dict):
        return None
    name = None
    for k in _NAME_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
        if isinstance(v, dict):                       # OpenAI 风格 {"function": {...}}
            inner = v.get("name")
            if isinstance(inner, str) and inner.strip():
                name = inner.strip()
                args = None
                for ak in _ARG_KEYS:
                    if ak in v:
                        args = _unwrap_arguments(v[ak])
                        break
                if args is None:
                    args = {kk: vv for kk, vv in v.items() if kk != "name"}
                return {"name": name, "arguments": args} if _NAME_OK.match(name) else None
    if not name or not _NAME_OK.match(name):
        return None
    args: Any = {}
    for k in _ARG_KEYS:
        if k in obj:
            args = _unwrap_arguments(obj[k])
            break
    else:
        # 没有显式参数字段：把其余键当参数（有些模型会把参数摊平写在同一层）
        rest = {k: v for k, v in obj.items()
                if k not in _NAME_KEYS and k not in ("id", "type")}
        args = rest
    if not isinstance(args, dict):
        return None
    if not args and not any(k in obj for k in _ARG_KEYS):
        return None                                    # 只有名字、没有参数 —— 视作噪声
    return {"name": name, "arguments": args}


def extract_tool_calls_from_content(content: str, known_names: Optional[set] = None) -> List[Dict[str, Any]]:
    """从正文里抠出候选工具调用（保持出现顺序，按 name+args 去重）。

    Parameters
    ----------
    content:
        模型返回的正文。
    known_names:
        可选的白名单（通常传 registry 的键集合）。给了它就能挡掉「正文里恰好有一段
        业务 JSON」这类误报；不给则只做结构合法性判断。
    """
    if not content or not isinstance(content, str):
        return []
    candidates: List[str] = []
    for m in _FENCE.finditer(content):
        candidates.extend(_braced_objects(m.group(1)))
    if not candidates:
        candidates = _braced_objects(content)
    if not candidates:
        # 字符串里带了未转义引号时，花括号配平可能被误导；退一步取首尾花括号之间的整段
        a, b = content.find("{"), content.rfind("}")
        if 0 <= a < b:
            candidates = [content[a:b + 1]]

    out: List[Dict[str, Any]] = []
    seen = set()
    for text in candidates:
        obj = repair_json_text(text)
        if obj is None:
            continue
        if isinstance(obj, list):
            objs = obj
        else:
            objs = [obj]
        for one in objs:
            norm = _normalize_obj(one)
            if norm is None:
                continue
            if known_names is not None and norm["name"] not in known_names:
                continue
            key = (norm["name"], json.dumps(norm["arguments"], sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            out.append(norm)
    return out


def looks_like_tool_call_text(content: str, known_names: Optional[set] = None) -> bool:
    """正文是否「看起来是在给工具调用」——用于把「模型想调工具」和「模型在聊天」分开。"""
    return bool(extract_tool_calls_from_content(content, known_names))

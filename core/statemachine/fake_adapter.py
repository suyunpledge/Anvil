# -*- coding: utf-8 -*-
"""fake_adapter.py —— 离线脚本化适配器 + 沙箱工厂。

供两处复用：
  · ``test_statemachine.py``：离线单测状态机（不碰模型、不花额度）；
  · ``cli.py --fake``：离线冒烟。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPAT = os.path.join(os.path.dirname(_HERE), "compatibility")
for _p in (_HERE, _COMPAT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mythos_core.types import FillResult  # noqa: E402

# ============================================================
# 沙箱样板：calc.py 里 average() 未实现（测试因此失败），模型的任务就是补上它
# ============================================================
CALC_SRC = '''# -*- coding: utf-8 -*-
"""演示用沙箱模块。注意：average() 还没实现，所以 test_calc 目前是红的。"""
from typing import List


def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


def average(nums: List[int]) -> float:
    """计算平均值；空列表返回 0.0。"""
    raise NotImplementedError("average() 还没有实现")


def main() -> None:
    print(add(1, 2))
    print(average([1, 2, 3]))


if __name__ == "__main__":
    main()
'''

TEST_CALC_SRC = '''# -*- coding: utf-8 -*-
import unittest

import calc


class TestCalc(unittest.TestCase):

    def test_add(self) -> None:
        self.assertEqual(calc.add(1, 2), 3)

    def test_average(self) -> None:
        self.assertAlmostEqual(calc.average([1, 2, 3]), 2.0)

    def test_average_empty(self) -> None:
        self.assertEqual(calc.average([]), 0.0)


if __name__ == "__main__":
    unittest.main()
'''

#: 常用补丁片段（各测试复用）
OLD_AVG = '''def average(nums: List[int]) -> float:
    """计算平均值；空列表返回 0.0。"""
    raise NotImplementedError("average() 还没有实现")'''

NEW_AVG_GOOD = '''def average(nums: List[int]) -> float:
    """计算平均值；空列表返回 0.0。"""
    if not nums:
        return 0.0
    return sum(nums) / len(nums)'''

NEW_AVG_BAD_SYNTAX = '''def average(nums: List[int]) -> float
    """计算平均值。"""
    return 1.0'''

NEW_AVG_UNDEFINED = '''def average(nums: List[int]) -> float:
    """计算平均值；空列表返回 0.0。"""
    if not nms:
        return 0.0
    return sum(nums) / len(nums)'''


def make_sandbox(root: str, calc_src: str = CALC_SRC, test_src: str = TEST_CALC_SRC) -> str:
    """在 root 下建一个最小沙箱项目（calc.py + test_calc.py）。"""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "calc.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(calc_src)
    with open(os.path.join(root, "test_calc.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(test_src)
    return root


# ============================================================
# 结果构造小工具
# ============================================================
def tc(*calls, content: str = "") -> FillResult:
    """tool_call 结果：tc(("apply_patch", {...}), ...)"""
    return FillResult(node="scripted", ok=True, kind="tool_call",
                      calls=[{"name": c[0], "args": c[1]} for c in calls], content=content)


def nc(content: str = "") -> FillResult:
    """no_call 结果（合法：反问 / 无需工具，R10）"""
    return FillResult(node="scripted", ok=True, kind="no_call", content=content)


def sv(error: str = "bad args") -> FillResult:
    return FillResult(node="scripted", ok=False, kind="schema_violation", error=error)


def unauth(error: str = "未授权工具：write_file") -> FillResult:
    return FillResult(node="scripted", ok=False, kind="unauthorized_tool", error=error)


def unsafe(error: str = "rm -rf 命中") -> FillResult:
    return FillResult(node="scripted", ok=False, kind="unsafe_argument", error=error)


def truncated(error: str = "网络中断") -> FillResult:
    return FillResult(node="scripted", ok=False, kind="transport_error", error=error)


def patch(old: str, new: str, path: str = "calc.py") -> tuple:
    """apply_patch 调用对。"""
    return ("apply_patch", {"path": path, "old_string": old, "new_string": new})


# ============================================================
# 脚本化适配器
# ============================================================
class ScriptedAdapter:
    """按脚本顺序返回结果；脚本项可以是 FillResult，也可以是 f(node, messages) 回调。"""

    def __init__(self, script: Optional[List[Any]] = None) -> None:
        self.script: List[Any] = list(script or [])
        self.calls: List[Dict[str, Any]] = []
        self.records: List[Dict[str, Any]] = []

    # ---- 兼容层门面同形 ----
    def fill_slot(self, node: str, messages: List[Dict[str, Any]], registry: Dict[str, Any],
                  node_kind: str = "slot", n: int = 1, node_type: Optional[str] = None,
                  **kw: Any) -> FillResult:
        self.calls.append({"node": node, "node_kind": node_kind, "node_type": node_type,
                           "n": n, "messages": list(messages)})
        item = self.script.pop(0) if self.script else None
        if item is None:
            r = nc("")
        elif callable(item):
            r = item(node, messages)
        elif isinstance(item, FillResult):
            r = item
        else:
            raise TypeError("脚本项类型不支持：%r" % type(item))
        r.node = node
        r.attempts = max(1, int(n))
        return r

    def record(self, node: str, tool_name: str, ok: bool, sec: float = None) -> None:
        self.records.append({"node": node, "tool": tool_name, "ok": bool(ok), "sec": sec})

    # ---- 整理类工单用：纯文本问答（不带工具）----
    def ask(self, messages: List[Dict[str, Any]], think: bool = False, **kw: Any) -> FillResult:
        self.calls.append({"node": "ask", "node_kind": "answer", "node_type": None,
                           "n": 1, "messages": list(messages)})
        item = self.script.pop(0) if self.script else None
        if item is None:
            r = FillResult(node="ask", ok=True, kind="answer", content="")
        elif callable(item):
            r = item("ask", messages)
        elif isinstance(item, FillResult):
            r = item
        else:
            raise TypeError("脚本项类型不支持：%r" % type(item))
        if r.kind == "no_call":          # ask 的形态是 answer
            r.kind = "answer"
        return r

    def health(self) -> Dict[str, Any]:
        return {"ok": True, "present": ["fake"], "want": "fake"}

    # ---- 断言辅助 ----
    def nodes(self) -> List[str]:
        return [c["node"] for c in self.calls]

    def calls_for(self, node: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["node"] == node]

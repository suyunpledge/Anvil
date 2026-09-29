# -*- coding: utf-8 -*-
"""test_context.py —— 交付件 C 的离线单测。

跑法：python context/tests/test_context.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import context.builder as B  # noqa: E402

SAMPLE = '''# -*- coding: utf-8 -*-
"""演示模块。"""
from typing import List

MAX_ITEMS = 10


def helper(x: int) -> int:
    """把输入乘二。"""
    return x * 2


class Store:
    """一个很小的存储。"""

    def put(self, key: str, value: int) -> None:
        """写入一条。"""
        self._d = {key: value}

    def get(self, key: str) -> int:
        """读一条。"""
        return 0


def average(nums: List[int]) -> float:
    """计算平均值；空列表返回 0.0。"""
    raise NotImplementedError("还没有实现")


def main() -> None:
    print(average([1, 2, 3]))
'''


class FakeSpec:
    def __init__(self, ctx_max=4096):
        self.ctx_max = ctx_max
        self.model = "fake"
        self.key = "fake"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ctx-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "pkg"), exist_ok=True)
        with open(os.path.join(self.root, "calc.py"), "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        with open(os.path.join(self.root, "pkg", "u.py"), "w", encoding="utf-8") as f:
            f.write('"""工具。"""\n\n\ndef use() -> None:\n    """调用平均。"""\n    pass\n')
        with open(os.path.join(self.root, "other.md"), "w", encoding="utf-8") as f:
            f.write("# 不是 python\n")


class ChunkTest(_Base):
    def test_chunks_top_level(self) -> None:
        r = B.chunk_python(os.path.join(self.root, "calc.py"))
        self.assertTrue(r["ok"])
        kinds = {c["kind"] for c in r["chunks"]}
        self.assertIn("function", kinds)
        self.assertIn("class", kinds)
        names = {c["name"] for c in r["chunks"]}
        for want in ("helper", "Store", "average", "main"):
            self.assertIn(want, names)

    def test_chunk_has_line_range_and_doc(self) -> None:
        r = B.chunk_python(os.path.join(self.root, "calc.py"))
        avg = next(c for c in r["chunks"] if c["name"] == "average")
        self.assertLess(avg["start"], avg["end"])
        self.assertIn("计算平均值", avg["doc"])
        self.assertIn("NotImplementedError", avg["text"])

    def test_class_chunk_lists_methods(self) -> None:
        r = B.chunk_python(os.path.join(self.root, "calc.py"))
        cls = next(c for c in r["chunks"] if c["kind"] == "class")
        quals = {m["qualname"] for m in cls["methods"]}
        self.assertEqual(quals, {"Store.put", "Store.get"})

    def test_syntax_error_degrades_to_whole_file(self) -> None:
        p = os.path.join(self.root, "broken.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("def f(:\n    pass\n")
        r = B.chunk_python(p)
        self.assertTrue(r["ok"])
        self.assertEqual(r["chunks"][0]["kind"], "file")
        self.assertIn("语法错误", r.get("note", ""))

    def test_missing_file(self) -> None:
        r = B.chunk_python(os.path.join(self.root, "nope.py"))
        self.assertFalse(r["ok"])


class RepoMapTest(_Base):
    def test_lists_python_only_with_symbols(self) -> None:
        rm = B.repo_map(self.root)
        paths = {e["path"] for e in rm["entries"]}
        self.assertIn("calc.py", paths)
        self.assertIn("pkg/u.py", paths)
        self.assertNotIn("other.md", paths, "repo map 只收 python")
        calc = next(e for e in rm["entries"] if e["path"] == "calc.py")
        syms = {s["name"] for s in calc["symbols"]}
        self.assertIn("average", syms)
        self.assertIn("Store", syms)
        self.assertIn("average", rm["text"])

    def test_max_files_truncates(self) -> None:
        rm = B.repo_map(self.root, max_files=1)
        self.assertEqual(len(rm["entries"]), 1)
        self.assertTrue(rm["truncated"])


class BudgetTest(unittest.TestCase):
    def test_drops_lowest_priority_first(self) -> None:
        b = B.Budget(1000, reserve_ratio=0.0)
        b.add("task", 100, "任务")
        b.add("target_source", 90, "T" * 2000)
        b.add("repo_map", 30, "M" * 2000)
        fit = b.fit()
        keys = [k["key"] for k in fit["kept"]]
        self.assertIn("task", keys)
        self.assertIn("repo_map", fit["dropped"] or keys, "低优先级应被优先丢弃")

    def test_reserve_ratio_shrinks_limit(self) -> None:
        tight = B.Budget(1000, reserve_ratio=0.5)
        loose = B.Budget(1000, reserve_ratio=0.0)
        self.assertLess(tight.limit, loose.limit)

    def test_totals(self) -> None:
        b = B.Budget(10000, reserve_ratio=0.0)
        b.add("a", 1, "x" * 40)
        self.assertGreater(b.total(), 0)
        self.assertEqual(b.total(), b.fit()["tokens"])

    def test_render_orders_sections(self) -> None:
        b = B.Budget(10000, reserve_ratio=0.0)
        b.add("repo_map", 30, "MAP")
        b.add("task", 100, "TASK")
        txt = B.render(b.fit()["kept"])
        self.assertLess(txt.index("TASK"), txt.index("MAP"), "渲染顺序应固定")


class BuildTest(_Base):
    def test_picks_target_symbol_from_task(self) -> None:
        r = B.build(target="calc.py", task="在 calc.py 中实现 average(nums)：返回平均值。",
                    spec=FakeSpec(), workdir=self.root)
        self.assertEqual(r["symbol"], "average")
        self.assertIn("NotImplementedError", r["text"])
        self.assertIn("average", r["text"])

    def test_falls_back_to_unimplemented_symbol(self) -> None:
        r = B.build(target="calc.py", task="把这个函数补上。", spec=FakeSpec(),
                    workdir=self.root)
        self.assertEqual(r["symbol"], "average", "猜不到时取第一个未实现的函数")

    def test_includes_file_head_for_imports(self) -> None:
        r = B.build(target="calc.py", task="实现 average", spec=FakeSpec(), workdir=self.root)
        self.assertIn("from typing import List", r["text"], "必须带上 import，否则模型写不出能跑的代码")
        self.assertIn("file_head", r["kept"])

    def test_trimming_beats_naive_full_file(self) -> None:
        """★ 一期验收的上下文目标：注入体积应明显小于「塞整文件」。"""
        big = SAMPLE + "\n\n" + "\n\n".join(
            "def filler%d(x: int) -> int:\n    \"\"\"填充函数。\"\"\"\n    return x + %d" % (i, i)
            for i in range(120))
        with open(os.path.join(self.root, "big.py"), "w", encoding="utf-8") as f:
            f.write(big)
        r = B.build(target="big.py", task="实现 average", spec=FakeSpec(ctx_max=4096),
                    workdir=self.root)
        self.assertLess(r["tokens_est"], r["naive_tokens_full_file"],
                        "有预算裁剪时必须小于整文件塞入")
        self.assertIn("target_source", r["kept"])

    def test_tiny_budget_drops_something(self) -> None:
        # 预算小到装不下全部：必须真的丢低优先级段，但目标函数不能丢
        big = SAMPLE + "\n\n" + "\n\n".join(
            "def filler%d(x: int) -> int:\n    \"\"\"填充。\"\"\"\n    return x + %d" % (i, i)
            for i in range(60))
        with open(os.path.join(self.root, "tiny.py"), "w", encoding="utf-8") as f:
            f.write(big)
        r = B.build(target="tiny.py", task="实现 average", spec=FakeSpec(ctx_max=600),
                    workdir=self.root)
        self.assertTrue(r["dropped"], "预算很小时必须真的丢掉低优先级段")
        self.assertIn("target_source", r["kept"], "目标函数不允许被丢")
        self.assertLessEqual(r["tokens_est"], r["limit_tokens"])

    def test_out_of_repo_target_is_absolute_safe(self) -> None:
        r = B.build(target=os.path.join(self.root, "calc.py"), task="实现 average",
                    spec=FakeSpec(), workdir=self.root)
        self.assertEqual(r["target"], "calc.py")

    def test_est_tokens_consistent_with_gateway(self) -> None:
        sys.path.insert(0, os.path.join(_ROOT, "gateway"))
        import gateway.app as G  # noqa
        for s in ("", "abc", "发票报销", "a" * 100):
            self.assertEqual(B.est_tokens(s), G.rough_tokens(s),
                             "上下文层与网关的 token 口径必须一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)

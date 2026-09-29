# -*- coding: utf-8 -*-
"""test_optimizations.py —— 本轮四项优化的离线单测（不调模型、不花额度）。

覆盖：
  1. rg 搜索（可用时走 rg，不可用时退 Python；两者行为一致）
  2. 错误与日志脱敏（凭据形状的串必须被打掉；正常内容不能误伤）
  3. 按内容归类（假嵌入替身；阈值以下判“不确定”；嵌入不可用时不报错）
  4. find → tidy 串联的数据通路

跑法：
    python statemachine/test_optimizations.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "compatibility")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import semantic_tidy as ST
import tools as T
from mythos_core import sanitize as SZ
from tools import ToolContext, find_rg, search_code, search_with_rg


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="opt-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "pkg"), exist_ok=True)
        with open(os.path.join(self.tmp, "a.py"), "w", encoding="utf-8") as f:
            f.write("def alpha() -> int:\n    return MARKER_VALUE\n")
        with open(os.path.join(self.tmp, "pkg", "b.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n# MARKER_VALUE here\n")
        with open(os.path.join(self.tmp, "notes.md"), "w", encoding="utf-8") as f:
            f.write("MARKER_VALUE in markdown\n")

    def ctx(self, target="a.py") -> ToolContext:
        return ToolContext(root=self.tmp, target=target)


# ============================================================
# 1. rg 搜索
# ============================================================
class RgSearchTest(_Base):
    def test_rg_available_or_skip(self) -> None:
        rg = find_rg()
        if not rg:
            self.skipTest("本机没找到 rg，走 Python 兜底路径")
        self.assertTrue(os.path.isfile(rg))

    def test_search_finds_hits(self) -> None:
        r = search_code(self.ctx(), pattern="MARKER_VALUE", glob="*.py", max_hits=50)
        self.assertIn("MARKER_VALUE", r["hits"])
        self.assertIn(r["engine"], ("rg", "python"))

    def test_glob_filters(self) -> None:
        r = search_code(self.ctx(), pattern="MARKER_VALUE", glob="*.md", max_hits=50)
        self.assertIn("notes.md", r["hits"])
        self.assertNotIn("a.py", r["hits"])

    def test_truncated_flag(self) -> None:
        r = search_code(self.ctx(), pattern="MARKER_VALUE", glob="*.py", max_hits=1)
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len([l for l in r["hits"].split("\n") if l]), 1)

    def test_no_match_is_not_error(self) -> None:
        r = search_code(self.ctx(), pattern="完全不存在的字符串_zzz", glob="*.py")
        self.assertIn("无匹配", r["hits"])
        self.assertFalse(r["truncated"])

    def test_rg_argv_safe_for_leading_dash(self) -> None:
        """★ 安全要点：以 - 开头的 pattern 必须被当成参数值，不能变成 flag。"""
        rg = find_rg()
        if not rg:
            self.skipTest("无 rg")
        r = search_with_rg(self.tmp, "--version", glob="*.py", max_hits=5)
        # 不应把 rg 的版本信息当结果返回；正常应是“无匹配”或极少命中
        self.assertIsNotNone(r)
        self.assertNotIn("ripgrep", (r or {}).get("hits", ""))

    def test_rg_flag_explicit(self) -> None:
        r = search_code(self.ctx(), pattern="MARKER_VALUE", glob="*.py", max_hits=50,
                        engine="rg")
        self.assertIn("MARKER_VALUE", r["hits"])

    def test_auto_engine_small_dir_uses_python(self) -> None:
        r = search_code(self.ctx(), pattern="MARKER_VALUE", glob="*.py", max_hits=50,
                        engine="auto")
        self.assertEqual(r["engine"], "python", "小目录应选 Python（实测更快）")

    def test_default_engine_is_python(self) -> None:
        r = search_code(self.ctx(), pattern="MARKER_VALUE", glob="*.py", max_hits=50)
        self.assertEqual(r["engine"], "python")

    def test_python_fallback_equivalence(self) -> None:
        """rg 与 Python 两套引擎在同一输入上应给出等价的命中集合。"""
        ctx = self.ctx()
        rg_res = search_with_rg(self.tmp, "MARKER_VALUE", glob="*.py", max_hits=50)
        T._RG_CACHE["path"] = None
        T._RG_CACHE["probed"] = True          # 临时禁用 rg，强制 Python 路径
        try:
            py_res = search_code(ctx, pattern="MARKER_VALUE", glob="*.py", max_hits=50)
        finally:
            T._RG_CACHE["probed"] = False
        py_files = {l.split(":")[0] for l in py_res["hits"].split("\n") if ":" in l}
        self.assertIn("a.py", py_files)
        if rg_res is not None:
            rg_files = {l.split(":")[0] for l in rg_res["hits"].split("\n") if ":" in l}
            self.assertEqual(rg_files, py_files, "两套引擎的命中文件集应一致")


# ============================================================
# 2. 脱敏
# ============================================================
class SanitizeTest(unittest.TestCase):
    def test_openai_style_key(self) -> None:
        s = "auth failed with sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        out = SZ.redact(s)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", out)
        self.assertIn("redacted", out)

    def test_bearer_header(self) -> None:
        out = SZ.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghij")
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", out)

    def test_key_value_form(self) -> None:
        for s in ('API_KEY=abcdef1234567890', 'token: "zzzzzzzzzzzzzzzz"',
                  'password=hunter2hunter2', 'secret=abcdefghijklmnop'):
            out = SZ.redact(s)
            self.assertIn("redacted", out, s)

    def test_local_username_removed(self) -> None:
        out = SZ.redact(r"D:\Users\zhangsan\x.py")
        self.assertNotIn("zhangsan", out)

    def test_normal_content_not_mangled(self) -> None:
        for s in ("HTTP 400: model does not support thinking",
                  "Traceback: KeyError 'nodes'",
                  "Ran 35 tests in 0.402s",
                  "平均相似度 0.68"):
            self.assertEqual(SZ.redact(s), s, s)

    def test_safe_error_shape(self) -> None:
        try:
            raise ValueError("bad token sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
        except ValueError as e:
            msg = SZ.safe_error(e, kind="transport")
        self.assertTrue(msg.startswith("[transport] ValueError:"))
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", msg)

    def test_credential_like_detector(self) -> None:
        self.assertTrue(SZ.is_credential_like("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"))
        self.assertFalse(SZ.is_credential_like("hello world"))

    def test_transport_imports_redact(self) -> None:
        from mythos_core import transport
        self.assertTrue(hasattr(transport, "redact"))


# ============================================================
# 3. 按内容归类（假嵌入替身）
# ============================================================
class SemanticTest(_Base):
    def _fake_embed(self, mapping):
        """替身：按关键词给一个 one-hot 风格的向量，使余弦结果可预期。"""
        def _embed(texts, model=ST.EMBED_MODEL, timeout=60):
            out = []
            for t in texts:
                vec = [0.0, 0.0, 0.0]
                low = t.lower()
                if "发票" in t or "invoice" in low or "报销" in t:
                    vec[0] = 1.0
                elif "合同" in t or "contract" in low or "协议" in t:
                    vec[1] = 1.0
                elif "照片" in t or "photo" in low or "image" in low or "截图" in t:
                    vec[2] = 1.0
                out.append(vec)
            return out
        return _embed

    def test_suggests_correct_category(self) -> None:
        files = [{"name": "发票-9月.pdf", "path": ""},
                 {"name": "房屋合同.pdf", "path": ""},
                 {"name": "照片-风景.jpg", "path": ""}]
        cats = {"票据": "发票、报销单", "合同": "合同、协议", "图片": "照片、截图"}
        orig = ST.embed
        ST.embed = self._fake_embed(None)
        try:
            got = ST.suggest_categories(files, cats)
        finally:
            ST.embed = orig
        self.assertEqual(got["发票-9月.pdf"]["suggest"], "票据")
        self.assertEqual(got["房屋合同.pdf"]["suggest"], "合同")
        self.assertEqual(got["照片-风景.jpg"]["suggest"], "图片")

    def test_below_threshold_is_unsure(self) -> None:
        # 文件向量与类别向量正交（余弦 0）→ 应低于阈值，判为不确定
        def _embed(texts, model=ST.EMBED_MODEL, timeout=60):
            out = []
            for t in texts:
                if "无关键词文件" in t:
                    out.append([1.0, 0.0])
                else:
                    out.append([0.0, 1.0])
            return out
        orig = ST.embed
        ST.embed = _embed
        try:
            got = ST.suggest_categories([{"name": "无关键词文件.xyz", "path": ""}],
                                        {"票据": "发票", "合同": "合同"}, threshold=0.9)
        finally:
            ST.embed = orig
        self.assertIsNone(got["无关键词文件.xyz"]["suggest"])
        self.assertIn("低于阈值", got["无关键词文件.xyz"]["reason"])

    def test_embed_failure_is_graceful(self) -> None:
        orig = ST.embed
        ST.embed = lambda texts, model=ST.EMBED_MODEL, timeout=60: None
        try:
            got = ST.suggest_categories([{"name": "a.pdf", "path": ""}], {"票据": "发票"})
        finally:
            ST.embed = orig
        self.assertIsNone(got["a.pdf"]["suggest"])
        self.assertIn("不可用", got["a.pdf"]["reason"])

    def test_cosine_basic(self) -> None:
        self.assertAlmostEqual(ST.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(ST.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(ST.cosine([], [1]), 0.0)

    def test_snippet_reads_text_files_only(self) -> None:
        p = os.path.join(self.tmp, "t.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("发票内容：报销单据")
        s = ST._snippet(p)
        self.assertIn("发票内容", s)
        binary = os.path.join(self.tmp, "t.pdf")
        with open(binary, "wb") as f:
            f.write(b"\x00\x01\x02")
        self.assertEqual(ST._snippet(binary), "t.pdf")

    def test_health_reports_dim(self) -> None:
        orig = ST.embed
        ST.embed = lambda texts, model=ST.EMBED_MODEL, timeout=60: [[0.0] * 1024 for _ in texts]
        try:
            h = ST.health()
        finally:
            ST.embed = orig
        self.assertTrue(h["ok"])
        self.assertEqual(h["dim"], 1024)

    def test_real_embed_available_locally(self) -> None:
        """真打一次本机嵌入（不出网、不花额度）；离线就跳过。"""
        h = ST.health()
        if not h["ok"]:
            self.skipTest("本机嵌入服务未就绪")
        self.assertEqual(h["dim"], 1024)


# ============================================================
# 4. find → tidy 串联
# ============================================================
class FindToTidyTest(_Base):
    def test_find_result_feeds_scan(self) -> None:
        """find 的命中路径可直接用作 tidy 的扫描输入（同一套相对路径约定）。"""
        import tidy as TD
        found = TD.find_files(self.tmp, pattern=r".*\.py$")
        paths = {h["path"] for h in found["hits"]}
        self.assertEqual(paths, {"a.py", "pkg/b.py"})
        scan = TD.scan_root(self.tmp)
        scanned = {f["name"] for f in scan["files"]}
        self.assertEqual(scanned, {"a.py", "notes.md"}, "scan 只看根目录文件（不递归）")

    def test_find_content_mode(self) -> None:
        import tidy as TD
        found = TD.find_files(self.tmp, content_re="MARKER_VALUE", exts=[".py"])
        names = {h["path"] for h in found["hits"]}
        self.assertEqual(names, {"a.py", "pkg/b.py"})
        self.assertTrue(all(h["line"] > 0 for h in found["hits"]))

    def test_find_truncated_flag(self) -> None:
        import tidy as TD
        found = TD.find_files(self.tmp, pattern=r".*", max_hits=1)
        self.assertTrue(found["truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""test_ops_tools.py —— 整理/电脑操作类工具 + 删除安全机制的单测（2026-10-03）。

覆盖用户明确的三条要求：
  ① 基础能力：文件整理类操作（move/copy/mkdir/find/info）能真干活；
  ② 权限控制：越界即拒、备份目录不可动、code 链 target_only 下动不了别的文件；
  ③ 安全机制：**删除必先确认**（不带 confirm 不删）**且自动备份**（备份内容可校验、
     校验不过就中止）。

跑法：
    python statemachine/test_ops_tools.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "compatibility")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import safeops
import tools as T
from tools import ToolContext


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class OpsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ops-tools-")
        # 一个典型待整理目录
        with open(os.path.join(self.root, "报告.pdf"), "w", encoding="utf-8") as f:
            f.write("PDF-CONTENT")
        with open(os.path.join(self.root, "note.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        os.makedirs(os.path.join(self.root, "sub"), exist_ok=True)
        with open(os.path.join(self.root, "sub", "deep.txt"), "w", encoding="utf-8") as f:
            f.write("deep")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def ctx(self, **kw) -> ToolContext:
        # 整理链用 write_scope="root"（可动整个根）；code 链是 target_only
        kw.setdefault("write_scope", "root")
        kw.setdefault("target", "报告.pdf")
        return ToolContext(root=self.root, **kw)


class PermTest(OpsTestBase):
    """② 权限控制。"""

    def test_absolute_path_rejected(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(T.ToolError):
            T.move_file(ctx, src="C:/Windows/system32/x.dll", dst="a.txt")

    def test_parent_escape_rejected(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(T.ToolError):
            T.copy_file(ctx, src="../外面.txt", dst="a.txt")

    def test_target_only_scope_blocks_other_files(self) -> None:
        """code 链的 target_only：不能动别的文件（结构性防线，不靠提示词）。"""
        ctx = ToolContext(root=self.root, target="报告.pdf", write_scope="target_only")
        with self.assertRaises(T.ToolError):
            T.move_file(ctx, src="note.txt", dst="sub/note.txt")

    def test_backup_dir_not_deletable(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(T.ToolError):
            T.delete_file(ctx, path=".ide-backup/x", confirm=True, recursive=True)


class BasicOpsTest(OpsTestBase):
    """① 基础能力。"""

    def test_file_info(self) -> None:
        r = T.file_info(self.ctx(), path="报告.pdf")
        self.assertEqual(r["kind"], "file")
        self.assertEqual(r["bytes"], len("PDF-CONTENT"))
        self.assertTrue(r["sha256"].startswith("sha256:"))

    def test_make_dir_and_move(self) -> None:
        ctx = self.ctx()
        T.make_dir(ctx, path="文档")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "文档")))
        r = T.move_file(ctx, src="报告.pdf", dst="文档/报告.pdf")
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "文档", "报告.pdf")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "报告.pdf")))

    def test_move_refuses_overwrite(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(T.ToolError):
            T.move_file(ctx, src="报告.pdf", dst="note.txt")

    def test_copy(self) -> None:
        ctx = self.ctx()
        r = T.copy_file(ctx, src="报告.pdf", dst="副本.pdf")
        self.assertTrue(r["ok"])
        self.assertEqual(_sha(os.path.join(self.root, "副本.pdf")),
                         _sha(os.path.join(self.root, "报告.pdf")))

    def test_find_files(self) -> None:
        r = T.find_files(self.ctx(), pattern=r"\.pdf$")
        self.assertIn("报告.pdf", r["files"])

    def test_open_path_returns_hint_without_executing(self) -> None:
        r = T.open_path(self.ctx(), path="报告.pdf")
        self.assertTrue(r["ok"])
        self.assertIn("reveal_hint", r)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "报告.pdf")),
                        "open_path 不应改动任何文件")


class DeleteSafetyTest(OpsTestBase):
    """③ 安全机制：删除必须确认 + 自动备份。"""

    def test_first_call_does_not_delete(self) -> None:
        """★ 不带 confirm：只回 needs_confirm，文件纹丝不动。"""
        ctx = self.ctx()
        before = _sha(os.path.join(self.root, "报告.pdf"))
        r = T.delete_file(ctx, path="报告.pdf")
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_confirm"])
        self.assertIn("will_backup_to", r)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "报告.pdf")))
        self.assertEqual(_sha(os.path.join(self.root, "报告.pdf")), before)

    def test_confirm_deletes_and_backs_up(self) -> None:
        """★ 带 confirm：删除成功，且备份内容与原文件逐字节一致。"""
        ctx = self.ctx()
        before = _sha(os.path.join(self.root, "报告.pdf"))
        r = T.delete_file(ctx, path="报告.pdf", confirm=True)
        self.assertTrue(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "报告.pdf")),
                         "确认后应真的删掉")
        bpath = os.path.join(self.root, r["backup"].replace("/", os.sep))
        self.assertTrue(os.path.isfile(bpath), "备份文件必须存在")
        self.assertEqual(_sha(bpath), before, "备份必须与原文件一致")
        self.assertEqual(r["sha256"], before)

    def test_restore_from_backup(self) -> None:
        """备份的意义：能恢复。"""
        ctx = self.ctx()
        before = _sha(os.path.join(self.root, "报告.pdf"))
        r = T.delete_file(ctx, path="报告.pdf", confirm=True)
        bpath = os.path.join(self.root, r["backup"].replace("/", os.sep))
        shutil.copy2(bpath, os.path.join(self.root, "报告.pdf"))
        self.assertEqual(_sha(os.path.join(self.root, "报告.pdf")), before)

    def test_dir_requires_recursive(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(T.ToolError):
            T.delete_file(ctx, path="sub", confirm=True)          # 没给 recursive
        self.assertTrue(os.path.isdir(os.path.join(self.root, "sub")))

    def test_dir_delete_with_backup(self) -> None:
        ctx = self.ctx()
        r = T.delete_file(ctx, path="sub", confirm=True, recursive=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], "dir")
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub")))
        bpath = os.path.join(self.root, r["backup"].replace("/", os.sep))
        self.assertTrue(os.path.isfile(os.path.join(bpath, "deep.txt")),
                        "目录备份里应有原文件")

    def test_delete_missing_file_errors(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(T.ToolError):
            T.delete_file(ctx, path="不存在.txt", confirm=True)

    def test_two_deletes_do_not_collide(self) -> None:
        """同秒多次删除，备份路径不能互相覆盖。"""
        ctx = self.ctx()
        r1 = T.delete_file(ctx, path="报告.pdf", confirm=True)
        r2 = T.delete_file(ctx, path="note.txt", confirm=True)
        self.assertNotEqual(r1["backup"], r2["backup"])
        for r in (r1, r2):
            self.assertTrue(os.path.exists(
                os.path.join(self.root, r["backup"].replace("/", os.sep))))


class SafeOpsUnitTest(unittest.TestCase):
    """safeops 自身的契约（被 tools 与 tidy 共用，必须稳）。"""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="safeops-")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_refuse_root(self) -> None:
        with self.assertRaises(ValueError):
            safeops.backup_and_delete(self.root, "")

    def test_refuse_backup_dir(self) -> None:
        with self.assertRaises(ValueError):
            safeops.backup_and_delete(self.root, ".ide-backup")

    def test_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            safeops.backup_and_delete(self.root, "a.txt")

    def test_plan_backup_rel_shape(self) -> None:
        rel = safeops.plan_backup_rel(self.root, "a/b.txt", "20261003-120000")
        self.assertEqual(rel, ".ide-backup/20261003-120000/a/b.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)

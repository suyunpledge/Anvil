# -*- coding: utf-8 -*-
"""safeops.py —— 「删除必先备份」的唯一真相源（2026-10-03 新增）。

需求（用户明确）：
  1. 每次执行删除命令，**必须经过用户同意**；
  2. 删除前**自动备份**，且备份可校验、可恢复。

设计取舍：
  · 备份落在**被操作目录自己下面**的 ``.ide-backup/<时间戳>/<原相对路径>``——
    用户最容易找到的地方，也不依赖任何外部配置；
  · 备份**先写、校验后删**：文件比 sha256，目录比（文件数 + 总字节数），
    不一致就中止并抛错——宁可删不掉，不可删了找不回；
  · 本模块**不做同意判定**，只做「给我一个已确认的删除请求，我安全地执行」。
    同意由上层负责：工具层要 ``confirm=True``，服务层由
    ``LOCAL_IDE_REQUIRE_CONFIRM=1`` + ``POST /wo/{id}/confirm`` 强制。

对外函数：
    plan_backup_rel(root, rel)              -> 预测备份相对路径（给前端展示）
    backup_and_delete(root, rel, recursive) -> {backup, kind, bytes, sha256?, files?}
    sha256_file(path)                       -> str
"""
from __future__ import annotations

import hashlib
import os
import random
import shutil
import time

BACKUP_DIRNAME = ".ide-backup"

__all__ = ["BACKUP_DIRNAME", "plan_backup_rel", "backup_and_delete", "sha256_file"]


def sha256_file(path: str, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _tree_stat(path: str) -> tuple:
    """(文件数, 总字节数)——目录备份的校验依据。"""
    n = 0
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            n += 1
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return (n, total)


def plan_backup_rel(root: str, rel: str, stamp: str = "") -> str:
    """预测备份会落在哪里（纯字符串运算，不落盘）。

    stamp 留空则取当前时间；同秒重复调用由 :func:`backup_and_delete` 保证不撞车。
    """
    ts = stamp or time.strftime("%Y%m%d-%H%M%S")
    rel = str(rel).replace("\\", "/").lstrip("/")
    return "%s/%s/%s" % (BACKUP_DIRNAME, ts, rel)


def _unique_backup_abs(root: str, rel: str) -> tuple:
    """返回 (备份绝对路径, 备份相对路径)，保证不与已有备份撞名。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for attempt in range(50):
        suffix = "" if attempt == 0 else "-%s" % "".join(
            random.choice("0123456789abcdef") for _ in range(4))
        brel = plan_backup_rel(root, rel, stamp + suffix)
        babs = os.path.join(root, brel.replace("/", os.sep))
        if not os.path.exists(babs):
            return babs, brel
    raise RuntimeError("备份路径连续 50 次撞名，放弃")


def backup_and_delete(root: str, rel: str, recursive: bool = False) -> dict:
    """先备份、校验、再删除。失败即中止（不删）。

    Parameters
    ----------
    root : 沙箱/整理根（绝对路径）
    rel  : 目标相对路径（已由调用方做过越界校验）
    recursive : 目标是目录时必须显式 True

    Returns
    -------
    ``{"deleted": rel, "backup": 备份相对路径, "kind": "file"|"dir",
       "bytes": int, "sha256": str|None, "files": int|None}``

    Raises
    ------
    FileNotFoundError / ValueError / RuntimeError / OSError
    """
    root = os.path.abspath(root)
    rel = str(rel).replace("\\", "/").strip("/")
    if not rel:
        raise ValueError("拒绝删除根目录")
    if rel == BACKUP_DIRNAME or rel.startswith(BACKUP_DIRNAME + "/"):
        raise ValueError("拒绝操作备份目录：%s" % rel)

    abs_p = os.path.join(root, rel.replace("/", os.sep))
    if not os.path.exists(abs_p):
        raise FileNotFoundError("要删除的路径不存在：%s" % rel)

    is_dir = os.path.isdir(abs_p)
    if is_dir and not recursive:
        raise ValueError("目标是目录，需 recursive=True 才能删除：%s" % rel)

    babs, brel = _unique_backup_abs(root, rel)
    os.makedirs(os.path.dirname(babs), exist_ok=True)

    info = {"deleted": rel, "backup": brel, "kind": "dir" if is_dir else "file"}

    if is_dir:
        # ① 复制整棵树
        shutil.copytree(abs_p, babs)
        # ② 校验：文件数 + 总字节
        src_n, src_b = _tree_stat(abs_p)
        dst_n, dst_b = _tree_stat(babs)
        if (src_n, src_b) != (dst_n, dst_b):
            shutil.rmtree(babs, ignore_errors=True)
            raise RuntimeError("备份校验失败（%d/%d 文件、%d/%d 字节），已中止删除"
                               % (dst_n, src_n, dst_b, src_b))
        info.update({"files": src_n, "bytes": src_b, "sha256": None})
        shutil.rmtree(abs_p)
    else:
        # ① 复制单文件（保留元数据）
        shutil.copy2(abs_p, babs)
        # ② 校验 sha256
        h1 = sha256_file(abs_p)
        h2 = sha256_file(babs)
        if h1 != h2:
            try:
                os.remove(babs)
            except OSError:
                pass
            raise RuntimeError("备份校验失败（sha256 不一致），已中止删除：%s" % rel)
        info.update({"files": 1, "bytes": os.path.getsize(abs_p), "sha256": h1})
        os.remove(abs_p)

    return info

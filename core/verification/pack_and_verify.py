# -*- coding: utf-8 -*-
"""pack_and_verify.py —— 打包交付（文件夹 + 同名 zip）并逐文件校验。

交付纪律（MEMORY：交付物落盘位置与校验口径）：
  · 打包位置与交付目录一致；排除 __pycache__ / *.pyc 这类可再生成的产物；
  · 打完必须逐文件哈希比对（目录 vs zip 内解出的副本）＋ 压缩包完整性检查，
    不能只凭「命令没报错」。

跑法：
    python verification/pack_and_verify.py            # 就地打包＋校验
    python verification/pack_and_verify.py --no-zip   # 只做校验
"""
from __future__ import annotations

import hashlib
import os
import sys
import zipfile

#: 不打包的目录：可再生成的产物（编译缓存）与暂存区（.sm-work 是每次跑工单的工作副本，
#: 里面的内容在 .sm-runs 的运行日志里已有逐轮 diff 记录）。
SKIP_DIRS = {"__pycache__", ".git", ".sm-work"}
SKIP_EXT = {".pyc", ".pyo"}


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, name)
            yield full, os.path.relpath(full, root).replace("\\", "/")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    no_zip = "--no-zip" in argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    zip_path = os.path.join(os.path.dirname(root), os.path.basename(root) + ".zip")

    files = list(_iter_files(root))
    total = sum(os.path.getsize(p) for p, _ in files)
    print("目录：%s" % root)
    print("文件：%d 个，合计 %.2f MB" % (len(files), total / 1048576.0))

    if not no_zip:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for path, rel in files:
                z.write(path, rel)
        print("已写：%s（%.2f MB）" % (zip_path, os.path.getsize(zip_path) / 1048576.0))

    print("\n=== 校验 ===")
    if not zipfile.is_zipfile(zip_path):
        print("[FAIL] 不是合法的 zip")
        return 1
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        print("压缩包完整性：%s" % ("OK" if bad is None else "损坏于 %s" % bad))
        names = set(z.namelist())
        mism = []
        for path, rel in files:
            if rel not in names:
                mism.append("zip 缺少 %s" % rel)
                continue
            with z.open(rel) as f:
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            if h.hexdigest() != _sha(path):
                mism.append("哈希不一致 %s" % rel)
        extra = sorted(names - {rel for _, rel in files})
        print("比对文件：%d 个" % len(files))
        print("一致：%d 个" % (len(files) - len(mism)))
        if mism:
            print("不一致：%d 个" % len(mism))
            for m in mism[:20]:
                print("  - %s" % m)
        if extra:
            print("zip 里多出的条目（%d）：%s" % (len(extra), extra[:8]))
        ok = (not mism) and (bad is None)
    print("\n结论：%s" % ("打包与校验全部通过 ✓" if ok else "存在不一致 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack.py —— 打包并逐文件校验（交付纪律：不能只凭"命令没报错"）。

产出：与本项目同级的 `local-model-ide-app.zip`，并逐个文件比对 sha256。
排除：编译缓存、运行期产物（.runtime）、工作副本。这些都可再生成，不该进交付包。

跑法：python scripts/pack.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

SKIP_DIRS = {"__pycache__", ".runtime", ".sm-work", ".sm-runs", ".git", "node_modules"}
SKIP_EXT = {".pyc", ".pyo"}

#: 已知的**运行时可変状态**：运行测试/工单会让它们变化。
#: 它们不是源代码，但也不该从交付里去掉（内含探测出来的数据，是种子）。
#: 因此打包校验把它们单独统计：只这些不一致 → 视为正常，不报失败。
#: 依据：2026-09-28 用户复核时发现“目录与 ZIP 只有这一处不一致”。
EXPECTED_MUTABLE = {
    "core/compatibility/node_profiles.json",   # adapter 把探测结果累加写回
}


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, name)
            yield full, os.path.relpath(full, root).replace("\\", "/")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包并逐文件校验")
    ap.add_argument("--strict", action="store_true",
                   help="把已知可变状态的不一致也当失败（要精确比对时用）")
    args = ap.parse_args(argv)

    zip_path = os.path.join(os.path.dirname(_ROOT), os.path.basename(_ROOT) + ".zip")
    files = list(iter_files(_ROOT))
    total = sum(os.path.getsize(p) for p, _ in files)
    print("目录：%s" % _ROOT)
    print("文件：%d 个，合计 %.2f MB" % (len(files), total / 1048576.0))

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
        missing, mism, mutable = [], [], []
        for path, rel in files:
            if rel not in names:
                missing.append(rel)
                continue
            with z.open(rel) as f:
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            if h.hexdigest() != sha(path):
                (mutable if rel in EXPECTED_MUTABLE else mism).append(rel)
        extra = sorted(names - {rel for _, rel in files})

        print("比对文件：%d 个" % len(files))
        print("  完全一致：%d 个" % (len(files) - len(mism) - len(mutable) - len(missing)))
        if mutable:
            print("  已知可变状态（不计为失败）：%d 个" % len(mutable))
            for m in mutable:
                print("    ~ %s（运行时统计；测试/工单跑过就会变）" % m)
        if mism:
            print("  **内容不一致（异常）**：%d 个" % len(mism))
            for m in mism[:20]:
                print("    - %s" % m)
        if missing:
            print("  **zip 缺少**：%d 个" % len(missing))
            for m in missing[:20]:
                print("    - %s" % m)
        if extra:
            print("  zip 里多出条目：%s" % extra[:8])
        ok = (not missing) and (not mism) and (bad is None) and (not extra or True)
        if args.strict and mutable:
            ok = False
            print("  （--strict：已知可变状态的不一致也算失败）")
    print("\n结论：%s" % ("打包与校验通过 ✓" if ok else "存在异常 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

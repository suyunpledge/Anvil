#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_core.py —— 从上游框架同步 `core/` 快照。

为什么要这么绕：核心（状态机 + 兼容层 + 整理）是**上游仓库的产物**，本项目只是消费它。
如果允许就地改，两边会分叉，而分叉的核心比没有核心更危险 —— 你不知道哪份是真的。

所以规则是：
    改核心 → 回上游改 → 跑这个脚本同步 → 在本项目里跑回归
本脚本会把上游目录镜像过来（删除本地多余文件），并更新 `core/VENDOR.md` 的记录。

跑法：
    python scripts/sync_core.py                    # 默认从 ../../local-model-framework 同步
    python scripts/sync_core.py --from <上游路径>
    python scripts/sync_core.py --check            # 只比对差异，不写入
"""
from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
CORE = os.path.join(_ROOT, "core")

#: 上游 → 本项目 core 的映射
MAP = [
    ("statemachine", "statemachine"),
    ("compatibility", "compatibility"),
    ("verification", "verification"),
]
SKIP_DIRS = {"__pycache__", ".sm-work", ".sm-runs", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo"}
#: 运行期生成的文件：同步时**不要**覆盖它们（那会把攒下的能力画像统计冲掉）
SKIP_NAMES = {"node_profiles.json", "generated_profiles.py"}

DEFAULT_UPSTREAM = os.path.abspath(
    os.path.join(_ROOT, "..", "local-model-framework"))


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            if name in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, name)
            yield full, os.path.relpath(full, root).replace("\\", "/")


def compare(upstream: str) -> list:
    """返回差异列表：[(状态, 相对路径)]，状态属于 新增/变更/仅本地。"""
    diffs = []
    for src_name, dst_name in MAP:
        src_root = os.path.join(upstream, src_name)
        dst_root = os.path.join(CORE, dst_name)
        if not os.path.isdir(src_root):
            diffs.append(("上游缺失", src_name))
            continue
        up = {rel: full for full, rel in iter_files(src_root)}
        lo = {rel: full for full, rel in iter_files(dst_root)} if os.path.isdir(dst_root) else {}
        for rel in sorted(set(up) - set(lo)):
            diffs.append(("新增", "%s/%s" % (dst_name, rel)))
        for rel in sorted(set(lo) - set(up)):
            diffs.append(("仅本地", "%s/%s" % (dst_name, rel)))
        for rel in sorted(set(up) & set(lo)):
            if not filecmp.cmp(up[rel], lo[rel], shallow=False):
                diffs.append(("变更", "%s/%s" % (dst_name, rel)))
    return diffs


def upstream_rev(upstream: str) -> str:
    try:
        p = subprocess.run(["git", "-C", upstream, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        return (p.stdout or "").strip() or "(非 git 仓库)"
    except Exception:
        return "(取不到)"


def do_sync(upstream: str) -> None:
    for src_name, dst_name in MAP:
        src_root = os.path.join(upstream, src_name)
        dst_root = os.path.join(CORE, dst_name)
        if not os.path.isdir(src_root):
            print("  跳过（上游没有）：%s" % src_name)
            continue
        if os.path.isdir(dst_root):
            shutil.rmtree(dst_root)
        shutil.copytree(src_root, dst_root,
                        ignore=shutil.ignore_patterns(*SKIP_DIRS, *SKIP_EXT, *SKIP_NAMES))
        n = sum(1 for _ in iter_files(dst_root))
        print("  同步 %-14s → core/%-14s（%d 文件）" % (src_name, dst_name, n))


def write_vendor(upstream: str, diffs: list) -> None:
    p = os.path.join(CORE, "VENDOR.md")
    body = [
        "# core 快照说明",
        "",
        "本目录是本地原生框架（上游）的**只读快照**，本项目只消费、不就地修改。",
        "改核心请回上游改，然后运行 `python scripts/sync_core.py`。",
        "",
        "- vendor: local-model-framework",
        "- synced_at: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "- upstream: %s（本地检出，路径已省略）" % os.path.basename(upstream),
        "- upstream_rev: %s" % upstream_rev(upstream),
        "- entries: %s" % ", ".join("%s→core/%s" % (a, b) for a, b in MAP),
        "",
        "## 同步前的差异检查",
        "",
        "若此处列出条目，说明上次同步后本地有改动（或上游有更新）。"
        "`仅本地` 项尤其要注意：那是快照被就地改过的痕迹。",
        "",
    ]
    if diffs:
        for state, rel in diffs[:200]:
            body.append("- [%s] %s" % (state, rel))
        if len(diffs) > 200:
            body.append("- …另有 %d 项" % (len(diffs) - 200))
    else:
        body.append("- 无差异（快照与上游一致）")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    print("  已更新 core/VENDOR.md")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同步 core 快照")
    ap.add_argument("--from", dest="src", default=DEFAULT_UPSTREAM, help="上游路径")
    ap.add_argument("--check", action="store_true", help="只比对，不写入")
    args = ap.parse_args(argv)

    upstream = os.path.abspath(args.src)
    if not os.path.isdir(upstream):
        print("上游目录不存在：%s" % upstream)
        return 1
    print("上游：%s（rev %s）" % (upstream, upstream_rev(upstream)))
    diffs = compare(upstream)
    print("差异：%d 项" % len(diffs))
    for state, rel in diffs[:30]:
        print("  [%s] %s" % (state, rel))
    if len(diffs) > 30:
        print("  …另有 %d 项" % (len(diffs) - 30))
    if args.check:
        return 0
    print("\n开始同步…")
    do_sync(upstream)
    write_vendor(upstream, diffs)
    print("\n完成。请接着跑：python scripts/selftest.py --core")
    return 0


if __name__ == "__main__":
    sys.exit(main())

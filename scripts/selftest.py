#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py —— 一键跑完所有不需要模型的检查（离线，约 5 秒）。

包含：
  · gateway 单测          （熔断 / 画像 / 映射 / 路由 / 审计）
  · context 单测          （切块 / repo map / 预算 / 组装）
  · service 单测          （状态机包装 / 未确认不落盘 / 取消 / 整理）
  · 扩展静态检查          （命令注册 / 配置键 / 入口 / 转义 / 无外连）
  · 核心回归（可选 --core）——内嵌快照自带的四套测试

跑法：
    python scripts/selftest.py
    python scripts/selftest.py --core        # 加上核心回归（约 4 秒）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

SUITES = [
    ("网关单测", ["gateway/tests/test_gateway.py"]),
    ("上下文层单测", ["context/tests/test_context.py"]),
    ("工单服务单测", ["service/tests/test_service.py"]),
    ("安全回归（审查 7 条）", ["service/tests/test_security_fixes.py"]),
    ("自查修复（0929 审查）", ["service/tests/test_review_fixes.py"]),
    ("读口鉴权（0930 补齐）", ["service/tests/test_read_auth_2026_09_30.py"]),
    ("整理链删除全链路（同意+备份）", ["service/tests/test_delete_flow.py"]),
]

# 端到端 + 并发取消验证（需要起服务，不算\"单元\"，单独跑）
# 它们被 scripts/e2e_check.py / service/tests/cancel_concurrency.py 独立处理

CORE_SUITES = [
    ("核心·状态机", ["core/statemachine/test_statemachine.py"]),
    ("核心·文件整理", ["core/statemachine/test_tidy.py"]),
    ("核心·Mythos 适配层", ["core/compatibility/test_mythos_adapter.py"]),
    ("核心·Qwen2.5-Coder 适配层", ["core/compatibility/test_qwen_coder_adapter.py"]),
    ("核心·GLM4 适配层", ["core/compatibility/test_glm4_adapter.py"]),
    ("核心·整理工具与删除安全", ["core/statemachine/test_ops_tools.py"]),
    ("核心·Job Object 隔离", ["core/statemachine/test_winjob.py"]),
]


def run_py(path: str) -> tuple:
    # ★ 内嵌解释器是隔离模式：明说 PYTHONPATH 没用，改成在子进程里显式补路径
    code = (
        "import sys, os, runpy;"
        "root = os.path.dirname(os.path.dirname(os.path.abspath(%r)));"
        "[sys.path.insert(0, p) for p in (root, os.path.join(root, 'service'),"
        " os.path.join(root, 'gateway'), os.path.join(root, 'context'),"
        " os.path.join(root, 'core', 'statemachine'),"
        " os.path.join(root, 'core', 'compatibility')) if p not in sys.path];"
        "runpy.run_path(%r, run_name='__main__')"
    ) % (path, path)
    p = subprocess.run([sys.executable, "-c", code],
                       cwd=_ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    tail = ((p.stdout or "") + (p.stderr or "")).strip().split("\n")
    summary = ""
    for line in reversed(tail):
        if line.startswith(("Ran ", "OK", "FAILED", "合计")):
            summary = line.strip()
            break
    return p.returncode == 0, summary


def run_node(rel: str) -> tuple:
    js = os.path.join(_ROOT, rel)
    try:
        p = subprocess.run(["node", js], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 node（扩展静态检查需要它）"
    out = ((p.stdout or "") + (p.stderr or ""))
    summary = ""
    for line in reversed(out.strip().split("\n")):
        if line.startswith("合计"):
            summary = line.strip()
            break
    return p.returncode == 0, summary or out.strip()[:80]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地 IDE 离线自检")
    ap.add_argument("--core", action="store_true", help="一并跑核心回归")
    args = ap.parse_args(argv)

    suites = list(SUITES)
    if args.core:
        suites += CORE_SUITES

    print("离线自检：%s\n" % _ROOT)
    results = []
    for name, files in suites:
        for f in files:
            ok, summary = run_py(f)
            results.append((name, ok, summary))
            print("  [%s] %-22s %s" % ("PASS" if ok else "FAIL", name, summary))

    ok, summary = run_node("extension/tests/check.js")
    results.append(("扩展静态检查", ok, summary))
    print("  [%s] %-22s %s" % ("PASS" if ok else "FAIL", "扩展静态检查", summary))

    bad = [r for r in results if not r[1]]
    print("\n合计：%d/%d 通过%s" % (len(results) - len(bad), len(results),
                                   "  ✓" if not bad else "  ✗"))
    for name, _, summary in bad:
        print("  ✗ %s  %s" % (name, summary))
    if not bad:
        print("\n下一步：python scripts/e2e_check.py   （端到端，需要本地模型）")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

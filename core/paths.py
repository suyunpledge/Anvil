# -*- coding: utf-8 -*-
"""paths.py —— 项目内的路径解析（唯一入口，别的模块不许自己拼路径）。

内嵌核心带来的一个现实问题：`core/statemachine` 与 `core/compatibility` 里的模块
是按「同级目录」互相 import 的（例如 `from contract import ...`、`from mythos_core...`）。
所以任何使用方都必须先把这两个目录挂到 sys.path 上——本模块就负责这一件事。

好处是所有入口（gateway / service / context / 测试）都调同一个函数，
将来核心挪位置只改这里一处。
"""
from __future__ import annotations

import os
import sys

#: 项目根目录（本文件的父目录的父目录）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 内嵌核心的两个需要上 sys.path 的目录
STATEMACHINE_DIR = os.path.join(ROOT, "core", "statemachine")
COMPATIBILITY_DIR = os.path.join(ROOT, "core", "compatibility")

#: 运行期产物目录（工作副本、运行日志、暂存、令牌）
#: 可用 ``LOCAL_IDE_RUNTIME`` 覆盖——测试与多实例需要把运行期产物隔离开。
RUNTIME_DIR = os.environ.get("LOCAL_IDE_RUNTIME") or os.path.join(ROOT, ".runtime")

_READY = False


def ensure_core_on_path() -> None:
    """把内嵌核心挂到 sys.path。幂等，可重复调用。"""
    global _READY
    if _READY:
        return
    for p in (STATEMACHINE_DIR, COMPATIBILITY_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    _READY = True


def runtime_path(*parts: str) -> str:
    """项目内运行期路径（自动建父目录）。"""
    p = os.path.join(RUNTIME_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def version() -> dict:
    """读快照版本（core/VENDOR.md 里的字段），用于自检与交接说明。"""
    md = os.path.join(ROOT, "core", "VENDOR.md")
    info = {"vendor": "unknown", "synced_at": "unknown"}
    try:
        with open(md, "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line and line.startswith("- "):
                    k, _, v = line[2:].partition(":")
                    info[k.strip()] = v.strip()
    except OSError:
        pass
    return info

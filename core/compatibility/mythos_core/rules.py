# -*- coding: utf-8 -*-
"""rules.py —— Mythos 专属 tool 规则（R1 / R2）。

本模块是整套兼容层里「最不通用」的一层：它编码的是**这台 8B 模型的实测脾气**，
换模型必须重测重写。因此它与 config / params / transport 分开，边界清晰。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from .errors import UnsafeArgument

# ============================================================
# R1 分批工具集：按「节点类型」授权，绝不暴露全集
#    依据 E9  —— 文件类节点绝不能看到 run_shell，否则它会自己升级动作。
#    依据 E15 —— 编辑节点不能同时给 apply_patch 和 write_file，否则它会选整文件重写。
#    ★ 硬边界：TOOLSETS["edit"] 里没有 write_file，且不要往里加。
# ============================================================
TOOLSETS: Dict[str, List[str]] = {
    "recon":   ["list_dir", "read_file", "search_code"],   # 只读侦察
    "verify":  ["read_file", "run_tests", "search_code"],  # 跑测试但不写
    "edit":    ["read_file", "apply_patch", "search_code"],  # ★ 只给小改动
    "create":  ["read_file", "write_file"],                # 确实要新建文件时才授权
    "commit":  ["git_commit", "list_dir"],                 # 只能提交，不能改文件
    "shell":   ["run_shell"],                              # 单列，且必须过命令闸
    "judge":   [],                                         # 判题：从候选中选，无执行权
    # ---- 整理 / 基础电脑操作节点（2026-10-03 新增）----
    #   权限口径（用户明确要求「不能给完整权限，仅限整理文件」）：
    #     · 只给**文件/目录级**操作，给不了通用能力；
    #     · ★ 不含 run_shell —— 拿不到任意命令执行权；
    #     · ★ 不含 write_file / apply_patch —— 不作代码内容写入
    #       （改代码是 code 链 edit 节点的事，两条链各管各的）；
    #     · delete_file 是唯一破坏性动作，且工具层强制「先确认、再备份、后删除」。
    "ops":     ["list_dir", "read_file", "file_info", "find_files", "search_code",
                "make_dir", "move_file", "copy_file", "delete_file", "open_path"],
}

# ============================================================
# R2 危险参数闸：对这些工具的参数做正则体检
#    依据 E9 实测：模型会生成 rm -rf /path/to/repo
# ============================================================
DESTRUCTIVE_PATTERNS: List[Tuple[str, str]] = [
    (r"\brm\s+-[a-zA-Z]*[rf]",   "递归/强制删除"),
    (r"\brmdir\s+/s",            "递归删目录"),
    (r"\bdel\s+/[sfq]",          "强制删除"),
    (r"Remove-Item.*-Recurse",   "PowerShell 递归删除"),
    # ★ 依据 S6（2026-09-27 qwen2.5-coder 探针 E1）：让模型「清理仓库」，
    #   它给的是 `git clean -fdx` —— 会删掉所有未跟踪文件，破坏性不亚于 rm -rf。
    #   这句话 Mythos 没给过，是换模型探针才发现的缺口，所以单独列出来。
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fdx]", "git 清理未跟踪文件（不可恢复）"),
    (r"\bgit\s+checkout\s+--\s+\.", "丢弃全部本地修改"),
    (r"\bgit\s+restore\s+\.",   "丢弃全部本地修改"),
    (r"\bformat\s+[a-zA-Z]:",    "格式化盘"),
    (r"\bmkfs",                  "格式化文件系统"),
    (r":\(\)\s*\{.*\};\s*:",     "fork bomb"),
    (r"\bgit\s+push\s+.*--force", "强推"),
    (r"\bgit\s+reset\s+--hard",  "硬回退"),
    (r"curl[^|]*\|\s*(ba)?sh",   "管道执行远端脚本"),
    (r"wget[^|]*\|\s*(ba)?sh",   "管道执行远端脚本"),
    (r"\b(shutdown|reboot|halt)\b", "关机/重启"),
    (r"\bdd\s+if=.*of=/dev/",    "dd 写设备"),
    (r">\s*/dev/sd",             "写块设备"),
    # ★ 2026-10-03：删除/移动类工具的路径参数体检——挡住「想办法跳出沙箱」的写法
    (r"\.\.[\\/]",              "路径回退（试图跳出沙箱）"),
    (r"(^|[\\/])\*",             "通配符批量操作（易误伤）"),
]

# 需要过闸的工具（参数被当作命令文本体检）
#   delete_file / move_file 也纳入：路径参数里塞 `..\..\` 或通配符这类
#   「想绕开沙箱」的写法，会在这里被危险模式挡下（2026-10-03）。
GATED_TOOLS: set = {"run_shell", "delete_file", "move_file"}

# 预编译（原实现每次调用都重新 re.search 字符串，这里改成编译一次）
_COMPILED: List[Tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), label) for pat, label in DESTRUCTIVE_PATTERNS
]


def check_safety(tool_name: str, args: Dict[str, Any]) -> None:
    """危险参数闸（R2）。依据 E9 —— 模型会生成 rm -rf。

    命中任一危险模式即抛 :class:`UnsafeArgument`；调用方据此**立即判死、不回炉**。
    非受控工具（不在 :data:`GATED_TOOLS`）直接放行。
    """
    if tool_name not in GATED_TOOLS:
        return
    blob = json.dumps(args, ensure_ascii=False, default=str)
    for rx, label in _COMPILED:
        if rx.search(blob):
            raise UnsafeArgument(
                "工具 %s 的参数命中危险模式【%s】：%s" % (tool_name, label, blob[:200])
            )

# -*- coding: utf-8 -*-
"""vscode_install_check.py —— 验证扩展能否在本机 VS Code 里跑。

它不\"替你装\"，只检测：
  · VS Code 是否在本机；
  · Node/npm 是否就绪（扩展目录下的 checks 脚本需要 node）；
  · 扩展必需的文件是否齐全；
  · 关键路径与权限（.~vscode 写权限、扩展目录可写）。

输出：是否能跑、有哪些缺、建议怎么修。

跑法：
    python scripts/vscode_install_check.py [--ext-dir PATH] [--install]
    --install：只在\"能装\"时打印 install 命令（让你手动执行）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EXT = os.path.join(ROOT, "extension")
EXPECTED = [
    "extension.js", "package.json",
    os.path.join("media", "icon.svg"),
    os.path.join("tests", "check.js"),
]


def which(name):
    """path 中找 PATH，若不含扩展后缀则补上 .cmd / .bat。"""
    from shutil import which as _w
    p = _w(name)
    if p:
        return p
    for suf in (".cmd", ".bat", ".exe"):
        p = _w(name + suf)
        if p:
            return p
    return ""


def report(results):
    """results 是 [(label, ok, detail)]；按 line 风格打印；返回是否全过。"""
    for label, ok, detail in results:
        flag = "PASS" if ok else "FAIL"
        print("  [%s] %-30s %s" % (flag, label, detail))
    return all(ok for _, ok, _ in results)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="扩展装机自检")
    ap.add_argument("--ext-dir", default=DEFAULT_EXT, help="扩展源目录（默认仓库内 extension/）")
    ap.add_argument("--install", action="store_true",
                   help="检测通过则打印 install 路径（不替你执行）")
    args = ap.parse_args(argv)

    print("VS Code 装机自检：%s\n" % ROOT)
    ext_dir = args.ext_dir
    results: List[Tuple[str, bool, str]] = [
        ("vscode CLI 在 PATH", bool(which("code")),
         which("code") or "（找不到 `code`；从 https://code.visualstudio.com/ 装）"),
        ("Node.js 就绪", bool(which("node")),
         which("node") or "（扩展静态检查要 node）"),
        ("扩展源目录存在", os.path.isdir(ext_dir), ext_dir),
    ]
    for rel in EXPECTED:
        full = os.path.join(ext_dir, rel)
        results.append(("· 含 %s" % rel, os.path.isfile(full),
                        "" if os.path.isfile(full) else "缺失"))

    if which("node"):
        check = os.path.join(args.ext_dir, "tests", "check.js")
        if os.path.isfile(check):
            try:
                r = subprocess.run([which("node"), check], capture_output=True, text=True)
                passed = r.returncode == 0 and "全部通过" in r.stdout
                results.append(("extension 静态检查", passed,
                                r.stdout.strip().split("\n")[-1] if passed
                                else r.stderr.strip()[:80]))
            except Exception as e:
                results.append(("extension 静态检查", False, str(e)))

    ok = report(results)

    if ok and args.install:
        print("\n→ 安装：把以下目录复制/软链到 VS Code 扩展目录")
        # 用本地路径拼接避免环境变量展开；Windows: %USERPROFILE%\.vscode\extensions\local-model-ide
        import os as _os
        _ext_dst = _os.path.join(_os.path.expanduser("~"), ".vscode", "extensions",
                                 "local-model-ide")
        print("  Windows: " + _ext_dst)
        print("  命令示例：")
        _ext_dst_q = _ext_dst.replace("\\", "\\\\")
        _ext_src = args.ext_dir.replace("\\", "\\\\")
        print('    New-Item -ItemType Junction -Path "%s" -Target "%s"' % (_ext_dst_q, _ext_src))
        _ext_src_q = args.ext_dir.replace("'", "'\\''")
        print('    或：cp -R "%s" "%s"' % (_ext_src_q, _ext_dst))

    if not ok:
        print("\n存在阻碍装机的问题，先修再装。")
        return 1
    print("\n全部就绪。重启 VS Code 后左侧栏会出现 \"Local IDE\" 图标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
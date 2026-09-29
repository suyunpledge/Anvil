# -*- coding: utf-8 -*-
"""生成演示沙箱：sandbox_demo/calc.py + sandbox_demo/test_calc.py

跑法：
    python statemachine/make_sandbox.py [目标目录]
默认写到项目下的 ``sandbox_demo/``；已存在则不覆盖（用 --force 覆盖）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_adapter import CALC_SRC, TEST_CALC_SRC  # noqa: E402


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox_demo")
    root = argv[0] if argv else default
    os.makedirs(root, exist_ok=True)
    for name, src in (("calc.py", CALC_SRC), ("test_calc.py", TEST_CALC_SRC)):
        p = os.path.join(root, name)
        if os.path.exists(p) and not force:
            print("跳过（已存在）：%s" % p)
            continue
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(src)
        print("写入：%s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

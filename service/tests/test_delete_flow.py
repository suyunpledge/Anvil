# -*- coding: utf-8 -*-
"""test_delete_flow.py —— 整理链「删除」全链路集成测试（2026-10-03）。

不调模型（注入脚本化方案），验证用户需求 3 的两条：
  · 每次删除必须经用户同意 —— 确认前文件不动、无备份；confirm 后才执行；
  · 删除自动备份        —— 备份落 .ide-backup/<时间戳>/，内容与原文件逐字节一致。

跑法：python service/tests/test_delete_flow.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, "core"), os.path.join(_ROOT, "core", "compatibility"),
           os.path.join(_ROOT, "core", "statemachine"), os.path.join(_ROOT, "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fake_adapter import ScriptedAdapter            # noqa: E402
from mythos_core.types import FillResult            # noqa: E402
import runner as R                                  # noqa: E402


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    wd = tempfile.mkdtemp(prefix="del-flow-")
    try:
        for n in ("草稿1.tmp", "草稿2.tmp"):
            with open(os.path.join(wd, n), "w", encoding="utf-8") as f:
                f.write("TEMP-" + n)
        with open(os.path.join(wd, "重要.md"), "w", encoding="utf-8") as f:
            f.write("# 保留")
        pre = {n: _sha(os.path.join(wd, n)) for n in ("草稿1.tmp", "草稿2.tmp", "重要.md")}

        plan = {"moves": [{"action": "delete", "src": "草稿1.tmp", "dst": ""},
                          {"action": "delete", "src": "草稿2.tmp", "dst": ""}],
                "note": "删除两个临时草稿"}
        ad = ScriptedAdapter([FillResult(node="plan", ok=True, kind="answer",
                                         content=json.dumps(plan, ensure_ascii=False))])
        opts = R.RunnerOpts(kind="tidy", task="删除 .tmp 草稿", workdir=wd,
                            adapter="mythos", allow_delete=True, require_confirm=True)
        r = R.WorkOrderRunner(opts, adapter=ad)
        r.start()
        r.join(180)

        checks = []
        checks.append(("方案闸通过并停在待确认", r.status == "awaiting_confirm"))
        still = set(os.listdir(wd))
        checks.append(("确认前文件未被动",
                       "草稿1.tmp" in still and "草稿2.tmp" in still))
        checks.append(("确认前无备份目录", not os.path.isdir(os.path.join(wd, ".ide-backup"))))

        res = r.confirm()
        after = set(os.listdir(wd))
        checks.append(("确认结果 ok", bool(res.get("ok"))))
        checks.append(("确认后目标已删除",
                       "草稿1.tmp" not in after and "草稿2.tmp" not in after))
        checks.append(("保留文件未误删", "重要.md" in after))

        bk = os.path.join(wd, ".ide-backup")
        checks.append(("自动备份已生成", os.path.isdir(bk)))
        found = {}
        for base, _d, files in os.walk(bk):
            for f in files:
                fp = os.path.join(base, f)
                found[f] = fp
        for n in ("草稿1.tmp", "草稿2.tmp"):
            checks.append(("备份逐字节一致(%s)" % n,
                           n in found and _sha(found[n]) == pre[n]))
        checks.append(("复核闸通过", bool((res.get("verify") or {}).get("ok"))))

        bad = 0
        for name, good in checks:
            print("  [%s] %s" % ("PASS" if good else "FAIL", name))
            bad += 0 if good else 1
        print("合计：%d/%d 通过  %s" % (len(checks) - bad, len(checks),
                                       "✓" if not bad else "✗"))
        return 1 if bad else 0
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

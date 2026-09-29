# -*- coding: utf-8 -*-
"""cli.py —— 状态机的命令行入口（v1 不接 UI，只跑命令行）。

    python -m statemachine.cli contract
    python -m statemachine.cli check
    python -m statemachine.cli gates --workdir D:\\proj --target calc.py --test test_calc.py
    python -m statemachine.cli run --workdir D:\\proj --target calc.py --task "..." --test test_calc.py

`run` 的退出码：0=完成；2=需要人补信息；3=降级人审；4=确定性失败；5=安全终止。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict

# ★ 本机内嵌解释器带 python313._pth（隔离模式）：脚本所在目录不进 sys.path，
#   所以包内模块必须自己把目录加进去，否则 `python statemachine\cli.py ...` 会 ImportError。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from contract import (STATUS_DONE, STATUS_ESCALATED, STATUS_FAILED, STATUS_NEEDS_INPUT,
                      STATUS_SECURITY_ABORT, WorkOrder, contract_summary)
from engine import WorkOrderStateMachine, check_contract_sync
from gates import run_gate
from tools import ToolContext
import tidy as TD
from tidy import TidyOrder
from tidy_engine import TidyStateMachine

_EXIT = {STATUS_DONE: 0, STATUS_NEEDS_INPUT: 2, STATUS_ESCALATED: 3,
         STATUS_FAILED: 4, STATUS_SECURITY_ABORT: 5}


def _cmd_contract(args: argparse.Namespace) -> int:
    print(json.dumps(contract_summary(), ensure_ascii=False, indent=2))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    s = check_contract_sync()
    print(("契约一致 ✓" if s["ok"] else "契约不一致 ✗"))
    for d in s["detail"]:
        print("  - %s" % d)
    return 0 if s["ok"] else 1


def _cmd_gates(args: argparse.Namespace) -> int:
    ctx = ToolContext(root=os.path.abspath(args.workdir), target=args.target,
                      timeout_sec=int(args.timeout or 180))
    all_ok = True
    for g in ("gate_syntax", "gate_type", "gate_test"):
        r = run_gate(g, ctx, test_path=args.test, test_cmd=(args.test_cmd.split() if args.test_cmd else None))
        print("[%s] %s  %.2fs" % ("OK  " if r.ok else "FAIL", g, r.sec))
        for i in r.issues[:8]:
            print("      - (%s, 行 %s) %s" % (i.type, i.line, i.message))
        if not r.ok and r.error:
            print("      - %s" % r.error)
        all_ok = all_ok and r.ok
    return 0 if all_ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    adapter = None
    if not args.fake:
        from adapter_factory import build_adapter
        adapter = build_adapter(kind=args.adapter, model=args.model, profile_path=args.profile)
        print(_model_line(adapter))
        h = adapter.health()
        if not h.get("ok"):
            print("模型不可用：%s" % h)
            return 4
    else:
        from fake_adapter import ScriptedAdapter  # 仅供离线冒烟
        adapter = ScriptedAdapter([])

    wo = WorkOrder(wo_id=args.id or ("wo-%d" % int(time.time())),
                   task=args.task, workdir=os.path.abspath(args.workdir),
                   target=args.target, test_path=args.test)
    wo.constraints.update({
        "max_repair_rounds": int(args.repair_rounds),
        "best_of_n": int(args.best_of),
    })
    sm = WorkOrderStateMachine(adapter, wo, best_of_n=int(args.best_of),
                               commit=not args.no_commit, verbose=True)
    res = sm.run()
    print("\n=== 工单 %s：%s（%.1fs）===" % (wo.wo_id, res.status,
                                            wo.artifacts.get("elapsed_sec", 0)))
    for g in wo.gate_results:
        print("  %-12s round=%s %s" % (g["gate"], g["round"], g["signal"]))
    if res.diff:
        print("\n--- 累计变更（原文件 → 改后）---\n%s" % res.diff)
    patches = wo.artifacts.get("patches") or []
    if len(patches) > 1:
        print("（本轮工单共 %d 次补丁，逐步 diff 见运行日志）" % len(patches))
    if res.error:
        print("\n原因：%s" % res.error)
    if res.runlog_path:
        print("\n运行日志：%s" % res.runlog_path)
    return _EXIT.get(res.status, 1)


def _model_line(adapter) -> str:
    spec = getattr(adapter, "spec", None)
    if spec is None:
        return "适配层：未知"
    return ("适配层：%s｜模型：%s｜发送 think=%s｜正文抠调用=%s｜ctx_max=%d"
            % (spec.key, adapter.model, spec.sends_think, spec.extract_from_content, spec.ctx_max))


def _cmd_find(args: argparse.Namespace) -> int:
    """文件查找：确定性搜索，不调模型、不联网。"""
    res = TD.find_files(os.path.abspath(args.root), pattern=args.name or None,
                        content_re=args.content or None,
                        exts=(args.ext.split(",") if args.ext else None),
                        max_hits=int(args.max_hits))
    print("根目录：%s｜扫描 %d 个文件｜命中 %d 个%s"
          % (res["root"], res["scanned"], len(res["hits"]),
             "（已达上限，结果被截断）" if res["truncated"] else ""))
    for h in res["hits"]:
        extra = (" 行 %d" % h["line"]) if h["line"] else ""
        print("  %-58s %9d B  %s%s" % (h["path"], h["size"], h["mtime"], extra))
    print("\n（本地查找：所有文件信息都在本机处理，未发生任何网络请求）")
    return 0


#: 常见整理目录的默认语义描述（比“<目录名> 类文件”准得多）
#: 实测：默认描述下照片类被判“不确定”（0.62 阈值差一点）；
#:       换成“照片、图像、摄影、光圈快门像素等拍摄参数”后稳定在 0.66–0.73。
_DEFAULT_HINTS = {
    "文档": "合同、报告、说明书、笔记、会议纪要等文字性文档",
    "票据": "发票、报销单、记账凭证、税额、开票与报销资料",
    "合同": "合同、协议、甲方乙方、租期、违约与保密条款",
    "照片": "照片、图像、摄影、光圈快门像素等拍摄参数",
    "图片": "照片、截图、图像、设计稿、插画",
    "压缩包": "压缩归档文件、备份包、资源包",
    "表格": "表格、数据表、清单、统计与预算数据",
    "视频": "视频、录像、影片、剪辑素材",
    "音频": "音频、录音、音乐、语音素材",
    "代码": "源代码、脚本、程序文件、项目源码",
    "安装包": "安装程序、可执行文件、软件安装包",
    "发票": "发票、收据、报销凭证",
}


def _default_category_hint(name: str) -> str:
    return _DEFAULT_HINTS.get(name, "%s 类文件" % name)


def _cmd_tidy(args: argparse.Namespace) -> int:
    from adapter_factory import build_adapter
    adapter = build_adapter(kind=args.adapter, model=args.model, profile_path=args.profile)
    print(_model_line(adapter))

    # —— 预筛（find → tidy 串联）：先确定性筛一遍，再交给整理流程 ——
    filters: Dict[str, Any] = {}
    if args.filter_ext:
        filters["ext"] = [e if e.startswith(".") else "." + e
                          for e in args.filter_ext.replace(" ", "").split(",") if e]
    if args.filter_name:
        filters["name_re"] = args.filter_name

    order = TidyOrder(wo_id=args.id or ("tidy-%d" % int(time.time())), task=args.task,
                      root=os.path.abspath(args.root), mode=args.mode,
                      target_dirs=(args.dirs.split(",") if args.dirs else []),
                      filters=filters, max_moves=int(args.max_moves))
    if filters:
        print("预筛条件：%s" % json.dumps(filters, ensure_ascii=False))

    hints = {}
    if args.semantic and order.target_dirs:
        hints = {d: _default_category_hint(d) for d in order.target_dirs}
        if args.hints:
            for kv in args.hints.split(";"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    hints[k.strip()] = v.strip()
    sm = TidyStateMachine(adapter, order, verbose=True, semantic=bool(args.semantic),
                          category_hints=hints)
    res = sm.run()
    if args.semantic and sm.semantic_notes:
        print("\n--- 语义建议（本地嵌入，零外网）---")
        for name, r in sm.semantic_notes.items():
            arrow = ("→ %s (%.3f)" % (r["suggest"], r["score"])) if r.get("suggest") \
                    else "→ 不确定"
            print("  %-28s %s" % (name, arrow))
    print("\n=== 整理工单 %s：%s（%.1fs）===" % (order.wo_id, res.status, res.elapsed))
    for g in res.gates:
        print("  %-12s %s" % (g["gate"], g["signal"]))
    for mv in (res.plan or {}).get("moves", [])[:12]:
        print("   → %s  ⇒  %s" % (mv.get("src"), mv.get("dst")))
    if res.applied and res.applied.get("moved"):
        print("  实际移动 %d 个" % len(res.applied["moved"]))
    if res.error:
        print("\n原因：%s" % res.error)
    if res.runlog_path:
        print("\n运行日志：%s" % res.runlog_path)
    return 0 if res.status == STATUS_DONE else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m statemachine.cli", description="单文件 Python 修改的最小状态机内核")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("contract", help="打印节点契约").set_defaults(fn=_cmd_contract)
    sub.add_parser("check", help="校验引擎与兼容层的工具集一致性").set_defaults(fn=_cmd_check)

    g = sub.add_parser("gates", help="只跑三个 gate（不调模型）")
    g.add_argument("--workdir", required=True)
    g.add_argument("--target", required=True)
    g.add_argument("--test")
    g.add_argument("--test-cmd")
    g.add_argument("--timeout", type=int)
    g.set_defaults(fn=_cmd_gates)

    r = sub.add_parser("run", help="跑一个真实工单")
    r.add_argument("--workdir", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--task", required=True)
    r.add_argument("--test")
    r.add_argument("--test-cmd")
    r.add_argument("--id")
    r.add_argument("--repair-rounds", type=int, default=3)
    r.add_argument("--best-of", type=int, default=1)
    r.add_argument("--model")
    r.add_argument("--profile")
    r.add_argument("--adapter", default="mythos",
                   help="适配层：mythos（默认）/ qwen-coder")
    r.add_argument("--no-commit", action="store_true")
    r.add_argument("--fake", action="store_true", help="离线冒烟（脚本化假适配器）")
    r.set_defaults(fn=_cmd_run)

    f = sub.add_parser("find", help="文件查找（本地、不调模型、不联网）")
    f.add_argument("--root", required=True, help="搜索根目录")
    f.add_argument("--name", help="文件名正则")
    f.add_argument("--content", help="文件内容正则")
    f.add_argument("--ext", help="后缀限定，逗号分隔，如 .md,.txt")
    f.add_argument("--max-hits", type=int, default=200)
    f.set_defaults(fn=_cmd_find)

    t = sub.add_parser("tidy", help="文件整理工单（scan → plan → 方案闸 → 执行 → 复核闸）")
    t.add_argument("--root", required=True, help="要整理的目录")
    t.add_argument("--task", required=True, help="用一句话说清楚怎么整理")
    t.add_argument("--dirs", help="允许归入的目标目录，逗号分隔")
    t.add_argument("--mode", choices=["dry_run", "execute"], default="dry_run",
                   help="dry_run（默认，只出方案）/ execute（真移动）")
    t.add_argument("--max-moves", type=int, default=200)
    t.add_argument("--id")
    t.add_argument("--model")
    t.add_argument("--profile")
    t.add_argument("--adapter", default="mythos")
    t.add_argument("--semantic", action="store_true",
                   help="用本地嵌入（bge-m3）给出「按内容归类」的参考建议（零外网）")
    t.add_argument("--hints", help="类别描述覆盖，形如 '发票=各类发票与报销单;合同=合同协议'")
    t.add_argument("--filter-ext", help="只处理这些后缀（逗号分隔），先筛后规划")
    t.add_argument("--filter-name", help="只处理文件名匹配该正则的（先筛后规划）")
    t.set_defaults(fn=_cmd_tidy)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

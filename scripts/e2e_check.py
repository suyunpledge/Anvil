#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_check.py —— 端到端验收脚本（走真实 HTTP + 真实本地模型）。

它验证 IDE 一期五条验收里可以被程序化判定的四条：
  ① 离线可用        —— 全链只连 127.0.0.1（脚本会检查地址）
  ② 闭环可验        —— 工单走完五节点、三 gate 全过、状态 done
  ③ 未确认不落盘    —— 工单完成后目标文件字节级不变；确认后才变
  ④ 可审计          —— 网关 audit.jsonl 里能看到本轮调用

不做真实修改的唯一例外：本脚本会在临时目录里建沙箱，**不会碰你的项目文件**。

依赖：Ollama 里已装 `qwen2.5-coder:7b`（实测最快，约 3-5 秒）。
跑法：
    python scripts/e2e_check.py
    python scripts/e2e_check.py --adapter mythos     # 换成别的画像
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.paths import ensure_core_on_path, runtime_path  # noqa: E402

ensure_core_on_path()

GW_PORT = 8098
SVC_PORT = 8099
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CALC = '''# -*- coding: utf-8 -*-
"""端到端检查用的沙箱模块。average 未实现，所以测试现在是红的。"""
from typing import List


def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


def average(nums: List[int]) -> float:
    """计算平均值；空列表返回 0.0。"""
    raise NotImplementedError("average() 还没有实现")


def main() -> None:
    print(add(1, 2))
    print(average([1, 2, 3]))


if __name__ == "__main__":
    main()
'''

TEST = '''# -*- coding: utf-8 -*-
import unittest

import calc


class TestCalc(unittest.TestCase):

    def test_add(self) -> None:
        self.assertEqual(calc.add(1, 2), 3)

    def test_average(self) -> None:
        self.assertAlmostEqual(calc.average([1, 2, 3]), 2.0)

    def test_average_empty(self) -> None:
        self.assertEqual(calc.average([]), 0.0)


if __name__ == "__main__":
    unittest.main()
'''

RESULTS = []

#: 首单用的任务措辞（实测能稳定通过）；版本基线那一单复用它，避免把“模型不稳定”误判成“防护失效”
TASK_LONG = ("在 calc.py 中实现 average(nums) 函数：返回整数列表的平均值，"
             "空列表返回 0.0。把 average 现有的函数体替换掉（不要留下旧代码）；"
             "确保测试全部通过。")


def res(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, detail))


def skip(name: str, detail: str = "") -> None:
    #: 不记入成败：通常是“模型本身没跑成”，与待验证的防护无关
    print("  [SKIP] %-34s %s" % (name, detail))


def http(url: str, method: str = "GET", body=None, timeout: int = 600, token: str = ""):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Local-Ide-Token"] = token          # ★ 写操作需要令牌（服务端策略）
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            text = r.read().decode("utf-8")
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, text
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, {"error": str(e)}


def wait_service(url: str, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, _ = http(url, timeout=5)
        if code == 200:
            return True
        time.sleep(0.5)
    return False


def sha(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            h.update(c)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IDE 一期端到端验收")
    ap.add_argument("--adapter", default="mythos", help="适配层 key（默认 mythos：本地实测最稳；qwen-coder 最快但对措辞更挑）")
    ap.add_argument("--keep", action="store_true", help="保留临时沙箱便于排查")
    args = ap.parse_args(argv)

    py = sys.executable
    procs = []
    tmp = tempfile.mkdtemp(prefix="ide-e2e-")
    print("沙箱：%s\n" % tmp)
    try:
        for name, src in (("calc.py", CALC), ("test_calc.py", TEST)):
            with open(os.path.join(tmp, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
        real = os.path.join(tmp, "calc.py")
        before = sha(real)

        print("— 起服务 —")
        env = dict(os.environ)
        # 把工单服务的模型出口指向本次起的网关（端口与默认不同，必须显式告知）
        env["LOCAL_LLM_GATEWAY"] = "http://127.0.0.1:%d" % GW_PORT
        env["LOCAL_LLM_VIA"] = "gateway"
        # 本次用固定令牌 + 固定 runtime 目录，便于脚本带上令牌
        TOKEN = "e2e-token-" + str(int(time.time()))
        rt = os.path.join(tmp, "runtime")
        os.makedirs(rt, exist_ok=True)
        env["LOCAL_IDE_TOKEN"] = TOKEN
        env["LOCAL_IDE_RUNTIME"] = rt
        gw = subprocess.Popen([py, os.path.join(_ROOT, "gateway", "app.py"),
                               "--port", str(GW_PORT)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, env=env)
        svc = subprocess.Popen([py, os.path.join(_ROOT, "service", "app.py"),
                                "--port", str(SVC_PORT)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env)
        procs = [gw, svc]
        ok_gw = wait_service("http://127.0.0.1:%d/health" % GW_PORT)
        ok_svc = wait_service("http://127.0.0.1:%d/health" % SVC_PORT)
        res("网关就绪", ok_gw, "127.0.0.1:%d" % GW_PORT)
        res("工单服务就绪", ok_svc, "127.0.0.1:%d" % SVC_PORT)
        if not (ok_gw and ok_svc):
            print("\n服务没起来，先看它们的日志再重跑")
            return 1

        code, health = http("http://127.0.0.1:%d/health" % GW_PORT)
        res("出网熔断生效", bool(isinstance(health, dict) and health.get("outbound_block")),
            json.dumps(health, ensure_ascii=False)[:100] if isinstance(health, dict) else "")

        print("\n— 鉴权：无令牌的写操作应被拒（审查 #2）—")
        code, denied = http("http://127.0.0.1:%d/wo" % SVC_PORT, "POST", {
            "kind": "code", "task": "x", "workdir": tmp, "target": "calc.py"})
        res("无令牌建单被拒", code == 401, "HTTP %s %s" % (code, str(denied)[:60]))

        print("\n— 建单（code 工单，要求人确认）—")
        code, created = http("http://127.0.0.1:%d/wo" % SVC_PORT, "POST", {
            "kind": "code", "task": TASK_LONG,
            "workdir": tmp, "target": "calc.py", "test_path": "test_calc.py",
            "adapter": args.adapter, "require_confirm": True,
        }, token=TOKEN)
        res("建单返回 wo_id", code == 200 and isinstance(created, dict) and created.get("wo_id"),
            str(created)[:120])
        wo = (created or {}).get("wo_id") if isinstance(created, dict) else None
        if not wo:
            return 1

        print("\n— SSE 事件流（只取首帧，确认通道通）—")
        sse_ok = False
        try:
            req_ev = urllib.request.Request(
                "http://127.0.0.1:%d/wo/%s/events?once=1" % (SVC_PORT, wo),
                headers={"X-Local-Ide-Token": TOKEN})
            with OPENER.open(req_ev, timeout=20) as r:
                first = r.read(400).decode("utf-8", "ignore")
                sse_ok = "snapshot" in first
        except Exception as e:
            first = str(e)
        res("SSE 通道可用", sse_ok, first.replace("\n", " ")[:90])

        print("\n— 等工单跑到「待确认」—")
        deadline = time.time() + 420
        snap = {}
        while time.time() < deadline:
            _, snap = http("http://127.0.0.1:%d/wo/%s" % (SVC_PORT, wo), timeout=30, token=TOKEN)
            st = snap.get("status") if isinstance(snap, dict) else None
            if st in ("awaiting_confirm", "done", "escalated", "failed", "needs_input",
                      "security_abort", "cancelled"):
                break
            time.sleep(1.0)
        status = snap.get("status") if isinstance(snap, dict) else "?"
        gates = (snap or {}).get("gates") or []
        sig = [(g.get("gate"), g.get("signal")) for g in gates]
        res("工单到达待确认", status == "awaiting_confirm", "status=%s" % status)
        res("三个 gate 全部通过", sig == [("gate_syntax", "ok"), ("gate_type", "ok"),
                                          ("gate_test", "ok")], str(sig))
        res("★ 未确认时文件字节级不变", sha(real) == before,
            "sha 前 12 位 %s" % before[:12])
        if status != "awaiting_confirm":
            print("\n未到达待确认，工单快照：")
            print(json.dumps({k: v for k, v in (snap or {}).items() if k != "timeline"},
                             ensure_ascii=False)[:900])
            return 1

        print("\n— 人确认落盘 —")
        code, conf = http("http://127.0.0.1:%d/wo/%s/confirm" % (SVC_PORT, wo), "POST", {},
                          token=TOKEN)
        res("确认请求成功", code == 200 and isinstance(conf, dict) and conf.get("ok"),
            str(conf)[:120])
        after = sha(real)
        res("落盘后文件已变化", after != before, "%s → %s" % (before[:8], after[:8]))
        content = open(real, encoding="utf-8").read()
        res("实现已写入且旧代码已删除",
            "return sum(nums) / len(nums)" in content and "NotImplementedError" not in content)

        print("\n— 测试真的能跑过 —")
        p = subprocess.run([py, "-m", "unittest", "discover", "-s", ".", "-t", ".", "-p", "test_calc.py"],
                           cwd=tmp, capture_output=True, text=True)
        res("沙箱内测试通过", p.returncode == 0, (p.stdout or p.stderr or "")[-120:].replace("\n", " "))

        print("\n— 版本基线：确认时若真实文件已被改，必须拒绝（审查 #3）—")
        # 说明：这条依赖模型真的跑成一个工单；模型本身不稳（会写出坏补丁）时
        # 就标 SKIP，而不是把「模型没跑成」算成「防护失效」。重试用完全相同的措辞。
        hit = None
        for attempt in (1, 2):
            tmp2 = tempfile.mkdtemp(prefix="ide-e2e-v%d-" % attempt)
            for name, src in (("calc.py", CALC), ("test_calc.py", TEST)):
                with open(os.path.join(tmp2, name), "w", encoding="utf-8", newline="\n") as f:
                    f.write(src)
            real2 = os.path.join(tmp2, "calc.py")
            code, c2 = http("http://127.0.0.1:%d/wo" % SVC_PORT, "POST", {
                "kind": "code", "task": TASK_LONG, "workdir": tmp2,
                "target": "calc.py", "test_path": "test_calc.py",
                "adapter": args.adapter, "require_confirm": True}, token=TOKEN)
            wo2 = (c2 or {}).get("wo_id") if isinstance(c2, dict) else None
            if not wo2:
                shutil.rmtree(tmp2, ignore_errors=True)
                continue
            deadline = time.time() + 420
            st2 = None
            while time.time() < deadline:
                _, s2 = http("http://127.0.0.1:%d/wo/%s" % (SVC_PORT, wo2), timeout=30, token=TOKEN)
                st2 = (s2 or {}).get("status")
                if st2 in ("awaiting_confirm", "failed", "escalated", "done", "needs_input",
                           "security_abort", "cancelled"):
                    break
                time.sleep(1.0)
            if st2 == "awaiting_confirm":
                hit = (wo2, tmp2, real2)
                break
            print("      （第 %d 次尝试未到待确认：%s，重试）" % (attempt, st2))
            shutil.rmtree(tmp2, ignore_errors=True)

        if hit:
            wo2, tmp2, real2 = hit
            with open(real2, "a", encoding="utf-8") as f:
                f.write("\n# 人在等待期间手动改的一行\n")
            code, cf2 = http("http://127.0.0.1:%d/wo/%s/confirm" % (SVC_PORT, wo2),
                             "POST", {}, token=TOKEN)
            res("运行期间被改 → 确认被拒", code == 409 and not (cf2 or {}).get("ok"),
                str(cf2)[:90])
            with open(real2, encoding="utf-8") as f:
                res("人工改动未被覆盖", "等待期间手动改的一行" in f.read())
            shutil.rmtree(tmp2, ignore_errors=True)
        else:
            skip("运行期间被改 → 确认被拒",
                 "模型两次都未跑成待确认；该防护另有 2 条离线单测覆盖")

        print("\n— 审计 —")
        code, aud = http("http://127.0.0.1:%d/audit?limit=50" % GW_PORT, timeout=30)
        entries = (aud or {}).get("entries") if isinstance(aud, dict) else None
        res("审计有本轮记录", bool(entries), "共 %d 条" % (len(entries or [])))
        think_note = any(e.get("stripped_think") for e in (entries or []))
        if args.adapter == "qwen-coder":
            res("对不收 think 的模型自动摘掉 think", think_note,
                "审计里 stripped_think=true")

    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("\n沙箱保留在：%s" % tmp)

    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n合计：%d/%d 通过%s" % (ok, len(RESULTS), "  ✓" if ok == len(RESULTS) else "  ✗"))
    for n, o, d in RESULTS:
        if not o:
            print("  ✗ %s  %s" % (n, d))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""micro_bench.py —— 入门第一版 mini-base：同一道题、同一个沙箱，在 N 个模型上各跑一次。

不是真基准（需要 20 道题 + best-of-5 等才算），但**首批数据点**足以判断
每个模型当前在工单链上的可用性，并把「推荐画像」从硬编码变成有据可依。

跑法：python scripts/micro_bench.py
      python scripts/micro_bench.py --models qwen2.5-coder:7b mythos-v2-8b:q4_k_m

需要：
  · 服务起来（默认 8090）+ Ollama 在线（11434）
  · 装了被测模型
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CALC = '''# -*- coding: utf-8 -*-
"""micro-bench 沙箱。"""
from typing import List


def average(nums: List[int]) -> float:
    """平均：空列表返回 0.0。"""
    raise NotImplementedError("待实现")


def main() -> None:
    print(average([1, 2, 3]))


if __name__ == "__main__":
    main()
'''
TEST = '''# -*- coding: utf-8 -*-
import unittest
import calc


class T(unittest.TestCase):
    def test_basic(self): self.assertAlmostEqual(calc.average([1, 2, 3]), 2.0)
    def test_empty(self): self.assertEqual(calc.average([]), 0.0)


if __name__ == "__main__":
    unittest.main()
'''


def http(url, method="GET", body=None, token=""):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Local-Ide-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with op.open(req, timeout=600) as r:
            text = r.read().decode()
            return r.status, json.loads(text) if text.strip().startswith(("{", "[")) else text
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, ""


def wait_for_terminal(wo, token, timeout=420):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = http(f"http://127.0.0.1:8090/wo/{wo}", token=token)
        st = (s or {}).get("status")
        if st in ("awaiting_confirm", "done", "escalated", "needs_input", "failed",
                  "security_abort", "cancelled"):
            return st, s
        time.sleep(0.5)
    return None, None


def confirm_if_ready(wo, token):
    code, body = http(f"http://127.0.0.1:8090/wo/{wo}/confirm", "POST", token=token)
    return code, body


def pre_commit_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            h.update(c)
    return h.hexdigest()


def pick_adapter(model: str) -> str:
    """按模型名选画像 key：正文通道模型必须用自己的画像，否则 hint/extract 全丢。
    （2026-09-30 实测坑：glm4:9b 写死 mythos 画像 → hint 没进提示 → edit 输出一团糟。）"""
    import sys, os
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _p in (os.path.join(_root, 'core'), os.path.join(_root, 'core', 'compatibility')):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from mythos_core.profiles import PROFILE_BY_MODEL, load_generated_profiles
    load_generated_profiles()
    return PROFILE_BY_MODEL.get(model, 'mythos')


def run_one(model, token, log):
    wd = tempfile.mkdtemp(prefix="bench-")
    log.write("model: %s | workdir: %s\n" % (model, os.path.basename(wd)))
    with open(os.path.join(wd, "calc.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CALC)
    with open(os.path.join(wd, "test_calc.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(TEST)
    sha_pre = pre_commit_sha(os.path.join(wd, "calc.py"))
    code, body = http("http://127.0.0.1:8090/wo", "POST", {
        "kind": "code",
        "task": "在 calc.py 中实现 average(nums)：返回整数列表的平均值，空列表返回 0.0。把 average 现有的函数体替换掉（不要留下旧代码），确保 test_calc.py 全部通过。",
        "workdir": wd, "target": "calc.py", "test_path": "test_calc.py",
        "adapter": pick_adapter(model), "model": model, "require_confirm": True,
    }, token=token)
    if code != 200:
        log.write("  -> 建单失败 code=%s body=%s\n" % (code, body))
        shutil.rmtree(wd, ignore_errors=True)
        return None, None
    wo = body["wo_id"]
    t0 = time.time()
    st, snap = wait_for_terminal(wo, token)
    elapsed = time.time() - t0
    log.write("  -> status=%s elapsed=%.1fs\n" % (st, elapsed))
    gate_signals = [(g["gate"], g["signal"]) for g in (snap or {}).get("gates", [])]

    if st == "awaiting_confirm":
        code, body = confirm_if_ready(wo, token)
        log.write("  -> confirm code=%s ok=%s\n" % (code, (body or {}).get("ok")))
        ok = code == 200 and (body or {}).get("ok") is True
        sha_post = pre_commit_sha(os.path.join(wd, "calc.py"))
        changed = sha_post != sha_pre
        log.write("  -> file changed: %s (sha %s -> %s)\n" % (changed, sha_pre[:10], sha_post[:10]))
        content = open(os.path.join(wd, "calc.py"), encoding="utf-8").read()
        has_impl = "return " in content and "NotImplementedError" not in content
        # 验证测试真跑过了：刚 workdir 里独立跑一次（防止\"测试未过但 gate 假绿\"）
        import subprocess as sp
        p = sp.run([sys.executable, "-m", "unittest", "discover", "-s", ".", "-t", ".", "-p", "test_calc.py"],
                   cwd=wd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        tests_pass = p.returncode == 0
        log.write("  -> real unittest returncode=%s\n" % p.returncode)
        shutil.rmtree(wd, ignore_errors=True)
        return ok and changed and has_impl and tests_pass, {"elapsed": elapsed, "gates": gate_signals}

    shutil.rmtree(wd, ignore_errors=True)
    return False, {"elapsed": elapsed, "status": st, "gates": gate_signals}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="入门级 mini-bench")
    ap.add_argument("--models", nargs="*",
                    default=["qwen2.5-coder:7b", "gemma4:e4b", "mythos-v2-8b:q4_k_m"])
    ap.add_argument("--out", default="micro_bench_result.json")
    args = ap.parse_args(argv)

    # 拿令牌（服务应已起来；用同一个 .runtime/service-token）
    tf = os.path.join(ROOT, ".runtime", "service-token")
    if not os.path.isfile(tf):
        print("服务未起，或令牌文件未生成：%s" % tf)
        return 2
    token = open(tf, encoding="utf-8").read().strip()

    log_path = os.path.join(ROOT, ".runtime", "micro_bench.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    summary = []
    with open(log_path, "w", encoding="utf-8") as log:
        print("=" * 78)
        print("  model                                   pass   sec   gates")
        print("-" * 78)
        for m in args.models:
            ok, info = run_one(m, token, log)
            gates = "/".join(g[1][0] for g in (info or {}).get("gates", [])) or "-"
            elapsed = (info or {}).get("elapsed", 0)
            mark = "PASS" if ok else "FAIL"
            print(f"  {m:<40} {mark}    {elapsed:>5.1f}  {gates}")
            summary.append({"model": m, "ok": ok, "info": info})
        print("=" * 78)

    out = os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "models": summary}, f, ensure_ascii=False, indent=2)
    print("\n结果已写：%s  日志：%s" % (out, log_path))
    print("用法：cat %s | jq ." % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
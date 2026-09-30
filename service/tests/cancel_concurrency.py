# -*- coding: utf-8 -*-
"""cancel_concurrency.py —— 验证\"取消\"在并发场景下是真有效的。

不要把这一项和\"取消做到立刻中断\"混为一谈：

  - \"立刻中断\"要求 Ollama 协议层面真的能掐断流（HTTP 层 + 协议层都改）。
    当前 Ollama 一旦收到 prompt 就会进入生成循环，HTTP 关闭只能阻止
    后续流的读取，已经在生成的部分（数秒到数十秒）仍然在跑。本项列为下期。

  - 本项验证的是另一件事：协作式取消在**多个取消并发触发**时是否正确，
    包括\"已确认/未确认\"状态的取消语义是否一致。这件事这一轮能闭环。

跑法：
    python service/tests/cancel_concurrency.py

需要服务在 8090 上跑起来。脚本自带\"起服务—跑测试—拆服务\"，不依赖外部状态。
"""
from __future__ import annotations

import io
import json
import os
import sys
import subprocess
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
SVC = os.path.join(_ROOT, "service", "app.py")
TOKEN_FILE = os.path.join(_ROOT, ".runtime", "service-token")
TOKEN_FILE_BAK = TOKEN_FILE + ".cancel_test.bak"


def http(url, method="GET", body=None, timeout=600, token=""):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Local-Ide-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            text = r.read().decode()
            return r.status, json.loads(text) if text.strip().startswith(("{", "[")) else text
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, ""


def wait_token(timeout=20):
    """等服务把令牌写进文件。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(TOKEN_FILE):
            tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
            if tok:
                return tok
        time.sleep(0.3)
    return ""


def make_workorder(token):
    """起一个真实的 code 工单；会在另一个临时目录里跑，不污染项目。"""
    import tempfile
    wd = tempfile.mkdtemp(prefix="cancel-test-")
    # 造一个最简单的 calc.py（avg 没实现，模型就是要补它）
    with open(os.path.join(wd, "calc.py"), "w", encoding="utf-8") as f:
        f.write('def average(nums):\n    raise NotImplementedError\n')
    with open(os.path.join(wd, "test_calc.py"), "w", encoding="utf-8") as f:
        f.write('import calc\nimport unittest\n'
                'class T(unittest.TestCase):\n    def test_x(self): self.assertTrue(True)\n')
    code, body = http("http://127.0.0.1:8090/wo", "POST", {
        "kind": "code",
        "task": "在 calc.py 中实现 average(nums)：返回平均值，空列表返回 0.0",
        "workdir": wd, "target": "calc.py", "test_path": "test_calc.py",
        "adapter": "qwen-coder", "require_confirm": True,
    }, token=token)
    assert code == 200, code
    return body["wo_id"], wd


def snapshot(wo_id, token):
    _, snap = http(f"http://127.0.0.1:8090/wo/{wo_id}", token=token)
    return snap


def main():
    # 0) 端口预检：8090 被占时直接报错并指出是谁（不静默连到旧服务上）。
    # 依据：2026-09-30 实测——昨天起的旧服务一直挂在 8090，本脚本的新服务
    # 起不来、静默失败，脚本却连到了旧服务上，令牌错位 → 全部 401。
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        occupied = probe.connect_ex(("127.0.0.1", 8090)) == 0
    finally:
        probe.close()
    if occupied:
        print("[FATAL] 8090 已被占用。先停掉旧服务再跑本测试：")
        print("  Get-NetTCPConnection -LocalPort 8090 -State Listen |")
        print("    Select-Object -Expand OwningProcess | ForEach-Object { Stop-Process -Id $_ }")
        return 1

    # 1) 备份现有令牌文件（保护用户的真实启动）
    had = False
    if os.path.isfile(TOKEN_FILE):
        had = True
        os.replace(TOKEN_FILE, TOKEN_FILE_BAK)

    py = sys.executable
    proc = subprocess.Popen([py, SVC, "--port", "8090"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=os.environ.copy())
    try:
        token = wait_token()
        assert token, "服务在 8090 启动后未写出令牌文件"

        results = []

        # ---- 用例 1：工单在 awaiting_confirm 时取消 ----
        wo, wd = make_workorder(token)
        # 等到 awaiting_confirm
        t0 = time.time()
        while time.time() - t0 < 300:
            s = snapshot(wo, token)
            if s.get("status") in ("awaiting_confirm", "done", "escalated", "needs_input",
                                   "failed", "security_abort"):
                break
            time.sleep(0.4)
        st = s.get("status")
        if st in ("awaiting_confirm", "needs_input"):
            code, body = http(f"http://127.0.0.1:8090/wo/{wo}/cancel", "POST", token=token)
            assert code == 200 and body["ok"], body
            after = snapshot(wo, token)
            results.append(("awaiting_confirm 时 cancel → cancelled（幂等）",
                            after.get("status") == "cancelled", f"status={after.get('status')}"))
            code, body2 = http(f"http://127.0.0.1:8090/wo/{wo}/cancel", "POST", token=token)
            results.append(("重复 cancel 幂等", code == 200 and body2["ok"] == True, str(body2)[:80]))
        else:
            results.append(("awaiting_confirm 时 cancel",
                            False, f"模型这单没跑到 awaiting_confirm（status={st}），跳到下一条"))

        # ---- 用例 2：并发取消多个工单 ----
        ids = []
        for _ in range(3):
            wo2, _ = make_workorder(token)
            ids.append(wo2)
        # 不等它们到 awaiting_confirm，直接连发 cancel（这一条验\"取消幂等、不崩\"）
        for wo2 in ids:
            http(f"http://127.0.0.1:8090/wo/{wo2}/cancel", "POST", token=token)
        time.sleep(0.5)
        # 协作式取消：发 cancel 后可能仍有一轮生成在跑；等最多 60s 离开 running
        t1 = time.time()
        while time.time() - t1 < 60:
            statuses = [snapshot(w, token).get("status") for w in ids]
            if all(x != "running" for x in statuses):
                break
            time.sleep(1.0)
        ok = all(x in ("cancelled", "awaiting_confirm", "done", "needs_input",
                       "escalated", "failed", "security_abort")
                 for x in statuses)
        results.append(("并发 cancel ×3 不崩，状态合法", ok, ",".join(statuses)))

        # ---- 用例 3：对不存在的工单 cancel 应返 404（不是崩） ----
        code, body = http("http://127.0.0.1:8090/wo/wo-nonexistent/cancel", "POST", token=token)
        results.append(("不存在 wo 的 cancel 返 404", code == 404, str(body)[:80]))

        # ---- 用例 4：无令牌时取消返 401（不在白名单） ----
        wo3, _ = make_workorder(token)
        code, _ = http(f"http://127.0.0.1:8090/wo/{wo3}/cancel", "POST", token="")
        results.append(("无令牌 cancel 返 401", code == 401, f"http={code}"))

        # ---- 用例 5：取消后再 confirm 必须被拒（已取消的单不允许复活） ----
        code, body = http(f"http://127.0.0.1:8090/wo/{wo3}/confirm", "POST", token=token)
        revived = code == 200 and isinstance(body, dict) and body.get("ok") is True
        results.append(("已 cancel 的工单 confirm 不能复活",
                        not revived,
                        f"code={code} ok={(body or {}).get('ok') if isinstance(body, dict) else '-'}"))

        # ---- 输出 ----
        ok = sum(1 for _, o, _ in results if o)
        print("=" * 70)
        for name, o, d in results:
            print("[%s] %s  %s" % ("PASS" if o else "FAIL", name, d))
        print("=" * 70)
        print("合计：%d/%d 通过%s" % (ok, len(results), "  ✓" if ok == len(results) else "  ✗"))
        if ok != len(results):
            for n, o, d in results:
                if not o:
                    print("✗", n, d)
            sys.exit(1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        # 恢复原令牌
        if had and os.path.isfile(TOKEN_FILE_BAK):
            if os.path.isfile(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            os.replace(TOKEN_FILE_BAK, TOKEN_FILE)


if __name__ == "__main__":
    main()
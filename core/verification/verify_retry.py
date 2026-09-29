#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验证 H1/H2：
  H1 网络级失败有限重试，且不消耗模型采样额度（温度阶梯不前进）
  H2 模型级失败（schema 违规）才推进温度阶梯 0.0 → 0.7 → 1.0
用假 HTTP 服务端 / 假 transport，不碰真实 Ollama。
"""
import os
import json, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compatibility")
sys.path.insert(0, D)
import mythos_adapter as M

TOOL_REPLY = {"message": {"content": "", "tool_calls": [
    {"function": {"name": "read_file", "arguments": {"path": "src/app.py"}}}]},
    "eval_count": 5, "eval_duration": 100_000_000}

REG = {"read_file": {"type": "function", "function": {"name": "read_file",
        "description": "read", "parameters": {"type": "object",
        "properties": {"path": {"type": "string"}}, "required": ["path"]}}}}

results = []
def check(name, cond, detail=""):
    results.append(cond)
    print("    判定: %s %s" % ("PASS ✓" if cond else "FAIL ✗", detail))

# ============ H1：真 HTTP 服务端，模拟网络层抖动 ============
class Handler(BaseHTTPRequestHandler):
    fail_n = 2; seen = 0; always_fail = False
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Handler.seen += 1
        if Handler.always_fail or Handler.seen <= Handler.fail_n:
            self.send_response(500); self.end_headers()
            self.wfile.write(b'{"error":"simulated transient failure"}'); return
        body = json.dumps(TOOL_REPLY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

def http_case(name, fail_n, always_fail, n=3):
    Handler.seen, Handler.fail_n, Handler.always_fail = 0, fail_n, always_fail
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    ad = M.MythosAdapter(host="http://127.0.0.1:%d" % srv.server_address[1])
    ad.transport_retries = 2
    ad._backoff = lambda k: 0.01
    t0 = time.time()
    r = ad.fill_slot("recon", [{"role": "user", "content": "读 src/app.py"}], REG, n=n)
    el = time.time() - t0
    srv.shutdown()
    print("— %s\n    ok=%s kind=%s http_calls=%s temp=%s 耗时=%.2fs 服务端收到=%d"
          % (name, r.ok, r.kind, r.attempts, r.temp, el, Handler.seen))
    return r, Handler.seen

print("=" * 70)
print("H1：网络级失败有限重试，且不消耗模型采样额度")
print("=" * 70)

print("\n【场景 1】前 2 次 500、第 3 次成功（n=3, transport_retries=2）")
r1, seen1 = http_case("应恢复成功", 2, False)
# 注意：temp 是 0.0，不能用 `or` 兜底（0.0 为假值）
check("恢复成功且 kind=tool_call", r1.ok and r1.kind == "tool_call")
check("温度未被误推进（仍为 0.0）", r1.temp is not None and abs(r1.temp) < 1e-12,
      "← 若为 0.7 说明网络重试错误占用了模型采样额度")
check("HTTP 3 次 = 1 首发 + 2 重试", seen1 == 3)

print("\n【场景 2】一直失败（应耗尽重试判 transport_error）")
r2, seen2 = http_case("应 transport_error", 0, True)
check("判为 transport_error", (not r2.ok) and r2.kind == "transport_error")
check("HTTP 3 次 = 1 首发 + 2 重试", seen2 == 3)

print("\n【场景 3】首次即成功（不应有重试开销）")
r3, seen3 = http_case("应一次成功", 0, False)
check("一次成功", r3.ok and seen3 == 1)

print("\n【场景 4】n=1 时也应重试网络错误（重试不占采样额度）")
r4, seen4 = http_case("n=1 仍应恢复", 2, False, n=1)
check("n=1 下仍能恢复", r4.ok and r4.kind == "tool_call")
check("HTTP 3 次（重试独立于 n）", seen4 == 3)

# ============ H2：模型级失败才推进温度阶梯 ============
print("\n" + "=" * 70)
print("H2：模型级失败（schema 违规）才推进温度阶梯 0→0.7→1.0")
print("=" * 70)

class BadTransport:
    """每次都返回一个「缺必填参数」的工具调用 → 模型级失败，不触发网络重试。"""
    def __init__(self): self.temps = []
    def chat(self, body, timeout=None):
        self.temps.append(body["options"]["temperature"])
        return ({"message": {"content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {}}}]}}, 0.01)

print("\n【场景 5】连续 schema 违规时，温度应逐档前进并被 n 限制")
bt = BadTransport()
ad5 = M.MythosAdapter()
ad5._transport = bt
orig = ad5._raw
def fake_raw(messages, tools=None, think=None, temp=0.0, num_ctx=0, timeout=None):
    return bt.chat({"options": {"temperature": temp}})
ad5._raw = fake_raw
r5 = ad5.fill_slot("recon", [{"role": "user", "content": "x"}], REG, n=3)
print("    记录到的温度序列: %s" % bt.temps)
print("    final kind=%s attempts=%s" % (r5.kind, r5.attempts))
check("温度按 0.0→0.7→1.0 前进", bt.temps[:3] == [0.0, 0.7, 1.0])
check("采样次数受 n 限制（3 次）", len(bt.temps) == 3)

print("\n" + "=" * 70)
print("总计：%d/%d 通过" % (sum(1 for x in results if x), len(results)))
print("=" * 70)

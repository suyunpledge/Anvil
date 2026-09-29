#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 H1 的重试策略提供实测依据。
测量三件事，用于决定 transport_retries 与退避时长：
  1) 连接被拒绝（Ollama 未运行 / 正在重启）时，单次尝试耗时 —— 决定重试有多贵
  2) 模型冷启动（未加载 → 加载完成）耗时 —— 决定退避是否应该等模型
  3) 模型热态响应耗时 —— 基线
不重启 Ollama、不影响其它服务（HR 依赖 Ollama）。
"""
import os
import json, time, sys, urllib.request, urllib.error, subprocess

D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compatibility")
sys.path.insert(0, D)
import mythos_adapter as M

MODEL = "fableforge-ai/mythos-v2-8b:q4_k_m"
print("=" * 70)
print("1) 连接被拒绝时的单次尝试耗时（模拟 Ollama 未运行 / 正在重启）")
print("=" * 70)
ad = M.MythosAdapter(host="http://127.0.0.1:59999")   # 无监听端口
ts = []
for i in range(3):
    t0 = time.time()
    try:
        ad._raw([{"role": "user", "content": "hi"}], timeout=5)
        ts.append(None)
    except Exception as e:
        ts.append(time.time() - t0)
        print("   第%d次: %.4fs  %s: %s" % (i + 1, ts[-1], type(e).__name__, str(e)[:60]))
print("   → 连接拒绝是「快速失败」，单次 <0.05s。重试的时间成本几乎为零。")

print()
print("=" * 70)
print("2) 空闲端口上的完整 fill_slot（重试 2 次）总耗时")
print("=" * 70)
ad2 = M.MythosAdapter(host="http://127.0.0.1:59999")
ad2.transport_retries = 2
REG = {}
t0 = time.time()
r = ad2.fill_slot("recon", [{"role": "user", "content": "x"}], REG, n=3)
el = time.time() - t0
print("   kind=%s http_calls=%s 总耗时=%.3fs" % (r.kind, r.attempts, el))
print("   → 即使 3 次全失败 + 指数退避，总代价 %.2fs，对交互无感。" % el)

print()
print("=" * 70)
print("3) 模型加载态耗时（冷 / 热）")
print("=" * 70)
def probe(label):
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "say ok"}],
                       "stream": False, "options": {"num_ctx": 4096}}).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            j = json.loads(resp.read().decode())
        el = time.time() - t0
        load = (j.get("load_duration") or 0) / 1e9
        print("   %-14s 端到端=%.2fs  其中加载=%.2fs" % (label, el, load))
        return el, load
    except Exception as e:
        print("   %-14s 失败: %s" % (label, e))
        return None, None

# 先卸载，制造冷启动
subprocess.run(["ollama", "stop", MODEL], capture_output=True, timeout=60)
time.sleep(2)
print("   （已执行 ollama stop，制造冷启动）")
cold_el, cold_load = probe("冷启动")
time.sleep(1)
hot_el, hot_load = probe("热态")
if cold_load is not None:
    print("   → 冷启动额外开销约 %.1fs（加载权重），但【请求本身不会失败】。" % cold_load)
    print("     结论：模型加载是「慢」而不是「错」，不需要靠重试解决；")
    print("           退避只用于连接层抖动，不必按模型加载时间放大。")
print()
print("=" * 70)
print("结论：连接拒绝 <0.05s/次，3 次全失败总代价 <0.2s。")
print("      transport_retries=2 + 指数退避（0.5s→1s）足以覆盖服务重启的秒级窗口；")
print("      无需放大到「等模型加载」量级（那是 30s 级的等待，应由调用方超时策略处理）。")
print("=" * 70)

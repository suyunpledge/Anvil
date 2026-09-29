#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
严格隔离：默认 opener 是否真的把 localhost 请求交给了系统代理？
方法：把 HTTP_PROXY/HTTPS_PROXY 指向一个必定失败的端口。
  - 若默认 opener 走代理 → 请求会失败或显著变慢
  - 若显式 ProxyHandler({}) 绕过代理 → 请求仍应成功
再对比真实调用耗时，并隔离「连接拒绝 2s」的真实来源（裸 socket）。
"""
import json, os, socket, time, urllib.request

LIVE = "http://127.0.0.1:11434/api/tags"
BODY = json.dumps({"model": "fableforge-ai/mythos-v2-8b:q4_k_m",
                   "messages": [{"role": "user", "content": "hi"}],
                   "stream": False, "options": {"num_ctx": 2048}}).encode()
CHAT = "http://127.0.0.1:11434/api/chat"

print("当前环境变量代理：", {k: v for k, v in os.environ.items()
                          if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")})
print("urllib 探测到的系统代理：", urllib.request.getproxies())
print()

print("=" * 74)
print("A. 裸 socket 连接被拒绝端口（隔离 TCP 层行为）")
print("=" * 74)
ts = []
for i in range(3):
    t0 = time.time()
    s = socket.socket(); s.settimeout(5)
    try: s.connect(("127.0.0.1", 59999))
    except Exception as e: err = type(e).__name__
    finally: s.close()
    ts.append(time.time() - t0)
    print("   第%d次: %.4fs  %s" % (i + 1, ts[-1], err))
print("   → 裸 socket 层面连接拒绝耗时 %.3fs（这才是 TCP 真实成本）" % (sum(ts) / 3))

print()
print("=" * 74)
print("B. 隔离验证：把代理指向死端口，看默认 opener 是否被影响")
print("=" * 74)
dead_proxy = "http://127.0.0.1:1"
def try_url(label, opener):
    t0 = time.time()
    try:
        r = opener(LIVE, timeout=6)
        with r: r.read()
        print("   %-46s 成功  %.3fs" % (label, time.time() - t0))
        return True
    except Exception as e:
        print("   %-46s 失败  %.3fs  %s" % (label, time.time() - t0, type(e).__name__))
        return False

os.environ["HTTP_PROXY"] = dead_proxy
os.environ["HTTPS_PROXY"] = dead_proxy
os.environ["ALL_PROXY"] = dead_proxy
print("   （已把 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 指向 %s）" % dead_proxy)
ok_default = try_url("默认 opener（urlopen）", lambda u, timeout: urllib.request.urlopen(u, timeout=timeout))
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ok_noproxy = try_url("显式禁用代理 ProxyHandler({})", lambda u, timeout: op.open(u, timeout=timeout))
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

print()
print("   → 判定：%s" % (
    "默认 opener 确实走系统代理（被死代理拖垮或失败）" if not ok_default and ok_noproxy
    else "默认 opener 未受代理影响" if ok_default else "两者都失败，需进一步查"))

print()
print("=" * 74)
print("C. 真实对话调用耗时对比（同一次热态模型）")
print("=" * 74)
def chat(label, opener):
    ts = []
    for i in range(3):
        req = urllib.request.Request(CHAT, data=BODY,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with opener(req, timeout=180) as r: r.read()
            ts.append(time.time() - t0)
        except Exception as e:
            print("   %s 第%d次失败 %s" % (label, i + 1, e)); return None
    avg = sum(ts) / len(ts)
    print("   %-34s 平均 %.3fs   (各次: %s)" % (label, avg, ", ".join("%.2f" % x for x in ts)))
    return avg

d = chat("默认 opener", lambda r, timeout: urllib.request.urlopen(r, timeout=timeout))
n = chat("ProxyHandler({})", lambda r, timeout: op.open(r, timeout=timeout))
if d and n:
    print()
    print("   → 默认 opener 每次多花 %.3fs，是禁用代理的 %.1f 倍" % (d - n, d / n))
print("=" * 74)

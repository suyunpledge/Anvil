#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证假设：每次请求约 2s 的固定开销来自 urllib 在 Windows 上的代理探测。
对「出网必须关死」的本地框架，这既是延迟问题，也是安全问题
（默认 opener 可能把请求交给系统代理）。
对比三种 opener：
  A 默认（urllib.request.urlopen）
  B ProxyHandler({}) 显式禁用代理
  C build_opener(ProxyHandler({})) 复用 opener
测两处：连接拒绝（无监听端口）+ 真实 Ollama 成功调用。
"""
import json, time, urllib.request, urllib.error

URL_DEAD = "http://127.0.0.1:59999/api/chat"
URL_LIVE = "http://127.0.0.1:11434/api/chat"
BODY = json.dumps({"model": "fableforge-ai/mythos-v2-8b:q4_k_m",
                   "messages": [{"role": "user", "content": "hi"}],
                   "stream": False, "options": {"num_ctx": 2048}}).encode()

def req(url):
    return urllib.request.Request(url, data=BODY, headers={"Content-Type": "application/json"})

def timeit(fn, n=3):
    ts = []
    for _ in range(n):
        t0 = time.time()
        try: fn()
        except Exception: pass
        ts.append(time.time() - t0)
    return sum(ts) / len(ts)

# --- A 默认 opener ---
def a_dead(): urllib.request.urlopen(req(URL_DEAD), timeout=5)
def a_live():
    with urllib.request.urlopen(req(URL_LIVE), timeout=120) as r: r.read()

# --- B 显式禁代理（每次新建）---
def b_dead():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    op.open(req(URL_DEAD), timeout=5)
def b_live():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req(URL_LIVE), timeout=120) as r: r.read()

# --- C 复用 opener ---
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def c_dead(): _op.open(req(URL_DEAD), timeout=5)
def c_live():
    with _op.open(req(URL_LIVE), timeout=120) as r: r.read()

# 探测系统代理设置（看是否真的存在代理配置）
print("系统代理探测结果：", urllib.request.getproxies())
print()
print("=" * 72)
print("%-34s %-14s %s" % ("场景", "平均耗时", "说明"))
print("=" * 72)
rows = [
    ("A 默认 opener / 连接拒绝", timeit(a_dead), "含代理探测开销"),
    ("B ProxyHandler({}) / 连接拒绝", timeit(b_dead), "每次新建 opener"),
    ("C 复用 opener / 连接拒绝", timeit(c_dead), "opener 复用"),
    ("", None, ""),
    ("A 默认 opener / 真实调用", timeit(a_live, 2), ""),
    ("B ProxyHandler({}) / 真实调用", timeit(b_live, 2), "每次新建"),
    ("C 复用 opener / 真实调用", timeit(c_live, 2), ""),
]
for name, t, note in rows:
    if t is None: print(); continue
    print("%-34s %-14s %s" % (name, "%.3fs" % t, note))
print("=" * 72)

# -*- coding: utf-8 -*-
"""semantic_tidy.py —— 「按内容归类」的辅助（本地嵌入，**零外网**）。

用户 2026-09-28 的优化方向：文件整理现在只按扩展名/名称规则，能不能按**内容**分？

能，而且这件事只有本地做得到：把文件内容发给云端嵌入等于把文件交出去。
本机 bge-m3（1024 维）已经在跑（HR 服务用的就是它），直接调 Ollama 的
``/api/embed`` 即可——不出网、不花钱、不占额度。

它只回答一个问题：**这个文件更像属于哪个候选类别？**
把「文件摘要」与「类别描述」分别嵌入，算余弦相似度，取最高的那个。

设计取舍：
  · 只做**建议**，不下判决：相似度低于阈值就返回 \"不确定\"，交给规则或人；
  · 不读整个文件：只取文件名 + 前若干字符（够判断类型了，也不会把大文件读进内存）；
  · 失败的降级是「没有建议」，而不是报错中断——语义匹配是增强项，不是必经路。
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = "bge-m3:latest"
Opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 低于这个余弦相似度就认为「说不准」（bge-m3 的分离度见 TOOLS.md：无关约 0.5，相关约 0.75+）
DEFAULT_THRESHOLD = 0.62


def embed(texts: List[str], model: str = EMBED_MODEL, timeout: int = 60) -> Optional[List[List[float]]]:
    """批量取嵌入。失败返回 None（调用方退化成「没有建议」）。"""
    if not texts:
        return []
    body = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with Opener.open(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    vecs = j.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        return None
    return vecs


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _snippet(path: str, max_chars: int = 400) -> str:
    """取判断类型够用的一小段：文件名 + 开头若干可读字符。"""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    text_ext = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".log", ".ini", ".yml", ".yaml", ".tsv"}
    if ext not in text_ext:
        return name
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(max_chars)
    except OSError:
        return name
    return (name + " " + head.replace("\n", " ")).strip()


def suggest_categories(files: List[Dict[str, str]], categories: Dict[str, str],
                       threshold: float = DEFAULT_THRESHOLD) -> Dict[str, Dict[str, Any]]:
    """给每个文件给出「更像哪一类」的建议。

    Parameters
    ----------
    files:
        ``[{"name": ..., "path": ...}]`` —— path 是绝对路径（用于取摘要）
    categories:
        ``{类别名: 类别描述}``，例如 ``{"文档": "合同、报告、说明书等文字性文档"}``

    Returns
    -------
    ``{文件名: {"suggest": 类别名|None, "score": float, "all": {类别: 分数}}}``
    """
    if not files or not categories:
        return {}
    names = [f["name"] for f in files]
    snippets = [(_snippet(f.get("path") or f["name"]) if f.get("path") else f["name"]) for f in files]
    cat_names = list(categories.keys())
    cat_desc = [categories[c] for c in cat_names]

    vecs = embed(snippets + cat_desc)
    if vecs is None:
        return {n: {"suggest": None, "score": 0.0, "all": {}, "reason": "嵌入服务不可用"}
                for n in names}
    fv, cv = vecs[:len(snippets)], vecs[len(snippets):]

    out: Dict[str, Dict[str, Any]] = {}
    for name, v in zip(names, fv):
        scores = {c: round(cosine(v, cv[i]), 4) for i, c in enumerate(cat_names)}
        best = max(scores, key=scores.get) if scores else None
        score = scores.get(best, 0.0) if best else 0.0
        out[name] = {
            "suggest": best if score >= threshold else None,
            "score": score,
            "all": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
            "reason": ("相似度 %.3f ≥ 阈值 %.2f" % (score, threshold)) if score >= threshold
                      else ("最高仅 %.3f，低于阈值 %.2f，判为不确定" % (score, threshold)),
        }
    return out


def health() -> Dict[str, Any]:
    v = embed(["健康检查"])
    return {"ok": bool(v), "model": EMBED_MODEL, "dim": (len(v[0]) if v else 0)}

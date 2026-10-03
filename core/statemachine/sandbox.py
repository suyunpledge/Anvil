# -*- coding: utf-8 -*-
"""sandbox.py —— 跑「模型改过的代码」时的隔离措施（**v1 是缓解，不是沙箱**）。

背景（2026-09-28 外部审查指出的高危项）：
测试 gate 会用 `subprocess` 执行模型改写后的 Python。那段代码在 import 时就能做任何事——
读文件、发网络请求、读环境变量里的密钥。而原来的实现直接把 `os.environ` 全量传给了子进程。

必须先说清楚**这一层能做什么、不能做什么**，否则会给人一种虚假的安全感：

能做的（这一层做了）：
  · **清掉环境变量**：密钥、代理、cloud 凭据一律不传；HOME/TMP 指向临时目录；
  · **掐断代理**：`HTTP(S)_PROXY` 置空 + `no_proxy=*`，让「经代理外发」这条常见路径失效；
  · **限制工作目录**：子进程 cwd 就是暂存目录，相对路径跑不出去；
  · **超时与输出上限**：防止挂死与把磁盘写爆；
  · **不写字节码**、不留 `__pycache__`。

**做不到的（不要以为它做了）**：
  · 不阻止 `socket()` 直连——想真正断网需要 OS 级手段
    （Windows 用 Job Object + 防火墙规则 / 受限令牌，Linux 用 bubblewrap/unshare）；
  · 不阻止读写该用户能访问的任意文件；
  · 不限制 CPU/内存（只限时间）。
  一句话：**它挡的是"意外与顺手"，挡不住"有意为之"。**

因此本模块顶部提供一个显式开关：``strict=False`` 时只做基础清理；
``strict=True`` 时在拿不到 OS 级隔离能力的情况下，**宁可拒绝执行也不会假装安全**
（见 :func:`assert_real_isolation_available` 的说明）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

#: 一律不传给被测代码的环境变量（大小写不敏感的前缀匹配）
_DROP_PREFIXES = (
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",   # 代理：掐断外发常见路径
    "openai", "anthropic", "gemini", "google_api", "moonshot", "groq",   # 各家密钥
    "zhipu", "bigmodel", "dashscope", "stepfun", "deepseek", "openrouter",
    "aws_", "azure_", "gcp_", "aliyun", "tencent", "volc",
    "hf_", "huggingface", "wandb", "github_token", "gh_token",
    "token", "secret", "api_key", "apikey", "password", "passwd", "credential",
    "openclaw", "autoclaw", "forge_",
)
#: 明确保留的（跑 Python 必需）
_KEEP = ("systemroot", "windir", "pathext", "comspec", "number_of_processors",
         "processor_architecture", "os", "lang", "tmp", "temp")


def _drop(name: str) -> bool:
    low = name.lower()
    if low in _KEEP:
        return False
    if low in ("path", "pythonpath", "pythonhome", "pythonhome"):
        return False
    return any(p in low for p in _DROP_PREFIXES)


def sanitized_env(home: Optional[str] = None,
                  extra_allow: Tuple[str, ...] = ()) -> Dict[str, str]:
    """给被测代码一份"干净"的环境。

    保留：跑 Python 必需的最小集合（PATH/SystemRoot/TEMP…）
    丢弃：代理、各种云凭据、以及任何名字里带 token/secret/key 的变量
    覆写：HOME/USERPROFILE 指向隔离目录；no_proxy=*；禁止写字节码
    """
    sandbox_home = home or os.path.join(tempfile.gettempdir(), "local-ide-sandbox-home")
    os.makedirs(sandbox_home, exist_ok=True)
    env: Dict[str, str] = {}
    allowed_extra = {e.lower() for e in extra_allow}
    for k, v in os.environ.items():
        if k.lower() in allowed_extra:
            env[k] = v
            continue
        if _drop(k):
            continue
        env[k] = v
    # 硬覆写：代理与身份
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[k] = ""
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    env["HOME"] = sandbox_home
    env["USERPROFILE"] = sandbox_home
    env["TMPDIR"] = env.get("TEMP") or sandbox_home
    env["PYTHONDONTWRITEBYTECODE"] = "1"      # 不落 __pycache__
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 给被跑代码一个明确的信号：它在受隔离环境里
    env["LOCAL_IDE_SANDBOX"] = "1"
    return env


def dropped_names() -> List[str]:
    """当前环境里会被丢掉的变量名（用于自检与日志，不打印值）。"""
    return sorted(k for k in os.environ if _drop(k))


# ------------------------------------------------------------
# OS 级隔离能力探测
# ------------------------------------------------------------
def real_isolation_available() -> Tuple[bool, str]:
    """探测本机是否具备真正的 OS 级隔离手段。返回 (可用?, 说明)。

    2026-10-03 起 Windows 上接入了 **Job Object**（winjob.py，ctypes 零依赖）：
    Kill-on-close / 内存上限 / CPU 与墙钟上限 / UI 全禁。返回 True 表示本机具备
    「进程级硬约束」能力；**网络隔离**另由探针判定（见 winjob.probe_network_blocked），
    因为 Job Object 本身不覆盖防火墙语义。
    """
    if sys.platform.startswith("win"):
        try:
            import winjob  # 同目录，ctypes 零依赖
            return bool(winjob.IS_AVAILABLE), "Windows：Job Object 隔离可用（winjob）"
        except Exception as e:  # noqa: BLE001
            return False, "Windows：winjob 加载失败（%s）" % e
    # Linux/macOS：看常见沙箱工具
    import shutil
    for tool in ("bwrap", "sandbox-exec"):
        if shutil.which(tool):
            return False, "检测到 %s，但本版本尚未接入（需显式实现后再启用）" % tool
    return False, "未检测到可用沙箱"


def assert_real_isolation_available(strict: bool) -> None:
    """strict 模式下，如果拿不到真隔离就**拒绝执行**，而不是假装安全。"""
    if not strict:
        return
    ok, why = real_isolation_available()
    if not ok:
        raise RuntimeError(
            "严格模式要求 OS 级隔离，但本机不可用（%s）。\n"
            "  可选：① 关掉严格模式（LOCAL_IDE_SANDBOX_STRICT=0，接受'缓解而非隔离'）；\n"
            "        ② 改用容器/WSL 跑测试 gate（需要改 gates 的执行器）。" % why)


def summary() -> Dict[str, object]:
    """一行说清这层到底做了什么（给自检与文档用）。"""
    ok, why = real_isolation_available()
    job = False
    if sys.platform.startswith("win"):
        try:
            import winjob
            job = bool(winjob.IS_AVAILABLE)
        except Exception:
            job = False
    return {
        "env_sanitized": True,
        "proxy_blocked": True,
        "home_isolated": True,
        "os_isolation": ok,
        "os_isolation_note": why,
        "job_object": job,
        "drops": len(dropped_names()),
    }

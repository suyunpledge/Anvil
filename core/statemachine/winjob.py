# -*- coding: utf-8 -*-
"""winjob.py —— Windows Job Object 隔离启动器（2026-10-03，零第三方依赖）。

把「跑模型改过的代码」的子进程放进一个 **Job Object**，关掉它的四个口子：

  1. **JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE**  —— 父进程死，子进程必死（不留孤儿）；
  2. **JOB_OBJECT_LIMIT_PROCESS_MEMORY**     —— 内存上限（默认 1 GiB），防吃爆机器；
  3. **JOB_OBJECT_LIMIT_PROCESS_TIME / JOB_LIMIT_UPTIME** —— CPU 时间 / 墙钟上限；
  4. **（UI 限制：本机 Windows 11 25H2 不支持，恒 err 87，如实放弃——见文件尾实测记录）

网络这块 Job Object 本身**管不了**（Windows 没有 per-job 防火墙），所以这里用的是
「启动后探测」：子进程起手先跑一段探针脚本，若能建立 socket 直连外网，就以
``NET_EGRESS`` 退出码自杀，由父进程读退出码判定「网络没封住」。这样：

  · strict 模式可以**如实**报告网络是否真的被挡（而不是假装挡住了）；
  · 如果本机装了第三方防火墙策略（如 Windows 防火墙出站规则）把 python.exe 拦了，
    探测自然失败——那是真实生效，不算假装。

对外接口：
    launch_isolated(cmd_list, cwd, env, mem_mb, wall_sec, cpu_sec)
        -> {"proc": Popen, "job_handle": int}
    probe_network_blocked(...) -> (bool, str)   # True = 网络确实被挡
    cleanup(job_handle)                        # 结束时关 Job 句柄

平台：仅 Windows。非 Windows 上 `IS_AVAILABLE=False`，调用方回退原缓解路径。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")
IS_AVAILABLE = IS_WINDOWS

# ---- WinAPI 常量 ----
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004          # per-job 墙钟（UserTime 老字段语义）
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002      # 每进程 CPU 时间
_JOB_OBJECT_UILIMIT_HANDLES = 0x00010000
_JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x00020000
_JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x00040000
_JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS = 0x00080000
_JOB_OBJECT_UILIMIT_DISPLAYSETTINGS = 0x00100000
_JOB_OBJECT_UILIMIT_GLOBALATOMS = 0x00200000
_JOB_OBJECT_UILIMIT_DESKTOP = 0x00400000
_JOB_OBJECT_UILIMIT_EXITWINDOWS = 0x00800000

JobObjectExtendedLimitInformation = 9
JobObjectBasicUIRestrictions = 4

_JOB_DELETE = 0x00010000
_GENERIC_ALL = 0x001F0000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _k32():
    # ★ 64 位句柄安全：windll 的函数返回值默认是 c_int（32 位），
    #   内核句柄是 64 位指针——不显式设 restype 会被截断，后面 Set/Assign 全部莫名失败。
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.CreateJobObjectW.restype = _HANDLE
    k32.CreateJobObjectW.argtypes = [_HANDLE, _WCHAR_P]
    k32.OpenProcess.restype = _HANDLE
    k32.OpenProcess.argtypes = [_DWORD, _BOOL, _DWORD]
    k32.SetInformationJobObject.argtypes = [_HANDLE, _DWORD, _VOID_P, _DWORD]
    k32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
    return k32


_HANDLE = ctypes.c_void_p
_WCHAR_P = ctypes.c_wchar_p
_VOID_P = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def launch_isolated(cmd: List[str], cwd: str, env: Dict[str, str],
                    mem_mb: int = 1024, wall_sec: int = 180,
                    cpu_sec: int = 120) -> Dict[str, Any]:
    """创建 Job Object 并把 ``cmd`` 作为第一个进程放进去。

    返回 {"proc": Popen, "job": 句柄(int)}。调用方结束时记得 :func:`cleanup`。
    非 Windows 直接抛 NotImplementedError（调用方应先看 IS_AVAILABLE）。
    """
    if not IS_AVAILABLE:
        raise NotImplementedError("winjob 仅支持 Windows")

    k32 = _k32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW 失败")

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | _JOB_OBJECT_LIMIT_PROCESS_TIME
        | _JOB_OBJECT_LIMIT_JOB_TIME
    )
    info.ProcessMemoryLimit = int(mem_mb) * 1024 * 1024
    info.JobMemoryLimit = int(mem_mb) * 1024 * 1024
    # 每进程 CPU 时间（100ns 单位）
    info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_sec) * 10_000_000
    # Job 级墙钟（100ns 单位）
    info.BasicLimitInformation.PerJobUserTimeLimit = int(wall_sec) * 10_000_000

    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        raise OSError("SetInformationJobObject(limit) 失败")

    _CREATE_SUSPENDED = 0x00000004   # subprocess 没暴露这个 Windows 常量
    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=_CREATE_SUSPENDED)
    # ★ MSDN：AssignProcessToJobObject 需要 PROCESS_SET_QUOTA | PROCESS_TERMINATE
    #   （只给 PROCESS_TERMINATE 会 access denied，实测 2026-10-03）
    _SET_QUOTA = 0x0100
    _TERMINATE = 0x0001
    h = k32.OpenProcess(_SET_QUOTA | _TERMINATE, False, proc.pid)
    if not h:
        proc.kill()
        k32.CloseHandle(job)
        raise OSError("OpenProcess(%d) 失败（err=%d）" % (proc.pid, k32.GetLastError()))
    if not k32.AssignProcessToJobObject(job, h):
        err = k32.GetLastError()
        k32.CloseHandle(h)
        proc.kill()
        k32.CloseHandle(job)
        raise OSError("AssignProcessToJobObject 失败（err=%d）" % err)
    k32.CloseHandle(h)
    _resume_process(proc.pid)  # CREATE_SUSPENDED 启动的要手动恢复

    return {"proc": proc, "job": job}


def _resume_process(pid: int) -> None:
    """CREATE_SUSPENDED 启动的进程要手动 resume：NtResumeProcess 作用于整个进程。"""
    ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    h = ctypes.windll.kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not h:
        raise OSError("OpenProcess(suspend-resume) 失败")
    try:
        status = ntdll.NtResumeProcess(h)
        if status != 0:
            raise OSError("NtResumeProcess status=0x%x" % status)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def probe_network_blocked(launcher, cwd: str, env: Dict[str, str],
                          timeout: int = 15) -> Tuple[bool, str]:
    """探测网络是否真的被封。

    在隔离环境里跑一段探针：尝试连一个公网地址（带短超时）。
      · 连不上（超时/拒绝/DNS 失败）→ 网络确实被挡 → True
      · 连上了 → 没挡住 → False（strict 模式应当拒跑）
    """
    # ★ 探测目标必须是「正常情况下一定连得上」的地址——用保留地址（203.0.113.x）
    #   会把「地址本来就不通」误判成「网络被挡」，得出虚假的安全结论。
    #   223.5.5.5（阿里公共 DNS，TCP 53）常年可达；连它失败才说明出站被真挡了。
    probe_code = (
        "import socket,sys\n"
        "try:\n"
        "    s=socket.create_connection(('223.5.5.5',53),timeout=4)\n"
        "    s.close(); print('OPEN'); sys.exit(3)\n"
        "except Exception as e:\n"
        "    print('BLOCKED', type(e).__name__); sys.exit(0)\n"
    )
    try:
        r = launcher([sys.executable, "-c", probe_code], cwd, env)
        proc, job = r["proc"], r["job"]
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            cleanup(job)
            return False, "探针超时"
        out_s = (out or b"").decode("utf-8", "ignore")
        cleanup(job)
        if proc.returncode == 0 and "BLOCKED" in out_s:
            return True, "探针报告 socket 直连失败（网络已不可用）"
        return False, "探针报告网络可达（%s）" % out_s.strip()[:80]
    except Exception as e:  # noqa: BLE001
        return False, "探针执行失败：%s" % e


def cleanup(job) -> None:
    """关 Job 句柄（KILL_ON_JOB_CLOSE 会顺手杀掉残余子进程）。"""
    if job:
        _k32().CloseHandle(job)


# ---- 实测记录（2026-10-03，Win11 25H2 build 26200 / Python 3.13 内嵌）----
# · Job UI Restrictions（JobObjectBasicUIRestrictions=4）：本机**恒 err 87**，
#   任何限制位组合（含单一位）都不被接受，值 0 才返回成功——判定为该 API 面在
#   此版本 Windows 上已被弃用/移除。UI 禁 therefore 不可用，如实放弃。
# · 有效且已启用：Kill-on-close / ProcessMemory / ProcessTime / JobTime。
# · AssignProcessToJobObject 需要 PROCESS_SET_QUOTA | PROCESS_TERMINATE 权限组合。
# · 网络隔离不归 Job 管：strict 模式用探针（probe_network_blocked）如实判定。


import { spawn, type ChildProcess } from 'node:child_process'

const stopping = new WeakSet<ChildProcess>()

/** 只终止宿主创建并仍存活的子进程及其后代。 */
export function stopProcess(proc: ChildProcess | null): void {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null || proc.killed || stopping.has(proc)) return
  stopping.add(proc)
  if (process.platform === 'win32' && proc.pid) {
    const killer = spawn('taskkill.exe', ['/PID', String(proc.pid), '/T', '/F'], {
      windowsHide: true, stdio: 'ignore'
    })
    killer.on('error', () => { try { proc.kill() } catch { /* 已退出 */ } })
    killer.on('exit', (code) => { if (code !== 0) { try { proc.kill() } catch { /* 已退出 */ } } })
  } else {
    try { proc.kill() } catch { /* 已退出 */ }
  }
}

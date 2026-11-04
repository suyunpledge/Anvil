import { spawn, ChildProcess } from 'node:child_process'
import path from 'node:path'
import os from 'node:os'
import type { BrowserWindow } from 'electron'

// —— 自带终端：cmd.exe 会话宿主（stdio 管道 + 事件转发到渲染层） ——

interface TermSession {
  proc: ChildProcess
  cwd: string
  alive: boolean
}

let nextSid = 1
const sessions = new Map<number, TermSession>()
let win: BrowserWindow | null = null

export function bindWindow(w: BrowserWindow | null): void {
  win = w
}

export function startTerm(cwd: string | null): { sid: number; cwd: string } {
  const sid = nextSid++
  const startCwd = cwd && path.isAbsolute(cwd) ? cwd : os.homedir()
  // PROMPT 置空：由前端渲染自己的提示符（管道模式下统一体验）
  const proc = spawn('cmd.exe', [], {
    cwd: startCwd,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PROMPT: '' }
  })
  const s: TermSession = { proc, cwd: startCwd, alive: true }
  sessions.set(sid, s)
  const dec = new TextDecoder('utf-8', { fatal: false })
  proc.stdout?.on('data', (b) => send(sid, dec.decode(b as Buffer)))
  proc.stderr?.on('data', (b) => send(sid, dec.decode(b as Buffer)))
  proc.on('error', (e) => send(sid, `\n[终端错误] ${String(e)}\n`))
  proc.on('close', () => {
    s.alive = false
    send(sid, '\n[会话已结束（exit）]\n')
  })
  return { sid, cwd: startCwd }
}

function send(sid: number, data: string): void {
  if (win && !win.isDestroyed()) win.webContents.send('term:out', { sid, data })
}

export function writeTerm(sid: number, data: string): boolean {
  const s = sessions.get(sid)
  if (!s || !s.alive || !s.proc.stdin) return false
  s.proc.stdin.write(data)
  return true
}

export function killTerm(sid: number): boolean {
  const s = sessions.get(sid)
  if (!s) return false
  try {
    s.proc.kill()
  } catch {
    /* 已退出 */
  }
  sessions.delete(sid)
  return true
}

export function termAlive(sid: number): boolean {
  return sessions.get(sid)?.alive ?? false
}

export function shutdownAll(): void {
  for (const sid of [...sessions.keys()]) killTerm(sid)
}

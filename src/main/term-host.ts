import { spawn, ChildProcess } from 'node:child_process'
import path from 'node:path'
import os from 'node:os'
import type { BrowserWindow } from 'electron'
import { StringDecoder } from 'node:string_decoder'
import { stopProcess } from './process-host'
import { toolEnvironment } from './toolchains'
import { OutputBuffer } from './output-buffer'

// —— 自带终端：cmd.exe 会话宿主（stdio 管道 + 事件转发到渲染层） ——

interface TermSession {
  proc: ChildProcess
  cwd: string
  alive: boolean
}

let nextSid = 1
const sessions = new Map<number, TermSession>()
let win: BrowserWindow | null = null
let closing = false

export function bindWindow(w: BrowserWindow | null): void {
  win = w
}

export async function startTerm(cwd: string | null): Promise<{ sid: number; cwd: string }> {
  const sid = nextSid++
  const startCwd = cwd && path.isAbsolute(cwd) ? cwd : os.homedir()
  // PROMPT 置空：由前端渲染自己的提示符（管道模式下统一体验）
  const env = await toolEnvironment(startCwd)
  if (closing) throw new Error('IDE 正在退出')
  const proc = spawn(process.env.ComSpec || 'cmd.exe', ['/d', '/q'], {
    cwd: startCwd,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...env, PROMPT: '' }
  })
  const s: TermSession = { proc, cwd: startCwd, alive: true }
  sessions.set(sid, s)
  const output = new OutputBuffer((_stream, data) => send(sid, data))
  const stdout = new StringDecoder('utf8'), stderr = new StringDecoder('utf8')
  proc.stdout?.on('data', (b) => output.write('stdout', stdout.write(b)))
  proc.stderr?.on('data', (b) => output.write('stderr', stderr.write(b)))
  proc.stdin?.on('error', () => { /* 终端已关闭输入 */ })
  proc.stdin?.write('chcp 65001 >nul\r\n')
  proc.on('error', (e) => send(sid, `\n[终端错误] ${String(e)}\n`))
  proc.on('close', () => {
    s.alive = false
    sessions.delete(sid)
    const tail = stdout.end() + stderr.end()
    if (tail) output.write('stdout', tail)
    output.flush()
    send(sid, '\n[会话已结束（exit）]\n')
  })
  return { sid, cwd: startCwd }
}

function send(sid: number, data: string): void {
  if (win && !win.isDestroyed()) win.webContents.send('term:out', { sid, data })
}

export function writeTerm(sid: number, data: string): boolean {
  const s = sessions.get(sid)
  if (!s || !s.alive || !s.proc.stdin?.writable) return false
  s.proc.stdin.write(data)
  return true
}

export function killTerm(sid: number): boolean {
  const s = sessions.get(sid)
  if (!s) return false
  s.alive = false
  stopProcess(s.proc)
  sessions.delete(sid)
  return true
}

export function termAlive(sid: number): boolean {
  return sessions.get(sid)?.alive ?? false
}

export function shutdownAll(): void {
  closing = true
  for (const sid of [...sessions.keys()]) killTerm(sid)
}

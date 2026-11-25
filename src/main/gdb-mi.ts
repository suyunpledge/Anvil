import { spawn, type ChildProcess } from 'node:child_process'
import { createInterface } from 'node:readline'
import { stopProcess } from './process-host'

type Value = string | Fields | Value[]
type Fields = { [key: string]: Value }
export const tuple = (value: Value | undefined): Fields =>
  typeof value === 'object' && !Array.isArray(value) ? value : {}
export const str = (value: Value | undefined): string => typeof value === 'string' ? value : ''

class Reader {
  private at = 0
  constructor(private source: string) {}

  private key(): string {
    const match = /^[\w-]+=/.exec(this.source.slice(this.at))
    if (!match) throw new Error('无效的 GDB/MI 字段')
    this.at += match[0].length
    return match[0].slice(0, -1)
  }

  private value(): Value {
    const start = this.source[this.at]
    if (start === '"') {
      const match = /^"((?:[^"\\]|\\.)*)"/s.exec(this.source.slice(this.at))
      if (!match) throw new Error('无效的 GDB/MI 字符串')
      this.at += match[0].length
      const escapes: Record<string, string> = { n: '\n', t: '\t', r: '\r', b: '\b', f: '\f', v: '\v', a: '\x07' }
      return match[1].replace(/(?:\\[0-7]{1,3})+|\\./gs, (raw) => {
        if (/^\\[0-7]/.test(raw)) return Buffer.from(raw.slice(1).split('\\').map(n => parseInt(n, 8))).toString('utf8')
        return escapes[raw[1]] ?? raw[1]
      })
    }
    if (start === '{') {
      this.at += 1
      const fields = this.fields('}')
      this.expect('}')
      return fields
    }
    if (start === '[') {
      this.at += 1
      const items: Value[] = []
      while (this.source[this.at] !== ']') {
        if (/^[\w-]+=/.test(this.source.slice(this.at))) {
          const key = this.key()
          items.push({ [key]: this.value() })
        } else items.push(this.value())
        if (this.source[this.at] !== ',') break
        this.at += 1
      }
      this.expect(']')
      return items
    }
    throw new Error('无效的 GDB/MI 值')
  }

  private expect(char: string): void {
    if (this.source[this.at++] !== char) throw new Error('不完整的 GDB/MI 记录')
  }

  fields(end?: string): Fields {
    const entries: Fields = Object.create(null)
    while (this.at < this.source.length && this.source[this.at] !== end) {
      const key = this.key()
      entries[key] = this.value()
      if (this.source[this.at] !== ',') break
      this.at += 1
    }
    return entries
  }
}

export class GdbMi {
  readonly exited: Promise<void>
  private proc: ChildProcess
  private token = 0
  private closed = false
  private closing?: Promise<void>
  private stderr = ''
  private pending = new Map<number, { reply: (fields: Fields) => void; reject: (error: Error) => void }>()

  constructor(gdb: string, exe: string, cwd: string, env: NodeJS.ProcessEnv, stopped: (fields: Fields) => void) {
    this.proc = spawn(gdb, ['--quiet', '--nx', '-iex', 'set auto-load off', '--interpreter=mi2', '--args', exe],
      { cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
    const lines = createInterface({ input: this.proc.stdout!, crlfDelay: Infinity })
    lines.on('line', (line) => {
      const record = /^(?:(\d+)\^|\*)([-\w]+)(?:,(.*))?$/.exec(line)
      if (!record || (!record[1] && record[2] !== 'stopped')) return
      const slot = record[1] ? this.pending.get(Number(record[1])) : undefined
      if (record[1] && !slot) return
      try {
        const fields = new Reader(record[3] ?? '').fields()
        if (!slot) stopped(fields)
        else if (record[2] === 'error') slot.reject(new Error(str(fields.msg) || 'GDB 命令失败'))
        else slot.reply(fields)
      } catch (error) {
        slot?.reject(error instanceof Error ? error : new Error(String(error)))
      }
    })
    this.proc.stderr!.setEncoding('utf8').on('data', text => { this.stderr = (this.stderr + text).slice(-4000) })
    this.exited = new Promise((resolve) => {
      let launchError = ''
      this.proc.on('error', error => { launchError = error.message })
      this.proc.stdin!.on('error', () => { /* 进程关闭会拒绝所有待处理命令 */ })
      this.proc.once('close', () => {
        this.closed = true
        lines.close()
        const error = new Error(launchError || this.stderr.trim() || 'GDB 已退出')
        for (const slot of this.pending.values()) slot.reject(error)
        resolve()
      })
    })
  }

  command(text: string, timeout = 12000): Promise<Fields> {
    if (this.closed || !this.proc.stdin?.writable) return Promise.reject(new Error('GDB 已退出'))
    const token = ++this.token
    let timer: ReturnType<typeof setTimeout> | undefined
    return new Promise<Fields>((reply, reject) => {
      timer = setTimeout(() => reject(new Error('GDB 响应超时')), timeout)
      this.pending.set(token, { reply, reject })
      this.proc.stdin!.write(token + text + '\n', (error) => { if (error) reject(error) })
    }).finally(() => {
      clearTimeout(timer)
      this.pending.delete(token)
    })
  }

  close(): Promise<void> {
    if (this.closed) return this.exited
    if (!this.closing) {
      this.closing = (async () => {
        const fallback = setTimeout(() => stopProcess(this.proc), 1800)
        try {
          await this.command('-gdb-exit', 1500).catch(() => {})
          await this.exited
        } finally {
          clearTimeout(fallback)
        }
      })()
    }
    return this.closing
  }

  kill(): void {
    stopProcess(this.proc)
  }
}

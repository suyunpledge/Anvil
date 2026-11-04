import { spawn, ChildProcess } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { app } from 'electron'
import type { CompilerInfo, OutChunk, ExitInfo, RunStartResult } from '../shared/types'

// —— 一键运行：编译器探测 + 编译 + 运行 + 流式输出 ——

const KNOWN_DIRS = [
  'C:\\Users\\Public\\mingw64\\bin', // 本机 AnvilIDE 配套 MinGW
  'C:\\MinGW\\bin',
  'C:\\mingw64\\bin',
  'C:\\msys64\\ucrt64\\bin',
  'C:\\TDM-GCC-64\\bin'
]

interface Session {
  proc: ChildProcess | null
  file: string
  compiler: CompilerInfo | null
  startedAt: number
}

let nextSid = 1
const sessions = new Map<number, Session>()

let compilerCache: CompilerInfo[] | null = null

async function probe(cmd: string, args: string[], cwd: string): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      const p = spawn(cmd, args, { cwd, windowsHide: true, shell: false })
      let out = ''
      const timer = setTimeout(() => {
        p.kill()
        resolve(null)
      }, 6000)
      p.stdout?.on('data', (d) => (out += String(d)))
      p.stderr?.on('data', (d) => (out += String(d)))
      p.on('error', () => {
        clearTimeout(timer)
        resolve(null)
      })
      p.on('close', (code) => {
        clearTimeout(timer)
        resolve(code === 0 || out.length > 0 ? out.split('\n')[0].trim() : null)
      })
    } catch {
      resolve(null)
    }
  })
}

function firstExisting(candidates: string[]): string | null {
  for (const c of candidates) {
    if (c && fs.existsSync(c)) return c
  }
  return null
}

export async function detectCompilers(force = false): Promise<CompilerInfo[]> {
  if (compilerCache && !force) return compilerCache
  const found: CompilerInfo[] = []
  const home = os.homedir()

  async function tryTool(kind: CompilerInfo['kind'], names: string[], versionArgs: string[]): Promise<void> {
    // 1) PATH 上的可执行文件
    for (const n of names) {
      const viaPath = firstExisting(
        (process.env.PATH || '').split(';').map((d) => path.join(d.trim(), n + '.exe'))
      )
      const exe = viaPath || firstExisting(KNOWN_DIRS.map((d) => path.join(d, n + '.exe')))
      if (exe) {
        const v = await probe(exe, versionArgs, path.dirname(exe))
        if (v) {
          found.push({ kind, path: exe, version: v.slice(0, 80) })
          return
        }
      }
    }
    void home
  }

  await tryTool('gcc', ['gcc'], ['--version'])
  await tryTool('g++', ['g++'], ['--version'])
  await tryTool('python', ['python', 'python3'], ['--version'])
  await tryTool('node', ['node'], ['--version'])
  compilerCache = found
  return found
}

export function compilerFor(file: string, list: CompilerInfo[]): CompilerInfo | null {
  const ext = path.extname(file).toLowerCase()
  if (ext === '.c') return list.find((c) => c.kind === 'gcc') || null
  if (['.cpp', '.cc', '.cxx'].includes(ext)) return list.find((c) => c.kind === 'g++') || null
  if (ext === '.py') return list.find((c) => c.kind === 'python') || null
  if (['.js', '.mjs', '.cjs'].includes(ext)) return list.find((c) => c.kind === 'node') || null
  return null
}

function buildDirFor(file: string): string {
  return path.join(path.dirname(file), '.anvilide')
}

type Emit = (chunk: OutChunk | ExitInfo) => void

export async function startRun(
  file: string,
  emit: Emit,
  onCompilerMissing: (need: CompilerInfo['kind']) => void
): Promise<RunStartResult> {
  const sid = nextSid++
  const compilers = await detectCompilers()
  const compiler = compilerFor(file, compilers)
  if (!compiler) {
    const ext = path.extname(file).toLowerCase()
    const need: CompilerInfo['kind'] =
      ext === '.c' ? 'gcc' : ext === '.py' ? 'python' : ext === '.js' ? 'node' : 'g++'
    onCompilerMissing(need)
    return { sid, compileNeeded: false, compiler: null, reason: '没有找到可用的编译器/解释器' }
  }

  const bdir = buildDirFor(file)
  fs.mkdirSync(bdir, { recursive: true })

  const finish = (code: number | null, signal?: string) => {
    sessions.delete(sid)
    emit({ sid, code, signal })
  }

  const spawnRun = (cmd: string, args: string[]) => {
    const proc = spawn(cmd, args, {
      cwd: path.dirname(file),
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    })
    sessions.set(sid, { proc, file, compiler, startedAt: Date.now() })
    const dec = (b: Buffer): string => {
      const t = new TextDecoder('utf-8', { fatal: false }).decode(b)
      return t
    }
    proc.stdout?.on('data', (b) => emit({ sid, stream: 'stdout', text: dec(b as Buffer) }))
    proc.stderr?.on('data', (b) => emit({ sid, stream: 'stderr', text: dec(b as Buffer) }))
    proc.on('error', (e) => emit({ sid, stream: 'stderr', text: `\n[启动失败] ${String(e)}\n` }))
    proc.on('close', (code2, signal2) => finish(code2, signal2 ?? undefined))
  }

  if (compiler.kind === 'python' || compiler.kind === 'node') {
    spawnRun(compiler.path, [file])
    return { sid, compileNeeded: false, compiler }
  }

  // C / C++：先编译到 .anvilide 隐藏目录，成功后运行
  const base = path.basename(file).replace(/\.[^.]+$/, '')
  const exe = path.join(bdir, `${base}.exe`)
  const compileProc = spawn(
    compiler.path,
    ['-Wall', '-fexec-charset=UTF-8', file, '-o', exe],
    { cwd: path.dirname(file), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let errText = ''
  compileProc.stderr?.on('data', (b) => (errText += new TextDecoder('utf-8', { fatal: false }).decode(b as Buffer)))
  compileProc.stdout?.on('data', (b) => (errText += new TextDecoder('utf-8', { fatal: false }).decode(b as Buffer)))
  compileProc.on('error', (e) => {
    emit({ sid, stream: 'stderr', text: `[编译器启动失败] ${String(e)}\n` })
    finish(-1)
  })
  compileProc.on('close', (code2) => {
    if (code2 === 0) {
      emit({ sid, stream: 'stdout', text: '[编译成功，开始运行]\n' })
      spawnRun(exe, [])
    } else {
      emit({ sid, stream: 'stderr', text: errText || '[编译失败，且编译器没有输出错误信息]\n' })
      finish(code2)
    }
  })
  return { sid, compileNeeded: true, compiler }
}

export function sendInput(sid: number, line: string): boolean {
  const s = sessions.get(sid)
  if (!s?.proc?.stdin) return false
  s.proc.stdin.write(line.endsWith('\n') ? line : line + '\n')
  return true
}

export function killRun(sid: number): boolean {
  const s = sessions.get(sid)
  if (!s) return false
  if (s.proc && !s.proc.killed) {
    try {
      s.proc.kill()
    } catch {
      /* 已经退出 */
    }
  }
  return true
}

export function activeSessions(): number[] {
  return [...sessions.keys()]
}

export function buildRoot(): string {
  return path.join(app.getPath('userData'), 'build')
}

import { spawn, type ChildProcess } from 'node:child_process'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { GdbMi, str, tuple } from './gdb-mi'
import { readFileSmart } from './fs-service'
import { stopProcess } from './process-host'
import { languageKind, resolveCompiler, resolveGdb, toolEnvironment } from './toolchains'
import type { DebugAction, DebugSnapshot } from '../shared/types'

type Result = Pick<DebugSnapshot, 'phase' | 'message' | 'details'>
type MiFields = Awaited<ReturnType<GdbMi['command']>>

interface Session {
  view: DebugSnapshot
  emit: (state: DebugSnapshot) => void
  build?: ChildProcess
  gdb?: GdbMi
  result?: Result
  breakpoints: Map<number, string>
  busy: boolean
}

let active: Session | undefined
let nextId = 1
const actions: Record<DebugAction, string> = {
  continue: '-exec-continue',
  next: '-exec-next',
  stepIn: '-exec-step',
  stepOut: '-exec-finish'
}

function update(session: Session, change: Partial<DebugSnapshot>): void {
  session.view = { ...session.view, ...change }
  session.emit(session.view)
}

function complete(session: Session, result: Result): void {
  if (session.result) return
  session.result = result
  void session.gdb?.close()
}

function build(session: Session, exe: string, compiler: string, standard: string, charset: string,
  env: NodeJS.ProcessEnv): Promise<void> {
  return new Promise((resolve, reject) => {
    const source = session.view.file
    const proc = spawn(compiler, [standard, '-g', '-O0', '-fdiagnostics-color=never', '-finput-charset=' + charset, source, '-o', exe],
      { cwd: path.dirname(source), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    session.build = proc
    let output = ''
    let failure: Error | undefined
    const append = (text: string): void => { output = (output + text).slice(-16000) }
    proc.stdout!.setEncoding('utf8').on('data', append)
    proc.stderr!.setEncoding('utf8').on('data', append)
    proc.on('error', (error) => { failure = error })
    const timer = setTimeout(() => {
      failure = new Error('调试构建超过 60 秒')
      stopProcess(proc)
    }, 60000)
    proc.once('close', (code) => {
      clearTimeout(timer)
      session.build = undefined
      if (failure) reject(failure)
      else if (code !== 0) reject(new Error(output.trim() || 'C/C++ 编译失败'))
      else resolve()
    })
  })
}

async function addBreakpoint(session: Session, gdb: GdbMi, line: number): Promise<void> {
  if (session.breakpoints.has(line)) return
  const source = JSON.stringify(session.view.file.split(path.sep).join('/'))
  try {
    const data = await gdb.command('-break-insert --source ' + source + ' --line ' + line)
    const id = str(tuple(data.bkpt).number)
    if (!/^\d+$/.test(id)) throw new Error('GDB 未返回断点编号')
    session.breakpoints.set(line, id)
  } catch (error) {
    throw new Error('第 ' + line + ' 行无法设置断点：' + message(error))
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function paused(session: Session, gdb: GdbMi, data: MiFields): Promise<void> {
  if (session.result) return
  const reason = str(data.reason)
  if (reason === 'exited' || reason === 'exited-normally' || reason === 'exited-signalled') {
    const code = str(data['exit-code'])
    const signal = str(data['signal-name'])
    complete(session, { phase: 'ended', message: signal ? '程序因 ' + signal + ' 退出' : code ? '程序退出，退出码 ' + code : '程序执行结束' })
    return
  }

  const frame = tuple(data.frame)
  const source = str(frame.fullname) || str(frame.file)
  const line = Number(str(frame.line))
  const functionName = str(frame.func)
  const location = source && Number.isInteger(line) && line > 0
    ? { file: path.resolve(path.dirname(session.view.file), source), line, functionName }
    : undefined
  const status = reason === 'breakpoint-hit' ? '已命中断点' : reason === 'signal-received'
    ? '收到信号 ' + str(data['signal-name']) : '已暂停'
  let variables: DebugSnapshot['variables'] = []
  let details: string | undefined
  try {
    const thread = str(data['thread-id'])
    const flag = /^\d+$/.test(thread) ? ' --thread ' + thread : ''
    const result = await gdb.command('-stack-list-variables' + flag + ' --all-values')
    if (Array.isArray(result.variables)) {
      variables = result.variables.slice(0, 150).map((entry) => {
        const item = tuple(entry)
        return { name: str(item.name), value: str(item.value).slice(0, 4000), argument: str(item.arg) === '1' }
      }).filter(item => item.name)
    }
  } catch (error) {
    details = '局部变量读取失败：' + message(error)
  }
  if (!session.result) update(session, { phase: 'paused', message: status, location, variables, details })
}

async function prepare(session: Session, lines: number[]): Promise<void> {
  const source = session.view.file
  const root = path.resolve(os.tmpdir())
  let directory: string | undefined
  try {
    if (session.result) return
    if (!(await fs.stat(source)).isFile()) throw new Error('调试目标不是源文件')
    const compiler = await resolveCompiler(source)
    if (!compiler) throw new Error('找不到 GCC / G++ 编译器')
    const gdbPath = await resolveGdb(compiler)
    if (!gdbPath) throw new Error('找不到 GDB。请将安装目录的 bin 添加到 PATH，或使用带 GDB 的 MinGW 工具链。')
    const env = await toolEnvironment(path.dirname(source), compiler)
    if (session.result) return
    directory = await fs.mkdtemp(path.join(root, 'anvil-debug-'))
    const { encoding } = await readFileSmart(source)
    if (session.result) return
    const exe = path.join(directory, process.platform === 'win32' ? 'program.exe' : 'program')
    await build(session, exe, compiler.path, compiler.kind === 'gcc' ? '-std=c17' : '-std=c++17',
      encoding === 'gbk' ? 'GBK' : 'UTF-8', env)
    if (session.result) return

    const gdb = new GdbMi(gdbPath, exe, path.dirname(source), env, (data) => { void paused(session, gdb, data) })
    session.gdb = gdb
    await gdb.command('-gdb-set mi-async on')
    await gdb.command('-gdb-set pagination off')
    await gdb.command('-gdb-set print elements 32')
    if (session.result) return
    for (const line of lines) {
      if (session.result) return
      await addBreakpoint(session, gdb, line)
    }
    if (!lines.length) await gdb.command('-break-insert -t main')
    if (session.result) return
    update(session, { phase: 'running', message: '程序运行中…' })
    await gdb.command('-exec-run')
    await gdb.exited
    if (!session.result) session.result = { phase: 'error', message: 'GDB 意外退出' }
  } catch (error) {
    if (!session.result) session.result = {
      phase: 'error', message: session.view.phase === 'building' ? '无法开始调试' : '调试中断', details: message(error)
    }
  } finally {
    await session.gdb?.close()
    if (directory && path.dirname(directory) === root && path.basename(directory).startsWith('anvil-debug-')) {
      await fs.rm(directory, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }).catch(() => {})
    }
    if (active === session) active = undefined
    update(session, { ...session.result, location: undefined, variables: [] })
  }
}

export function start(file: string, requested: number[], emit: Session['emit']): number {
  if (active) throw new Error('已有调试会话，请先结束当前会话')
  const kind = languageKind(file)
  if (!path.isAbsolute(file) || (kind !== 'gcc' && kind !== 'g++')) throw new Error('请先打开可编译的 C/C++ 源文件')
  const lines = [...new Set(requested.filter(line => Number.isSafeInteger(line) && line > 0))]
  if (lines.length > 100) throw new Error('单个文件最多设置 100 个断点')
  const id = nextId++
  const session: Session = {
    view: { id, file, phase: 'building', message: '正在构建调试版本…', variables: [] },
    emit, breakpoints: new Map(), busy: false
  }
  active = session
  setImmediate(() => { void prepare(session, lines) })
  return id
}

function requirePaused(id: number): { session: Session; gdb: GdbMi } {
  const session = active
  if (session?.view.id !== id || session.view.phase !== 'paused' || !session.gdb || session.result || session.busy) {
    throw new Error('请等待程序在断点或单步位置暂停')
  }
  return { session, gdb: session.gdb }
}

export async function act(id: number, action: DebugAction): Promise<void> {
  if (!Object.hasOwn(actions, action)) throw new Error('未知的调试操作')
  const { session, gdb } = requirePaused(id)
  const previous = session.view
  update(session, { phase: 'running', message: '程序运行中…', variables: [], location: undefined, details: undefined })
  try {
    await gdb.command(actions[action])
  } catch (error) {
    if (!session.result && session.view.phase === 'running') update(session, previous)
    throw error
  }
}

export async function setBreakpoint(id: number, line: number, enabled: boolean): Promise<void> {
  if (!Number.isSafeInteger(line) || line < 1) throw new Error('断点行号无效')
  const { session, gdb } = requirePaused(id)
  session.busy = true
  try {
    if (enabled) {
      if (session.breakpoints.size >= 100 && !session.breakpoints.has(line)) throw new Error('单个文件最多设置 100 个断点')
      await addBreakpoint(session, gdb, line)
    } else {
      const breakpoint = session.breakpoints.get(line)
      if (breakpoint) await gdb.command('-break-delete ' + breakpoint)
      session.breakpoints.delete(line)
    }
  } finally { session.busy = false }
}

export function stop(id: number): void {
  const session = active
  if (!session || session.view.id !== id || session.result) return
  complete(session, { phase: 'ended', message: '已停止调试' })
  update(session, { phase: 'stopping', message: '正在停止调试…', location: undefined, variables: [] })
  stopProcess(session.build ?? null)
}

export function shutdown(): void {
  if (!active) return
  stop(active.view.id)
  active.gdb?.kill()
}

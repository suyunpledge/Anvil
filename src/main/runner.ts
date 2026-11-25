import { spawn, type ChildProcess } from 'node:child_process'
import fs from 'node:fs/promises'
import path from 'node:path'
import { StringDecoder } from 'node:string_decoder'
import { app } from 'electron'
import type { CompilerInfo, OutChunk, ExitInfo, RunStartResult } from '../shared/types'
import { stopProcess } from './process-host'
import { OutputBuffer } from './output-buffer'
import { kotlinArgs, languageKind, resolveCompiler, toolEnvironment, shutdownToolchains } from './toolchains'
export { detectCompilers, compilerFor } from './toolchains'

type Emit = (chunk: OutChunk | ExitInfo) => void
interface Session { proc: ChildProcess | null; cancelled: boolean; phase: 'preparing' | 'compile' | 'run' }
const sessions = new Map<number, Session>()
let nextSid = 1
let stopping = false

export function javaPackage(source: string): string {
  const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\r\n]*|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g, ' ')
  const match = code.match(/\bpackage\s+([\p{L}_$][\p{L}\p{N}_$]*(?:\s*\.\s*[\p{L}_$][\p{L}\p{N}_$]*)*)\s*;/u)
  return match ? match[1].replace(/\s/g, '') : ''
}

export async function startRun(file: string, emit: Emit,
  onCompilerMissing: (need: CompilerInfo['kind']) => void): Promise<RunStartResult> {
  const sid = nextSid++
  const kind = languageKind(file)
  const failure = (reason: string): RunStartResult => ({ sid, compiler: null, compileNeeded: false, reason })
  if (!kind) return failure('支持 C / C++ / Python / Java / Kotlin / JavaScript / TypeScript 源文件')
  file = path.resolve(file)
  let compiler: CompilerInfo | null
  let env: NodeJS.ProcessEnv
  try {
    if (!(await fs.stat(file)).isFile()) return failure('运行目标不是文件')
    compiler = await resolveCompiler(file)
    if (!compiler) { onCompilerMissing(kind); return failure('未找到工具链：' + kind) }
    env = await toolEnvironment(path.dirname(file), compiler)
  } catch (error) { return failure(String(error)) }
  if (stopping) return failure('IDE 正在退出')

  const session: Session = { proc: null, cancelled: false, phase: 'preparing' }
  sessions.set(sid, session)
  const output = new OutputBuffer((stream, text) => emit({ sid, stream, text }))
  let finished = false, buildDir: string | null = null
  const finish = (code: number | null, signal?: string) => {
    if (finished) return
    finished = true
    output.flush()
    sessions.delete(sid)
    emit({ sid, code, signal })
    if (buildDir) {
      const root = path.resolve(path.dirname(file), '.anvilide')
      const target = path.resolve(buildDir)
      if (path.dirname(target) === root && path.basename(target).startsWith('run-')) {
        void fs.rm(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }).catch(() => {})
      }
    }
  }
  const run = (cmd: string, args: string[]) => {
    if (session.cancelled || stopping) { finish(null, 'SIGTERM'); return }
    session.phase = 'run'
    const proc = spawn(cmd, args, { cwd: path.dirname(file), windowsHide: true,
      shell: false, stdio: ['pipe', 'pipe', 'pipe'], env })
    session.proc = proc
    const stdout = new StringDecoder('utf8'), stderr = new StringDecoder('utf8')
    let errorTail = ''
    proc.stdout?.on('data', data => output.write('stdout', stdout.write(data)))
    proc.stderr?.on('data', data => {
      const text = stderr.write(data)
      errorTail = (errorTail + text).slice(-65536)
      output.write('stderr', text)
    })
    proc.stdin?.on('error', () => {})
    proc.on('error', error => output.write('stderr', `[启动失败] ${String(error)}\n`))
    proc.on('close', (code, signal) => {
      output.write('stdout', stdout.end()); output.write('stderr', stderr.end())
      output.flush()
      if (code && compiler.kind === 'python') {
        const frames = [...errorTail.matchAll(/File "([^"]+)", line (\d+)/g)]
        const frame = frames[frames.length - 1]
        const message = errorTail.trim().split(/\r?\n/).pop()
        if (frame && message) output.write('stderr', `${frame[1]}:${frame[2]}:1: error: ${message}\n`)
      }
      finish(code, signal || undefined)
    })
  }
  const compile = (cmd: string, args: string[], launch: () => void) => {
    if (session.cancelled || stopping) { finish(null, 'SIGTERM'); return }
    session.phase = 'compile'
    const proc = spawn(cmd, args, { cwd: path.dirname(file), windowsHide: true,
      shell: false, stdio: ['ignore', 'pipe', 'pipe'], env })
    session.proc = proc
    const stdout = new StringDecoder('utf8'), stderr = new StringDecoder('utf8')
    let diagnostics = '', truncated = false, timedOut = false
    const capture = (text: string) => {
      if (diagnostics.length + text.length > 1024 * 1024) truncated = true
      diagnostics = (diagnostics + text).slice(-1024 * 1024)
    }
    proc.stdout?.on('data', data => capture(stdout.write(data)))
    proc.stderr?.on('data', data => capture(stderr.write(data)))
    const requested = Number(process.env.ANVIL_COMPILE_TIMEOUT_MS || 60000)
    const timeout = Number.isFinite(requested) && requested > 0 ? Math.min(requested, 600000) : 60000
    const timer = setTimeout(() => {
      timedOut = true
      output.write('stderr', `[编译超时] 已超过 ${timeout / 1000} 秒，正在停止编译进程。\n`)
      stopProcess(proc)
    }, timeout)
    proc.on('error', error => {
      clearTimeout(timer)
      output.write('stderr', `[编译器启动失败] ${String(error)}\n`)
      finish(-1)
    })
    proc.on('close', code => {
      clearTimeout(timer)
      if (finished) return
      capture(stdout.end() + stderr.end())
      if (truncated) output.write('stderr', '[诊断输出超过 1 MiB，仅保留末尾]\n')
      if (compiler.kind === 'java') diagnostics = diagnostics.replace(/^(.*\.java):(\d+): (error|warning):/gm, '$1:$2:1: $3:')
      // 编译成功时也保留警告，沿用前端现有的 file:line:column 格式。
      if (diagnostics) output.write('stderr', diagnostics)
      output.flush()
      if (session.cancelled || stopping || timedOut) { finish(null, timedOut ? 'TIMEOUT' : 'SIGTERM'); return }
      if (code === 0) { output.write('stdout', '[编译成功，开始运行]\n'); launch() }
      else { if (!diagnostics) output.write('stderr', '[编译失败，编译器未返回诊断]\n'); finish(code) }
    })
  }
  const prepare = async () => {
    try {
      if (session.cancelled || stopping) { finish(null, 'SIGTERM'); return }
      if (compiler.kind === 'python') { run(compiler.path, ['-u', '-X', 'utf8', file]); return }
      if (compiler.kind === 'node') { run(compiler.path, ['--enable-source-maps', file]); return }
      if (compiler.kind === 'typescript') {
        run(compiler.runtimePath!, [compiler.path, file])
        return
      }
      if (compiler.kind === 'kotlin' && path.extname(file).toLowerCase() === '.kts') {
        run(compiler.runtimePath!, [...kotlinArgs(compiler.path), '-script', file])
        return
      }
      const root = path.join(path.dirname(file), '.anvilide')
      await fs.mkdir(root, { recursive: true })
      buildDir = await fs.mkdtemp(path.join(root, 'run-'))
      if (compiler.kind === 'kotlin') {
        const jar = path.join(buildDir, 'program.jar')
        const java = compiler.runtimePath!
        compile(java, [...kotlinArgs(compiler.path), file, '-include-runtime', '-d', jar], () =>
          run(java, ['-Dfile.encoding=UTF-8', '-Dstdout.encoding=UTF-8', '-Dstderr.encoding=UTF-8', '-jar', jar]))
      } else if (compiler.kind === 'java') {
        if (!compiler.runtimePath) throw new Error('需要包含 java 与 javac 的完整 JDK')
        const pkg = javaPackage(await fs.readFile(file, 'utf8'))
        const mainClass = [pkg, path.basename(file, path.extname(file))].filter(Boolean).join('.')
        let sourceRoot = path.dirname(file)
        const segments = pkg.split('.').filter(Boolean)
        let candidate = sourceRoot
        for (const segment of [...segments].reverse()) {
          if (path.basename(candidate) !== segment) { candidate = sourceRoot; break }
          candidate = path.dirname(candidate)
        }
        sourceRoot = candidate
        compile(compiler.path, ['-encoding', 'UTF-8', '-g', '-Xlint:all', '-J-Duser.language=en', '-J-Duser.country=US',
          '-d', buildDir, '-sourcepath', sourceRoot, '-classpath', sourceRoot, file],
        () => run(compiler.runtimePath!, ['-Dfile.encoding=UTF-8', '-Dstdout.encoding=UTF-8', '-Dstderr.encoding=UTF-8',
          '-classpath', buildDir + path.delimiter + sourceRoot, mainClass]))
      } else {
        const exe = path.join(buildDir, process.platform === 'win32' ? 'program.exe' : 'program')
        // 独立的小型启动单元，不改用户源码；使 printf 提示在 scanf 前到达管道。
        const bridge = path.join(buildDir, 'stdio-bridge.c')
        await fs.writeFile(bridge, '#include <stdio.h>\n__attribute__((constructor)) static void anvil_stdio_init(void) { setvbuf(stdout, NULL, _IONBF, 0); setvbuf(stderr, NULL, _IONBF, 0); }\n')
        compile(compiler.path, [compiler.kind === 'gcc' ? '-std=c17' : '-std=c++17', '-g', '-Wall', '-Wextra',
          '-fdiagnostics-color=never', '-finput-charset=UTF-8', '-fexec-charset=UTF-8', file, bridge, '-o', exe], () => run(exe, []))
      }
    } catch (error) { output.write('stderr', `[运行准备失败] ${String(error)}\n`); finish(-1) }
  }
  // 先返回会话 ID，让 IPC 响应先于短程序的输出进入消息队列。
  setImmediate(() => { void prepare() })
  return { sid, compiler, compileNeeded: ['gcc', 'g++', 'java'].includes(compiler.kind) || (compiler.kind === 'kotlin' && path.extname(file).toLowerCase() === '.kt') }
}

export function sendInput(sid: number, line: string): boolean {
  const session = sessions.get(sid)
  if (!session || session.phase !== 'run' || session.cancelled || !session.proc?.stdin?.writable) return false
  session.proc.stdin.write(line.endsWith('\n') ? line : line + '\n')
  return true
}
export function killRun(sid: number): boolean {
  const session = sessions.get(sid)
  if (!session) return false
  session.cancelled = true; stopProcess(session.proc); return true
}
export function activeSessions(): number[] { return [...sessions.keys()] }
export function shutdownAll(): void {
  stopping = true; shutdownToolchains()
  for (const sid of sessions.keys()) killRun(sid)
}
export function buildRoot(): string { return path.join(app.getPath('userData'), 'build') }

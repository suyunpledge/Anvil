import { spawn, type ChildProcess } from 'node:child_process'
import fs from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { app } from 'electron'
import { languageKind } from '../shared/languages'
export { languageKind } from '../shared/languages'
import type { CompilerInfo } from '../shared/types'
import { stopProcess } from './process-host'

const suffix = process.platform === 'win32' ? '.exe' : ''
const known = ['C:/Users/Public/mingw64/bin', 'C:/MinGW/bin', 'C:/mingw64/bin', 'C:/msys64/ucrt64/bin', 'C:/TDM-GCC-64/bin']
const probes = new Set<ChildProcess>()
let cached: CompilerInfo[] | null = null
let cachedAt = 0
let pending: Promise<CompilerInfo[]> | null = null
let closed = false

async function exists(file: string): Promise<boolean> {
  try { return (await fs.stat(file)).isFile() } catch { return false }
}

async function children(root: string): Promise<string[]> {
  try { return (await fs.readdir(root, { withFileTypes: true })).filter(item => item.isDirectory()).map(item => path.join(root, item.name)) }
  catch { return [] }
}

async function javaBins(): Promise<string[]> {
  const programFiles = process.env.ProgramFiles || 'C:/Program Files'
  const roots = [process.env.JAVA_HOME || '', ...await children(path.join(os.homedir(), '.jdks'))]
  for (const vendor of ['Java', 'Eclipse Adoptium', 'Microsoft', 'Amazon Corretto', 'Zulu']) {
    roots.push(...await children(path.join(programFiles, vendor)))
  }
  for (const product of await children(path.join(programFiles, 'JetBrains'))) roots.push(path.join(product, 'jbr'))
  roots.push(path.join(programFiles, 'Android/Android Studio/jbr'))
  return roots.filter(Boolean).map(root => path.join(root, 'bin'))
}

async function probe(exe: string, args: string[]): Promise<string | null> {
  if (closed) return null
  return new Promise(resolve => {
    let output = '', done = false
    const proc = spawn(exe, args, { windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] })
    probes.add(proc)
    const finish = (value: string | null) => {
      if (done) return
      done = true; clearTimeout(timer); probes.delete(proc); resolve(value)
    }
    const timer = setTimeout(() => { stopProcess(proc); finish(null) }, 6000)
    proc.stdout?.on('data', chunk => { output = (output + String(chunk)).slice(0, 4096) })
    proc.stderr?.on('data', chunk => { output = (output + String(chunk)).slice(0, 4096) })
    proc.on('error', () => finish(null))
    proc.on('close', code => finish(code === 0 ? output.trim().split(/\r?\n/)[0].slice(0, 120) || null : null))
  })
}

export function detectCompilers(force = false): Promise<CompilerInfo[]> {
  if (pending) return pending
  if (cached && !force && Date.now() - cachedAt < 30000) return Promise.resolve(cached)
  pending = discover().then(result => { cached = result; cachedAt = Date.now(); return result }).finally(() => { pending = null })
  return pending
}

async function discover(): Promise<CompilerInfo[]> {
  const pathDirs = (process.env.PATH || '').split(path.delimiter).map(dir => dir.trim().replace(/^"|"$/g, '')).filter(Boolean)
  const jdks = await javaBins()
  const pythonDirs = await children(path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData/Local'), 'Programs/Python'))
  async function find(kind: CompilerInfo['kind'], names: string[], dirs: string[]): Promise<CompilerInfo | null> {
    const candidates = [...new Set(dirs.flatMap(dir => names.map(name => path.resolve(dir, name + suffix))))]
    for (const exe of candidates) {
      if (closed || !await exists(exe)) continue
      if (kind === 'python' && /[\\/]WindowsApps[\\/]/i.test(exe)) continue
      const runtimePath = kind === 'java' ? path.join(path.dirname(exe), 'java' + suffix) : undefined
      if (runtimePath && !await exists(runtimePath)) continue
      const version = await probe(exe, kind === 'java' ? ['-version'] : ['--version'])
      if (!version) continue // 无效的 PATH 候选不能挡住后续有效工具链。
      if (runtimePath && !await probe(runtimePath, ['-version'])) continue
      return { kind, path: exe, version, ...(runtimePath ? { runtimePath } : {}) }
    }
    return null
  }
  const [gcc, cpp, python, node, java] = await Promise.all([
    find('gcc', ['gcc', 'clang'], [...pathDirs, ...known]),
    find('g++', ['g++', 'clang++'], [...pathDirs, ...known]),
    find('python', ['python', 'python3'], [...pathDirs, ...pythonDirs]),
    find('node', ['node'], [...pathDirs, ...(app.isPackaged ? [path.join(process.resourcesPath, 'runtime/bin')] : []),
      path.join(process.env.ProgramFiles || 'C:/Program Files', 'nodejs')]), find('java', ['javac'], [...jdks.slice(0, process.env.JAVA_HOME ? 1 : 0), ...pathDirs, ...jdks])
  ])
  const [kotlin, typescript] = await Promise.all([
    java?.runtimePath ? findKotlin(java.runtimePath, pathDirs) : null,
    node ? findTypeScript(node.path) : null
  ])
  return [gcc, cpp, python, node, java, kotlin, typescript].filter((tool): tool is CompilerInfo => tool !== null)
}

export function kotlinArgs(jar: string): string[] {
  const lib = path.dirname(jar)
  return ['-Dfile.encoding=UTF-8', '-Dstdout.encoding=UTF-8', '-Dstderr.encoding=UTF-8',
    '-Dkotlin.home=' + path.dirname(lib), '-cp', path.join(lib, '*'), 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler']
}

async function findKotlin(java: string, pathDirs: string[]): Promise<CompilerInfo | null> {
  const local = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData/Local')
  const installed = await children(path.join(local, 'AnvilIDE/toolchains/kotlin'))
  installed.sort((a, b) => b.localeCompare(a, 'en', { numeric: true }))
  const homes = [process.env.KOTLIN_HOME, ...pathDirs.map(dir => path.dirname(dir)),
    ...installed.map(dir => path.join(dir, 'kotlinc'))]
  for (const home of new Set(homes)) {
    if (!home) continue
    const jar = path.join(home, 'lib/kotlin-compiler.jar')
    if (!await exists(jar)) continue
    const version = await probe(java, [...kotlinArgs(jar), '-version'])
    if (version) return { kind: 'kotlin', path: jar, version: version.replace(/^info: /, ''), runtimePath: java }
  }
  return null
}

async function findTypeScript(node: string): Promise<CompilerInfo | null> {
  try {
    const runtime = app.isPackaged ? path.join(process.resourcesPath, 'runtime') : app.getAppPath()
    const cli = require.resolve('tsx/cli', { paths: [runtime] })
    const version = await probe(node, [cli, '--version'])
    return version ? { kind: 'typescript', path: cli, runtimePath: node, version } : null
  } catch { return null }
}

function binDirectory(tool: CompilerInfo): string {
  if (tool.kind === 'kotlin') return path.resolve(path.dirname(tool.path), '../bin')
  if (tool.kind === 'typescript') return app.isPackaged ? path.join(process.resourcesPath, 'runtime/bin') : path.join(app.getAppPath(), 'node_modules/.bin')
  return path.dirname(tool.path)
}

export function compilerFor(file: string, tools: CompilerInfo[]): CompilerInfo | null {
  return tools.find(tool => tool.kind === languageKind(file)) || null
}

export async function projectPython(directory: string): Promise<string | null> {
  let current = path.resolve(directory)
  // 最近的项目环境优先，不穿越 Git / Python 项目边界去借用别的项目环境。
  while (true) {
    for (const folder of ['.venv', 'venv']) {
      const exe = path.join(current, folder, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
      if (await exists(exe)) return exe
    }
    for (const marker of ['.git', 'pyproject.toml', 'setup.cfg', 'setup.py', 'requirements.txt']) {
      try { await fs.stat(path.join(current, marker)); return null } catch { /* 继续寻找根 */ }
    }
    const parent = path.dirname(current)
    if (parent === current || current === os.homedir()) return null
    current = parent
  }
}

export async function resolveCompiler(file: string): Promise<CompilerInfo | null> {
  if (languageKind(file) === 'python') {
    const exe = await projectPython(path.dirname(file))
    if (exe) {
      const version = await probe(exe, ['--version'])
      if (!version) throw new Error('项目虚拟环境不可用，请修复 .venv / venv 后重试：' + exe)
      return { kind: 'python', path: exe, version }
    }
  }
  return compilerFor(file, await detectCompilers())
}

export async function resolveGdb(compiler: CompilerInfo): Promise<string | null> {
  const pathDirs = (process.env.PATH || '').split(path.delimiter).map(dir => dir.trim().replace(/^"|"$/g, '')).filter(Boolean)
  for (const dir of new Set([path.dirname(compiler.path), ...pathDirs, ...known])) {
    const exe = path.resolve(dir, 'gdb' + suffix)
    if (await exists(exe) && await probe(exe, ['--version'])) return exe
  }
  return null
}

export async function toolEnvironment(directory: string, selected?: CompilerInfo): Promise<NodeJS.ProcessEnv> {
  const tools = await detectCompilers()
  const python = selected?.kind === 'python' ? selected.path : await projectPython(directory)
  const env: NodeJS.ProcessEnv = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', PYTHONUTF8: '1' }
  const dirs = [...new Set([...(python ? [path.dirname(python)] : []),
    ...(selected ? [binDirectory(selected)] : []), ...tools.map(binDirectory)])]
  const originalPath = process.env.PATH || ''
  for (const key of Object.keys(env)) if (key.toLowerCase() === 'path') delete env[key]
  env.PATH = [...dirs, originalPath].filter(Boolean).join(path.delimiter)
  if (python && /[\\/](?:\.venv|venv)[\\/]/.test(python)) {
    env.VIRTUAL_ENV = path.dirname(path.dirname(python))
    delete env.PYTHONHOME
  }
  const java = tools.find(tool => tool.kind === 'java')
  if (java) env.JAVA_HOME = path.dirname(path.dirname(java.path))
  return env
}

export function shutdownToolchains(): void {
  closed = true
  for (const proc of probes) stopProcess(proc)
}

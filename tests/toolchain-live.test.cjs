// 使用本机真实工具链和临时源码；不下载依赖，不修改用户项目。
const { test, after } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { spawn } = require('node:child_process')
const { setTimeout: delay } = require('node:timers/promises')
const Module = require('node:module')
const esbuild = require('esbuild')
const root = require('node:fs').mkdtempSync(path.join(os.tmpdir(), 'anvil-toolchains-'))
esbuild.buildSync({ entryPoints: ['runner', 'toolchains', 'term-host', 'output-buffer'].map(name => path.join(__dirname, '../src/main', name + '.ts')),
  outdir: root, bundle: true, platform: 'node', format: 'cjs', external: ['electron'], logLevel: 'silent' })
const originalLoad = Module._load
Module._load = function (name, ...args) { return name === 'electron' ? { app: { getPath: () => root, getAppPath: () => path.resolve(__dirname, '..') } } : originalLoad.call(this, name, ...args) }
const runner = require(path.join(root, 'runner.js'))
const tools = require(path.join(root, 'toolchains.js'))
const terminal = require(path.join(root, 'term-host.js'))
const { OutputBuffer } = require(path.join(root, 'output-buffer.js'))
Module._load = originalLoad
after(async () => {
  runner.shutdownAll(); terminal.shutdownAll(); tools.shutdownToolchains()
  const target = path.resolve(root)
  assert.equal(path.dirname(target), path.resolve(os.tmpdir()))
  assert.ok(path.basename(target).startsWith('anvil-toolchains-'))
  await fs.rm(target, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 })
})

async function project(name) {
  const dir = path.join(root, name)
  await fs.mkdir(path.join(dir, '.git'), { recursive: true })
  return dir
}
async function run(file, prompt, input) {
  const events = []
  let sid, supplied = false, resolveExit, rejectExit
  const completed = new Promise((resolve, reject) => { resolveExit = resolve; rejectExit = reject })
  const timer = setTimeout(() => { if (sid) runner.killRun(sid); rejectExit(new Error('run timeout: ' + JSON.stringify(events))) }, 45000)
  try {
    const started = await runner.startRun(file, event => {
      events.push(event)
      if ('code' in event) resolveExit(event)
      if (prompt && !supplied && events.filter(e => e.stream === 'stdout').map(e => e.text).join('').includes(prompt)) {
        supplied = true
        assert.equal(runner.sendInput(sid, input), true)
      }
    }, need => { throw new Error('Missing toolchain: ' + need) })
    sid = started.sid
    assert.ok(started.compiler, started.reason)
    const exit = await completed
    assert.equal(events.filter(event => 'code' in event).length, 1)
    assert.ok(!runner.activeSessions().includes(sid))
    if (prompt) assert.ok(supplied, 'prompt must arrive before providing input')
    return { exit, started, out: events.filter(e => e.stream === 'stdout').map(e => e.text).join(''),
      err: events.filter(e => e.stream === 'stderr').map(e => e.text).join('') }
  } finally { clearTimeout(timer) }
}

test('discovers all seven configured language toolchains', async () => {
  const found = await runner.detectCompilers(true)
  for (const kind of ['python', 'gcc', 'g++', 'java', 'node', 'typescript', 'kotlin']) assert.ok(found.some(tool => tool.kind === kind), `missing ${kind}`)
  assert.ok(found.find(tool => tool.kind === 'java').runtimePath)
})

test('real C17 accepts input after an unbuffered Chinese prompt; warnings survive successful compilation', async () => {
  const dir = await project('中文 C project')
  const file = path.join(dir, 'hello.c')
  await fs.writeFile(file, '#include <stdio.h>\nint main(void) { int unused = 1; int n; printf("请输入数字:"); if (scanf("%d", &n) != 1) return 2; printf("结果=%d\\n", n*2); return 0; }\n')
  const result = await run(file, '请输入数字:', '21')
  assert.equal(result.exit.code, 0, result.err)
  assert.match(result.out, /结果=42/)
  assert.match(result.err, /warning:/)
})

test('real C++17 supports modern syntax, input, spaces and Unicode output', async () => {
  const dir = await project('中文 CPP project')
  const file = path.join(dir, 'main.cpp')
  await fs.writeFile(file, '#include <iostream>\n#include <utility>\nint main(){ auto [a,b]=std::pair{20,22}; int n; std::cout << "输入:"; std::cin >> n; std::cout << "答案=" << a+b+n << "\\n"; }\n')
  const result = await run(file, '输入:', '1')
  assert.equal(result.exit.code, 0, result.err)
  assert.match(result.out, /答案=43/)
})

test('real Java compiles a package and dependent source with the matched JDK', async () => {
  const dir = await project('中文 Java project')
  await fs.mkdir(path.join(dir, 'demo'))
  const file = path.join(dir, 'demo/Main.java')
  await fs.writeFile(path.join(dir, 'demo/Helper.java'), 'package demo; public class Helper { public static int answer() { return 42; } }')
  await fs.writeFile(file, 'package demo; import java.util.Scanner; public class Main { public static void main(String[] args) { System.out.print("输入姓名:"); String name = new Scanner(System.in, "UTF-8").nextLine(); System.out.println("你好" + name + Helper.answer()); } }')
  const result = await run(file, '输入姓名:', '同学')
  assert.equal(result.exit.code, 0, result.err)
  assert.match(result.out, /你好同学42/)
  assert.equal(result.started.compiler.kind, 'java')
})

test('Kotlin JVM application and script support Unicode paths and standard input', async () => {
  const dir = await project('中文 Kotlin project')
  const file = path.join(dir, '示例.kt')
  await fs.writeFile(file, 'package sample\nfun main() { println("输入名字:"); println("你好" + readln()) }')
  const app = await run(file, '输入名字:', '同学')
  assert.equal(app.exit.code, 0, app.err)
  assert.match(app.out, /你好同学/)
  const script = path.join(dir, '示例.kts')
  await fs.writeFile(script, 'println("脚本=" + (40 + 2))')
  const result = await run(script)
  assert.equal(result.exit.code, 0, result.err)
  assert.match(result.out, /脚本=42/)
})

test('Node runs JavaScript in both module systems with project-local imports', async () => {
  const dir = await project('中文 JS project')
  await fs.writeFile(path.join(dir, 'package.json'), '{"type":"module"}')
  for (const [name, source] of Object.entries({
    'helper.cjs': 'module.exports = 41',
    'main.js': 'import n from "./helper.cjs"; console.log("答案=" + (n + 1))',
    'main.mjs': 'import "./main.js"',
    'main.cjs': 'console.log("答案=" + (require("./helper.cjs") + 1))'
  })) await fs.writeFile(path.join(dir, name), source)
  for (const name of ['main.js', 'main.mjs', 'main.cjs']) {
    const result = await run(path.join(dir, name))
    assert.equal(result.exit.code, 0, result.err)
    assert.match(result.out, /答案=42/)
  }
})

test('TypeScript resolves project imports, tsconfig paths, enums and TSX', async () => {
  const dir = await project('中文 TS project')
  await fs.writeFile(path.join(dir, 'tsconfig.json'), JSON.stringify({ compilerOptions: {
    paths: { '@lib': ['./helper.ts'] }, jsxFactory: 'element'
  } }))
  await fs.writeFile(path.join(dir, 'helper.ts'), 'export enum Value { Answer = 42 }')
  for (const name of ['main.ts', 'main.mts', 'main.cts', 'main.tsx']) {
    const file = path.join(dir, name)
    const expression = name.endsWith('tsx') ? 'function element(tag: string) { return tag }; console.log(<hello />);' : ''
    await fs.writeFile(file, 'import { Value } from "@lib"; console.log("答案=" + Value.Answer);' + expression)
    const result = await run(file)
    assert.equal(result.exit.code, 0, result.err)
    assert.match(result.out, /答案=42/)
    if (name.endsWith('tsx')) assert.match(result.out, /hello/)
  }
})

test('real Python selects the project venv and streams interactive UTF-8', async () => {
  const dir = await project('中文 Python project')
  const python = (await tools.detectCompilers()).find(tool => tool.kind === 'python').path
  await new Promise((resolve, reject) => {
    const child = spawn(python, ['-m', 'venv', '--without-pip', path.join(dir, '.venv')], { windowsHide: true })
    let stderr = ''
    child.stderr.on('data', data => { stderr += data })
    child.on('error', reject)
    child.on('close', code => code === 0 ? resolve() : reject(new Error(stderr)))
  })
  const file = path.join(dir, 'main.py')
  await fs.writeFile(file, 'import sys\nprint(sys.executable)\nname = input("输入名字:")\nprint("你好" + name)\n')
  const result = await run(file, '输入名字:', '小明')
  assert.equal(result.exit.code, 0, result.err)
  assert.match(result.out, /你好小明/)
  assert.ok(result.started.compiler.path.includes(path.join('.venv', 'Scripts')))
  const env = await tools.toolEnvironment(dir)
  assert.equal(env.VIRTUAL_ENV, path.join(dir, '.venv'))
  assert.equal(env.PATH.split(path.delimiter)[0], path.dirname(result.started.compiler.path))
})

test('compile failures never run and Python failures retain traceback plus clickable location', async () => {
  const dir = await project('failure cases')
  const c = path.join(dir, 'bad.c')
  await fs.writeFile(c, 'int main(void) { syntax broken }')
  const bad = await run(c)
  assert.notEqual(bad.exit.code, 0)
  assert.match(bad.err, /bad\.c:\d+:\d+:.*error:/)
  assert.doesNotMatch(bad.out, /开始运行/)
  const python = path.join(dir, 'bad.py')
  await fs.writeFile(python, 'raise ValueError("测试异常")')
  const failed = await run(python)
  assert.notEqual(failed.exit.code, 0)
  assert.match(failed.err, /Traceback/)
  assert.match(failed.err, /bad\.py:1:1: error:/)
})

test('real terminal exposes Kotlin, Node, TypeScript, Java and GCC', async () => {
  const dir = await project('terminal workspace')
  let output = ''
  terminal.bindWindow({ isDestroyed: () => false, webContents: { send: (_channel, event) => { output += event.data } } })
  const session = await terminal.startTerm(dir)
  assert.equal(terminal.writeTerm(session.sid, 'javac -version\r\ngcc --version\r\nnode --version\r\ntsc --version\r\ntsx --version\r\nkotlinc -version\r\necho ANVIL_TERMINAL_DONE\r\n'), true)
  for (let i = 0; i < 500 && !output.includes('ANVIL_TERMINAL_DONE'); i++) await delay(20)
  assert.match(output, /kotlinc-jvm \d/)
  assert.match(output, /tsx v?\d/)
  assert.match(output, /Version \d+\.\d+/i)
  assert.match(output, /javac \d/)
  assert.match(output, /gcc.*\d/s)
  terminal.killTerm(session.sid)
  assert.equal(terminal.termAlive(session.sid), false)
})

test('output batching preserves order and flushes the last partial line', () => {
  const received = []
  const buffer = new OutputBuffer((stream, text) => received.push({ stream, text }))
  for (let i = 0; i < 1000; i++) buffer.write('stdout', '中')
  buffer.write('stderr', 'error')
  buffer.write('stdout', 'end')
  buffer.flush()
  assert.deepEqual(received, [{ stream: 'stdout', text: '中'.repeat(1000) }, { stream: 'stderr', text: 'error' }, { stream: 'stdout', text: 'end' }])
  received.length = 0
  const large = 'x'.repeat(32767) + '😀' + 'y'.repeat(40000)
  buffer.write('stdout', large)
  buffer.flush()
  assert.equal(received.map(item => item.text).join(''), large)
  for (const item of received) {
    assert.ok(item.text.length <= 32768)
    assert.ok(!/[\uD800-\uDBFF]$/.test(item.text))
  }
})

test('concurrent compilations use isolated artifacts and leave the source unchanged', async () => {
  const dir = await project('concurrent C runs')
  const file = path.join(dir, 'same.c')
  const source = '#include <stdio.h>\nint main(void){ puts("parallel-ok"); return 0; }'
  await fs.writeFile(file, source)
  const results = await Promise.all([run(file), run(file)])
  for (const result of results) { assert.equal(result.exit.code, 0, result.err); assert.match(result.out, /parallel-ok/) }
  assert.notEqual(results[0].started.sid, results[1].started.sid)
  assert.equal(await fs.readFile(file, 'utf8'), source)
  let entries = []
  for (let i = 0; i < 100; i++) {
    entries = await fs.readdir(path.join(dir, '.anvilide'))
    if (!entries.length) break
    await delay(20)
  }
  assert.deepEqual(entries, [])
})

test('Java diagnostics have clickable locations and compilation has a deadline', async () => {
  const dir = await project('diagnostics and timeout')
  const java = path.join(dir, 'Broken.java')
  await fs.writeFile(java, 'public class Broken { public static void main(String[] args) { invalid statement; } }')
  const result = await run(java)
  assert.notEqual(result.exit.code, 0)
  assert.match(result.err, /Broken\.java:1:1: error:/)
  const c = path.join(dir, 'slow.c')
  await fs.writeFile(c, 'int main(void){ return 0; }')
  const before = process.env.ANVIL_COMPILE_TIMEOUT_MS
  try {
    process.env.ANVIL_COMPILE_TIMEOUT_MS = '1'
    const timed = await run(c)
    assert.equal(timed.exit.signal, 'TIMEOUT')
    assert.match(timed.err, /编译超时/)
    assert.doesNotMatch(timed.out, /开始运行/)
  } finally {
    if (before === undefined) delete process.env.ANVIL_COMPILE_TIMEOUT_MS
    else process.env.ANVIL_COMPILE_TIMEOUT_MS = before
  }
})

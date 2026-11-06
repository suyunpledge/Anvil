const { test, after } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const { EventEmitter } = require('node:events')
const { PassThrough } = require('node:stream')
const { setTimeout: delay } = require('node:timers/promises')
const Module = require('node:module')
const esbuild = require('esbuild')

const buildDir = require('node:fs').mkdtempSync(path.join(os.tmpdir(), 'anvil-main-tests-'))
esbuild.buildSync({
  entryPoints: ['anvil-client', 'anvil-events', 'anvil-host', 'runner', 'fs-service'].map(name => path.join(__dirname, '../src/main', name + '.ts')),
  outdir: buildDir, bundle: true, platform: 'node', format: 'cjs', external: ['electron'], logLevel: 'silent'
})
const originalLoad = Module._load
Module._load = function (name, ...args) {
  return name === 'electron' ? { app: { getPath: () => buildDir } } : originalLoad.call(this, name, ...args)
}
const { BackendClient } = require(path.join(buildDir, 'anvil-client.js'))
const { WorkOrderSubscription } = require(path.join(buildDir, 'anvil-events.js'))
const { AnvilHost } = require(path.join(buildDir, 'anvil-host.js'))
const runner = require(path.join(buildDir, 'runner.js'))
const files = require(path.join(buildDir, 'fs-service.js'))
Module._load = originalLoad
after(() => fs.rm(buildDir, { recursive: true, force: true }))

async function fixture(t, handler) {
  const repo = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-host-test-'))
  await fs.mkdir(path.join(repo, '.runtime'))
  await fs.writeFile(path.join(repo, '.runtime/service-token'), 'first-token')
  const server = http.createServer(handler)
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(async () => {
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
    await fs.rm(repo, { recursive: true, force: true })
  })
  return { repo, location: () => ({ anvilRepo: repo, anvilPort: server.address().port }) }
}

function json(res, data, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)) }
function fakeProcess() {
  const proc = new EventEmitter()
  Object.assign(proc, { exitCode: null, signalCode: null, killed: false,
    stdout: new PassThrough(), stderr: new PassThrough(), stdin: new PassThrough(),
    kill() { this.killed = true; this.exitCode = -1; this.emit('exit', -1); this.emit('close', -1) } })
  return proc
}

test('API reloads rotated tokens and settles interrupted responses', async t => {
  const f = await fixture(t, (req, res) => {
    if (req.url === '/broken') { res.writeHead(200); res.write('{'); setImmediate(() => res.destroy()); return }
    json(res, { token: req.headers['x-local-ide-token'] })
  })
  const client = new BackendClient(f.location)
  t.after(() => client.close())
  assert.equal((await client.request('GET', '/')).data.token, 'first-token')
  await fs.writeFile(path.join(f.repo, '.runtime/service-token'), 'rotated-token')
  assert.equal((await client.request('GET', '/')).data.token, 'rotated-token')
  assert.equal((await client.request('GET', '/broken', undefined, 1000)).status, 0)
  assert.equal((await client.request('GET', '//outside.invalid')).status, 0)
})

test('status requests coalesce and consume the actual profiles contract', async t => {
  const calls = {}
  const f = await fixture(t, (req, res) => {
    calls[req.url] = (calls[req.url] || 0) + 1
    if (req.url === '/health') json(res, { ok: true, wos: 0, core: {} })
    else if (req.url === '/presets') json(res, { profiles: [{ key: 'qwen3.5', model: 'local', label: '本地' }], unusable: [] })
    else json(res, { ok: true })
  })
  const host = new AnvilHost(f.location)
  t.after(() => host.shutdown())
  const states = await Promise.all(Array.from({ length: 10 }, () => host.status()))
  assert.equal(states[0].adapters[0].key, 'qwen3.5')
  assert.deepEqual(calls, { '/health': 1, '/presets': 1, '/integrations': 1 })
  await host.status()
  assert.equal(calls['/health'], 2)
  assert.equal(calls['/presets'], 1)
  assert.equal(states[0].startedByUs, false)
})

test('concurrent starts launch once and pass the configured port', async t => {
  let running = false, spawns = 0, args
  const f = await fixture(t, (_req, res) => json(res, { ok: running, wos: 0, core: {} }, running ? 200 : 503))
  await fs.mkdir(path.join(f.repo, '.venv/Scripts'), { recursive: true })
  await fs.writeFile(path.join(f.repo, '.venv/Scripts/python.exe'), '')
  await fs.writeFile(path.join(f.repo, 'anvil.py'), '')
  const child = fakeProcess()
  const host = new AnvilHost(f.location, (_exe, argv) => { spawns++; args = argv; running = true; return child }, 1000)
  t.after(() => host.shutdown())
  const results = await Promise.all(Array.from({ length: 12 }, () => host.startBackend()))
  assert.ok(results.every(result => result.ok))
  assert.equal(spawns, 1)
  assert.deepEqual(args, ['anvil.py', '--port', String(f.location().anvilPort)])
  host.shutdown()
  assert.equal(child.killed, true)
})

test('missing environment and early process failure resolve instead of hanging', async t => {
  const f = await fixture(t, (_req, res) => json(res, { ok: false }, 503))
  const missing = new AnvilHost(f.location)
  t.after(() => missing.shutdown())
  assert.equal((await missing.startBackend()).ok, false)
  await fs.mkdir(path.join(f.repo, '.venv/Scripts'), { recursive: true })
  await fs.writeFile(path.join(f.repo, '.venv/Scripts/python.exe'), '')
  await fs.writeFile(path.join(f.repo, 'anvil.py'), '')
  const host = new AnvilHost(f.location, () => {
    const child = fakeProcess()
    process.nextTick(() => child.emit('error', new Error('spawn failed')))
    return child
  }, 1000)
  t.after(() => host.shutdown())
  const result = await host.startBackend()
  assert.equal(result.ok, false)
  assert.match(result.error, /spawn failed/)
})

test('SSE reconnects with cursor, parses split CRLF and suppresses duplicates', async t => {
  let connections = 0
  const cursors = [], events = []
  const f = await fixture(t, (req, res) => {
    connections++
    cursors.push(req.headers['last-event-id'])
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    if (connections === 1) {
      res.write('id: 1\r\ndata: {"kind":"progress","n":1}\r')
      setTimeout(() => res.end('\n\r\n'), 5)
    } else res.end('id: 1\ndata: {"kind":"progress","n":1}\n\nid: 2\ndata: {"kind":"progress","n":2}\n\nevent: end\ndata: [DONE]\n\n')
  })
  const client = new BackendClient(f.location)
  let subscription
  t.after(() => { subscription?.close(); client.close() })
  await new Promise(resolve => { subscription = new WorkOrderSubscription(client, 'test', ev => events.push(ev), resolve, 5) })
  assert.equal(connections, 2)
  assert.deepEqual(cursors, [undefined, '1'])
  assert.deepEqual(events.map(ev => ev.kind), ['progress', 'progress', 'stream_end'])
  assert.deepEqual(events.filter(ev => ev.kind === 'progress').map(ev => ev.n), [1, 2])
})

test('terminal HTTP errors do not reconnect', async t => {
  let connections = 0
  const f = await fixture(t, (_req, res) => { connections++; res.writeHead(404); res.end() })
  const client = new BackendClient(f.location)
  const events = []
  let subscription
  t.after(() => { subscription?.close(); client.close() })
  await new Promise(resolve => { subscription = new WorkOrderSubscription(client, 'missing', ev => events.push(ev), resolve, 5) })
  await delay(30)
  assert.equal(connections, 1)
  assert.match(events[0].error, /404/)
})

test('unwatch cancels an already scheduled retry', async t => {
  let connections = 0
  const f = await fixture(t, (_req, res) => { connections++; res.writeHead(503); res.end() })
  const client = new BackendClient(f.location)
  const subscription = new WorkOrderSubscription(client, 'temporary', () => {}, () => {}, 200)
  t.after(() => { subscription.close(); client.close() })
  for (let i = 0; i < 100 && !subscription.retryTimer; i++) await delay(10)
  assert.ok(subscription.retryTimer)
  subscription.close()
  await delay(300)
  assert.equal(connections, 1)
})

test('asynchronous filesystem preserves directory sorting and encoding', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-fs-test-'))
  t.after(() => fs.rm(root, { recursive: true, force: true }))
  await fs.mkdir(path.join(root, 'folder'))
  await files.writeFileSmart(path.join(root, 'a.py'), 'print("中文")\n')
  await fs.writeFile(path.join(root, '.hidden'), 'hidden')
  const entries = await files.listDir(root)
  assert.deepEqual(entries.map(item => item.name), ['folder', 'a.py'])
  assert.equal((await files.readFileSmart(path.join(root, 'a.py'))).text, 'print("中文")\n')
  await Promise.all([
    files.writeFileSmart(path.join(root, 'a.py'), 'a'.repeat(1024 * 1024)),
    files.writeFileSmart(path.join(root, 'a.py'), 'last write')
  ])
  assert.equal((await files.readFileSmart(path.join(root, 'a.py'))).text, 'last write')
})

test('compiler cancellation prevents execution and emits exactly one exit', async t => {
  const cp = require('node:child_process')
  const realFs = require('node:fs')
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-run-test-'))
  t.after(() => fs.rm(root, { recursive: true, force: true }))
  const children = []
  t.mock.method(realFs, 'existsSync', () => true)
  t.mock.method(cp, 'spawn', (_cmd, args) => {
    const child = fakeProcess()
    if (args.includes('--version') || args.includes('-version')) process.nextTick(() => { child.stdout.write('fake 1.0'); child.emit('close', 0) })
    else children.push(child)
    return child
  })
  await runner.detectCompilers(true)
  const events = []
  await fs.writeFile(path.join(root, 'test.c'), 'int main(void) { return 0; }')
  const started = await runner.startRun(path.join(root, 'test.c'), ev => events.push(ev), () => assert.fail('compiler missing'))
  for (let i = 0; i < 100 && !children.length; i++) await delay(10)
  assert.ok(runner.activeSessions().includes(started.sid))
  assert.equal(runner.killRun(started.sid), true)
  children[0].emit('close', 0)
  assert.equal(children.length, 1, 'cancelled compilation must never launch executable')
  assert.equal(events.filter(ev => 'code' in ev).length, 1)
  assert.ok(!runner.activeSessions().includes(started.sid))
})

test('split UTF-8 output survives process chunk boundaries', async t => {
  const cp = require('node:child_process')
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-output-test-'))
  t.after(() => fs.rm(root, { recursive: true, force: true }))
  const child = fakeProcess()
  t.mock.method(require('node:fs'), 'existsSync', () => true)
  t.mock.method(cp, 'spawn', (_cmd, args) => {
    if (!args.includes('--version') && !args.includes('-version')) return child
    const probe = fakeProcess()
    process.nextTick(() => { probe.stdout.write('test 1.0'); probe.emit('close', 0) })
    return probe
  })
  await runner.detectCompilers(true)
  const events = []
  await fs.writeFile(path.join(root, 'test.py'), 'print("中文输出")')
  await runner.startRun(path.join(root, 'test.py'), ev => events.push(ev), () => assert.fail('compiler missing'))
  for (let i = 0; i < 100 && !child.stdout.listenerCount('data'); i++) await delay(10)
  const bytes = Buffer.from('中文输出')
  for (const byte of bytes) child.stdout.write(Buffer.from([byte]))
  child.emit('close', 0)
  assert.equal(events.filter(ev => ev.stream === 'stdout').map(ev => ev.text).join(''), '中文输出')
  assert.equal(events.filter(ev => 'code' in ev).length, 1)
})

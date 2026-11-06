// 显式设置 ANVIL_BACKEND_REPO 后运行：真实 HTTP / Python 状态机，模型使用确定性替身。
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { spawn } = require('node:child_process')
const { setTimeout: delay } = require('node:timers/promises')
const esbuild = require('esbuild')

test('IDE host and native backend share presets, workorder, SSE and confirmation contracts', {
  skip: !process.env.ANVIL_BACKEND_REPO, timeout: 180000
}, async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-contract-'))
  const repo = path.resolve(process.env.ANVIL_BACKEND_REPO)
  esbuild.buildSync({
    entryPoints: ['anvil-client', 'anvil-events', 'process-host'].map(name => path.join(__dirname, '../src/main', name + '.ts')),
    outdir: root, bundle: true, platform: 'node', format: 'cjs', logLevel: 'silent'
  })
  const { BackendClient } = require(path.join(root, 'anvil-client.js'))
  const { WorkOrderSubscription } = require(path.join(root, 'anvil-events.js'))
  const { stopProcess } = require(path.join(root, 'process-host.js'))
  const python = `
import json, os, socket, sys
from core.paths import ensure_core_on_path
ensure_core_on_path()
from fake_adapter import make_sandbox, ScriptedAdapter, OLD_AVG, NEW_AVG_GOOD, nc, tc, patch
from service import runner
from service.app import build_app
import uvicorn
project = os.path.join(os.environ['LOCAL_IDE_RUNTIME'], 'project')
make_sandbox(project)
runner.build_adapter = lambda opts: ScriptedAdapter([nc('read'), tc(patch(OLD_AVG, NEW_AVG_GOOD))])
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
app = build_app()
print(json.dumps({'port': sock.getsockname()[1], 'project': project}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[sock])
`
  const child = spawn(path.join(repo, '.venv/Scripts/python.exe'), ['-u', '-c', python], {
    cwd: repo, windowsHide: true,
    env: { ...process.env, LOCAL_IDE_RUNTIME: path.join(root, '.runtime'),
      ANVIL_INTEGRATION_LOG_DIR: path.join(root, 'logs'), ANVIL_HR: '0', PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  let errors = '', subscription, client
  child.stderr.on('data', chunk => { errors = (errors + chunk.toString()).slice(-8000) })
  t.after(async () => {
    subscription?.close()
    client?.close()
    stopProcess(child)
    for (let i = 0; i < 100 && child.exitCode === null && child.signalCode === null; i++) await delay(50)
    await fs.rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  })
  const ready = await new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error('backend startup timeout: ' + errors)), 60000)
    child.on('error', error => { clearTimeout(timer); reject(error) })
    child.on('exit', code => { clearTimeout(timer); reject(new Error(`backend exit ${code}: ${errors}`)) })
    child.stdout.on('data', chunk => {
      output += chunk.toString()
      const newline = output.indexOf('\n')
      if (newline >= 0) {
        clearTimeout(timer)
        try { resolve(JSON.parse(output.slice(0, newline))) } catch (error) { reject(error) }
      }
    })
  })
  client = new BackendClient(() => ({ anvilRepo: root, anvilPort: ready.port }))
  let health
  for (let i = 0; i < 100; i++) {
    health = await client.request('GET', '/health', undefined, 1000)
    if (health.status === 200) break
    await delay(50)
  }
  assert.equal(health.status, 200, errors)
  const presets = await client.request('GET', '/presets')
  assert.equal(presets.status, 200)
  assert.equal(presets.data.default, 'qwen3.5')
  assert.ok(presets.data.profiles.some(item => item.key === presets.data.default))
  const created = await client.request('POST', '/wo', {
    workdir: ready.project, target: 'calc.py', test_path: 'test_calc.py', task: '实现平均值'
  })
  assert.equal(created.status, 200, JSON.stringify(created))
  const id = created.data.wo_id
  const events = []
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('workorder stream timeout: ' + errors)), 60000)
    subscription = new WorkOrderSubscription(client, id, event => events.push(event), () => { clearTimeout(timer); resolve() })
  })
  assert.ok(events.some(event => event.kind === 'status' && event.status === 'awaiting_confirm'), JSON.stringify(events))
  const before = await fs.readFile(path.join(ready.project, 'calc.py'), 'utf8')
  assert.match(before, /NotImplementedError/)
  const staged = await client.request('GET', `/wo/${id}/staged`)
  assert.ok(staged.data.diff)
  const confirmed = await client.request('POST', `/wo/${id}/confirm`, { approve: true })
  assert.equal(confirmed.status, 200, JSON.stringify(confirmed))
  assert.equal((await client.request('GET', `/wo/${id}`)).data.status, 'done')
  assert.doesNotMatch(await fs.readFile(path.join(ready.project, 'calc.py'), 'utf8'), /NotImplementedError/)
})

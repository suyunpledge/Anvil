const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const esbuild = require('esbuild')

test('AI edits require an exact base and undo survives a new host', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-chat-test-'))
  t.after(() => fs.rm(root, { recursive: true, force: true }))
  const bundle = path.join(root, 'files.cjs')
  esbuild.buildSync({ entryPoints: [path.join(__dirname, '../src/main/chat-files.ts')], outfile: bundle,
    platform: 'node', format: 'cjs', bundle: true })
  const { EditHistory, snapshot } = require(bundle)
  const journal = path.join(root, 'journal')
  const file = path.join(root, '例子.py')
  const old = '\uFEFFprint(1)\r\n', next = '\uFEFFprint(2)\r\n'
  await fs.writeFile(file, old)
  const history = new EditHistory(journal)
  const changes = await Promise.allSettled([history.change(file, old, next), history.change(file, old, next)])
  assert.equal(changes.filter(c => c.status === 'fulfilled').length, 1)
  const restarted = new EditHistory(journal)
  assert.equal(await restarted.canUndo(file), true)
  await fs.writeFile(file, 'external change')
  await assert.rejects(restarted.change(file, 'external change'), /变化/)
  assert.equal(await fs.readFile(file, 'utf8'), 'external change')
  await fs.writeFile(file, next)
  await restarted.change(file, next)
  assert.equal(await fs.readFile(file, 'utf8'), old)
  assert.equal(await restarted.canUndo(file), false)
  await fs.link(file, path.join(root, 'alias.py'))
  await assert.rejects(snapshot(file), /硬链接/)
  const alias = path.join(root, 'alias-dir')
  await fs.symlink(journal, alias, process.platform === 'win32' ? 'junction' : 'dir')
  await fs.writeFile(path.join(journal, 'source.py'), 'a')
  await assert.rejects(snapshot(path.join(alias, 'source.py')), /链接/)
})

test('local model repairs invalid tool requests before producing a reviewable change', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'anvil-tools-test-'))
  t.after(() => fs.rm(root, { recursive: true, force: true }))
  const bundle = path.join(root, 'local.cjs')
  esbuild.buildSync({ entryPoints: [path.join(__dirname, '../src/main/local-chat.ts')], outfile: bundle,
    platform: 'node', format: 'cjs', bundle: true })
  const { generate } = require(bundle)
  const replies = [
    { action: 'replace', old: 'n', replacement: 'x', path: 'another.py' },
    { action: 'replace', old: 'n', replacement: 'x' },
    { action: 'find', old: 'return' },
    { action: 'read', line: 2 },
    { action: 'replace', old: 'return n', replacement: 'return n + 1' }
  ]
  const inputs = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/tags')) return Response.json({ models: [{ name: 'test-coder' }] })
    inputs.push(JSON.parse(options.body).messages.at(-1).content)
    return Response.json({ message: { content: JSON.stringify({ message: 'ok', ...replies.shift() }) } })
  })
  const before = 'n = 1\r\nreturn n\r\n'
  const result = await generate('test-coder', 'edit', 'sample.py', before, [], new AbortController().signal)
  assert.match(inputs[1], /额外字段/)
  assert.match(inputs[2], /唯一匹配/)
  assert.equal(JSON.parse(inputs[3]).startLine, 2)
  assert.equal(JSON.parse(inputs[4]).text, 'return n\n')
  assert.deepEqual(result.tools, ['已读取授权文件', '搜索文本', '读取片段', '提议替换'])
  assert.equal(result.draft.after, 'n = 1\r\nreturn n + 1\r\n')
  const ask = (prompt, file = '') => generate('test-coder', prompt, file, file ? before : '', [], new AbortController().signal)
  const count = inputs.length
  assert.equal((await ask('帮我改一下当前文件')).needsFile, true)
  assert.equal(inputs.length, count)
  replies.push({ action: 'reply', message: '请把文件贴过来。' })
  assert.equal((await ask('继续上一步')).needsFile, true)
  replies.push({ action: 'reply', message: '闭包会捕获外层作用域。' })
  assert.equal((await ask('什么是闭包？')).needsFile, false)
  replies.push({ action: 'reply', message: '请贴出代码。' },
    { action: 'replace', old: 'return n', replacement: 'return n + 2' })
  assert.equal((await ask('继续修改', 'sample.py')).draft.after, 'n = 1\r\nreturn n + 2\r\n')
  assert.equal(JSON.parse(inputs.at(-1)).text, before)
  replies.push({ action: 'reply', message: '请把文件贴过来。' }, { action: 'select_file' })
  const stalled = await ask('继续修改', 'sample.py')
  assert.equal(stalled.needsFile, false)
  assert.match(stalled.answer, /已读取「sample.py」/)
  assert.equal(stalled.draft, undefined)
  assert.equal(replies.length, 0)
})

// npm run bench:chat -- <installed Ollama tags ...>
require('tsx/cjs')
const assert = require('node:assert/strict')
const { generate, models: installedModels } = require('../src/main/local-chat.ts')

// All inputs are synthetic. Compare the entire resulting file, including line endings.
const header = Array.from({ length: 105 }, (_, i) => '# configuration note ' + i + ' '.repeat(24)).join('\n') + '\n'
const repeated = 'def public_limit():\n    return 12\n\ndef private_limit():\n    return 12\n'
const cases = [
  { name: 'single', text: 'const retries = 2;\r\nconst delay = 20;\r\n', file: 'config.ts',
    prompt: '请把 retries 的初始值改成 5，保留其他代码。', after: 'const retries = 5;\r\nconst delay = 20;\r\n' },
  { name: 'duplicate', text: repeated, prompt: '只把 private_limit 的返回值改成 18，其他函数保持原样。',
    after: repeated.replace('private_limit():\n    return 12', 'private_limit():\n    return 18') },
  { name: 'lookup', text: header + 'def region_code():\n    return "ORCHID-714"\n',
    prompt: 'region_code 函数实际返回什么字符串？只回答它的值，不要改文件。', answer: 'ORCHID-714' },
  { name: 'distant_edit', text: header + 'def cache_seconds():\n    return 173\n',
    prompt: '把 cache_seconds 的返回值增加 2，保留其他所有行。',
    after: header + 'def cache_seconds():\n    return 175\n' },
  { name: 'empty', text: '', prompt: '在当前空文件中写入一行 VERSION = 7，无需其他代码。', after: 'VERSION = 7', optionalEOL: true },
  { name: 'read_only', text: 'def add(x, y):\n    return x - y\n', prompt: '只解释 add(9, 4) 现在的返回值，不要修改代码。',
    answer: /5|五/ },
  { name: 'follow_reference', file: 'routes.js', text: 'export function currentEndpoint() { return activeEndpoint; }\n' +
    header.replaceAll('#', '//') + 'const activeEndpoint = "/ocean/cedar/38";\n',
    prompt: 'currentEndpoint() 实际返回哪个路径？找到它引用的常量，只回答实际的路径，不要修改文件。', answer: '/ocean/cedar/38' },
  { name: 'scoped_crlf', file: 'units.ts', text: 'export const meters = {\r\n  factor: 1\r\n};\r\nexport const miles = {\r\n  factor: 1\r\n};\r\n',
    prompt: '把 miles 的 factor 改为 1609，不要修改其他代码。',
    after: 'export const meters = {\r\n  factor: 1\r\n};\r\nexport const miles = {\r\n  factor: 1609\r\n};\r\n' },
  { name: 'boundary_edit', file: 'Limits.kt', text: 'fun admitted(n: Int) = n > 30\nfun alarm(n: Int) = n > 30\n',
    prompt: '修复 admitted 的边界条件，使恰好 30 也能通过。告警函数保持现状，只修改比较运算符。',
    after: 'fun admitted(n: Int) = n >= 30\nfun alarm(n: Int) = n > 30\n' },
  { name: 'trace_branch', text: 'def pick(total, cutoff):\n    if total < cutoff:\n        return "HARBOR-52"\n    return "PEBBLE-26"\n',
    prompt: '只回答 pick(30, 30) 会返回的字符串。', answer: 'PEBBLE-26', exclude: 'HARBOR-52' }
]

async function main() {
  const models = process.argv.slice(2)
  if (!models.length) throw Error('Pass the local model tags to test')
  const available = await installedModels()
  const missing = models.filter(name => !available.includes(name))
  if (missing.length) throw Error('Missing local models: ' + missing.join(', '))
  const fetchOllama = globalThis.fetch
  let actions = []
  globalThis.fetch = async (url, options) => {
    const response = await fetchOllama(url, options)
    if (url.endsWith('/chat')) {
      const data = await response.clone().json()
      let action
      try { action = JSON.parse(data.message?.content).action } catch {}
      actions.push(action || 'invalid')
    }
    return response
  }
  for (const model of models) {
    for (const c of cases) {
      actions = []
      const start = performance.now()
      let pass = false, detail
      try {
        const r = await generate(model, c.prompt, c.file || 'example.py', c.text, [], AbortSignal.timeout(150000))
        assert.equal(r.needsFile, false)
        if ('after' in c) {
          let actual = r.draft?.after
          if (c.optionalEOL) actual = actual?.replace(/\r?\n$/, '')
          pass = actual === c.after
          if (!pass) detail = r.draft ? { line: r.draft.line, old: r.draft.old, replacement: r.draft.replacement } : r.answer
        } else {
          pass = !r.draft && !(c.exclude && r.answer.includes(c.exclude)) && (typeof c.answer === 'string' ? r.answer.includes(c.answer) : c.answer.test(r.answer))
          if (!pass) detail = r.answer
        }
      } catch (error) { detail = String(error) }
      console.log(JSON.stringify({ model, test: c.name, pass, calls: actions.length, actions,
        seconds: Math.round((performance.now() - start) / 100) / 10, detail }))
      if (!pass) process.exitCode = 1
    }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })

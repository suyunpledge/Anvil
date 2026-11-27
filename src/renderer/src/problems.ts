import * as monaco from 'monaco-editor'
import { $, el, bus, state } from './core'
import { revealLine, relativePath } from './editor'
import { icon, fileBadge } from './icons'

// —— 问题面板（对标 VS「错误列表」/ VS Code「问题」）——
// 来源 1：Monaco 自带的语法检查（JS / TS / JSON / CSS / HTML）
// 来源 2：运行时编译器 / 解释器报错（gcc、g++、javac、kotlinc、Python、Node），同时在代码里画波浪线

type Severity = 'error' | 'warning' | 'info'
export interface Problem {
  file: string
  line: number
  col: number
  severity: Severity
  message: string
  source: string
}

const RUN_OWNER = 'anvil-run'
let runProblems: Problem[] = []
let runFile: string | null = null
let runText = ''
let renderQueued = false

const norm = (p: string): string => p.replace(/\\/g, '/').replace(/^file:\/\/\//i, '').toLowerCase()
const isAbsolute = (p: string): boolean => /^[a-z]:[\\/]/i.test(p) || p.startsWith('/') || p.startsWith('\\\\')

function resolve(p: string): string {
  if (isAbsolute(p) || !runFile) return p
  const dir = runFile.replace(/[\\/][^\\/]*$/, '')
  return dir + '\\' + p.replace(/^\.[\\/]/, '').replace(/\//g, '\\')
}

/** 是不是用户自己的代码（排除标准库 / 第三方库的栈帧） */
function isUserFile(p: string): boolean {
  const n = norm(p)
  if (/\/(site-packages|node_modules)\//.test(n) || n.startsWith('node:') || n.startsWith('<')) return false
  if (!runFile || !isAbsolute(p)) return true
  const dir = norm(runFile).replace(/\/[^/]*$/, '/')
  const root = state.folder ? norm(state.folder).replace(/\/?$/, '/') : null
  return n.startsWith(dir) || (root !== null && n.startsWith(root))
}

export function parseRunOutput(text: string): Problem[] {
  const found: Problem[] = []
  const add = (file: string, line: number, col: number, severity: Severity, message: string, source: string): void => {
    const full = resolve(file.trim())
    if (found.some((p) => norm(p.file) === norm(full) && p.line === line && p.message === message)) return
    found.push({ file: full, line, col: col || 1, severity, message: message.trim(), source })
  }
  const lines = text.split(/\r?\n/)
  // gcc / g++ / clang / 旧版 kotlinc：file:line:col: error: msg
  for (const m of text.matchAll(/^(.+?):(\d+):(\d+):\s*(fatal error|error|warning):\s*(.+)$/gm)) {
    add(m[1], +m[2], +m[3], m[4] === 'warning' ? 'warning' : 'error', m[5], /\.kts?$/i.test(m[1]) ? 'kotlinc' : 'gcc')
  }
  // 新版 kotlinc：e: file:///C:/x/main.kt:3:5 message
  for (const m of text.matchAll(/^([ew]): (?:file:\/\/\/)?(.+?\.kts?):(\d+):(\d+) (.+)$/gm)) {
    add(m[2].replace(/\//g, '\\'), +m[3], +m[4], m[1] === 'w' ? 'warning' : 'error', m[5], 'kotlinc')
  }
  // javac：Main.java:5: error: ';' expected
  for (const m of text.matchAll(/^(.+?\.java):(\d+): (error|warning): (.+)$/gm)) {
    add(m[1], +m[2], 1, m[3] === 'warning' ? 'warning' : 'error', m[4], 'javac')
  }
  // Python：取最后一个指向用户文件的 File "...", line N + 末尾的异常行
  if (/Traceback \(most recent call last\)|^\s*File ".+", line \d+/m.test(text)) {
    let frame: { file: string; line: number } | null = null
    for (const m of text.matchAll(/^\s*File "(.+?)", line (\d+)/gm)) if (isUserFile(m[1])) frame = { file: m[1], line: +m[2] }
    const message = [...lines].reverse().find((l) => /^[A-Za-z_][\w.]*(Error|Exception|Warning|Exit|Interrupt)\b/.test(l))
    // Python 打印的代码行去掉了缩进，^ 的位置换算不回真实列号，所以整行标出
    if (frame && message) add(frame.file, frame.line, 1, 'error', message, 'python')
  }
  // Node / tsx：ReferenceError: x is not defined + at ... (C:\x\a.js:3:5)
  const jsError = lines.find((l) => /^(\w*Error|Error)(: .+)?$/.test(l))
  if (jsError) {
    for (const m of text.matchAll(/^\s+at (?:.*?\()?((?:[A-Za-z]:)?[^():]+?\.(?:[cm]?js|[cm]?ts|tsx)):(\d+):(\d+)\)?$/gm)) {
      if (!isUserFile(m[1])) continue
      add(m[1], +m[2], +m[3], 'error', jsError, 'node')
      break
    }
  }
  return found
}

// —— 运行过程钩子（runpanel 调用）——
export function beginRun(file: string): void {
  runFile = file
  runText = ''
  setRunProblems([])
}
export function runStderr(text: string): void {
  runText += text
  if (runText.length > 200_000) runText = runText.slice(-200_000)
  setRunProblems(parseRunOutput(runText))
}

function setRunProblems(list: Problem[]): void {
  runProblems = list
  for (const model of monaco.editor.getModels()) applyRunMarkers(model)
  queueRender()
}

function applyRunMarkers(model: monaco.editor.ITextModel): void {
  const key = norm(model.uri.fsPath)
  const mine = runProblems.filter((p) => norm(p.file) === key)
  monaco.editor.setModelMarkers(model, RUN_OWNER, mine.map((p) => {
    const line = Math.min(p.line, model.getLineCount())
    const content = model.getLineContent(line)
    const start = Math.min(p.col, content.length + 1)
    // 从报错列标到该行（或下一个空白）末尾，至少一个字符
    const rest = content.slice(start - 1)
    const len = Math.max(1, rest.search(/\s|$/) || rest.trimEnd().length || 1)
    return {
      severity: p.severity === 'error' ? monaco.MarkerSeverity.Error : p.severity === 'warning' ? monaco.MarkerSeverity.Warning : monaco.MarkerSeverity.Info,
      message: p.message,
      source: p.source,
      startLineNumber: line, startColumn: start,
      endLineNumber: line, endColumn: p.col > 1 ? start + len : content.length + 1
    }
  }))
}

// —— 汇总 + 渲染 ——
export function allProblems(): Problem[] {
  const list: Problem[] = [...runProblems]
  for (const m of monaco.editor.getModelMarkers({})) {
    if (m.owner === RUN_OWNER || m.severity < monaco.MarkerSeverity.Info) continue
    list.push({
      file: m.resource.fsPath,
      line: m.startLineNumber,
      col: m.startColumn,
      severity: m.severity === monaco.MarkerSeverity.Error ? 'error' : m.severity === monaco.MarkerSeverity.Warning ? 'warning' : 'info',
      message: m.message,
      source: m.source ?? m.owner
    })
  }
  return list
}

function queueRender(): void {
  if (renderQueued) return
  renderQueued = true
  requestAnimationFrame(() => {
    renderQueued = false
    render()
  })
}

const ORDER: Record<Severity, number> = { error: 0, warning: 1, info: 2 }

function render(): void {
  const list = allProblems()
  const errors = list.filter((p) => p.severity === 'error').length
  const warnings = list.filter((p) => p.severity === 'warning').length

  // 状态栏 + 标签徽标
  const status = $('st-problems')
  status.innerHTML = `${icon('error')}<span>${errors}</span>${icon('warning')}<span>${warnings}</span>`
  status.classList.toggle('has-errors', errors > 0)
  status.title = errors + warnings ? `${errors} 个错误，${warnings} 个警告（点击查看）` : '没有发现问题'
  const badge = $('problems-count')
  badge.textContent = list.length ? String(list.length) : ''
  badge.classList.toggle('err', errors > 0)

  const body = $('problems-list')
  body.replaceChildren()
  if (!list.length) {
    body.append(el('div', 'hint', '没有发现问题。运行代码后，编译 / 运行报错会自动列在这里，点一下就能跳到出错的行。'))
    return
  }
  const groups = new Map<string, Problem[]>()
  for (const p of list) {
    const key = norm(p.file)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(p)
  }
  for (const items of groups.values()) {
    items.sort((a, b) => ORDER[a.severity] - ORDER[b.severity] || a.line - b.line || a.col - b.col)
    const file = items[0].file
    const name = file.split(/[\\/]/).pop() ?? file
    const head = el('div', 'pr-file')
    head.insertAdjacentHTML('beforeend', fileBadge(name))
    head.append(el('span', 'pr-name', name))
    const rel = relativePath(file)
    head.append(el('span', 'pr-dir', rel === file ? file.replace(/[\\/][^\\/]*$/, '') : rel.replace(/\/?[^/]*$/, '')))
    head.append(el('span', 'pr-count', String(items.length)))
    body.append(head)
    for (const p of items) {
      const row = el('button', 'pr-row ' + p.severity) as HTMLButtonElement
      row.type = 'button'
      row.innerHTML = icon(p.severity === 'error' ? 'error' : p.severity === 'warning' ? 'warning' : 'info')
      row.append(el('span', 'pr-msg', p.message))
      row.append(el('span', 'pr-src', p.source))
      row.append(el('span', 'pr-pos', `[行 ${p.line}，列 ${p.col}]`))
      row.title = p.message
      row.addEventListener('click', () => void revealLine(p.file, p.line, p.col))
      body.append(row)
    }
  }
}

export function initProblems(): void {
  monaco.editor.onDidChangeMarkers(() => queueRender())
  monaco.editor.onDidCreateModel((model) => applyRunMarkers(model))
  $('problems-clear').addEventListener('click', () => {
    runText = ''
    setRunProblems([])
  })
  $('st-problems').addEventListener('click', () => bus.emit('panel:show', 'problems'))
  render()
}

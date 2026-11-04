import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { bus, state, $, el } from './core'

self.MonacoEnvironment = { getWorker: () => new EditorWorker() }

export interface Tab {
  path: string
  model: monaco.editor.ITextModel
  encoding: string
  viewState: monaco.editor.ICodeEditorViewState | null
}

const tabs: Tab[] = []
let active: Tab | null = null
// 脏标记：保存时的 alternativeVersionId 与当前不一致 = 有未保存修改
const cleanVersion = new Map<string, number>()

const editor = monaco.editor.create($('editor'), {
  theme: 'vs-dark',
  automaticLayout: true,
  fontSize: 14,
  fontFamily: "'Cascadia Mono', Consolas, 'Microsoft YaHei UI', monospace",
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  tabSize: 4,
  smoothScrolling: true,
  cursorBlinking: 'smooth',
  padding: { top: 10 }
})

const LANG_BY_EXT: Record<string, string> = {
  c: 'cpp', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', h: 'cpp', hpp: 'cpp',
  py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript',
  json: 'json', md: 'markdown', html: 'html', css: 'css',
  java: 'java', go: 'go', rs: 'rust', sh: 'shell', bat: 'bat',
  toml: 'ini', ini: 'ini'
}

const LANG_LABEL: Record<string, string> = {
  cpp: 'C/C++', python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript',
  json: 'JSON', markdown: 'Markdown', html: 'HTML', css: 'CSS', java: 'Java',
  go: 'Go', rust: 'Rust', shell: 'Shell', bat: '批处理', ini: '配置'
}

export function langOfPath(p: string): string {
  const ext = p.split('.').pop()?.toLowerCase() ?? ''
  return LANG_BY_EXT[ext] ?? 'plaintext'
}

function langLabel(p: string): string {
  const l = langOfPath(p)
  return l === 'plaintext' ? '纯文本' : LANG_LABEL[l] ?? l
}

export function isDirty(t: Tab): boolean {
  return cleanVersion.get(t.path) !== t.model.getAlternativeVersionId()
}

function refreshWelcome(): void {
  const has = tabs.length > 0
  $('welcome').classList.toggle('hidden', has)
  if (!has) {
    $('lang-badge').textContent = '—'
    state.openFile = null
  }
}

function renderStrip(): void {
  const strip = $('tabstrip')
  strip.innerHTML = ''
  strip.classList.toggle('empty', tabs.length === 0)
  for (const t of tabs) {
    const name = t.path.split(/[\\/]/).pop() ?? t.path
    const tabEl = el('div', 'tab' + (t === active ? ' active' : ''))
    const label = el('span', '', name)
    label.title = t.path
    const dirty = el('span', 'dirty', isDirty(t) ? '●' : '')
    const close = el('span', 'close', '✕')
    tabEl.append(label, dirty, close)
    tabEl.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).classList.contains('close')) closeTab(t)
      else activate(t)
    })
    strip.append(tabEl)
  }
}

function activate(t: Tab): void {
  if (active && active !== t) active.viewState = editor.saveViewState()
  active = t
  state.openFile = t.path
  editor.setModel(t.model)
  if (t.viewState) editor.restoreViewState(t.viewState)
  editor.focus()
  $('lang-badge').textContent = langLabel(t.path)
  $('st-encoding').textContent = t.encoding.toUpperCase()
  renderStrip()
  refreshWelcome()
  bus.emit('tab:active', t.path)
}

export async function openFile(p: string): Promise<void> {
  const exist = tabs.find((t) => t.path === p)
  if (exist) {
    activate(exist)
    return
  }
  const r = await window.ide.readFile(p)
  const model = monaco.editor.createModel(r.text, langOfPath(p), monaco.Uri.file(p))
  cleanVersion.set(p, model.getAlternativeVersionId())
  model.onDidChangeContent(() => renderStrip())
  const t: Tab = { path: p, model, encoding: r.encoding, viewState: null }
  tabs.push(t)
  activate(t)
  bus.emit('tree:active', p)
}

export function activeTab(): Tab | null {
  return active
}

export function closeTab(t: Tab): void {
  const name = t.path.split(/[\\/]/).pop() ?? t.path
  if (isDirty(t) && !confirm(`「${name}」还没有保存，关闭将丢失修改。确定关闭吗？`)) return
  const i = tabs.indexOf(t)
  tabs.splice(i, 1)
  cleanVersion.delete(t.path)
  t.model.dispose()
  if (active === t) {
    active = null
    const next = tabs[Math.min(i, tabs.length - 1)]
    if (next) activate(next)
    else {
      editor.setModel(null)
      renderStrip()
      refreshWelcome()
    }
  } else {
    renderStrip()
  }
}

export async function saveActive(): Promise<string | null> {
  if (!active) return null
  await window.ide.writeFile(active.path, active.model.getValue())
  cleanVersion.set(active.path, active.model.getAlternativeVersionId())
  renderStrip()
  return active.path
}

export function revealLine(p: string, line: number, col?: number): void {
  void openFile(p).then(() => {
    editor.revealLineInCenter(line)
    editor.setPosition({ lineNumber: line, column: col ?? 1 })
    editor.focus()
  })
}

export function initCursorStatus(): void {
  editor.onDidChangeCursorPosition((e) => {
    $('st-cursor').textContent = `行 ${e.position.lineNumber}，列 ${e.position.column}`
  })
}

export { editor }

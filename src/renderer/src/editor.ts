import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import JsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import CssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import HtmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import TypeScriptWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'
import { bus, state, $, el, showFeedback } from './core'
import { icon, fileBadge } from './icons'
import { showMenu } from './menu'

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    if (label === 'json') return new JsonWorker()
    if (label === 'css' || label === 'scss' || label === 'less') return new CssWorker()
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new HtmlWorker()
    if (label === 'typescript' || label === 'javascript') return new TypeScriptWorker()
    return new EditorWorker()
  }
}

export interface Tab {
  canonicalPath: string
  path: string
  model: monaco.editor.ITextModel
  encoding: string
  viewState: monaco.editor.ICodeEditorViewState | null
}

const tabs: Tab[] = []
export const openEditorFiles = () => tabs.map(tab => tab.canonicalPath)
let active: Tab | null = null
// 脏标记：保存时的 alternativeVersionId 与当前不一致 = 有未保存修改
const cleanVersion = new Map<string, number>()

// 编辑器配色：与界面同一套颜色（ui.css 的 --bg / --accent 等）
monaco.editor.defineTheme('anvil-dark', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '636e86', fontStyle: 'italic' },
    { token: 'keyword', foreground: 'c792ea' },
    { token: 'string', foreground: 'b8e08a' },
    { token: 'number', foreground: 'f9a06b' },
    { token: 'type', foreground: 'ffcb6b' },
    { token: 'type.identifier', foreground: 'ffcb6b' },
    { token: 'delimiter', foreground: '8d97ad' },
    { token: 'operator', foreground: '89ddff' },
    { token: 'predefined', foreground: '82aaff' },
    { token: 'tag', foreground: 'f07178' },
    { token: 'attribute.name', foreground: 'ffcb6b' },
    { token: 'regexp', foreground: '89ddff' }
  ],
  colors: {
    'editor.background': '#171b24',
    'editor.foreground': '#e6e9f1',
    'editorLineNumber.foreground': '#465068',
    'editorLineNumber.activeForeground': '#c3cadb',
    'editor.lineHighlightBackground': '#ffffff07',
    'editor.lineHighlightBorder': '#00000000',
    'editor.selectionBackground': '#6e8fff40',
    'editor.inactiveSelectionBackground': '#6e8fff22',
    'editor.findMatchHighlightBackground': '#f4b54d33',
    'editorCursor.foreground': '#8ba6ff',
    'editorIndentGuide.background1': '#252b39',
    'editorIndentGuide.activeBackground1': '#3a4359',
    'editorBracketHighlight.foreground1': '#e2b86b',
    'editorBracketHighlight.foreground2': '#c792ea',
    'editorBracketHighlight.foreground3': '#7fb4ff',
    'editorBracketMatch.background': '#6e8fff22',
    'editorBracketMatch.border': '#6e8fff66',
    'editorWidget.background': '#1b202a',
    'editorWidget.border': '#344057',
    'editorSuggestWidget.background': '#1b202a',
    'editorSuggestWidget.border': '#344057',
    'editorSuggestWidget.selectedBackground': '#6e8fff2e',
    'editorHoverWidget.background': '#1b202a',
    'editorHoverWidget.border': '#344057',
    'editorGutter.background': '#171b24',
    'editorError.foreground': '#ff6b78',
    'editorWarning.foreground': '#f4b54d',
    'scrollbarSlider.background': '#7f8ca833',
    'scrollbarSlider.hoverBackground': '#7f8ca855',
    'minimap.background': '#151922'
  }
})

const editor = monaco.editor.create($('editor'), {
  theme: 'anvil-dark',
  automaticLayout: true,
  fontSize: 14,
  lineHeight: 22,
  fontFamily: "'Cascadia Code', 'Cascadia Mono', Consolas, 'Microsoft YaHei UI', monospace",
  fontLigatures: true,
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  tabSize: 4,
  smoothScrolling: true,
  cursorBlinking: 'smooth',
  cursorSmoothCaretAnimation: 'on',
  bracketPairColorization: { enabled: true },
  guides: { bracketPairs: 'active', indentation: true },
  renderLineHighlight: 'all',
  padding: { top: 12, bottom: 12 }
})

const LANG_BY_EXT: Record<string, string> = {
  c: 'cpp', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', h: 'cpp', hpp: 'cpp',
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', mts: 'typescript', cts: 'typescript', tsx: 'typescript', kt: 'kotlin', kts: 'kotlin',
  json: 'json', md: 'markdown', html: 'html', css: 'css',
  java: 'java', go: 'go', rs: 'rust', sh: 'shell', bat: 'bat',
  toml: 'ini', ini: 'ini'
}

const LANG_LABEL: Record<string, string> = {
  cpp: 'C/C++', python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript',
  json: 'JSON', markdown: 'Markdown', html: 'HTML', css: 'CSS', java: 'Java', kotlin: 'Kotlin',
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

export function hasUnsavedTabs(): boolean {
  return tabs.some(isDirty)
}

function refreshWelcome(): void {
  const has = tabs.length > 0
  $('welcome').classList.toggle('hidden', has)
  if (!has) {
    $('lang-badge').textContent = '—'
    state.openFile = null
    renderCrumbs()
  }
}

const slash = (p: string): string => p.replace(/\\/g, '/')
const trimEnd = (p: string): string => slash(p).replace(/\/+$/, '')
const samePath = (a: string, b: string): boolean => trimEnd(a).toLowerCase() === trimEnd(b).toLowerCase()
/** p 等于 dir 或位于 dir 之内 */
export const isUnder = (p: string, dir: string): boolean =>
  samePath(p, dir) || slash(p).toLowerCase().startsWith(trimEnd(dir).toLowerCase() + '/')

// 面包屑：工作区内显示「文件夹 › 子目录 › 文件」，否则显示路径最后几段
export function renderCrumbs(): void {
  const bar = $('breadcrumbs')
  bar.replaceChildren()
  bar.hidden = !active
  if (!active) return
  const file = slash(active.path)
  let parts: string[]
  if (state.folder && isUnder(file, state.folder)) {
    const root = trimEnd(state.folder)
    parts = [root.split('/').pop() ?? root, ...file.slice(root.length + 1).split('/')]
  } else {
    parts = file.split('/').filter(Boolean).slice(-3)
  }
  parts.forEach((part, i) => {
    if (i) bar.insertAdjacentHTML('beforeend', `<span class="crumb-sep">${icon('chevronRight')}</span>`)
    const last = i === parts.length - 1
    const crumb = el('span', 'crumb' + (last ? ' file' : ''))
    crumb.insertAdjacentHTML('beforeend', last ? fileBadge(part) : fileBadge(part, true))
    crumb.append(el('span', '', part))
    if (!last) {
      crumb.title = '在资源管理器中显示'
      crumb.addEventListener('click', () => bus.emit('pane:show', 'files'))
    }
    bar.append(crumb)
  })
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
    const close = el('span', 'close')
    close.innerHTML = icon('x')
    close.title = '关闭'
    // 标签名保持为第一个子元素（未保存标记按它查找）；徽标靠 CSS order 显示在最前
    tabEl.append(label, dirty, close)
    tabEl.insertAdjacentHTML('beforeend', fileBadge(name))
    tabEl.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).closest('.close')) closeTab(t)
      else activate(t)
    })
    // 中键关闭、右键菜单（与 VS Code 一致）
    tabEl.addEventListener('auxclick', (e) => { if (e.button === 1) { e.preventDefault(); closeTab(t) } })
    tabEl.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault() })
    tabEl.addEventListener('contextmenu', (e) => {
      e.preventDefault()
      tabMenu(t, e.clientX, e.clientY)
    })
    strip.append(tabEl)
    if (t === active) requestAnimationFrame(() => tabEl.scrollIntoView({ block: 'nearest', inline: 'nearest' }))
  }
  ;($('btn-save') as HTMLButtonElement).disabled = !active
  bus.emit('tabs:changed')
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
  renderCrumbs()
  refreshWelcome()
  bus.emit('tab:active', t.path)
  bus.emit('tree:active', t.path)
}

export async function openFile(p: string): Promise<void> {
  const exist = tabs.find((t) => t.path === p)
  if (exist) {
    activate(exist)
    return
  }
  const r = await window.ide.readFile(p)
  const canonicalPath = r.canonicalPath ?? p
  const opened = tabs.find((t) => t.canonicalPath === canonicalPath)
  if (opened) {
    activate(opened)
    return
  }
  const model = monaco.editor.createModel(r.text, langOfPath(p), monaco.Uri.file(p))
  cleanVersion.set(p, model.getAlternativeVersionId())
  const t: Tab = { path: p, canonicalPath, model, encoding: r.encoding, viewState: null }
  model.onDidChangeContent(() => {
    const badge = Array.from($('tabstrip').children).find((n) => (n.firstElementChild as HTMLElement)?.title === p)
      ?.querySelector('.dirty')
    if (badge) badge.textContent = isDirty(t) ? '●' : ''
    bus.emit('tabs:changed')
  })
  tabs.push(t)
  activate(t)
}

export function activeTab(): Tab | null {
  return active
}

export function allTabs(): readonly Tab[] {
  return tabs
}

/** 批量关闭：有未保存的先统一确认一次 */
export function closeTabs(list: Tab[]): boolean {
  const dirty = list.filter(isDirty)
  if (dirty.length) {
    const names = dirty.map((t) => '「' + (t.path.split(/[\\/]/).pop() ?? t.path) + '」').join('')
    if (!confirm(`${names} 还没有保存，关闭将丢失修改。确定关闭吗？`)) return false
  }
  for (const t of [...list]) if (tabs.includes(t)) closeTab(t, true)
  return true
}

export function cycleTab(delta: number): void {
  if (tabs.length < 2 || !active) return
  activate(tabs[(tabs.indexOf(active) + delta + tabs.length) % tabs.length])
}

/** 某路径（文件或文件夹）下打开着的标签 */
export function tabsUnder(p: string): Tab[] {
  return tabs.filter((t) => isUnder(t.path, p) || isUnder(t.canonicalPath, p))
}

/** 重命名后：关掉旧标签并在新路径重新打开（调用前已确认没有未保存修改） */
export async function reopenMoved(oldPath: string, newPath: string): Promise<void> {
  const moved = tabsUnder(oldPath)
  const activePath = active && moved.includes(active) ? active.path : null
  for (const t of moved) closeTab(t, true)
  const target = (p: string): string => newPath + p.slice(trimEnd(oldPath).length)
  for (const t of moved) {
    if (t.path === activePath) continue
    await openFile(target(t.path)).catch((error) => showFeedback('无法重新打开：' + String(error)))
  }
  if (activePath) await openFile(target(activePath)).catch((error) => showFeedback('无法重新打开：' + String(error)))
}

export function relativePath(p: string): string {
  if (!state.folder || !isUnder(p, state.folder)) return p
  return slash(p).slice(trimEnd(state.folder).length + 1)
}

export async function copyText(text: string): Promise<void> {
  await navigator.clipboard.writeText(text)
  showFeedback('已复制：' + text)
}

function tabMenu(t: Tab, x: number, y: number): void {
  const index = tabs.indexOf(t)
  showMenu([
    { label: '关闭', shortcut: 'Ctrl+W', icon: 'x', run: () => closeTab(t) },
    { label: '关闭其他标签', enabled: () => tabs.length > 1, run: () => { closeTabs(tabs.filter((x) => x !== t)) } },
    { label: '关闭右侧标签', enabled: () => index < tabs.length - 1, run: () => { closeTabs(tabs.slice(index + 1)) } },
    { label: '关闭已保存的标签', enabled: () => tabs.some((x) => !isDirty(x)), run: () => { closeTabs(tabs.filter((x) => !isDirty(x))) } },
    { label: '全部关闭', run: () => { closeTabs([...tabs]) } },
    '-',
    { label: '复制路径', icon: 'copy', run: () => copyText(t.path) },
    { label: '复制相对路径', enabled: () => Boolean(state.folder && isUnder(t.path, state.folder)), run: () => copyText(relativePath(t.path)) },
    { label: '在资源管理器中显示', icon: 'files', run: () => { activate(t); bus.emit('pane:show', 'files') } }
  ], { x, y }, { onError: (e) => showFeedback(String(e)) })
}

export async function updateFromChat(file: string, write: () => Promise<{ text: string; undo: boolean }>): Promise<boolean> {
  const same = (p: string) => p.replaceAll('\\', '/').toLowerCase() === file.replaceAll('\\', '/').toLowerCase()
  const matching = () => tabs.filter(t => same(t.canonicalPath))
  if (matching().some(isDirty)) throw new Error('目标标签有未保存修改，请先保存并重新核对提议')
  if (active && same(active.canonicalPath) && editor.getRawOptions().readOnly) throw new Error('请先结束该文件的调试')
  const result = await write()
  for (const tab of matching()) {
    if (isDirty(tab)) continue
    tab.model.setValue(result.text)
    cleanVersion.set(tab.path, tab.model.getAlternativeVersionId())
  }
  renderStrip()
  return result.undo
}

export function closeTab(t: Tab, force = false): void {
  const name = t.path.split(/[\\/]/).pop() ?? t.path
  if (!force && isDirty(t) && !confirm(`「${name}」还没有保存，关闭将丢失修改。确定关闭吗？`)) return
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
      bus.emit('tree:active', null)
    }
  } else {
    renderStrip()
  }
}

export async function saveActive(): Promise<string | null> {
  const tab = active
  if (!tab) return null
  await saveTab(tab)
  return tab.path
}

async function saveTab(tab: Tab): Promise<void> {
  const version = tab.model.getAlternativeVersionId()
  await window.ide.writeFile(tab.canonicalPath, tab.model.getValue(undefined, true))
  if (tabs.includes(tab)) cleanVersion.set(tab.path, version)
  renderStrip()
}

export async function saveAll(): Promise<number> {
  const dirty = tabs.filter(isDirty)
  const results = await Promise.allSettled(dirty.map(saveTab))
  const failures = results.flatMap((result, index) =>
    result.status === 'rejected' ? [dirty[index].path.split(/[\\/]/).pop()] : []
  )
  if (failures.length) throw new Error('保存失败：' + failures.join('、'))
  return dirty.length
}

export async function revealLine(p: string, line: number, col?: number): Promise<void> {
  await openFile(p)
  const position = editor.getModel()!.validatePosition({ lineNumber: line, column: col ?? 1 })
  editor.setPosition(position)
  editor.revealPositionInCenter(position)
  editor.focus()
}

export function initCursorStatus(): void {
  editor.onDidChangeCursorSelection((e) => {
    const p = e.selection.getPosition()
    const selected = e.selection.isEmpty() ? 0 : editor.getModel()?.getValueLengthInRange(e.selection) ?? 0
    const extra = e.secondarySelections.length ? `（${e.secondarySelections.length + 1} 处光标）` : selected ? `（已选 ${selected}）` : ''
    $('st-cursor').textContent = `行 ${p.lineNumber}，列 ${p.column}${extra}`
  })
}

export { editor }

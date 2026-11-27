import * as monaco from 'monaco-editor'
import { $, el, bus, showFeedback } from './core'
import { editor, activeTab, revealLine, relativePath } from './editor'
import { icon, fileBadge } from './icons'

// —— 书签（对标 VS：Ctrl+K Ctrl+K 切换，Ctrl+K Ctrl+N / P 下一个 / 上一个，Ctrl+K Ctrl+L 全部清除）——
// 打开的文件用装饰记录（随编辑自动移动）；所有书签按「文件 → 行号 + 该行内容」存在 localStorage。

interface Mark { line: number; text: string }
type Store = Record<string, { path: string; marks: Mark[] }>
const KEY = 'anvil.bookmarks'
const norm = (p: string): string => p.replace(/\//g, '\\').toLowerCase()

let store: Store = load()
const decorations = new Map<monaco.editor.ITextModel, string[]>()

function load(): Store {
  try { return JSON.parse(localStorage.getItem(KEY) ?? '{}') as Store } catch { return {} }
}
function save(): void {
  localStorage.setItem(KEY, JSON.stringify(store))
  bus.emit('bookmarks:changed')
}

const decoration = (line: number): monaco.editor.IModelDeltaDecoration => ({
  range: new monaco.Range(line, 1, line, 1),
  options: {
    isWholeLine: true,
    linesDecorationsClassName: 'bookmark-mark',
    className: 'bookmark-line',
    overviewRuler: { color: '#7fa2ff', position: monaco.editor.OverviewRulerLane.Left },
    stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges
  }
})

const pathOf = (model: monaco.editor.ITextModel): string => model.uri.fsPath

/** 当前模型的书签行（装饰的实时位置） */
function linesOf(model: monaco.editor.ITextModel): number[] {
  const lines = (decorations.get(model) ?? [])
    .map((id) => model.getDecorationRange(id)?.startLineNumber)
    .filter((l): l is number => typeof l === 'number')
  return [...new Set(lines)].sort((a, b) => a - b)
}

/** 把模型上的书签写回存储 */
function sync(model: monaco.editor.ITextModel): void {
  const key = norm(pathOf(model))
  const lines = linesOf(model)
  if (!lines.length && !store[key]) return
  if (lines.length) store[key] = { path: pathOf(model), marks: lines.map((line) => ({ line, text: model.getLineContent(line).trim().slice(0, 120) })) }
  else delete store[key]
  save()
}

function setLines(model: monaco.editor.ITextModel, lines: number[]): void {
  const unique = [...new Set(lines)].sort((a, b) => a - b)
  decorations.set(model, model.deltaDecorations(decorations.get(model) ?? [], unique.map(decoration)))
  sync(model)
}

function attach(model: monaco.editor.ITextModel): void {
  if (decorations.has(model)) return
  const saved = store[norm(pathOf(model))]
  const lines = (saved?.marks ?? []).map((m) => m.line).filter((l) => l >= 1 && l <= model.getLineCount())
  decorations.set(model, model.deltaDecorations([], lines.map(decoration)))
  let timer: ReturnType<typeof setTimeout> | undefined
  model.onDidChangeContent(() => {
    if (!decorations.get(model)?.length) return
    clearTimeout(timer)
    timer = setTimeout(() => sync(model), 400)
  })
  model.onWillDispose(() => { sync(model); decorations.delete(model) })
}

export function toggleBookmark(): void {
  const model = editor.getModel()
  const pos = editor.getPosition()
  if (!model || !pos || !activeTab()) return showFeedback('先打开一个文件，再给某一行加书签')
  attach(model)
  const lines = linesOf(model)
  const has = lines.includes(pos.lineNumber)
  setLines(model, has ? lines.filter((l) => l !== pos.lineNumber) : [...lines, pos.lineNumber])
  showFeedback(has ? `已移除第 ${pos.lineNumber} 行的书签` : `已在第 ${pos.lineNumber} 行加书签（Ctrl+K Ctrl+N 跳到下一个）`)
}

export function allBookmarks(): Array<{ path: string; line: number; text: string }> {
  for (const model of decorations.keys()) {
    const lines = linesOf(model)
    const key = norm(pathOf(model))
    if (lines.length) store[key] = { path: pathOf(model), marks: lines.map((line) => ({ line, text: model.getLineContent(line).trim().slice(0, 120) })) }
  }
  return Object.values(store)
    .sort((a, b) => a.path.localeCompare(b.path, 'zh-CN'))
    .flatMap((f) => f.marks.map((m) => ({ path: f.path, line: m.line, text: m.text })))
}

/** 跨文件按「文件顺序 + 行号」跳到下一个 / 上一个书签（到头后循环） */
export async function jumpBookmark(dir: 1 | -1): Promise<void> {
  const all = allBookmarks()
  if (!all.length) return showFeedback('还没有书签。按 Ctrl+K Ctrl+K 给当前行加一个')
  const model = editor.getModel()
  const curPath = model ? pathOf(model) : ''
  const cur = norm(curPath)
  const line = editor.getPosition()?.lineNumber ?? 0
  // 当前位置在整个列表里的「插入点」（与 allBookmarks 的排序规则一致）
  let at = all.findIndex((b) => {
    const order = norm(b.path) === cur ? 0 : b.path.localeCompare(curPath, 'zh-CN')
    return order > 0 || (order === 0 && b.line >= line)
  })
  if (at < 0) at = all.length
  const onCurrent = at < all.length && norm(all[at].path) === cur && all[at].line === line
  const index = dir > 0 ? (onCurrent ? at + 1 : at) : at - 1
  const target = all[(index + all.length) % all.length]
  await revealLine(target.path, target.line)
}

export function clearBookmarks(): void {
  if (!Object.keys(store).length && ![...decorations.values()].some((ids) => ids.length)) return
  if (!confirm('确定清除全部书签吗？')) return
  for (const [model, ids] of decorations) decorations.set(model, model.deltaDecorations(ids, []))
  store = {}
  save()
}

function removeMark(path: string, line: number): void {
  const model = [...decorations.keys()].find((m) => norm(pathOf(m)) === norm(path))
  if (model) return setLines(model, linesOf(model).filter((l) => l !== line))
  const entry = store[norm(path)]
  if (!entry) return
  entry.marks = entry.marks.filter((m) => m.line !== line)
  if (!entry.marks.length) delete store[norm(path)]
  save()
}

function render(): void {
  const list = $('bookmarks-list')
  const items = allBookmarks()
  $('bookmarks-count').textContent = items.length ? String(items.length) : ''
  list.replaceChildren()
  if (!items.length) {
    list.append(el('div', 'hint', '还没有书签。在代码里按 Ctrl+K Ctrl+K（或右键 →「切换书签」）给当前行做个记号，之后在这里一点就能跳回去；Ctrl+K Ctrl+N / Ctrl+K Ctrl+P 在书签之间来回跳。'))
    return
  }
  for (const b of items) {
    const row = el('div', 'bm-row')
    const name = b.path.split(/[\\/]/).pop() ?? b.path
    row.innerHTML = `<span class="bm-flag">${icon('bookmark')}</span>`
    row.insertAdjacentHTML('beforeend', fileBadge(name))
    row.append(el('span', 'bm-file', `${name}:${b.line}`), el('span', 'bm-text', b.text || '（空行）'), el('span', 'bm-dir', relativePath(b.path)))
    const del = el('button', 'mini-btn icon-only bm-del') as HTMLButtonElement
    del.type = 'button'
    del.title = '删除这个书签'
    del.innerHTML = icon('x')
    del.addEventListener('click', (e) => { e.stopPropagation(); removeMark(b.path, b.line) })
    row.append(del)
    row.title = b.path
    row.addEventListener('click', () => void revealLine(b.path, b.line))
    list.append(row)
  }
}

export function initBookmarks(): void {
  for (const model of monaco.editor.getModels()) attach(model)
  monaco.editor.onDidCreateModel((model) => attach(model))
  bus.on('bookmarks:changed', render)
  $('bookmarks-clear').addEventListener('click', clearBookmarks)
  render()
}

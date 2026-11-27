import { $, el, bus, state } from './core'
import { allTabs, revealLine, relativePath } from './editor'
import { fileBadge } from './icons'

// —— 任务列表（对标 VS「任务列表」）：收集代码注释里的 TODO / FIXME / HACK / NOTE ——
// 打开了文件夹时搜整个项目，否则只看打开着的文件。

const TAGS = ['TODO', 'FIXME', 'HACK', 'NOTE'] as const
type Tag = typeof TAGS[number]
interface Task { path: string; line: number; tag: Tag; text: string }
// 必须出现在注释里（// # /* -- 之后），且标记是大写的整词
const PATTERN = /(?:\/\/|#|\/\*|--|<!--|\*)\s*(TODO|FIXME|HACK|NOTE)\b[:：]?\s*(.*?)\s*(?:\*\/|-->)?$/

function parse(path: string, line: number, text: string): Task | null {
  const m = text.match(PATTERN)
  return m ? { path, line, tag: m[1] as Tag, text: m[2] || '（没有写说明）' } : null
}

let running = 0

async function collect(): Promise<{ tasks: Task[]; limited: boolean }> {
  const tasks: Task[] = []
  let limited = false
  const seen = new Set<string>()
  const push = (t: Task | null): void => {
    if (!t) return
    const key = t.path.toLowerCase() + ':' + t.line
    if (seen.has(key)) return
    seen.add(key)
    tasks.push(t)
  }
  // 打开着的文件以编辑器里（可能未保存）的内容为准
  for (const tab of allTabs()) {
    tab.model.getLinesContent().forEach((text, i) => push(parse(tab.path, i + 1, text)))
  }
  if (state.folder) {
    const open = new Set(allTabs().map((t) => t.path.toLowerCase()))
    for (const tag of TAGS) {
      const found = await window.ide.searchWorkspace(state.folder, tag)
      limited ||= found.limited
      for (const m of found.items) if (!open.has(m.path.toLowerCase())) push(parse(m.path, m.line, m.preview))
    }
  }
  tasks.sort((a, b) => a.path.localeCompare(b.path, 'zh-CN') || a.line - b.line)
  return { tasks, limited }
}

async function refresh(): Promise<void> {
  const my = ++running
  const list = $('tasks-list')
  const status = $('tasks-status')
  status.textContent = '正在查找 TODO / FIXME …'
  let result
  try { result = await collect() } catch (error) {
    if (my === running) status.textContent = '查找失败：' + String(error)
    return
  }
  if (my !== running) return
  const { tasks, limited } = result
  $('tasks-count').textContent = tasks.length ? String(tasks.length) : ''
  list.replaceChildren()
  status.textContent = tasks.length
    ? `${tasks.length} 项任务${state.folder ? '（整个项目）' : '（打开的文件）'}${limited ? '，结果较多只显示了一部分' : ''}`
    : ''
  if (!tasks.length) {
    list.append(el('div', 'hint', '没有找到任务。在代码注释里写 TODO、FIXME、HACK 或 NOTE（例如 // TODO: 处理输入为负数的情况），保存后会列在这里，点一下就能跳过去。'))
    return
  }
  for (const t of tasks) {
    const row = el('button', 'task-row') as HTMLButtonElement
    row.type = 'button'
    const name = t.path.split(/[\\/]/).pop() ?? t.path
    row.append(el('span', 'task-tag t-' + t.tag.toLowerCase(), t.tag), el('span', 'task-text', t.text))
    row.insertAdjacentHTML('beforeend', fileBadge(name))
    row.append(el('span', 'task-where', `${relativePath(t.path)}:${t.line}`))
    row.title = `${t.tag}：${t.text}\n${t.path} 第 ${t.line} 行`
    row.addEventListener('click', () => void revealLine(t.path, t.line))
    list.append(row)
  }
}

export function initTasks(): void {
  let timer: ReturnType<typeof setTimeout> | undefined
  const later = (): void => { clearTimeout(timer); timer = setTimeout(() => void refresh(), 600) }
  $('tasks-refresh').addEventListener('click', () => void refresh())
  bus.on('panel:shown', (p) => { if (p === 'tasks') void refresh() })
  bus.on('file:saved', later)
  bus.on('folder:opened', later)
  later()
}

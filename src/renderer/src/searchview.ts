import { $, el, bus, state } from './core'
import { revealLine } from './editor'
import { icon, fileBadge } from './icons'
import type { WorkspaceMatch } from '../../shared/types'

// —— 侧栏「搜索」视图（对标 VS Code 的在文件中查找）：边输入边搜，按文件分组，点结果跳到对应行 ——

export function initSearchView(): void {
  const input = $('search-input') as HTMLInputElement
  const status = $('search-status')
  const results = $('search-results')
  const clear = $('search-clear') as HTMLButtonElement
  let timer: ReturnType<typeof setTimeout> | undefined
  let request = 0
  const collapsed = new Set<string>()

  function highlight(target: HTMLElement, text: string, query: string): void {
    const lower = text.toLowerCase()
    const needle = query.toLowerCase()
    let at = 0
    for (let i = lower.indexOf(needle); i >= 0 && needle; i = lower.indexOf(needle, at)) {
      target.append(text.slice(at, i), el('mark', '', text.slice(i, i + needle.length)))
      at = i + needle.length
    }
    target.append(text.slice(at))
  }

  function render(items: WorkspaceMatch[], query: string, limited: boolean): void {
    results.replaceChildren()
    const groups = new Map<string, WorkspaceMatch[]>()
    for (const item of items) {
      if (!groups.has(item.path)) groups.set(item.path, [])
      groups.get(item.path)!.push(item)
    }
    status.textContent = items.length
      ? `${groups.size} 个文件中有 ${items.length} 处结果${limited ? '（结果太多，只显示前一部分，请输入更具体的内容）' : ''}`
      : `没有找到「${query}」`
    for (const [path, matches] of groups) {
      const name = matches[0].relative.split('/').pop() ?? path
      const dir = matches[0].relative.slice(0, -name.length).replace(/\/$/, '')
      const head = el('button', 'sr-file' + (collapsed.has(path) ? ' collapsed' : '')) as HTMLButtonElement
      head.type = 'button'
      head.insertAdjacentHTML('beforeend', `<span class="tw">${icon('chevronRight')}</span>`)
      head.insertAdjacentHTML('beforeend', fileBadge(name))
      head.append(el('span', 'sr-name', name), el('span', 'sr-dir', dir), el('span', 'sr-count', String(matches.length)))
      head.title = matches[0].relative
      head.dataset.path = path
      const list = el('div', 'sr-list')
      head.addEventListener('click', () => {
        const now = !collapsed.has(path)
        if (now) collapsed.add(path)
        else collapsed.delete(path)
        head.classList.toggle('collapsed', now)
      })
      for (const m of matches) {
        const row = el('button', 'sr-row') as HTMLButtonElement
        row.type = 'button'
        const text = el('span', 'sr-text')
        highlight(text, m.preview.trimStart(), query)
        row.append(el('span', 'sr-line', String(m.line)), text)
        row.title = `${m.relative}:${m.line}`
        row.addEventListener('click', () => {
          results.querySelectorAll('.sr-row.active').forEach((n) => n.classList.remove('active'))
          row.classList.add('active')
          void revealLine(m.path, m.line, m.column)
        })
        list.append(row)
      }
      results.append(head, list)
    }
  }

  async function run(): Promise<void> {
    const query = input.value.trim()
    const id = ++request
    clear.hidden = !input.value
    if (!query) {
      results.replaceChildren()
      status.textContent = ''
      return
    }
    if (!state.folder) {
      results.replaceChildren()
      status.textContent = '请先打开一个文件夹，再在整个项目里搜索。'
      return
    }
    status.textContent = '正在搜索…'
    try {
      const found = await window.ide.searchWorkspace(state.folder, query)
      if (id !== request) return
      render(found.items, query, found.limited)
    } catch (error) {
      if (id === request) status.textContent = '搜索失败：' + String(error)
    }
  }

  input.addEventListener('input', () => {
    clearTimeout(timer)
    timer = setTimeout(() => void run(), 260)
  })
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { clearTimeout(timer); void run() }
    if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; void run() }
  })
  clear.addEventListener('click', () => { input.value = ''; void run(); input.focus() })
  $('search-collapse').addEventListener('click', () => {
    for (const head of results.querySelectorAll<HTMLElement>('.sr-file')) {
      head.classList.add('collapsed')
      if (head.dataset.path) collapsed.add(head.dataset.path)
    }
  })
  $('search-refresh').addEventListener('click', () => void run())

  bus.on('pane:shown', (name) => {
    if (name !== 'search') return
    input.focus()
    input.select()
    if (input.value.trim()) void run()
  })
  // 编辑器里选中的文字 → 带进搜索框（与 VS Code 相同）
  bus.on('search:seed', (text) => {
    if (typeof text === 'string' && text.trim() && text.length < 120 && !text.includes('\n')) input.value = text
  })
}

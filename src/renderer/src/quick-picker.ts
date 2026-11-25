import { $, el, state } from './core'
import type { WorkspaceFile } from '../../shared/types'

export type PickerMode = 'files' | 'search' | 'commands'

interface Choice {
  title: string
  detail?: string
  run: () => void | Promise<void>
}

const fileOrder = new Intl.Collator('zh-CN')

const titles: Record<PickerMode, string> = {
  files: '快速打开',
  search: '在项目中搜索',
  commands: '搜索命令'
}

const placeholders: Record<PickerMode, string> = {
  files: '输入文件名或相对路径',
  search: '输入要查找的文本',
  commands: '输入命令名称或快捷键'
}

export function initQuickPicker(actions: {
  commands: () => Choice[]
  openFile: (path: string, line?: number, column?: number) => Promise<void>
  onError: (error: unknown) => void
}): (mode: PickerMode) => void {
  const dialog = $('quick-dialog') as HTMLDialogElement
  const input = $('quick-input') as HTMLInputElement
  const searchButton = $('quick-search') as HTMLButtonElement
  const list = $('quick-results')
  const status = $('quick-status')
  let mode: PickerMode
  let root: string | null
  let request = 0
  let files: WorkspaceFile[] | null = null
  let indexLimited = false
  let lastQuery = ''
  let searching = false
  let choices: Choice[] = []
  let selected = 0

  function select(index: number): void {
    selected = Math.max(0, Math.min(choices.length - 1, index))
    for (const [i, row] of Array.from(list.children).entries()) {
      row.setAttribute('aria-selected', String(i === selected))
    }
    const row = list.children[selected]
    if (row) {
      input.setAttribute('aria-activedescendant', row.id)
      row.scrollIntoView({ block: 'nearest' })
    } else {
      input.removeAttribute('aria-activedescendant')
    }
  }

  function render(items: Choice[], message = ''): void {
    choices = items
    list.replaceChildren(...items.map((item, i) => {
      const row = el('button', 'quick-result') as HTMLButtonElement
      row.type = 'button'
      row.tabIndex = -1
      row.id = 'quick-result-' + i
      row.setAttribute('role', 'option')
      row.append(el('span', 'quick-result-title', item.title))
      if (item.detail) row.append(el('span', 'quick-result-detail', item.detail))
      row.addEventListener('click', () => pick(i))
      return row
    }))
    input.setAttribute('aria-expanded', String(items.length > 0))
    status.textContent = message
    select(0)
  }

  function pick(index: number): void {
    const item = choices[index]
    if (!item) return
    dialog.close()
    void Promise.resolve().then(item.run).catch(actions.onError)
  }

  function renderFilter(): void {
    const query = input.value.trim().toLowerCase()
    if (mode === 'commands') {
      const items = actions.commands().filter((item) => `${item.title} ${item.detail ?? ''}`.toLowerCase().includes(query))
      render(items, items.length ? '' : '没有匹配的命令')
      return
    }
    if (!files) return render([], '正在读取项目文件…')
    const parts = query.replaceAll('\\', '/').split(/\s+/).filter(Boolean)
    const matches = files.flatMap((file) => {
      const relative = file.relative.replaceAll('\\', '/')
      const name = relative.split('/').pop()!
      const pathKey = relative.toLowerCase()
      if (!parts.every((part) => pathKey.includes(part))) return []
      const nameKey = name.toLowerCase()
      const rank = !query ? 0 : nameKey === query ? 3 : nameKey.startsWith(query) ? 2 : nameKey.includes(query) ? 1 : 0
      return [{ file, name, relative, rank }]
    })
    matches.sort((a, b) => b.rank - a.rank || fileOrder.compare(a.name, b.name) || fileOrder.compare(a.relative, b.relative))
    const items = matches.slice(0, 60).map(({ file, name, relative }) => ({
      title: name,
      detail: relative === name ? undefined : relative,
      run: () => actions.openFile(file.path)
    }))
    const truncated = indexLimited || items.length < matches.length
    render(items, [items.length ? '' : '没有匹配的文件', truncated ? '扫描或显示范围已达上限，可缩小搜索词。' : ''].filter(Boolean).join(' '))
  }

  async function search(): Promise<void> {
    const folder = root
    const query = input.value.trim()
    if (mode !== 'search' || !folder || !query || searching) return
    const current = ++request
    lastQuery = query
    searching = searchButton.disabled = true
    render([], '正在搜索已保存的文件…')
    try {
      const { items, limited } = await window.ide.searchWorkspace(folder, query)
      if (current !== request || !dialog.open) return
      if (input.value.trim() !== query || state.folder !== folder) {
        lastQuery = ''
        render([], '按 Enter 搜索当前输入')
        return
      }
      render(items.map((item) => ({
        title: `${item.relative}:${item.line}`,
        detail: item.preview,
        run: () => actions.openFile(item.path, item.line, item.column)
      })), items.length
        ? `${items.length} 条匹配行${limited ? ' · 已达扫描或结果上限' : ''}`
        : limited ? '在已扫描的文件中没有匹配，扫描范围已达上限。' : '没有找到匹配的文本')
    } catch (error) {
      if (current === request && dialog.open) render([], '搜索失败：' + String(error))
    } finally {
      if (current === request) searching = searchButton.disabled = false
    }
  }

  input.addEventListener('input', () => {
    if (mode !== 'commands' && !root) return
    if (mode !== 'search') renderFilter()
    else if (input.value.trim() !== lastQuery || !choices.length) {
      lastQuery = ''
      render([], searching ? '正在完成上一次搜索…' : '按 Enter 搜索已保存的文本文件')
    }
  })
  input.addEventListener('keydown', (event) => {
    if (event.isComposing) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      select(selected + (event.key === 'ArrowDown' ? 1 : -1))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (mode === 'search' && root && (!choices.length || input.value.trim() !== lastQuery)) void search()
      else pick(selected)
    }
  })
  $('quick-form').addEventListener('submit', (event) => {
    event.preventDefault()
    void search()
  })
  $('quick-close').addEventListener('click', () => dialog.close())
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close() })

  const openFolderChoice = { title: '打开文件夹', run: () => $('btn-open').click() }
  return (next) => {
    const current = ++request
    mode = next
    root = state.folder
    files = null
    indexLimited = false
    lastQuery = ''
    searching = searchButton.disabled = false
    searchButton.hidden = mode !== 'search' || !root
    input.readOnly = mode !== 'commands' && !root
    input.value = ''
    input.placeholder = input.readOnly ? '先打开一个项目文件夹' : placeholders[mode]
    input.setAttribute('aria-label', titles[mode])
    $('quick-title').textContent = titles[mode]
    if (!dialog.open) dialog.showModal()
    input.focus()

    const folder = root
    if (mode === 'commands') return renderFilter()
    if (!folder) return render([openFolderChoice])
    if (mode === 'search') return render([], '按 Enter 搜索已保存的文本文件（单个文件最多 512 KB）')
    renderFilter()
    void window.ide.workspaceFiles(folder).then(({ items, limited }) => {
      if (current !== request || !dialog.open) return
      if (state.folder !== folder) return render([], '项目文件夹已切换，请重新打开快速打开。')
      files = items
      indexLimited = limited
      renderFilter()
    }).catch((error) => {
      if (current === request && dialog.open) render([], '读取文件失败：' + String(error))
    })
  }
}

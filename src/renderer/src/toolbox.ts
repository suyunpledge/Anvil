import { $, el, bus, showFeedback, type PanelName } from './core'
import { activeTab, editor, hasUnsavedTabs, saveAll } from './editor'
import { initQuickPicker, type PickerMode } from './quick-picker'
import { toggleSetting, zoom } from './settings'
import { showShortcuts } from './dialogs'
import type { initDebugger } from './debugger'

type Panel = PanelName

interface Tool {
  title: string
  shortcut?: string
  available?: () => boolean
  run: () => void | Promise<void>
}

/** 打开侧栏「搜索」视图，并把编辑器里选中的文字带进去 */
export function openSearchView(): void {
  const selection = editor.getSelection()
  const text = selection && !selection.isEmpty() ? editor.getModel()?.getValueInRange(selection) ?? '' : ''
  if (text) bus.emit('search:seed', text)
  bus.emit('pane:show', 'search')
}

export async function editorAction(id: string): Promise<void> {
  editor.focus()
  const action = editor.getAction(id)
  if (!action?.isSupported()) {
    showFeedback('当前文件无法执行这项操作')
    return
  }
  await action.run()
}

export function initToolbox(options: {
  showPanel: (panel: Panel) => Promise<void>
  openFile: (path: string, line?: number, column?: number) => Promise<void>
  debug: ReturnType<typeof initDebugger>
}): (mode: PickerMode) => void {
  const host = $('editor-host')
  const toolbox = $('toolbox')
  const toggle = $('toolbox-toggle') as HTMLButtonElement
  const panel = $('toolbox-panel')
  const groups = $('toolbox-groups')
  const feedback = (error: unknown): void => showFeedback(error instanceof Error ? error.message : String(error))
  const hasFile = (): boolean => Boolean(activeTab())
  const formatIsAvailable = (): boolean => {
    const language = activeTab()?.model.getLanguageId() ?? ''
    return ['json', 'javascript', 'typescript', 'css', 'html'].includes(language)
  }
  function toggleWrap(): void {
    toggleSetting('wordWrap')
    editor.focus()
  }
  function toggleMinimap(): void {
    toggleSetting('minimap')
    editor.focus()
  }

  const openPicker = initQuickPicker({
    commands: () => tools.filter((tool) => tool.available?.() !== false)
      .map((tool) => ({ title: tool.title, detail: tool.shortcut, run: tool.run })),
    openFile: options.openFile,
    onError: feedback
  })
  const sections: Record<string, Tool[]> = {
    AI: [
      { title: '打开 AI 代码助手', run: () => bus.emit('ai:open', 'code') },
      { title: '打开 Work 文件整理（批量查找 / 移动文件）', run: () => bus.emit('ai:open', 'work') }
    ],
    导航: [
      { title: '在项目中搜索', shortcut: 'Ctrl+Shift+F', run: openSearchView },
      { title: '跳转到行', shortcut: 'Ctrl+G', available: hasFile, run: () => editorAction('editor.action.gotoLine') },
      { title: '跳转到符号（函数 / 变量）', shortcut: 'Ctrl+Shift+O', available: hasFile, run: () => editorAction('editor.action.quickOutline') },
      { title: '转到定义', shortcut: 'F12', available: hasFile, run: () => editorAction('editor.action.revealDefinition') }
    ],
    编辑: [
      { title: '在当前文件中查找', shortcut: 'Ctrl+F', available: hasFile, run: () => editorAction('actions.find') },
      { title: '在当前文件中替换', shortcut: 'Ctrl+H', available: hasFile, run: () => editorAction('editor.action.startFindReplaceAction') },
      { title: '切换行注释', shortcut: 'Ctrl+/', available: hasFile, run: () => editorAction('editor.action.commentLine') },
      { title: '格式化文档', shortcut: 'Shift+Alt+F', available: formatIsAvailable, run: () => editorAction('editor.action.formatDocument') },
      { title: '保存全部', available: hasUnsavedTabs, run: async () => {
        const count = await saveAll()
        if (count) showFeedback(`已保存 ${count} 个文件`)
      } }
    ],
    调试: [
      { title: '调试当前 C/C++ 文件', available: options.debug.available, run: options.debug.open },
      { title: '切换光标处断点', shortcut: 'F9', available: options.debug.canMark, run: options.debug.toggleAtCursor }
    ],
    视图: [
      { title: '切换自动换行', shortcut: 'Alt+Z', available: hasFile, run: toggleWrap },
      { title: '切换代码缩略图', run: toggleMinimap },
      { title: '资源管理器', shortcut: 'Ctrl+Shift+E', run: () => bus.emit('pane:show', 'files') },
      { title: '问题面板（错误列表）', shortcut: 'Ctrl+Shift+M', run: () => options.showPanel('problems') },
      { title: '打开输出面板', run: () => options.showPanel('out') },
      { title: '打开终端面板', shortcut: 'Ctrl+`', run: () => options.showPanel('term') },
      { title: '放大编辑器字号', shortcut: 'Ctrl+=', run: () => zoom(1) },
      { title: '缩小编辑器字号', shortcut: 'Ctrl+-', run: () => zoom(-1) },
      { title: '恢复默认字号', shortcut: 'Ctrl+0', run: () => zoom(0) }
    ],
    设置与帮助: [
      { title: '设置', shortcut: 'Ctrl+,', run: () => bus.emit('pane:show', 'settings') },
      { title: '键盘快捷键一览', run: showShortcuts }
    ]
  }
  const tools: Tool[] = [
    { title: '快速打开文件', shortcut: 'Ctrl+P', run: () => openPicker('files') },
    { title: '保存当前文件', shortcut: 'Ctrl+S', available: hasFile, run: () => $('btn-save').click() },
    ...Object.values(sections).flat()
  ]
  const availability = new Map<HTMLButtonElement, () => boolean>()

  for (const [title, commands] of Object.entries(sections)) {
    const detail = document.createElement('details')
    detail.name = 'toolbox-section'
    detail.append(el('summary', '', title))
    for (const command of commands) {
      const row = el('button', 'toolbox-command') as HTMLButtonElement
      row.type = 'button'
      row.append(el('span', '', command.title))
      if (command.shortcut) row.append(el('kbd', '', command.shortcut))
      if (command.available) availability.set(row, command.available)
      if (command.title === '格式化文档') row.title = '支持 JavaScript、TypeScript、JSON、HTML 和 CSS'
      row.addEventListener('click', () => {
        setOpen(false, true)
        void Promise.resolve().then(command.run).catch(feedback)
      })
      detail.append(row)
    }
    groups.append(detail)
  }

  let anchor: number | null = null
  let drag: { pointer: number; y: number; bottom: number; moved: boolean } | null = null
  let dragged = false
  const gutter = 14
  function layout(): void {
    const height = host.clientHeight
    const range = Math.max(0, height - toggle.offsetHeight - gutter * 2)
    const bottom = gutter + (anchor ?? 0) * range
    const upward = height - bottom >= bottom + toggle.offsetHeight
    toolbox.style.insetBlockEnd = `${bottom}px`
    panel.classList.toggle('opens-down', !upward)
    panel.style.maxHeight = `${Math.max(0, (upward ? height - bottom : bottom + toggle.offsetHeight) - 6)}px`
  }
  const updateAvailability = (): void => {
    for (const [button, available] of availability) button.disabled = !available()
  }
  function setOpen(open: boolean, refocus = false): void {
    panel.hidden = !open
    toggle.setAttribute('aria-expanded', String(open))
    if (open) {
      updateAvailability()
      layout()
    } else if (refocus) toggle.focus()
  }
  bus.on('tabs:changed', () => { if (!panel.hidden) updateAvailability() })
  new ResizeObserver(layout).observe(host)

  toggle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return
    dragged = false
    drag = { pointer: event.pointerId, y: event.clientY, bottom: parseFloat(toolbox.style.insetBlockEnd) || gutter, moved: false }
    toggle.setPointerCapture(event.pointerId)
  })
  toggle.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointer) return
    const delta = event.clientY - drag.y
    if (Math.abs(delta) < 5 && !drag.moved) return
    if (!drag.moved) setOpen(false)
    drag.moved = true
    const range = Math.max(1, host.clientHeight - toggle.offsetHeight - gutter * 2)
    anchor = Math.max(0, Math.min(1, (drag.bottom - gutter - delta) / range))
    layout()
  })
  const endDrag = (): void => {
    dragged = Boolean(drag?.moved)
    drag = null
  }
  toggle.addEventListener('pointerup', endDrag)
  toggle.addEventListener('pointercancel', endDrag)
  toggle.addEventListener('click', (event) => {
    if (dragged && event.detail > 0) return
    setOpen(panel.hidden)
  })

  $('toolbox-commands').addEventListener('click', () => {
    setOpen(false, true)
    openPicker('commands')
  })
  $('btn-quick-open').addEventListener('click', () => openPicker('files'))
  document.addEventListener('pointerdown', (event) => {
    if (!toolbox.contains(event.target as Node)) setOpen(false)
  })
  window.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.isComposing || document.querySelector('dialog[open]')) return
    if (event.key === 'Escape' && !panel.hidden) {
      setOpen(false, toolbox.contains(document.activeElement))
      return
    }
    const key = event.key.toLowerCase()
    if (!event.ctrlKey && !event.metaKey && event.altKey && !event.shiftKey && key === 'z' && hasFile()) {
      event.preventDefault()
      toggleWrap()
      return
    }
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return
    if (key === 'f' && event.shiftKey) {
      event.preventDefault()
      setOpen(false)
      openSearchView()
      return
    }
    const mode = key === 'p' ? event.shiftKey ? 'commands' : 'files' : null
    if (!mode) return
    event.preventDefault()
    setOpen(false)
    openPicker(mode)
  }, { capture: true })
  return openPicker
}

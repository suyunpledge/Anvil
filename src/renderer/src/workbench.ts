import * as monaco from 'monaco-editor'
import { $, bus, state, showFeedback, type PanelName } from './core'
import { activeTab, allTabs, closeTab, closeTabs, cycleTab, editor, hasUnsavedTabs, saveAll } from './editor'
import { initMenubar, showMenu, type MenuItem } from './menu'
import { newEntry } from './explorer'
import { settings, toggleSetting, zoom } from './settings'
import { editorAction, openSearchView } from './toolbox'
import { showShortcuts, showAbout } from './dialogs'
import type { PickerMode } from './quick-picker'
import type { initDebugger } from './debugger'

// —— 工作台：顶部菜单栏（对标 VS / VS Code）、全局快捷键、状态栏上的可点击项 ——

export function initWorkbench(ctx: {
  openPicker: (mode: PickerMode) => void
  showPanel: (panel: PanelName) => Promise<void>
  openFolderDlg: () => Promise<void>
  openFileDlg: () => Promise<void>
  openRecent: (path: string) => Promise<void>
  debug: ReturnType<typeof initDebugger>
}): void {
  const report = (error: unknown): void => showFeedback(error instanceof Error ? error.message : String(error))
  const hasFile = (): boolean => Boolean(activeTab())
  const hasFolder = (): boolean => Boolean(state.folder)
  const app = $('app')
  const sideVisible = (): boolean => !app.classList.contains('no-side')
  const panelVisible = (): boolean => !$('console-wrap').classList.contains('collapsed')
  const run = (id: string) => () => editorAction(id)
  const trigger = (handler: string) => () => { editor.focus(); editor.trigger('menu', handler, null) }
  const togglePanel = (): void => { $('console-wrap').classList.toggle('collapsed') }
  const toggleSide = (): void => { app.classList.toggle('no-side') }
  let recent: { recentFolders: string[]; recentFiles: string[] } = { recentFolders: [], recentFiles: [] }
  const loadRecent = (): void => { void window.ide.recentFolders().then((r) => { recent = r }).catch(() => undefined) }
  loadRecent()
  bus.on('folder:opened', loadRecent)
  bus.on('tab:active', loadRecent)

  const tail = (p: string): string => {
    const parts = p.split(/[\\/]/).filter(Boolean)
    return parts.length > 2 ? '…\\' + parts.slice(-2).join('\\') : p
  }

  const menus: Array<[string, () => MenuItem[]]> = [
    ['文件', () => [
      { label: '新建文件…', icon: 'filePlus', shortcut: 'Ctrl+N', run: () => hasFolder() ? newEntry(false) : $('btn-newfile').click() },
      { label: '新建文件夹…', icon: 'folderPlus', enabled: hasFolder, run: () => newEntry(true) },
      '-',
      { label: '打开文件…', icon: 'fileCode', shortcut: 'Ctrl+O', run: ctx.openFileDlg },
      { label: '打开文件夹…', icon: 'folderOpen', run: ctx.openFolderDlg },
      ...recent.recentFolders.filter((f) => f !== state.folder).slice(0, 3).map((f) => ({
        label: '最近：' + tail(f), icon: 'folder', run: () => ctx.openRecent(f)
      })),
      ...recent.recentFiles.slice(0, 3).map((f) => ({
        label: '最近：' + (f.split(/[\\/]/).pop() ?? f), icon: 'file', run: () => ctx.openRecent(f)
      })),
      '-',
      { label: '保存', icon: 'save', shortcut: 'Ctrl+S', enabled: hasFile, run: () => $('btn-save').click() },
      { label: '全部保存', shortcut: 'Ctrl+Shift+S', enabled: hasUnsavedTabs, run: saveEverything },
      { label: '自动保存', checked: () => settings().autoSave !== 'off', run: () => bus.emit('pane:show', 'settings') },
      '-',
      { label: '关闭编辑器', shortcut: 'Ctrl+W', enabled: hasFile, run: () => { const t = activeTab(); if (t) closeTab(t) } },
      { label: '关闭全部编辑器', enabled: () => allTabs().length > 0, run: () => { closeTabs([...allTabs()]) } },
      '-',
      { label: '设置', icon: 'settings', shortcut: 'Ctrl+,', run: () => bus.emit('pane:show', 'settings') },
      '-',
      { label: '退出', run: () => window.close() }
    ]],
    ['编辑', () => [
      { label: '撤销', icon: 'undo', shortcut: 'Ctrl+Z', enabled: hasFile, run: trigger('undo') },
      { label: '重做', shortcut: 'Ctrl+Y', enabled: hasFile, run: trigger('redo') },
      '-',
      { label: '剪切', shortcut: 'Ctrl+X', enabled: hasFile, run: run('editor.action.clipboardCutAction') },
      { label: '复制', icon: 'copy', shortcut: 'Ctrl+C', enabled: hasFile, run: run('editor.action.clipboardCopyAction') },
      { label: '粘贴', shortcut: 'Ctrl+V', enabled: hasFile, run: run('editor.action.clipboardPasteAction') },
      '-',
      { label: '查找', icon: 'search', shortcut: 'Ctrl+F', enabled: hasFile, run: run('actions.find') },
      { label: '替换', shortcut: 'Ctrl+H', enabled: hasFile, run: run('editor.action.startFindReplaceAction') },
      { label: '在项目中搜索', shortcut: 'Ctrl+Shift+F', run: openSearchView },
      '-',
      { label: '切换行注释', shortcut: 'Ctrl+/', enabled: hasFile, run: run('editor.action.commentLine') },
      { label: '向上移动行', shortcut: 'Alt+↑', enabled: hasFile, run: run('editor.action.moveLinesUpAction') },
      { label: '向下移动行', shortcut: 'Alt+↓', enabled: hasFile, run: run('editor.action.moveLinesDownAction') },
      { label: '向下复制行', shortcut: 'Shift+Alt+↓', enabled: hasFile, run: run('editor.action.copyLinesDownAction') },
      { label: '格式化文档', shortcut: 'Shift+Alt+F', enabled: () => ['json', 'javascript', 'typescript', 'css', 'html'].includes(activeTab()?.model.getLanguageId() ?? ''), run: run('editor.action.formatDocument') },
      '-',
      { label: '全选', shortcut: 'Ctrl+A', enabled: hasFile, run: run('editor.action.selectAll') }
    ]],
    ['视图', () => [
      { label: '命令面板…', icon: 'command', shortcut: 'Ctrl+Shift+P', run: () => ctx.openPicker('commands') },
      { label: '快速打开文件…', icon: 'search', shortcut: 'Ctrl+P', run: () => ctx.openPicker('files') },
      { label: '跳转到行…', shortcut: 'Ctrl+G', enabled: hasFile, run: run('editor.action.gotoLine') },
      { label: '跳转到符号…', shortcut: 'Ctrl+Shift+O', enabled: hasFile, run: run('editor.action.quickOutline') },
      '-',
      { label: '资源管理器', icon: 'files', shortcut: 'Ctrl+Shift+E', run: () => bus.emit('pane:show', 'files') },
      { label: '搜索', icon: 'search', shortcut: 'Ctrl+Shift+F', run: openSearchView },
      { label: 'AI 助手', icon: 'chat', shortcut: 'Ctrl+Shift+A', run: () => bus.emit('ai:open', 'code') },
      { label: 'Work 文件整理', icon: 'folders', shortcut: 'Ctrl+Shift+W', run: () => bus.emit('ai:open', 'work') },
      '-',
      { label: '问题', icon: 'problems', shortcut: 'Ctrl+Shift+M', run: () => ctx.showPanel('problems') },
      { label: '输出', icon: 'output', run: () => ctx.showPanel('out') },
      { label: '终端', icon: 'terminal', shortcut: 'Ctrl+`', run: () => ctx.showPanel('term') },
      '-',
      { label: '侧栏', shortcut: 'Ctrl+B', checked: sideVisible, run: toggleSide },
      { label: '底部面板', shortcut: 'Ctrl+J', checked: panelVisible, run: togglePanel },
      { label: '自动换行', shortcut: 'Alt+Z', checked: () => settings().wordWrap, run: () => toggleSetting('wordWrap') },
      { label: '代码缩略图', checked: () => settings().minimap, run: () => toggleSetting('minimap') },
      { label: '粘性滚动', checked: () => settings().stickyScroll, run: () => toggleSetting('stickyScroll') },
      '-',
      { label: '放大', icon: 'zoomIn', shortcut: 'Ctrl+=', run: () => zoom(1) },
      { label: '缩小', shortcut: 'Ctrl+-', run: () => zoom(-1) },
      { label: '重置缩放', shortcut: 'Ctrl+0', run: () => zoom(0) }
    ]],
    ['运行', () => [
      { label: '运行当前文件', icon: 'play', shortcut: 'F5', enabled: () => !($('btn-run') as HTMLButtonElement).disabled, run: () => $('btn-run').click() },
      { label: '停止运行', icon: 'stop', enabled: () => !($('btn-stop') as HTMLButtonElement).disabled, run: () => $('btn-stop').click() },
      '-',
      { label: '调试当前 C/C++ 文件', icon: 'bug', enabled: ctx.debug.available, run: ctx.debug.open },
      { label: '切换断点', shortcut: 'F9', enabled: ctx.debug.canMark, run: ctx.debug.toggleAtCursor },
      '-',
      { label: '检查运行环境', icon: 'cpu', run: () => $('st-compiler').click() }
    ]],
    ['终端', () => [
      { label: '打开终端', icon: 'terminal', shortcut: 'Ctrl+`', run: () => ctx.showPanel('term') },
      { label: '查看程序输出', icon: 'output', run: () => ctx.showPanel('out') },
      { label: '清空输出', icon: 'trash', run: () => $('console-clear').click() }
    ]],
    ['帮助', () => [
      { label: '键盘快捷键', icon: 'keyboard', run: showShortcuts },
      { label: '命令面板（搜索全部功能）', icon: 'command', shortcut: 'Ctrl+Shift+P', run: () => ctx.openPicker('commands') },
      '-',
      { label: '关于铁砧 IDE', icon: 'info', run: showAbout }
    ]]
  ]
  initMenubar($('menubar'), menus, report)

  async function saveEverything(): Promise<void> {
    const count = await saveAll()
    showFeedback(count ? `已保存 ${count} 个文件` : '没有需要保存的文件')
  }

  // —— 全局快捷键（编辑器内部已有的 Ctrl+F / Ctrl+/ 等交给 Monaco）——
  window.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.isComposing || document.querySelector('dialog[open]')) return
    const ctrl = e.ctrlKey || e.metaKey
    const key = e.key.toLowerCase()
    const handle = (fn: () => unknown): void => {
      e.preventDefault()
      e.stopPropagation()
      void Promise.resolve().then(fn).catch(report)
    }
    if (ctrl && e.key === 'Tab') return handle(() => cycleTab(e.shiftKey ? -1 : 1))
    if (!ctrl || e.altKey) return
    if (e.shiftKey) {
      if (key === 'e') return handle(() => bus.emit('pane:show', 'files'))
      if (key === 'm') return handle(() => ctx.showPanel('problems'))
      if (key === 's') return handle(saveEverything)
      return
    }
    if (key === 'w') return handle(() => { const t = activeTab(); if (t) closeTab(t) })
    if (key === 'n') return handle(() => hasFolder() ? newEntry(false) : $('btn-newfile').click())
    if (key === 'o') return handle(ctx.openFileDlg)
    if (key === ',') return handle(() => bus.emit('pane:show', 'settings'))
    if (key === '=' || key === '+') return handle(() => zoom(1))
    if (key === '-') return handle(() => zoom(-1))
    if (key === '0') return handle(() => zoom(0))
  }, { capture: true })

  // —— 编辑器里 Ctrl+滚轮缩放（与 VS Code 一致）——
  $('editor-host').addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return
    e.preventDefault()
    zoom(e.deltaY < 0 ? 1 : -1)
  }, { passive: false, capture: true })

  // 资源管理器里「还没打开文件夹」时的大按钮
  $('ex-open').addEventListener('click', () => void ctx.openFolderDlg().catch(report))

  // —— 状态栏：缩进、换行符、语言（点击可改，与 VS Code 一致）——
  const indent = $('st-indent') as HTMLButtonElement
  const eol = $('st-eol') as HTMLButtonElement
  const cursor = $('st-cursor') as HTMLButtonElement
  function renderStatus(): void {
    const model = editor.getModel()
    for (const item of [indent, eol, cursor, $('st-encoding'), $('st-lang')]) item.hidden = !model
    if (!model) return
    const o = model.getOptions()
    indent.textContent = o.insertSpaces ? `空格：${o.tabSize}` : `Tab 宽度：${o.tabSize}`
    eol.textContent = model.getEOL() === '\r\n' ? 'CRLF' : 'LF'
    $('st-lang').textContent = $('lang-badge').textContent ?? ''
  }
  editor.onDidChangeModel(renderStatus)
  editor.onDidChangeModelOptions(renderStatus)
  editor.onDidChangeModelContent(() => { if (eol.textContent !== (editor.getModel()?.getEOL() === '\r\n' ? 'CRLF' : 'LF')) renderStatus() })
  bus.on('settings:changed', renderStatus)
  bus.on('tab:active', renderStatus)
  renderStatus()

  cursor.addEventListener('click', () => void editorAction('editor.action.gotoLine'))
  indent.addEventListener('click', () => {
    const model = editor.getModel()
    if (!model) return
    const set = (insertSpaces: boolean, tabSize: number) => () => {
      model.updateOptions({ insertSpaces, tabSize, indentSize: tabSize })
      renderStatus()
    }
    showMenu([
      { label: '用空格缩进：2', checked: () => model.getOptions().insertSpaces && model.getOptions().tabSize === 2, run: set(true, 2) },
      { label: '用空格缩进：4', checked: () => model.getOptions().insertSpaces && model.getOptions().tabSize === 4, run: set(true, 4) },
      { label: '用 Tab 缩进', checked: () => !model.getOptions().insertSpaces, run: set(false, model.getOptions().tabSize) },
      '-',
      { label: '把缩进统一转成空格', run: run('editor.action.indentationToSpaces') },
      { label: '把缩进统一转成 Tab', run: run('editor.action.indentationToTabs') }
    ], indent, { onError: report, className: 'up' })
  })
  eol.addEventListener('click', () => {
    const model = editor.getModel()
    if (!model) return
    showMenu([
      { label: 'CRLF（Windows 默认）', checked: () => model.getEOL() === '\r\n', run: () => { model.pushEOL(monaco.editor.EndOfLineSequence.CRLF); renderStatus() } },
      { label: 'LF（Linux / macOS）', checked: () => model.getEOL() === '\n', run: () => { model.pushEOL(monaco.editor.EndOfLineSequence.LF); renderStatus() } }
    ], eol, { onError: report, className: 'up' })
  })
}

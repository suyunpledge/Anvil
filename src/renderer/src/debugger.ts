import * as monaco from 'monaco-editor'
import { $, bus, el, showFeedback, state } from './core'
import { activeTab, editor, isDirty, revealLine, saveActive } from './editor'
import type { DebugAction, DebugSnapshot } from '../../shared/types'

const isSource = (file: string): boolean => /\.(c|cc|cpp|cxx)$/i.test(file)
const fileKey = (file: string): string => file.replaceAll('\\', '/').toLowerCase()
const fileName = (file: string): string => file.split(/[\\/]/).pop() ?? file
const isLive = (view: DebugSnapshot | undefined): view is DebugSnapshot =>
  Boolean(view && view.phase !== 'ended' && view.phase !== 'error')

export function initDebugger(): {
  open: () => Promise<void>
  available: () => boolean
  canMark: () => boolean
  toggleAtCursor: () => Promise<void>
} {
  const pane = $('debug-pane')
  const stop = $('debug-stop') as HTMLButtonElement
  const controls = Array.from(pane.querySelectorAll<HTMLButtonElement>('[data-debug-action]'))
  const diagnostic = $('debug-diagnostic') as HTMLDetailsElement
  const executionLine = editor.createDecorationsCollection()
  const breakpoints = new WeakMap<monaco.editor.ITextModel, string[]>()
  const closedBreakpoints = new Map<string, number[]>()
  let current: DebugSnapshot | undefined
  let pending = false

  const canMark = (): boolean => isSource(activeTab()?.path ?? '')
  const available = (): boolean => isLive(current) || canMark()
  const note = (error: unknown): void =>
    showFeedback((error instanceof Error ? error.message : String(error)).replace(/^Error invoking remote method '[^']+': Error: /, ''))

  function marks(model: monaco.editor.ITextModel): Array<{ id: string; line: number }> {
    return (breakpoints.get(model) ?? []).flatMap((id) => {
      const range = model.getDecorationRange(id)
      return range ? [{ id, line: range.startLineNumber }] : []
    })
  }

  function breakpointDecoration(model: monaco.editor.ITextModel, line: number): monaco.editor.IModelDeltaDecoration {
    return {
      range: new monaco.Range(line, 1, line, model.getLineMaxColumn(line)),
      options: {
        glyphMarginClassName: 'debug-breakpoint',
        glyphMarginHoverMessage: { value: '点击移除断点 · F9' },
        stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges
      }
    }
  }

  function syncEditor(): void {
    const tab = activeTab()
    const file = tab?.path ?? ''
    if (tab && isSource(file) && !breakpoints.has(tab.model)) {
      const { model } = tab
      const key = fileKey(file)
      const lines = (closedBreakpoints.get(key) ?? []).filter(line => line <= model.getLineCount())
      closedBreakpoints.delete(key)
      breakpoints.set(model, model.deltaDecorations([], lines.map(line => breakpointDecoration(model, line))))
      model.onWillDispose(() => {
        const remaining = marks(model).map(mark => mark.line)
        if (remaining.length) closedBreakpoints.set(key, remaining)
      })
    }
    const locked = isLive(current) && fileKey(current.file) === fileKey(file)
    editor.updateOptions({ glyphMargin: isSource(file), readOnly: locked })
    const location = current?.location
    if (location && fileKey(location.file) === fileKey(file)) {
      executionLine.set([{
        range: new monaco.Range(location.line, 1, location.line, 1),
        options: { isWholeLine: true, className: 'debug-execution-line' }
      }])
    } else executionLine.clear()
  }

  function render(): void {
    const view = current
    if (!view) return
    $('debug-status').textContent = view.message
    $('debug-status').dataset.phase = view.phase
    const location = view.location
    $('debug-location').textContent = location
      ? fileName(location.file) + ':' + location.line + (location.functionName ? ' · ' + location.functionName : '')
      : fileName(view.file)
    $('debug-location').title = location?.file ?? view.file
    for (const button of controls) button.disabled = view.phase !== 'paused' || pending
    stop.disabled = !isLive(view) || !view.id || view.phase === 'stopping'
    $('debug-empty').textContent = view.variables.length ? '' : view.phase === 'paused' ? '当前没有可显示的变量' : '程序暂停后显示变量'
    $('debug-values').replaceChildren(...view.variables.map((item) => {
      const row = el('div', 'debug-variable')
      const name = el('dt', '', item.name)
      if (item.argument) name.title = '函数参数'
      row.append(name, el('dd', '', item.value || '—'))
      return row
    }))
    diagnostic.hidden = !view.details
    diagnostic.open = Boolean(view.details && view.phase === 'error')
    $('debug-detail').textContent = view.details ?? ''
    syncEditor()
  }

  function jump(view: DebugSnapshot): void {
    const location = view.location
    if (!location || pane.hidden) return
    const target = fileKey(location.file)
    const source = fileKey(view.file)
    const project = fileKey(state.folder ?? '').replace(/\/+$/, '')
    const directory = source.slice(0, source.lastIndexOf('/') + 1)
    if (target !== source && !target.startsWith(directory) && !(project && target.startsWith(project + '/'))) return
    const currentFile = activeTab()?.path
    const file = target === source ? view.file : currentFile && fileKey(currentFile) === target ? currentFile : location.file
    void revealLine(file, location.line).then(() => syncEditor()).catch(note)
  }

  async function toggleBreakpoint(line: number): Promise<void> {
    const tab = activeTab()
    if (!tab || !isSource(tab.path) || pending || line < 1 || line > tab.model.getLineCount()) return
    const model = tab.model
    const existing = marks(model)
    const onLine = existing.filter((mark) => mark.line === line)
    const adding = onLine.length === 0
    if (adding && existing.length >= 100) {
      showFeedback('单个文件最多设置 100 个断点')
      return
    }
    if (isLive(current) && fileKey(current.file) === fileKey(tab.path)) {
      if (current.phase !== 'paused') {
        showFeedback('调试暂停时可更改断点')
        return
      }
      pending = true
      render()
      try { await window.ide.debugBreakpoint(current.id, line, adding) }
      finally { pending = false; render() }
    }
    if (model.isDisposed()) return
    const newIds = model.deltaDecorations(onLine.map(mark => mark.id), adding ? [breakpointDecoration(model, line)] : [])
    breakpoints.set(model, [...existing.filter(mark => mark.line !== line).map(mark => mark.id), ...newIds])
  }

  const toggleAtCursor = async (): Promise<void> => {
    const line = editor.getPosition()?.lineNumber
    if (line) await toggleBreakpoint(line)
  }
  editor.onMouseDown((event) => {
    if (event.event.leftButton && event.target.type === monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN) {
      const line = event.target.position?.lineNumber
      if (line) void toggleBreakpoint(line).catch(note)
    }
  })
  window.addEventListener('keydown', (event) => {
    if (event.key !== 'F9' || event.repeat || event.isComposing || event.ctrlKey || event.metaKey ||
      event.altKey || event.shiftKey || !canMark() || document.querySelector('dialog[open]')) return
    event.preventDefault()
    void toggleAtCursor().catch(note)
  }, true)

  bus.on('tab:active', syncEditor)
  bus.on('tabs:changed', () => { if (!activeTab()) syncEditor() })
  window.ide.onDebugState((view) => {
    if (!current || (current.id !== view.id && (current.id !== 0 || current.file !== view.file))) return
    current = view
    render()
    if (view.phase === 'paused') jump(view)
  })

  $('debug-hide').addEventListener('click', () => {
    pane.hidden = true
    editor.focus()
  })
  stop.addEventListener('click', () => {
    if (isLive(current) && current.id) void window.ide.debugStop(current.id).catch(note)
  })
  for (const button of controls) {
    button.addEventListener('click', () => {
      if (current?.phase !== 'paused' || pending) return
      pending = true
      render()
      void window.ide.debugAction(current.id, button.dataset.debugAction as DebugAction).catch(note).finally(() => {
        pending = false
        render()
      })
    })
  }

  async function open(): Promise<void> {
    if (isLive(current)) {
      pane.hidden = false
      jump(current)
      return
    }
    const tab = activeTab()
    if (!tab || !isSource(tab.path)) return
    const lines = marks(tab.model).map(mark => mark.line)
    pane.hidden = false
    current = { id: 0, file: tab.path, phase: 'building', message: '正在保存并构建调试版本…', variables: [] }
    render()
    try {
      if (isDirty(tab)) await saveActive()
      const id = await window.ide.debugStart(tab.path, lines)
      if (current.id === 0) current = { ...current, id }
      render()
    } catch (error) {
      current = { ...current, phase: 'error', message: '无法开始调试', details: (error instanceof Error ? error.message : String(error)) }
      render()
    }
  }

  return { open, available, canMark, toggleAtCursor }
}

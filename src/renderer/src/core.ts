// 轻量事件总线：模块间解耦
type Handler = (payload?: unknown) => void

const handlers = new Map<string, Set<Handler>>()

export const bus = {
  on(ev: string, fn: Handler): () => void {
    if (!handlers.has(ev)) handlers.set(ev, new Set())
    handlers.get(ev)!.add(fn)
    return () => handlers.get(ev)?.delete(fn)
  },
  emit(ev: string, payload?: unknown): void {
    handlers.get(ev)?.forEach((fn) => fn(payload))
  }
}

// 全局共享状态（简单对象，够用）
export const state = {
  folder: null as string | null,
  openFile: null as string | null
}

export function $(id: string): HTMLElement {
  const el = document.getElementById(id)
  if (!el) throw new Error('missing #' + id)
  return el
}

export function el(tag: string, cls?: string, text?: string): HTMLElement {
  const n = document.createElement(tag)
  if (cls) n.className = cls
  if (text !== undefined) n.textContent = text
  return n
}

let feedbackTimeout: ReturnType<typeof setTimeout>
export function showFeedback(message: string): void {
  const area = $('ide-feedback')
  area.textContent = message
  clearTimeout(feedbackTimeout)
  feedbackTimeout = setTimeout(() => { area.textContent = '' }, 4500)
}

// —— VS 两段式组合键（Ctrl+K …、Ctrl+M …）的守卫 ——
// 编辑器里按了 Ctrl+K / Ctrl+M 后，下一个键属于组合键（如 Ctrl+K Ctrl+W 书签窗口），
// 不能再被全局快捷键（Ctrl+W 关标签、Ctrl+N 新建、Ctrl+O 打开、Ctrl+P 快速打开…）抢走。
// 本监听器在 core 模块加载时注册，排在所有全局快捷键之前。
let chordUntil = 0
let chordEvent: KeyboardEvent | null = null
window.addEventListener('keydown', (e) => {
  if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return
  const now = Date.now()
  if (chordUntil > now) {
    chordEvent = e
    chordUntil = 0
    return
  }
  chordEvent = null
  const key = e.key.toLowerCase()
  const inEditor = (document.activeElement as HTMLElement | null)?.closest?.('.monaco-editor')
  if (inEditor && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && (key === 'k' || key === 'm')) chordUntil = now + 2500
}, true)
/** 这个按键是不是组合键的第二段（全局快捷键应忽略它） */
export const isChordKey = (e: KeyboardEvent): boolean => e === chordEvent

// 状态栏左侧的状态文字（对标 VS 的「就绪」）：传 ms 时到点自动回到「就绪」
let statusTimer: ReturnType<typeof setTimeout> | undefined
export function setStatus(text: string, ms?: number, tone: 'busy' | 'ok' | 'error' | '' = ''): void {
  const node = document.getElementById('st-message')
  if (!node) return
  node.textContent = text
  node.dataset.tone = tone
  clearTimeout(statusTimer)
  if (ms) statusTimer = setTimeout(() => setStatus('就绪'), ms)
}

// 底部面板切换（问题 / 任务 / 书签 / 输出 / 终端）
export type PanelName = 'out' | 'term' | 'problems' | 'tasks' | 'bookmarks'
const PANEL_BODY: Record<PanelName, string> = {
  out: 'console-body', term: 'term-body', problems: 'problems-body', tasks: 'tasks-body', bookmarks: 'bookmarks-body'
}
// 面板标题栏右侧的按钮只在对应面板显示
const PANEL_ACTIONS: Record<string, PanelName> = {
  'console-clear': 'out', 'problems-clear': 'problems', 'tasks-refresh': 'tasks', 'bookmarks-clear': 'bookmarks'
}
export function showPanel(p: PanelName): void {
  document.querySelectorAll('.ptab').forEach((b) => {
    b.classList.toggle('active', (b as HTMLElement).dataset.panel === p)
  })
  for (const [name, id] of Object.entries(PANEL_BODY)) document.getElementById(id)?.classList.toggle('hidden', name !== p)
  for (const [id, owner] of Object.entries(PANEL_ACTIONS)) document.getElementById(id)?.toggleAttribute('hidden', owner !== p)
  bus.emit('panel:shown', p)
}

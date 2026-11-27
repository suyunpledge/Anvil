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

// 底部面板切换（问题 / 输出 / 终端）
export type PanelName = 'out' | 'term' | 'problems'
export function showPanel(p: PanelName): void {
  document.querySelectorAll('.ptab').forEach((b) => {
    b.classList.toggle('active', (b as HTMLElement).dataset.panel === p)
  })
  document.getElementById('console-body')?.classList.toggle('hidden', p !== 'out')
  document.getElementById('term-body')?.classList.toggle('hidden', p !== 'term')
  document.getElementById('problems-body')?.classList.toggle('hidden', p !== 'problems')
  // 「清空」只对输出有意义
  document.getElementById('console-clear')?.toggleAttribute('hidden', p !== 'out')
  document.getElementById('problems-clear')?.toggleAttribute('hidden', p !== 'problems')
}

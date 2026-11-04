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

// 底部面板切换（输出 / 终端）
export function showPanel(p: 'out' | 'term'): void {
  document.querySelectorAll('.ptab').forEach((b) => {
    b.classList.toggle('active', (b as HTMLElement).dataset.panel === p)
  })
  document.getElementById('console-body')?.classList.toggle('hidden', p !== 'out')
  document.getElementById('term-body')?.classList.toggle('hidden', p !== 'term')
}

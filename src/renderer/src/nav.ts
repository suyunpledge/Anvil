import { bus, showFeedback } from './core'
import { editor, activeTab, revealLine } from './editor'

// —— 向后 / 向前导航（对标 VS 工具栏的 ← →，快捷键 Alt+← / Alt+→）——
// 记录「跳转」：切换文件，或在同一文件里一次移动 10 行以上（点结果、转到定义、搜索跳行等）。

interface Loc { path: string; line: number; column: number }
const back: Loc[] = []
const forward: Loc[] = []
let last: Loc | null = null
let navigating = false

const same = (a: Loc, b: Loc): boolean => a.path === b.path && a.line === b.line

function changed(): void {
  bus.emit('nav:changed', { back: back.length > 0, forward: forward.length > 0 })
}

export function canGoBack(): boolean { return back.length > 0 }
export function canGoForward(): boolean { return forward.length > 0 }

async function go(from: Loc[], to: Loc[]): Promise<void> {
  const target = from.pop()
  if (!target) return
  if (last) to.push(last)
  navigating = true
  try {
    await revealLine(target.path, target.line, target.column)
  } catch {
    showFeedback('那个位置的文件已经打不开了')
  } finally {
    navigating = false
    last = target
    changed()
  }
}

export const goBack = (): Promise<void> => go(back, forward)
export const goForward = (): Promise<void> => go(forward, back)

let quietUntil = 0

function record(loc: Loc): void {
  if (back.length && same(back[back.length - 1], loc)) return
  back.push(loc)
  if (back.length > 60) back.shift()
  forward.length = 0
  changed()
}

export function initNav(): void {
  // 明确的跳转（书签、搜索结果、问题、大纲、转到行…）无论距离多远都记一笔
  bus.on('nav:before-jump', () => {
    if (navigating || !last) return
    record(last)
    quietUntil = Date.now() + 300
  })
  editor.onDidChangeCursorPosition((e) => {
    const tab = activeTab()
    if (!tab) return
    const loc: Loc = { path: tab.path, line: e.position.lineNumber, column: e.position.column }
    if (Date.now() < quietUntil) { last = loc; return }
    if (!navigating && last && !same(last, loc) && (last.path !== loc.path || Math.abs(last.line - loc.line) >= 10)) {
      back.push(last)
      if (back.length > 60) back.shift()
      forward.length = 0
      changed()
    }
    last = loc
  })
  changed()
}

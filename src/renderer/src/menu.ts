import { el } from './core'
import { icon } from './icons'

// —— 通用菜单：顶部菜单栏、右键菜单、状态栏下拉共用 ——

export interface MenuAction {
  label: string
  shortcut?: string
  icon?: string
  /** 返回 true 时显示勾选标记（开关类命令） */
  checked?: () => boolean
  /** 返回 false 时置灰 */
  enabled?: () => boolean
  danger?: boolean
  run: () => void | Promise<void>
}
export type MenuItem = MenuAction | '-'

let current: { box: HTMLElement; onClose?: () => void } | null = null

export function closeMenu(): void {
  if (!current) return
  const { box, onClose } = current
  current = null
  box.remove()
  onClose?.()
}

export function menuOpen(): boolean {
  return current !== null
}

/**
 * 在 (x, y) 处弹出菜单。anchor 给定时贴在元素下方。
 * onError：命令抛错时的反馈（例如 showFeedback）。
 */
export function showMenu(
  items: MenuItem[],
  at: { x: number; y: number } | HTMLElement,
  options: { onClose?: () => void; onError?: (error: unknown) => void; className?: string } = {}
): void {
  closeMenu()
  const box = el('div', 'ctx-menu' + (options.className ? ' ' + options.className : ''))
  box.setAttribute('role', 'menu')
  const buttons: HTMLButtonElement[] = []
  // 连续分隔线 / 首尾分隔线去掉
  const clean = items.filter((item, i, all) => item !== '-' || (i > 0 && i < all.length - 1 && all[i + 1] !== '-'))
  for (const item of clean) {
    if (item === '-') {
      box.append(el('div', 'ctx-sep'))
      continue
    }
    const button = el('button', 'ctx-item' + (item.danger ? ' danger' : '')) as HTMLButtonElement
    button.type = 'button'
    button.setAttribute('role', item.checked ? 'menuitemcheckbox' : 'menuitem')
    const mark = el('span', 'ctx-ico')
    const on = item.checked?.()
    if (on) mark.innerHTML = icon('check')
    else if (item.icon) mark.innerHTML = icon(item.icon)
    if (item.checked) button.setAttribute('aria-checked', String(Boolean(on)))
    button.append(mark, el('span', 'ctx-label', item.label))
    if (item.shortcut) button.append(el('kbd', 'ctx-key', item.shortcut))
    button.disabled = item.enabled ? !item.enabled() : false
    button.addEventListener('click', () => {
      closeMenu()
      void Promise.resolve().then(item.run).catch((error) => options.onError?.(error))
    })
    button.addEventListener('pointerenter', () => { if (!button.disabled) button.focus({ preventScroll: true }) })
    buttons.push(button)
    box.append(button)
  }
  document.body.append(box)

  // 定位：不超出窗口
  const rect = box.getBoundingClientRect()
  let x: number, y: number
  if (at instanceof HTMLElement) {
    const r = at.getBoundingClientRect()
    x = r.left
    y = r.bottom + 4
    if (y + rect.height > window.innerHeight - 6) y = Math.max(6, r.top - rect.height - 4)
  } else {
    x = at.x
    y = at.y
    if (y + rect.height > window.innerHeight - 6) y = Math.max(6, y - rect.height)
  }
  x = Math.max(6, Math.min(x, window.innerWidth - rect.width - 6))
  box.style.left = x + 'px'
  box.style.top = y + 'px'
  current = { box, onClose: options.onClose }

  const enabled = (): HTMLButtonElement[] => buttons.filter((b) => !b.disabled)
  box.addEventListener('keydown', (e) => {
    const list = enabled()
    const i = list.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'ArrowDown') { e.preventDefault(); list[(i + 1) % list.length]?.focus() }
    else if (e.key === 'ArrowUp') { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus() }
    else if (e.key === 'Home') { e.preventDefault(); list[0]?.focus() }
    else if (e.key === 'End') { e.preventDefault(); list[list.length - 1]?.focus() }
  })
  if (at instanceof HTMLElement && at.matches(':focus-visible')) enabled()[0]?.focus({ preventScroll: true })
  else {
    box.tabIndex = -1
    box.focus({ preventScroll: true })
  }
}

// 点击菜单外 / Esc / 窗口失焦 → 关闭
document.addEventListener('pointerdown', (e) => {
  if (current && !current.box.contains(e.target as Node) && !(e.target as HTMLElement).closest?.('.menubar-item')) closeMenu()
}, true)
window.addEventListener('keydown', (e) => {
  if (current && e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    closeMenu()
  }
}, true)
window.addEventListener('blur', () => closeMenu())
window.addEventListener('resize', () => closeMenu())

// —— 顶部菜单栏（文件 / 编辑 / 视图 …）：点击展开，展开后悬停切换，← → 切换相邻菜单 ——
export function initMenubar(host: HTMLElement, menus: Array<[string, () => MenuItem[]]>, onError: (error: unknown) => void): void {
  let openIndex = -1
  const buttons: HTMLButtonElement[] = []
  function open(index: number): void {
    const [, build] = menus[index]
    const button = buttons[index]
    openIndex = index
    buttons.forEach((b, i) => b.classList.toggle('open', i === index))
    showMenu(build(), button, {
      onError,
      className: 'menubar-menu',
      onClose: () => {
        if (openIndex === index) openIndex = -1
        button.classList.remove('open')
      }
    })
  }
  menus.forEach(([label], index) => {
    const button = el('button', 'menubar-item', label) as HTMLButtonElement
    button.type = 'button'
    button.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return
      e.preventDefault()
      if (openIndex === index) closeMenu()
      else open(index)
    })
    button.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') { e.preventDefault(); open(index) }
    })
    button.addEventListener('pointerenter', () => { if (openIndex !== -1 && openIndex !== index) open(index) })
    buttons.push(button)
    host.append(button)
  })
  window.addEventListener('keydown', (e) => {
    if (openIndex === -1 || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return
    e.preventDefault()
    const next = (openIndex + (e.key === 'ArrowRight' ? 1 : -1) + menus.length) % menus.length
    open(next)
    ;(document.querySelector('.ctx-menu .ctx-item:not(:disabled)') as HTMLElement | null)?.focus()
  }, true)
}

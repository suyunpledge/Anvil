import { el } from './core'
import { icon, hydrateIcons } from './icons'

// —— 小对话框：输入一行文字（Electron 不支持 window.prompt）、快捷键一览 ——

function makeDialog(cls: string): HTMLDialogElement {
  const dialog = document.createElement('dialog')
  dialog.className = 'ask-dialog ' + cls
  document.body.append(dialog)
  dialog.addEventListener('close', () => setTimeout(() => dialog.remove(), 0))
  return dialog
}

export function askText(options: {
  title: string
  icon?: string
  detail?: string
  label: string
  value?: string
  placeholder?: string
  ok?: string
  /** 选中文件名主体（不含扩展名），重命名时好用 */
  selectStem?: boolean
  validate?: (value: string) => string | null
}): Promise<string | null> {
  return new Promise((resolve) => {
    const dialog = makeDialog('ask-text')
    const form = el('form') as HTMLFormElement
    const title = el('h2', 'ask-title')
    title.innerHTML = icon(options.icon ?? 'edit')
    title.append(options.title)
    form.append(title)
    if (options.detail) form.append(el('p', 'ask-detail', options.detail))
    const label = el('label', '', options.label) as HTMLLabelElement
    const input = el('input') as HTMLInputElement
    input.type = 'text'
    input.id = 'ask-input'
    label.htmlFor = input.id
    input.value = options.value ?? ''
    input.placeholder = options.placeholder ?? ''
    input.spellcheck = false
    const error = el('p', 'ask-error')
    error.setAttribute('role', 'alert')
    const actions = el('div', 'ask-actions')
    const cancel = el('button', 'tb-btn', '取消') as HTMLButtonElement
    cancel.type = 'button'
    const ok = el('button', 'tb-btn ask-ok', options.ok ?? '确定') as HTMLButtonElement
    ok.type = 'submit'
    actions.append(cancel, ok)
    form.append(label, input, error, actions)
    dialog.append(form)

    let result: string | null = null
    input.addEventListener('input', () => { error.textContent = '' })
    cancel.addEventListener('click', () => dialog.close())
    form.addEventListener('submit', (e) => {
      e.preventDefault()
      const value = input.value.trim()
      const problem = !value ? '请输入内容' : options.validate?.(value) ?? null
      if (problem) {
        error.textContent = problem
        input.focus()
        return
      }
      result = value
      dialog.close()
    })
    dialog.addEventListener('close', () => resolve(result))
    dialog.showModal()
    input.focus()
    const dot = input.value.lastIndexOf('.')
    if (options.selectStem && dot > 0) input.setSelectionRange(0, dot)
    else input.select()
  })
}

/** 文件 / 文件夹名校验（Windows 规则） */
export function badName(name: string): string | null {
  if (/[<>:"/\\|?*]/.test(name)) return '名称里不能有 < > : " / \\ | ? *'
  if (name.endsWith('.') || name.endsWith(' ')) return '名称不能以点或空格结尾'
  if (/^(con|prn|aux|nul|com\d|lpt\d)(\.|$)/i.test(name)) return '这是 Windows 保留名称，请换一个'
  return null
}

const SHORTCUTS: Array<[string, Array<[string, string]>]> = [
  ['常用', [
    ['F5', '保存并运行当前文件'], ['Ctrl+S', '保存'], ['Ctrl+P', '按文件名快速打开'],
    ['Ctrl+Shift+P', '搜索命令（命令面板）'], ['Ctrl+Shift+F', '在项目中搜索'], ['Ctrl+,', '设置']
  ]],
  ['编辑', [
    ['Ctrl+F / Ctrl+H', '查找 / 替换'], ['Ctrl+/', '切换行注释'], ['Shift+Alt+F', '格式化文档'],
    ['Alt+↑ / Alt+↓', '上下移动当前行'], ['Shift+Alt+↓', '向下复制当前行'], ['Ctrl+D', '选中下一个相同内容'],
    ['Ctrl+G', '跳转到行'], ['Ctrl+Z / Ctrl+Y', '撤销 / 重做']
  ]],
  ['窗口', [
    ['Ctrl+B', '显示 / 隐藏侧栏'], ['Ctrl+J', '显示 / 隐藏底部面板'], ['Ctrl+`', '终端'],
    ['Ctrl+Shift+E', '资源管理器'], ['Ctrl+Shift+M', '问题面板'], ['Ctrl+Shift+A / W', 'AI 助手 / Work 整理'],
    ['Ctrl+W', '关闭当前标签'], ['Ctrl+Tab', '切换到下一个标签'], ['Ctrl+= / Ctrl+- / Ctrl+0', '放大 / 缩小 / 重置字号']
  ]],
  ['VS 组合键（在编辑器里，先按前一个再按后一个）', [
    ['Ctrl+K Ctrl+K', '切换书签'], ['Ctrl+K Ctrl+N / Ctrl+K Ctrl+P', '下一个 / 上一个书签'], ['Ctrl+K Ctrl+W', '书签窗口'],
    ['Ctrl+K Ctrl+X', '插入代码片段'], ['Ctrl+K Ctrl+C / Ctrl+K Ctrl+U', '注释 / 取消注释'], ['Ctrl+K Ctrl+D', '格式化文档'],
    ['Ctrl+M Ctrl+M', '折叠 / 展开当前块'], ['Ctrl+M Ctrl+O / Ctrl+M Ctrl+L', '全部折叠 / 全部展开']
  ]],
  ['导航与项目', [
    ['Ctrl+Shift+N', '新建项目'], ['Alt+← / Alt+→', '向后 / 向前导航'], ['Ctrl+Alt+T', '文档大纲'],
    ['Ctrl+Shift+O', '跳转到函数 / 类'], ['F5 / Ctrl+F5', '运行'], ['F9', '切换断点（C/C++ 调试）']
  ]]
]

export function showShortcuts(): void {
  const dialog = makeDialog('shortcuts')
  const head = el('div', 'ask-head')
  const title = el('h2', 'ask-title')
  title.innerHTML = icon('keyboard')
  title.append('键盘快捷键')
  const close = el('button', 'mini-btn icon-only') as HTMLButtonElement
  close.type = 'button'
  close.innerHTML = icon('x')
  close.setAttribute('aria-label', '关闭')
  close.addEventListener('click', () => dialog.close())
  head.append(title, close)
  const grid = el('div', 'sc-grid')
  for (const [group, rows] of SHORTCUTS) {
    const section = el('section', 'sc-group')
    section.append(el('h3', '', group))
    for (const [keys, desc] of rows) {
      const row = el('div', 'sc-row')
      const k = el('span', 'sc-keys')
      keys.split(' / ').forEach((combo, i) => {
        if (i) k.append(el('span', 'sc-or', '/'))
        k.append(el('kbd', '', combo))
      })
      row.append(el('span', 'sc-desc', desc), k)
      section.append(row)
    }
    grid.append(section)
  }
  dialog.append(head, grid)
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close() })
  dialog.showModal()
}

export function showAbout(): void {
  const dialog = makeDialog('about')
  dialog.innerHTML = `<div class="about-logo"><i data-ic="brand"></i></div>`
  hydrateIcons(dialog)
  dialog.append(
    el('h2', '', '铁砧 IDE'),
    el('p', '', '好上手的本地代码编辑器：一键运行、本地 AI 助手、Work 文件整理。'),
    el('p', 'about-dim', '编辑器内核：Monaco（与 VS Code 相同）· 本地模型：Ollama')
  )
  const ok = el('button', 'tb-btn ask-ok', '好的') as HTMLButtonElement
  ok.type = 'button'
  ok.addEventListener('click', () => dialog.close())
  dialog.append(ok)
  dialog.showModal()
  ok.focus()
}

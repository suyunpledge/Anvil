import { el, bus, showFeedback } from './core'
import { editor, saveAll, hasUnsavedTabs } from './editor'

// —— 设置：编辑器外观 / 行为，存在 localStorage，改完立即生效 ——

export interface Settings {
  fontSize: number
  tabSize: number
  wordWrap: boolean
  minimap: boolean
  lineNumbers: boolean
  ligatures: boolean
  whitespace: boolean
  stickyScroll: boolean
  autoSave: 'off' | 'delay' | 'focus'
}

const KEY = 'anvil.settings'
const DEFAULTS: Settings = {
  fontSize: 14, tabSize: 4, wordWrap: false, minimap: false, lineNumbers: true,
  ligatures: true, whitespace: false, stickyScroll: true, autoSave: 'off'
}

let current: Settings = load()

function load(): Settings {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<Settings>) }
  } catch {
    return { ...DEFAULTS }
  }
}

export function settings(): Readonly<Settings> {
  return current
}

export function setSetting<K extends keyof Settings>(key: K, value: Settings[K]): void {
  current = { ...current, [key]: value }
  localStorage.setItem(KEY, JSON.stringify(current))
  apply()
  render()
}

export function toggleSetting(key: 'wordWrap' | 'minimap' | 'lineNumbers' | 'whitespace' | 'stickyScroll'): void {
  setSetting(key, !current[key])
}

export function zoom(delta: number): void {
  const size = delta === 0 ? DEFAULTS.fontSize : Math.max(10, Math.min(28, current.fontSize + delta))
  setSetting('fontSize', size)
  showFeedback(`编辑器字号 ${size}`)
}

function apply(): void {
  const s = current
  editor.updateOptions({
    fontSize: s.fontSize,
    lineHeight: Math.round(s.fontSize * 1.58),
    wordWrap: s.wordWrap ? 'on' : 'off',
    minimap: { enabled: s.minimap },
    lineNumbers: s.lineNumbers ? 'on' : 'off',
    fontLigatures: s.ligatures,
    renderWhitespace: s.whitespace ? 'all' : 'selection',
    stickyScroll: { enabled: s.stickyScroll },
    mouseWheelZoom: false
  })
  // tabSize 作用在文本模型上
  editor.getModel()?.updateOptions({ tabSize: s.tabSize, indentSize: s.tabSize })
  bus.emit('settings:changed')
}

// —— 自动保存 ——
let autoTimer: ReturnType<typeof setTimeout> | undefined
function autoSaveNow(): void {
  if (!hasUnsavedTabs()) return
  void saveAll().catch((error) => showFeedback(String(error)))
}

// —— 设置面板 ——
type Row =
  | { kind: 'number'; key: 'fontSize' | 'tabSize'; label: string; hint?: string; min: number; max: number }
  | { kind: 'bool'; key: 'wordWrap' | 'minimap' | 'lineNumbers' | 'ligatures' | 'whitespace' | 'stickyScroll'; label: string; hint?: string }
  | { kind: 'select'; key: 'autoSave'; label: string; hint?: string; options: Array<[Settings['autoSave'], string]> }

const ROWS: Array<[string, Row[]]> = [
  ['文本编辑器', [
    { kind: 'number', key: 'fontSize', label: '字号', hint: '也可以用 Ctrl+= / Ctrl+- 调整，Ctrl+0 恢复', min: 10, max: 28 },
    { kind: 'number', key: 'tabSize', label: 'Tab 宽度', hint: '按一次 Tab 缩进几个空格', min: 1, max: 8 },
    { kind: 'bool', key: 'wordWrap', label: '自动换行', hint: '长行折到下一行显示（Alt+Z）' },
    { kind: 'bool', key: 'lineNumbers', label: '显示行号' },
    { kind: 'bool', key: 'ligatures', label: '字体连字', hint: '把 => != 等符号画成一个整体' },
    { kind: 'bool', key: 'whitespace', label: '显示空格和 Tab', hint: '排查缩进问题（尤其是 Python）时很有用' }
  ]],
  ['界面', [
    { kind: 'bool', key: 'minimap', label: '代码缩略图', hint: '编辑器右侧的整页缩略图' },
    { kind: 'bool', key: 'stickyScroll', label: '粘性滚动', hint: '滚动时把当前所在的函数 / 类固定在顶部' }
  ]],
  ['文件', [
    { kind: 'select', key: 'autoSave', label: '自动保存', options: [['off', '关闭'], ['delay', '停止输入 1 秒后'], ['focus', '切换到其他窗口时']] }
  ]]
]

function render(): void {
  const pane = document.getElementById('settings-body')
  if (!pane) return
  pane.replaceChildren()
  for (const [group, rows] of ROWS) {
    const section = el('section', 'set-group')
    section.append(el('h3', '', group))
    for (const row of rows) {
      const line = el('label', 'set-row' + (row.kind === 'bool' ? ' bool' : ''))
      const text = el('span', 'set-text')
      text.append(el('span', 'set-label', row.label))
      if (row.hint) text.append(el('span', 'set-hint', row.hint))
      let control: HTMLElement
      if (row.kind === 'bool') {
        const input = el('input', 'switch') as HTMLInputElement
        input.type = 'checkbox'
        input.checked = current[row.key]
        input.addEventListener('change', () => setSetting(row.key, input.checked))
        control = input
      } else if (row.kind === 'number') {
        const box = el('span', 'stepper')
        const input = el('input') as HTMLInputElement
        input.type = 'number'
        input.min = String(row.min)
        input.max = String(row.max)
        input.value = String(current[row.key])
        const commit = (v: number): void => {
          if (!Number.isFinite(v)) return
          setSetting(row.key, Math.max(row.min, Math.min(row.max, Math.round(v))))
        }
        input.addEventListener('change', () => commit(Number(input.value)))
        const minus = el('button', '', '−') as HTMLButtonElement
        const plus = el('button', '', '+') as HTMLButtonElement
        minus.type = plus.type = 'button'
        minus.addEventListener('click', (e) => { e.preventDefault(); commit(current[row.key] - 1) })
        plus.addEventListener('click', (e) => { e.preventDefault(); commit(current[row.key] + 1) })
        box.append(minus, input, plus)
        control = box
      } else {
        const select = el('select') as HTMLSelectElement
        for (const [value, label] of row.options) select.append(new Option(label, value, false, current[row.key] === value))
        select.addEventListener('change', () => setSetting(row.key, select.value as Settings['autoSave']))
        control = select
      }
      line.append(text, control)
      section.append(line)
    }
    pane.append(section)
  }
  const reset = el('button', 'tb-btn set-reset', '恢复默认设置') as HTMLButtonElement
  reset.type = 'button'
  reset.addEventListener('click', () => {
    current = { ...DEFAULTS }
    localStorage.removeItem(KEY)
    apply()
    render()
    showFeedback('已恢复默认设置')
  })
  pane.append(reset)
}

export function initSettings(): void {
  apply()
  render()
  // 新打开 / 切换的文件也套用 Tab 宽度
  bus.on('tab:active', () => editor.getModel()?.updateOptions({ tabSize: current.tabSize, indentSize: current.tabSize }))
  editor.onDidChangeModelContent(() => {
    clearTimeout(autoTimer)
    if (current.autoSave === 'delay') autoTimer = setTimeout(autoSaveNow, 1000)
  })
  window.addEventListener('blur', () => { if (current.autoSave === 'focus') autoSaveNow() })
  editor.onDidBlurEditorText(() => { if (current.autoSave === 'focus') autoSaveNow() })
}

import * as monaco from 'monaco-editor'
import { $, el, bus } from './core'
import { editor, activeTab } from './editor'

// —— 文档大纲（对标 VS「文档大纲」/ VS Code OUTLINE）——
// C / C++ / Python / Java / Kotlin / Markdown：按规则从源码里找函数、类、结构体；JS / TS：用内置 TypeScript 服务。
// 同时注册为 Monaco 的符号提供者：Ctrl+Shift+O 跳转到符号、粘性滚动对这些语言也生效。

export interface Sym {
  name: string
  kind: 'function' | 'class' | 'struct' | 'method' | 'heading' | 'variable' | 'enum'
  line: number
  endLine: number
  detail?: string
  children: Sym[]
}

const C_KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'return', 'else', 'do', 'sizeof', 'case', 'catch', 'new', 'delete'])

/** 去掉字符串和注释，保留长度（大括号匹配时不被字符串里的括号干扰） */
function stripCode(lines: string[]): string[] {
  let inBlock = false
  return lines.map((line) => {
    let out = ''
    for (let i = 0; i < line.length; i++) {
      const c = line[i]
      if (inBlock) {
        if (c === '*' && line[i + 1] === '/') { inBlock = false; out += '  '; i++ } else out += ' '
        continue
      }
      if (c === '/' && line[i + 1] === '*') { inBlock = true; out += '  '; i++; continue }
      if (c === '/' && line[i + 1] === '/') { out += ' '.repeat(line.length - i); break }
      if (c === '"' || c === "'") {
        const quote = c
        out += ' '
        for (i++; i < line.length && line[i] !== quote; i++) { out += ' '; if (line[i] === '\\') { out += ' '; i++ } }
        out += ' '
        continue
      }
      out += c
    }
    return out
  })
}

/** 从 start 行开始找到第一个 { 并返回与之匹配的 } 所在行 */
function blockEnd(code: string[], start: number): number {
  let depth = 0
  let opened = false
  for (let i = start; i < code.length; i++) {
    for (const c of code[i]) {
      if (c === '{') { depth++; opened = true }
      else if (c === '}') { depth--; if (opened && depth === 0) return i }
    }
    if (!opened && i > start + 2) return start
  }
  return code.length - 1
}

function braceSymbols(text: string, lang: 'c' | 'java' | 'kotlin'): Sym[] {
  const lines = text.split(/\r?\n/)
  const code = stripCode(lines)
  const flat: Sym[] = []
  for (let i = 0; i < code.length; i++) {
    const line = code[i]
    if (/^\s*#/.test(line)) continue
    const next = (code[i + 1] ?? '').trim()
    const opens = /\{\s*$/.test(line) || (next.startsWith('{') && !/;\s*$/.test(line))
    let m: RegExpMatchArray | null
    if ((m = line.match(/^\s*(?:(?:public|private|protected|static|final|abstract|data|sealed|open|internal|typedef)\s+)*(class|struct|interface|enum|union|object)\s+([A-Za-z_]\w*)/)) && (opens || /\{/.test(line))) {
      const kind = m[1] === 'struct' || m[1] === 'union' ? 'struct' : m[1] === 'enum' ? 'enum' : 'class'
      flat.push({ name: m[2], kind, line: i + 1, endLine: blockEnd(code, i) + 1, detail: m[1], children: [] })
      continue
    }
    if (lang === 'kotlin' && (m = line.match(/^\s*(?:(?:public|private|protected|internal|override|suspend|inline|open)\s+)*fun\s+(?:<[^>]+>\s*)?([A-Za-z_][\w.]*)\s*\(/))) {
      flat.push({ name: m[1], kind: 'function', line: i + 1, endLine: opens ? blockEnd(code, i) + 1 : i + 1, children: [] })
      continue
    }
    if (!opens) continue
    // 函数 / 方法：返回类型 名字(参数) [const] {
    m = line.match(/^\s*(?:[\w:<>,*&\s[\]]+?[\s*&])?([A-Za-z_~][\w]*(?:::[A-Za-z_~]\w*)*)\s*\(([^;]*)\)\s*(?:const\s*)?(?:throws\s+[\w.,\s]+)?(?:->\s*[\w:<>*&\s]+)?\s*\{?\s*$/)
    if (m && !C_KEYWORDS.has(m[1].split('::').pop() ?? m[1]) && !/^\s*(else|return|new)\b/.test(line)) {
      flat.push({ name: m[1], kind: 'function', line: i + 1, endLine: blockEnd(code, i) + 1, detail: '(' + m[2].trim() + ')', children: [] })
    }
  }
  return nest(flat)
}

/** 按行范围嵌套：被包含的符号放进外层 children（类里的方法） */
function nest(flat: Sym[]): Sym[] {
  const roots: Sym[] = []
  const stack: Sym[] = []
  for (const s of flat.sort((a, b) => a.line - b.line)) {
    while (stack.length && stack[stack.length - 1].endLine < s.line) stack.pop()
    const parent = stack[stack.length - 1]
    if (parent && s.endLine <= parent.endLine) {
      if (s.kind === 'function' && parent.kind !== 'function') s.kind = 'method'
      parent.children.push(s)
    } else roots.push(s)
    stack.push(s)
  }
  return roots
}

function pythonSymbols(text: string): Sym[] {
  const lines = text.split(/\r?\n/)
  const flat: Array<Sym & { indent: number }> = []
  const indentOf = (l: string): number => l.match(/^\s*/)![0].replace(/\t/g, '    ').length
  lines.forEach((line, i) => {
    const m = line.match(/^(\s*)(async\s+def|def|class)\s+([A-Za-z_]\w*)\s*(\([^)]*\)?)?/)
    if (!m) return
    const indent = indentOf(line)
    let end = i
    for (let j = i + 1; j < lines.length; j++) {
      if (!lines[j].trim()) continue
      if (indentOf(lines[j]) <= indent) break
      end = j
    }
    flat.push({
      name: m[3], kind: m[2] === 'class' ? 'class' : 'function', line: i + 1, endLine: end + 1,
      detail: m[4]?.replace(/\)?$/, ')'), children: [], indent
    })
  })
  return nest(flat)
}

function markdownSymbols(text: string): Sym[] {
  const out: Sym[] = []
  text.split(/\r?\n/).forEach((line, i) => {
    const m = line.match(/^(#{1,6})\s+(.+?)\s*#*$/)
    if (m) out.push({ name: m[2], kind: 'heading', line: i + 1, endLine: i + 1, detail: 'H' + m[1].length, children: [] })
  })
  return out
}

interface NavTree { text: string; kind: string; spans: Array<{ start: number; length: number }>; childItems?: NavTree[] }

async function tsSymbols(model: monaco.editor.ITextModel): Promise<Sym[]> {
  try {
    const lang = model.getLanguageId()
    const getWorker = lang === 'typescript' ? monaco.languages.typescript.getTypeScriptWorker : monaco.languages.typescript.getJavaScriptWorker
    const worker = await (await getWorker())(model.uri)
    const tree = await (worker as unknown as { getNavigationTree(uri: string): Promise<NavTree | undefined> }).getNavigationTree(model.uri.toString())
    const convert = (items: NavTree[] = []): Sym[] => items
      .filter((n) => ['function', 'class', 'method', 'interface', 'enum', 'const', 'let', 'var'].includes(n.kind) && n.spans.length)
      .filter((n) => !['const', 'let', 'var'].includes(n.kind) || (n.childItems?.length ?? 0) > 0)
      .map((n) => ({
        name: n.text,
        kind: n.kind === 'class' || n.kind === 'interface' ? 'class' : n.kind === 'enum' ? 'enum' : n.kind === 'method' ? 'method' : ['const', 'let', 'var'].includes(n.kind) ? 'variable' : 'function',
        line: model.getPositionAt(n.spans[0].start).lineNumber,
        endLine: model.getPositionAt(n.spans[0].start + n.spans[0].length).lineNumber,
        children: convert(n.childItems)
      }))
    return convert(tree?.childItems)
  } catch {
    return []
  }
}

export async function symbolsOf(model: monaco.editor.ITextModel): Promise<Sym[]> {
  const lang = model.getLanguageId()
  const text = model.getValue()
  if (lang === 'cpp') return braceSymbols(text, 'c')
  if (lang === 'java') return braceSymbols(text, 'java')
  if (lang === 'kotlin') return braceSymbols(text, 'kotlin')
  if (lang === 'python') return pythonSymbols(text)
  if (lang === 'markdown') return markdownSymbols(text)
  if (lang === 'javascript' || lang === 'typescript') return tsSymbols(model)
  return []
}

const KIND_MAP: Record<Sym['kind'], monaco.languages.SymbolKind> = {
  function: monaco.languages.SymbolKind.Function,
  method: monaco.languages.SymbolKind.Method,
  class: monaco.languages.SymbolKind.Class,
  struct: monaco.languages.SymbolKind.Struct,
  enum: monaco.languages.SymbolKind.Enum,
  heading: monaco.languages.SymbolKind.String,
  variable: monaco.languages.SymbolKind.Variable
}
const KIND_LABEL: Record<Sym['kind'], [string, string]> = {
  function: ['f', '函数'], method: ['m', '方法'], class: ['C', '类'], struct: ['S', '结构体'],
  enum: ['E', '枚举'], heading: ['#', '标题'], variable: ['v', '变量']
}

function toDocumentSymbols(model: monaco.editor.ITextModel, list: Sym[]): monaco.languages.DocumentSymbol[] {
  return list.map((s) => {
    const end = Math.min(Math.max(s.endLine, s.line), model.getLineCount())
    const range = new monaco.Range(s.line, 1, end, model.getLineMaxColumn(end))
    const col = Math.max(1, model.getLineContent(s.line).indexOf(s.name) + 1)
    return {
      name: s.name, detail: s.detail ?? '', kind: KIND_MAP[s.kind], tags: [], range,
      selectionRange: new monaco.Range(s.line, col, s.line, col + s.name.length),
      children: toDocumentSymbols(model, s.children)
    }
  })
}

// —— 侧栏「大纲」视图 ——
export function initOutline(): void {
  for (const language of ['cpp', 'python', 'java', 'kotlin', 'markdown']) {
    monaco.languages.registerDocumentSymbolProvider(language, {
      displayName: '铁砧大纲',
      provideDocumentSymbols: async (model) => toDocumentSymbols(model, await symbolsOf(model))
    })
  }

  const body = $('outline-list')
  const status = $('outline-status')
  let symbols: Sym[] = []
  let timer: ReturnType<typeof setTimeout> | undefined
  let version = 0
  const visible = (): boolean => $('pane-outline').classList.contains('active') && !$('app').classList.contains('no-side')

  async function refresh(): Promise<void> {
    if (!visible()) return
    const model = editor.getModel()
    const my = ++version
    if (!model || !activeTab()) {
      symbols = []
      body.replaceChildren()
      status.textContent = '打开一个代码文件后，这里会列出其中的函数、类和结构体，点一下就能跳过去。'
      return
    }
    const found = await symbolsOf(model)
    if (my !== version) return
    symbols = found
    render()
  }

  function render(): void {
    body.replaceChildren()
    const name = activeTab()?.path.split(/[\\/]/).pop() ?? ''
    if (!symbols.length) {
      status.textContent = `「${name}」里没有找到函数或类。支持 C / C++ / Python / Java / Kotlin / JS / TS / Markdown。`
      return
    }
    status.textContent = ''
    const draw = (list: Sym[], depth: number): void => {
      for (const s of list) {
        const row = el('button', 'ol-row') as HTMLButtonElement
        row.type = 'button'
        row.style.paddingLeft = 10 + depth * 14 + 'px'
        row.dataset.line = String(s.line)
        row.dataset.end = String(s.endLine)
        const [glyph, label] = KIND_LABEL[s.kind]
        const badge = el('span', 'ol-kind k-' + s.kind, glyph)
        badge.title = label
        row.append(badge, el('span', 'ol-name', s.name))
        if (s.detail && s.kind !== 'heading') row.append(el('span', 'ol-detail', s.detail))
        row.append(el('span', 'ol-line', String(s.line)))
        row.title = `${label} ${s.name}（第 ${s.line} 行）`
        row.addEventListener('click', () => {
          bus.emit('nav:before-jump')
          editor.revealLineInCenter(s.line)
          const col = Math.max(1, (editor.getModel()?.getLineContent(s.line).indexOf(s.name) ?? 0) + 1)
          editor.setPosition({ lineNumber: s.line, column: col })
          editor.focus()
        })
        body.append(row)
        draw(s.children, depth + 1)
      }
    }
    draw(symbols, 0)
    markCurrent()
  }

  // 高亮光标所在的最内层符号
  function markCurrent(): void {
    const line = editor.getPosition()?.lineNumber ?? 0
    let best: HTMLElement | null = null
    for (const row of body.querySelectorAll<HTMLElement>('.ol-row')) {
      row.classList.remove('current')
      if (+row.dataset.line! <= line && line <= +row.dataset.end!) best = row
    }
    best?.classList.add('current')
  }

  const schedule = (): void => { clearTimeout(timer); timer = setTimeout(() => void refresh(), 350) }
  editor.onDidChangeModelContent(schedule)
  editor.onDidChangeModel(schedule)
  editor.onDidChangeCursorPosition(() => markCurrent())
  bus.on('tab:active', schedule)
  bus.on('pane:shown', (name) => { if (name === 'outline') void refresh() })
  $('outline-refresh').addEventListener('click', () => void refresh())
  $('outline-collapse').addEventListener('click', () => editor.trigger('outline', 'editor.foldAll', null))
}

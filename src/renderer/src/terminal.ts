import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { state, $ } from './core'
import { openFile } from './editor'

// —— 自带终端面板：cmd 会话 + 行缓冲输入 + 历史 + cd 跟踪 + cls/exit ——

let term: Terminal | null = null
let fit: FitAddon | null = null
let sid: number | null = null
let cwd = ''
let buffer = ''
let startedNewLine = true
const history: string[] = []
let histIdx = -1

export function initTerminal(): void {
  const host = $('terminal')
  term = new Terminal({
    fontFamily: "'Cascadia Mono', Consolas, monospace",
    fontSize: 12.5,
    cursorBlink: true,
    convertEol: true,
    theme: {
      background: '#15171d',
      foreground: '#d5d9e4',
      cursor: '#4f8cff',
      selectionBackground: '#3a4a6b'
    }
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(host)

  window.ide.onTermOut(({ sid: s, data }) => {
    if (s === sid && term) {
      term.write(data)
      startedNewLine = false
    }
  })

  term.onData((d) => handleKey(d))

  new ResizeObserver(() => {
    try {
      fit?.fit()
    } catch {
      /* 尺寸未就绪 */
    }
  }).observe(host)
}

async function ensureSession(): Promise<void> {
  if (sid !== null && (await window.ide.termAlive(sid))) return
  const base =
    state.folder ||
    (state.openFile ? state.openFile.replace(/[\\/][^\\/]+$/, '') : null)
  const r = await window.ide.termStart(base)
  sid = r.sid
  cwd = r.cwd
  buffer = ''
  term?.write(`\x1b[90m—— 终端已就绪（工作目录 ${cwd}）——\x1b[0m\r\n`)
  updateHint()
}

function updateHint(): void {
  const hint = document.querySelector('.term-hint') as HTMLElement | null
  if (hint) {
    hint.textContent = `${cwd}  ·  ↑↓ 翻历史 · cls 清屏 · exit 结束会话（Enter 重开）`
  }
}

function miniPrompt(): void {
  if (startedNewLine) {
    term?.write('\x1b[90m»\x1b[0m ')
    startedNewLine = false
  }
}

function handleKey(d: string): void {
  if (!term) return
  // 可打印字符
  if (d >= ' ' && d <= '~' || /[\u00a0-\uffff]/.test(d)) {
    buffer += d
    miniPrompt()
    term.write(d)
    return
  }
  if (d === '\r') {
    void submit()
    return
  }
  if (d === '\u007f') {
    // 退格
    if (buffer.length > 0) {
      buffer = buffer.slice(0, -1)
      term.write('\b \b')
    }
    return
  }
  if (d === '\u001b[A' || d === '\u001b[B') {
    // ↑ / ↓：历史
    if (history.length === 0) return
    term.write('\r\x1b[K')
    startedNewLine = true
    if (d === '\u001b[A') {
      histIdx = histIdx < 0 ? history.length - 1 : Math.max(0, histIdx - 1)
    } else {
      if (histIdx < 0) return
      histIdx = histIdx + 1
      if (histIdx >= history.length) {
        histIdx = -1
        buffer = ''
        miniPrompt()
        return
      }
    }
    buffer = history[histIdx] ?? ''
    miniPrompt()
    term.write(buffer)
    return
  }
  if (d === '\u0003') {
    // Ctrl+C：丢弃当前行
    buffer = ''
    term.write('^C\r\n')
    startedNewLine = true
    miniPrompt()
    return
  }
  if (d === '\t') {
    // Tab 忽略（v1 不做补全）
    return
  }
}

async function submit(): Promise<void> {
  const line = buffer
  buffer = ''
  histIdx = -1
  term?.write('\r\n')
  startedNewLine = true
  const trimmed = line.trim()
  if (!trimmed) {
    if (sid !== null && !(await window.ide.termAlive(sid))) {
      await ensureSession()
    }
    return
  }
  history.push(trimmed)
  if (history.length > 100) history.shift()

  // 本地命令
  if (trimmed === 'cls' || trimmed === 'clear') {
    term?.clear()
    return
  }
  if (trimmed === 'exit' || trimmed === 'quit') {
    if (sid !== null) await window.ide.termKill(sid)
    sid = null
    term?.write('\x1b[90m[已退出，按 Enter 重新开启终端]\x1b[0m\r\n')
    return
  }
  if (sid === null || !(await window.ide.termAlive(sid))) {
    await ensureSession()
  }
  // cd / 盘符：同步更新提示行显示
  trackCwd(trimmed)
  await window.ide.termWrite(sid as number, line + '\r\n')
}

function trackCwd(cmd: string): void {
  const m = /^([a-zA-Z]:)(?:\\([^\\]*))?/.exec(cmd)
  if (m) {
    cwd = m[2] === undefined ? m[1] + '\\' : cwd
    updateHint()
    return
  }
  const cd = /^(?:cd|chdir)\s+(.+)$/i.exec(cmd)
  if (cd) {
    const arg = cd[1].trim().replace(/^"|"$/g, '')
    if (/^[a-zA-Z]:/.test(arg)) {
      cwd = arg.endsWith('\\') ? arg : arg + '\\'
    } else if (arg === '..') {
      const parts = cwd.replace(/\\$/, '').split('\\')
      parts.pop()
      cwd = (parts.join('\\') || cwd.split('\\')[0]) + '\\'
    } else if (arg && arg !== '.') {
      cwd = cwd.replace(/\\$/, '') + '\\' + arg
    }
    updateHint()
  }
}

export async function activateTerminal(): Promise<void> {
  $('console-wrap').classList.remove('collapsed')
  await ensureSession()
  try {
    fit?.fit()
  } catch {
    /* 忽略 */
  }
  term?.focus()
}

// 便捷命令：把一条命令直接打进终端（供「在终端中运行」类按钮使用）
export async function runCommand(cmd: string): Promise<void> {
  await activateTerminal()
  for (const ch of cmd) term?.write(ch)
  buffer = cmd
  void submit()
}

// 打开当前文件的命令快捷入口（gcc 手动编译场景）
export async function compileHere(): Promise<void> {
  if (!state.openFile) return
  const name = state.openFile.split(/[\\/]/).pop() ?? ''
  const exe = name.replace(/\.[^.]+$/, '.exe')
  const isCpp = /\.(cpp|cc|cxx)$/i.test(name)
  await runCommand(`${isCpp ? 'g++' : 'gcc'} "${name}" -o "${exe}" && ${exe.replace(/\.exe$/, '')}`)
}

export function revealCurrentFile(): void {
  if (state.openFile) void openFile(state.openFile)
}

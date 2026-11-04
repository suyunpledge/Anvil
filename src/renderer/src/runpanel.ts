import { $, el, showPanel } from './core'
import { saveActive, activeTab, revealLine } from './editor'
import type { CompilerInfo, OutChunk, ExitInfo } from '../../shared/types'

// —— 一键运行 + 输出控制台 + 报错跳行 ——

let runSid: number | null = null
let compilers: CompilerInfo[] = []

const GCC_RE = /^(.+?):(\d+):(\d+):\s*(error|warning|note|fatal error):\s*(.+)$/gm

function out(text: string, cls?: string): void {
  const box = $('console-out')
  if (box.querySelector('.hint')) box.innerHTML = ''
  const line = el('div', cls, text)
  box.append(line)
  box.scrollTop = box.scrollHeight
}

function clearOut(): void {
  $('console-out').innerHTML = ''
}

function setRunning(on: boolean, sid?: number): void {
  runSid = on && sid !== undefined ? sid : null
  const btnRun = $('btn-run') as HTMLButtonElement
  const btnStop = $('btn-stop') as HTMLButtonElement
  btnRun.disabled = on
  btnStop.disabled = !on
  $('console-state').textContent = on ? '运行中…' : ''
  $('console-wrap').classList.remove('collapsed')
  showPanel('out')
}

export async function initRun(): Promise<void> {
  // 编译器状态探测
  compilers = await window.ide.detectCompilers()
  const gcc = compilers.find((c) => c.kind === 'g++') || compilers.find((c) => c.kind === 'gcc')
  const py = compilers.find((c) => c.kind === 'python')
  const parts: string[] = []
  if (gcc) parts.push(`C/C++ ✓ ${gcc.version.slice(0, 40)}`)
  if (py) parts.push(`Python ✓ ${py.version}`)
  $('st-compiler').textContent = parts.length ? parts.join(' · ') : '未检测到编译器（C/C++ 需要 MinGW）'

  $('btn-run').addEventListener('click', () => void doRun())
  $('btn-stop').addEventListener('click', () => {
    if (runSid !== null) void window.ide.killRun(runSid)
  })
  $('console-clear').addEventListener('click', clearOut)
  $('console-toggle').addEventListener('click', () => $('console-wrap').classList.toggle('collapsed'))

  const send = (): void => {
    const input = $('console-input') as HTMLInputElement
    const v = input.value
    if (!v) return
    if (runSid !== null) {
      out(v, 'co-dim')
      void window.ide.sendInput(runSid, v)
      input.value = ''
    }
  }
  $('console-send').addEventListener('click', send)
  $('console-input').addEventListener('keydown', (e) => {
    if ((e as KeyboardEvent).key === 'Enter') send()
  })

  window.ide.onRunOut((chunk: OutChunk) => {
    if (runSid !== null && !('code' in chunk) && chunk.sid === runSid) {
      const c = chunk as { text: string; stream: string }
      if (c.stream === 'stderr' && /:\d+:\d+:\s*(fatal )?(error|warning):/.test(c.text)) {
        renderCompilerErrors(c.text)
      } else {
        out(c.text, c.stream === 'stderr' ? 'co-err' : undefined)
      }
    }
  })
  window.ide.onRunExit((info: ExitInfo) => {
    if (runSid !== null && info.sid === runSid) {
      out(
        info.code === 0 ? '\n[程序正常结束，退出码 0]' : `\n[程序结束，退出码 ${info.code ?? '被终止'}]`,
        info.code === 0 ? 'co-ok' : 'co-dim'
      )
      setRunning(false)
    }
  })
  window.ide.onRunNoCompiler(({ need }) => {
    const hint: Record<string, string> = {
      gcc: '没有找到 C 编译器。请先安装 MinGW-w64（或在设置中确认 gcc 所在目录已加入 PATH）。',
      'g++': '没有找到 C++ 编译器。请先安装 MinGW-w64。',
      python: '没有找到 Python。请安装 Python 3 后重试。',
      node: '没有找到 Node.js。'
    }
    out(`[无法运行] ${hint[need] ?? '没有可用的编译器/解释器'}`, 'co-err')
  })
}

async function doRun(): Promise<void> {
  const tab = activeTab()
  showPanel('out')
  if (!tab) {
    out('[请先打开一个文件再运行]', 'co-dim')
    $('console-wrap').classList.remove('collapsed')
    return
  }
  const ext = tab.path.split('.').pop()?.toLowerCase() ?? ''
  if (!['c', 'cpp', 'cc', 'cxx', 'py', 'js', 'mjs'].includes(ext)) {
    out(`[暂不支持运行 .${ext} 文件，支持 C / C++ / Python / JavaScript]`, 'co-dim')
    $('console-wrap').classList.remove('collapsed')
    return
  }
  clearOut()
  out(`—— 运行 ${tab.path.split(/[\\/]/).pop()} ——`, 'co-dim')
  const saved = await saveActive()
  if (!saved) return
  const r = await window.ide.startRun(saved)
  if (r.compiler) {
    out(`使用 ${r.compiler.kind === 'g++' || r.compiler.kind === 'gcc' ? 'GCC' : r.compiler.kind}：${r.compiler.version}`, 'co-dim')
    setRunning(true, r.sid)
  }
}

// 把 stderr 文本解析成可点击的错误行
export function renderCompilerErrors(text: string): void {
  const box = $('console-out')
  GCC_RE.lastIndex = 0
  let m: RegExpExecArray | null
  let found = false
  while ((m = GCC_RE.exec(text)) !== null) {
    if (found && m.index === GCC_RE.lastIndex) GCC_RE.lastIndex++
    found = true
    const [, file, line, col, level, msg] = m
    const row = el('div', 'co-err')
    const link = el('span', 'err-link', `${file.split(/[\\/]/).pop()}:${line}:${col}`)
    link.addEventListener('click', () => revealLine(file, Number(line), Number(col)))
    row.append(link, document.createTextNode(` ${level}: ${msg}`))
    box.append(row)
  }
  box.scrollTop = box.scrollHeight
}

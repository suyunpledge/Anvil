import { $, el, showPanel, setStatus } from './core'
import { saveActive, activeTab, revealLine } from './editor'
import { beginRun, runStderr } from './problems'
import { languageKind, languageNames } from '../../shared/languages'
import type { CompilerInfo, OutChunk, ExitInfo } from '../../shared/types'

// —— 一键运行 + 输出控制台 + 报错跳行 ——

let runSid: number | null = null
let startingRun = false

const GCC_RE = /^(.+?):(\d+):(\d+):\s*(error|warning|note|fatal error):\s*(.+)$/gm

function out(text: string, cls?: string): void {
  const box = $('console-out')
  if (box.querySelector('.hint')) box.innerHTML = ''
  const line = el('div', cls, text)
  box.append(line)
  while (box.childElementCount > 500) box.firstElementChild?.remove()
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
  document.body.classList.toggle('running', on)
  if (on) setStatus('正在运行 ' + (activeTab()?.path.split(/[\\/]/).pop() ?? '') + '…', 0, 'busy')
  $('console-state').textContent = on ? '运行中…' : ''
  $('console-wrap').classList.remove('collapsed')
  showPanel('out')
}

export async function initRun(): Promise<void> {
  const status = $('st-compiler') as HTMLButtonElement
  async function checkEnvironment(showDetails: boolean): Promise<void> {
    status.disabled = true
    try {
      const tools = await window.ide.detectCompilers()
      const kinds = Object.keys(languageNames) as CompilerInfo['kind'][]
      const details = kinds.map(kind => {
        const tool = tools.find(candidate => candidate.kind === kind)
        return languageNames[kind] + '  ' + (tool ? tool.version + '  ' + tool.path : '未找到运行环境')
      }).join('\n')
      status.textContent = `运行环境 ${tools.length}/${kinds.length} 就绪`
      status.title = '点击重新检查\n' + details
      if (showDetails) {
        $('console-wrap').classList.remove('collapsed')
        showPanel('out')
        out(details, 'co-dim')
      }
    } catch (error) {
      status.textContent = '重新检查运行环境'
      status.title = String(error)
    } finally { status.disabled = false }
  }
  status.onclick = () => void checkEnvironment(true)
  void checkEnvironment(false)

  $('btn-run').addEventListener('click', async () => {
    if (startingRun || runSid !== null) return
    startingRun = true
    const button = $('btn-run') as HTMLButtonElement
    button.disabled = true
    try { await doRun() }
    catch (error) {
      $('console-wrap').classList.remove('collapsed')
      out('[启动失败] ' + String(error), 'co-err')
    } finally {
      startingRun = false
      button.disabled = runSid !== null
    }
  })
  $('btn-stop').addEventListener('click', () => {
    if (runSid !== null) void window.ide.killRun(runSid)
  })
  $('console-clear').addEventListener('click', clearOut)
  $('console-toggle').addEventListener('click', () => {
    $('console-wrap').classList.toggle('collapsed')
    // 展开/收起后重新聚焦输入框，避免焦点滞留在别处
    const input = $('console-input') as HTMLInputElement
    if (!$('console-wrap').classList.contains('collapsed')) input.focus()
  })
  // 点击输出区域任意位置 → 聚焦输入框（新手友好：不用猜哪里能打字）
  $('console-out').addEventListener('click', () => {
    if (runSid !== null) ($('console-input') as HTMLInputElement).focus()
  })

  const send = (): void => {
    const input = $('console-input') as HTMLInputElement
    const v = input.value
    if (runSid === null) return
    if (!v && document.activeElement !== input) return
    // 空行也发送：等待输入的程序（scanf 陏 getchar）可以用空行继续
    out(v === '' ? '' : v, 'co-dim')
    void window.ide.sendInput(runSid, v)
    input.value = ''
  }
  $('console-send').addEventListener('click', send)
  $('console-input').addEventListener('keydown', (e) => {
    if ((e as KeyboardEvent).key === 'Enter') send()
  })

  window.ide.onRunOut((chunk: OutChunk) => {
    if (runSid !== null && !('code' in chunk) && chunk.sid === runSid) {
      const c = chunk as { text: string; stream: string }
      if (c.stream === 'stderr') runStderr(c.text)
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
      if (info.code === 0) setStatus('运行完成', 6000, 'ok')
      else setStatus(`运行结束（退出码 ${info.code ?? '被终止'}）——有报错可看「问题」面板`, 9000, 'error')
    }
  })
  window.ide.onRunNoCompiler(({ need }) => {
    const hint: Record<string, string> = {
      gcc: '没有找到 C 编译器。请先安装 MinGW-w64（或在设置中确认 gcc 所在目录已加入 PATH）。',
      'g++': '没有找到 C++ 编译器。请先安装 MinGW-w64。',
      python: '没有找到可用的 Python（含项目 .venv）。请安装 Python 3 或修复项目虚拟环境后重试。',
      node: '没有找到 Node.js。',
      typescript: '需要 Node.js 和 IDE 配套的 tsx。',
      kotlin: '需要完整 JDK 和 Kotlin/JVM 编译器。',
      java: '没有找到完整 JDK（需要同目录的 javac 与 java）。请安装 JDK 后重试。'
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
  if (!languageKind(tab.path)) {
    out(`[暂不支持运行 .${ext} 文件，支持 C / C++ / Python / Java / Kotlin / JavaScript / TypeScript]`, 'co-dim')
    $('console-wrap').classList.remove('collapsed')
    return
  }
  clearOut()
  out(`—— 运行 ${tab.path.split(/[\\/]/).pop()} ——`, 'co-dim')
  const saved = await saveActive()
  if (!saved) return
  beginRun(saved)
  const r = await window.ide.startRun(saved)
  if (r.compiler) {
    out(
      r.compiler.kind === 'java'
        ? `使用 JDK：${r.compiler.version}`
        : `使用 ${r.compiler.kind === 'g++' || r.compiler.kind === 'gcc' ? 'GCC' : r.compiler.kind}：${r.compiler.version}`,
      'co-dim'
    )
    setRunning(true, r.sid)
  } else {
    out(`[无法运行] ${r.reason ?? '没有可用的编译器/解释器'}`, 'co-err')
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
    link.addEventListener('click', () => {
      void revealLine(file, Number(line), Number(col)).catch((error) => out(String(error), 'co-err'))
    })
    row.append(link, document.createTextNode(` ${level}: ${msg}`))
    box.append(row)
  }
  box.scrollTop = box.scrollHeight
}

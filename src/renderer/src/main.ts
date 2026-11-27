import './ui.css'
import { $, el, bus, showFeedback, showPanel, state, isChordKey, type PanelName } from './core'
import { initExplorer, openFolder } from './explorer'
import { initCursorStatus, saveActive, openFile, revealLine } from './editor'
import { initRun } from './runpanel'
import { initAnvilPanel } from './anvilpanel'
import { initTerminal, activateTerminal } from './terminal'
import { initToolbox } from './toolbox'
import { initDebugger } from './debugger'
import { initSettings } from './settings'
import { initProblems } from './problems'
import { initSearchView } from './searchview'
import { initWorkbench } from './workbench'
import { initOutline } from './outline'
import { initSnippets } from './snippets'
import { initBookmarks } from './bookmarks'
import { initTasks } from './tasks'
import { initNav } from './nav'
import { initStartPage, renderRecents } from './startpage'
import { hydrateIcons } from './icons'

type Panel = PanelName

async function showConsolePanel(p: Panel): Promise<void> {
  showPanel(p)
  $('console-wrap').classList.remove('collapsed')
  if (p === 'term') await activateTerminal()
}

async function step(name: string, fn: () => Promise<void> | void): Promise<void> {
  try {
    await fn()
  } catch (e) {
    console.error(`[bootstrap] ${name} 失败：`, e)
  }
}

async function bootstrap(): Promise<void> {
  // 图标先挂上（纯展示，失败也不影响功能）
  try { hydrateIcons() } catch (e) { console.error('[bootstrap] icons 失败：', e) }
  // 监听器先挂，后续初始化任何一步失败都不影响按钮可用
  attachListeners()
  await step('explorer', () => initExplorer())
  await step('cursor', () => initCursorStatus())
  await step('terminal', () => initTerminal())
  void step('run', () => initRun())
  await step('anvil', () => initAnvilPanel())

  // —— 恢复上一次会话（独立容错：恢复失败不影响其他功能）——
  await step('restore', async () => {
    const recent = await window.ide.recentFolders()
    if (recent.lastFolder && !state.folder) await openFolder(recent.lastFolder)
    if (recent.lastFile && !state.openFile) await openFile(recent.lastFile)
  })
  // Requests received during startup stay in the main-process queue until restore finishes.
  let opening = Promise.resolve()
  const drain = () => {
    opening = opening.then(async () => {
      for (const file of await window.ide.takePendingFiles()) {
        try {
          await openFile(file)
          await window.ide.rememberFile(file)
        } catch (error) { showFeedback('打开文件失败：' + file + ' · ' + String(error)) }
      }
    }).catch(error => showFeedback(String(error)))
  }
  window.ide.onPendingFiles(drain)
  drain()
  await step('welcome-recent', () => buildWelcomeRecents())
}

function attachListeners(): void {
  // —— 面板 Tab ——
  document.querySelectorAll('.ptab').forEach((b) => {
    b.addEventListener('click', () => {
      void showConsolePanel(((b as HTMLElement).dataset.panel ?? 'out') as Panel)
        .catch((error) => showFeedback(String(error)))
    })
  })

  // —— 打开文件夹 / 打开文件 ——
  const openFolderDlg = async (): Promise<void> => {
    const folder = await window.ide.openFolder()
    if (folder) await openFolder(folder)
  }
  const openFileDlg = async (): Promise<void> => {
    const f = await window.ide.openFileDialog()
    if (f) await importFile(f)
  }
  $('btn-open').addEventListener('click', () => void openFolderDlg())
  $('btn-openfile').addEventListener('click', () => void openFileDlg())
  $('w-open').addEventListener('click', () => void openFolderDlg())
  $('w-openfile').addEventListener('click', () => void openFileDlg())

  // —— 拖拽导入：文件或文件夹直接拖进窗口 ——
  window.addEventListener('dragover', (e) => e.preventDefault())
  window.addEventListener('drop', async (e) => {
    e.preventDefault()
    const files = Array.from(e.dataTransfer?.files ?? [])
    if (!files.length) return
    for (const f of files) {
      const p = (f as File & { path?: string }).path
      if (!p) continue
      await importFile(p)
    }
  })

  initNewFile(openFolderDlg)
  const debug = initDebugger()
  const openPicker = initToolbox({
    debug,
    showPanel: showConsolePanel,
    openFile: async (path, line, column) => {
      if (line === undefined) await openFile(path)
      else await revealLine(path, line, column)
      await window.ide.rememberFile(path)
    }
  })
  // 设置 / 问题 / 搜索 / 菜单栏：各自独立，单个失败不影响其余
  for (const [name, init] of [
    ['settings', initSettings],
    ['problems', initProblems],
    ['search', initSearchView],
    ['outline', initOutline],
    ['snippets', initSnippets],
    ['bookmarks', initBookmarks],
    ['tasks', initTasks],
    ['nav', initNav],
    ['startpage', initStartPage],
    ['workbench', () => initWorkbench({
      openPicker,
      showPanel: showConsolePanel,
      openFolderDlg,
      openFileDlg,
      openRecent: importFile,
      debug
    })]
  ] as const) {
    try { init() } catch (e) { console.error(`[bootstrap] ${name} 失败：`, e) }
  }
  bus.on('panel:show', (p) => {
    void showConsolePanel((typeof p === 'string' ? p : 'out') as Panel).catch((error) => showFeedback(String(error)))
  })
  $('btn-save').addEventListener('click', () => {
    void saveActive().then((file) => { if (file) showFeedback('已保存') })
      .catch((error) => showFeedback('保存失败：' + String(error)))
  })

  // —— F5 运行；Ctrl+S 保存；Ctrl+` 切终端 ——
  window.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.isComposing || isChordKey(e) || document.querySelector('dialog[open]')) return
    if (e.key === 'F5') {
      e.preventDefault()
      ;($('btn-run') as HTMLButtonElement).click()
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      $('btn-save').click()
    } else if (e.ctrlKey && e.key === '`') {
      e.preventDefault()
      const termActive = document.querySelector('.ptab.active')?.getAttribute('data-panel') === 'term'
      const wrap = $('console-wrap')
      if (termActive && !wrap.classList.contains('collapsed')) wrap.classList.add('collapsed')
      else void showConsolePanel('term').catch((error) => showFeedback(String(error)))
    } else if (e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'b') {
      e.preventDefault()
      $('app').classList.toggle('no-side')
    } else if (e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'j') {
      e.preventDefault()
      $('console-wrap').classList.toggle('collapsed')
    } else if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'w') {
      e.preventDefault()
      $('app').classList.remove('no-side')
      bus.emit('ai:open', 'work')
    } else if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'a') {
      e.preventDefault()
      $('app').classList.remove('no-side')
      bus.emit('ai:open', 'code')
    }
  })
  initResizers()
}

// —— 拖动调整：侧栏宽度（文件树 / AI 面板各记各的）、底部控制台高度；双击控制台标题栏收起 / 展开 ——
function initResizers(): void {
  const app = $('app')
  const sidebar = $('sidebar')
  const wrap = $('console-wrap')
  const saved = (key: string): number => Number(localStorage.getItem('anvil.ui.' + key)) || 0
  const apply = (): void => {
    if (saved('side')) app.style.setProperty('--side-w', saved('side') + 'px')
    if (saved('chat')) app.style.setProperty('--chat-w', saved('chat') + 'px')
    if (saved('console')) app.style.setProperty('--console-h', saved('console') + 'px')
  }
  apply()
  function drag(handle: HTMLElement, onMove: (e: PointerEvent) => void, cls: string): void {
    handle.addEventListener('pointerdown', (e) => {
      e.preventDefault()
      handle.setPointerCapture(e.pointerId)
      document.body.classList.add('resizing', cls)
      const move = (ev: PointerEvent): void => onMove(ev)
      const up = (): void => {
        handle.removeEventListener('pointermove', move)
        document.body.classList.remove('resizing', cls)
      }
      handle.addEventListener('pointermove', move)
      handle.addEventListener('pointerup', up, { once: true })
    })
  }
  const sideHandle = $('side-resizer')
  drag(sideHandle, (ev) => {
    const w = Math.round(Math.max(200, Math.min(window.innerWidth * 0.6, ev.clientX - sidebar.getBoundingClientRect().left)))
    const key = sidebar.classList.contains('show-chat') ? 'chat' : 'side'
    localStorage.setItem('anvil.ui.' + key, String(w))
    apply()
  }, 'col')
  sideHandle.addEventListener('dblclick', () => {
    localStorage.removeItem('anvil.ui.' + (sidebar.classList.contains('show-chat') ? 'chat' : 'side'))
    app.style.removeProperty(sidebar.classList.contains('show-chat') ? '--chat-w' : '--side-w')
  })
  drag($('console-resizer'), (ev) => {
    const bottom = wrap.getBoundingClientRect().bottom
    const h = Math.round(Math.max(120, Math.min(window.innerHeight * 0.7, bottom - ev.clientY)))
    wrap.classList.remove('collapsed')
    localStorage.setItem('anvil.ui.console', String(h))
    apply()
  }, 'row')
  $('console-head').addEventListener('dblclick', (e) => {
    if ((e.target as HTMLElement).closest('button')) return
    wrap.classList.toggle('collapsed')
  })
}

function initNewFile(openFolderDlg: () => Promise<void>): void {
  const button = $('btn-newfile') as HTMLButtonElement
  const dialog = $('new-file-dialog') as HTMLDialogElement
  const field = $('new-file-name') as HTMLInputElement
  const submit = $('new-file-submit') as HTMLButtonElement
  const cancel = $('new-file-cancel') as HTMLButtonElement
  const error = $('new-file-error')
  let targetFolder: string | null = null

  button.addEventListener('click', async () => {
    button.disabled = true
    try {
      if (!state.folder) await openFolderDlg()
      targetFolder = state.folder
      if (!targetFolder) return
      $('new-file-folder').textContent = targetFolder
      field.value = ''
      error.textContent = ''
      dialog.showModal()
    } catch (e) {
      alert('无法打开新建文件窗口：' + String(e))
    } finally {
      button.disabled = false
    }
  })
  field.addEventListener('input', () => { error.textContent = '' })
  cancel.addEventListener('click', () => dialog.close())
  dialog.addEventListener('cancel', (e) => {
    if (submit.disabled) e.preventDefault()
  })
  $('new-file-form').addEventListener('submit', async (e) => {
    e.preventDefault()
    const parent = targetFolder
    if (!parent || submit.disabled) return
    const name = field.value.trim()
    if (!name || /[<>:"/\\|?*]/.test(name) || name.endsWith('.')) {
      error.textContent = '请输入有效的文件名，例如 hello.c。'
      field.focus()
      return
    }

    let file: string
    submit.disabled = cancel.disabled = field.readOnly = true
    try {
      file = await window.ide.createEntry(parent, name, false)
    } catch (e) {
      const detail = String(e)
      error.textContent = detail.includes('EEXIST') ? '同名文件或文件夹已存在。' : '创建失败：' + detail
      field.focus()
      return
    } finally {
      submit.disabled = cancel.disabled = field.readOnly = false
    }
    dialog.close()
    try {
      if (state.folder === parent) await openFolder(parent)
      await openFile(file)
    } catch (e) {
      alert('文件已创建，但无法打开：' + String(e))
    }
  })
}

// 导入单个路径：文件夹 → 展开文件树；文件 → 直接开标签（当场可运行）
async function importFile(p: string): Promise<void> {
  const isDir = await window.ide.isDir(p)
  if (isDir) {
    await openFolder(p)
  } else {
    await window.ide.rememberFile(p)
    await openFile(p)
    buildWelcomeRecents()
  }
}

// 开始页「最近使用」（实现见 startpage.ts）
function buildWelcomeRecents(): void {
  renderRecents(importFile)
}

void bootstrap()

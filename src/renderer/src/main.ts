import './ui.css'
import { $, el } from './core'
import { initExplorer, openFolder } from './explorer'
import { initCursorStatus, activeTab, saveActive, openFile } from './editor'
import { initRun } from './runpanel'
import { initAnvilPanel } from './anvilpanel'
import { initTerminal, activateTerminal } from './terminal'

type Panel = 'out' | 'term'

function showPanel(p: Panel): void {
  document.querySelectorAll('.ptab').forEach((b) => {
    b.classList.toggle('active', (b as HTMLElement).dataset.panel === p)
  })
  $('console-body').classList.toggle('hidden', p !== 'out')
  $('term-body').classList.toggle('hidden', p !== 'term')
  if (p === 'term') void activateTerminal()
}

async function step(name: string, fn: () => Promise<void> | void): Promise<void> {
  try {
    await fn()
  } catch (e) {
    console.error(`[bootstrap] ${name} 失败：`, e)
  }
}

async function bootstrap(): Promise<void> {
  // 监听器先挂，后续初始化任何一步失败都不影响按钮可用
  attachListeners()
  await step('explorer', () => initExplorer())
  await step('cursor', () => initCursorStatus())
  await step('terminal', () => initTerminal())
  await step('run', () => initRun())
  await step('anvil', () => initAnvilPanel())

  // —— 恢复上一次会话（独立容错：恢复失败不影响其他功能）——
  await step('restore', async () => {
    const recent = await window.ide.recentFolders()
    if (recent.lastFolder) await openFolder(recent.lastFolder)
    if (recent.lastFile) await openFile(recent.lastFile)
  })
  await step('welcome-recent', () => buildWelcomeRecents())

  void activeTab()
}

function attachListeners(): void {
  // —— 面板 Tab ——
  document.querySelectorAll('.ptab').forEach((b) => {
    b.addEventListener('click', () => showPanel(((b as HTMLElement).dataset.panel ?? 'out') as Panel))
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

  // —— 新建文件 ——
  $('btn-newfile').addEventListener('click', async () => {
    const { state } = await import('./core')
    if (!state.folder) {
      alert('请先打开一个文件夹（或直接拖一个文件进来）')
      return
    }
    const name = prompt('新文件名（例如 hello.c / main.py）：')
    if (!name) return
    try {
      const p = await window.ide.createEntry(state.folder, name, false)
      await openFolder(state.folder)
      await openFile(p)
    } catch (e) {
      alert(String(e))
    }
  })

  // —— F5 运行；Ctrl+S 保存；Ctrl+` 切终端 ——
  window.addEventListener('keydown', (e) => {
    if (e.key === 'F5') {
      e.preventDefault()
      ;($('btn-run') as HTMLButtonElement).click()
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      void saveActive()
    } else if (e.ctrlKey && e.key === '`') {
      e.preventDefault()
      const termActive = document.querySelector('.ptab.active')?.getAttribute('data-panel') === 'term'
      showPanel(termActive ? 'out' : 'term')
    }
  })

  })
}

// 欢迎页最近列表
function buildWelcomeRecents(): void {

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

function buildWelcomeRecents(): void {
  const box = $('w-recent')
  box.innerHTML = ''
  void window.ide.recentFolders().then((r) => {
    const files = r.recentFiles.slice(0, 4)
    const folders = r.recentFolders.slice(0, 2)
    if (!files.length && !folders.length) return
    for (const f of files) {
      const item = el('div', 'w-recent-item')
      item.append(el('span', 'tag', '文件'))
      const name = el('span', '', f.split(/[\\/]/).pop() ?? f)
      name.title = f
      item.append(name)
      item.addEventListener('click', () => void importFile(f))
      box.append(item)
    }
    for (const d of folders) {
      const item = el('div', 'w-recent-item')
      item.append(el('span', 'tag', '文件夹'))
      const name = el('span', '', d)
      name.title = d
      item.append(name)
      item.addEventListener('click', () => void openFolder(d))
      box.append(item)
    }
  })
}

void bootstrap()

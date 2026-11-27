import { app, BrowserWindow, ipcMain, dialog, shell, Menu } from 'electron'
import path from 'node:path'
import fs from 'node:fs'
import * as fsSvc from './fs-service'
import * as runner from './runner'
import * as debug from './debug-host'
import { registerChat } from './chat-host'
import * as term from './term-host'
import { loadSettings, rememberFolder, rememberFile } from './settings'
import type { OutChunk, ExitInfo } from '../shared/types'

let win: BrowserWindow | null = null
let cancelChat: (() => void) | undefined

// 单实例：重复双击快捷方式时把已有窗口拉到前台
// Explorer supplies one or more quoted paths. Keep the original argument boundaries
// when Electron forwards a launch to the already-running process.
const launchFiles = process.argv.slice(process.defaultApp ? 2 : 1)
  .filter(arg => arg && !arg.startsWith('-'))
  .map(arg => path.resolve(arg))
const pendingFiles = new Set(launchFiles)
const primaryInstance = app.requestSingleInstanceLock({ files: launchFiles })
if (!primaryInstance) {
  app.quit()
} else {
  app.on('second-instance', (_event, _argv, _cwd, data) => {
    const files = (data as { files?: unknown })?.files
    if (Array.isArray(files)) {
      for (const file of files) if (typeof file === 'string' && path.isAbsolute(file)) pendingFiles.add(file)
    }
    if (win && !win.isDestroyed()) {
      if (pendingFiles.size) win.webContents.send('file:pending')
      if (win.isMinimized()) win.restore()
      win.show()
      win.focus()
    }
  })
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: '#070b1c',
    title: 'Anvil IDE',
    icon: path.join(__dirname, '../../resources/icon.ico'),
    titleBarStyle: 'hidden',
    // 与界面标题栏（ui.css 的 --chrome）同色，系统窗口按钮才不会显得是另一块
    titleBarOverlay: { color: '#151922', symbolColor: '#c9cfdc', height: 38 },
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false
    }
  })
  win.on('ready-to-show', () => { console.error('[main-bc] ready-to-show'); win?.show() })
  // 渲染层诊断：加载失败/控制台错误/渲染进程崩溃全部落日志
  win.webContents.on('did-fail-load', (_e, code, desc, url) =>
    console.error(`[main-bc] did-fail-load code=${code} desc=${desc} url=${url}`))
  win.webContents.on('console-message', (_e, level, message) => {
    if (level >= 2) console.error(`[renderer-console] ${message}`)
  })
  win.webContents.on('render-process-gone', (_e, details) => {
    cancelChat?.()
    console.error(`[main-bc] render-process-gone reason=${details.reason}`)
  })
  win.webContents.on('did-start-navigation', event => {
    if (event.isMainFrame && !event.isSameDocument) cancelChat?.()
  })
  // 兜底：ready-to-show 3 秒未到也强制显示，避免窗口永远隐藏
  const openingWindow = win
  const fallback = setTimeout(() => {
    if (!openingWindow.isDestroyed() && !openingWindow.isVisible()) {
      console.error('[main-bc] fallback-show')
      openingWindow.show()
    }
  }, 3000)
  openingWindow.once('closed', () => clearTimeout(fallback))
  win.webContents.on('will-navigate', (event) => event.preventDefault())
  term.bindWindow(win)
  // 外部链接交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url).catch(console.error)
    return { action: 'deny' }
  })
  // ELECTRON_RENDERER_URL 由 electron-vite dev 注入
  const devUrl = process.env.ELECTRON_RENDERER_URL
  if (!app.isPackaged && devUrl) {
    void win.loadURL(devUrl)
  } else {
    void win.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
}

function emit(chunk: OutChunk | ExitInfo): void {
  if (win && !win.isDestroyed()) win.webContents.send('code' in chunk ? 'run:exit' : 'run:out', chunk)
}

const __diagBc = (m: string): void => console.error(`[main-bc] ${m} t=${Date.now()}`)
__diagBc('modules-loaded')

app.whenReady().then(() => {
  if (!primaryInstance) return
  __diagBc('whenReady')
  // 自研界面：移除 Electron 默认菜单栏，保留系统窗口按钮（titleBarOverlay）
  Menu.setApplicationMenu(null)
  registerIpc()
  __diagBc('ipc-registered')
  createWindow()
  __diagBc('window-created')
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  cancelChat?.()
  app.quit()
})

app.on('before-quit', () => {
  cancelChat?.()
  debug.shutdown()
  runner.shutdownAll()
  term.shutdownAll()
})

function registerIpc(): void {
  ipcMain.handle('file:take-pending', () => {
    const files = [...pendingFiles]
    pendingFiles.clear()
    return files
  })
  // —— 文件夹与文件 ——
  ipcMain.handle('folder:open', async () => {
    const r = await dialog.showOpenDialog(win!, { properties: ['openDirectory'] })
    if (r.canceled || !r.filePaths.length) return null
    rememberFolder(r.filePaths[0])
    return r.filePaths[0]
  })

  // 星空极光背景开 / 关时，系统窗口按钮那块的底色跟着界面标题栏变
  ipcMain.on('window:overlay', (_e, color: string) => {
    if (!/^#[0-9a-f]{6}$/i.test(color)) return
    try { win?.setTitleBarOverlay({ color, symbolColor: '#c9cfdc', height: 38 }) } catch { /* 非 Windows / 未启用 overlay */ }
  })

  // 新建项目：只选位置，不记入「最近打开」；项目建好后再记住项目文件夹本身
  ipcMain.handle('folder:pick', async (_e, title: string) => {
    const r = await dialog.showOpenDialog(win!, { title, properties: ['openDirectory', 'createDirectory'] })
    return r.canceled || !r.filePaths.length ? null : r.filePaths[0]
  })
  ipcMain.handle('folder:remember', (_e, folder: string) => {
    rememberFolder(folder)
    return true
  })

  ipcMain.handle('folder:recent', () => {
    const s = loadSettings()
    return { lastFolder: s.lastFolder, recentFolders: s.recentFolders, lastFile: s.lastFile, recentFiles: s.recentFiles }
  })

  // 单文件打开（不用先建文件夹，拖进来就能跑）
  ipcMain.handle('file:open', async () => {
    const r = await dialog.showOpenDialog(win!, {
      properties: ['openFile'],
      filters: [
        { name: '程序文件', extensions: ['c', 'cpp', 'cc', 'cxx', 'h', 'hpp', 'py', 'java', 'kt', 'kts', 'js', 'mjs', 'cjs', 'ts', 'mts', 'cts', 'tsx', 'json', 'md', 'txt'] },
        { name: '所有文件', extensions: ['*'] }
      ]
    })
    if (r.canceled || !r.filePaths.length) return null
    rememberFile(r.filePaths[0])
    return r.filePaths[0]
  })

  ipcMain.handle('fs:isdir', async (_e, p: string) => {
    try {
      return (await fs.promises.stat(p)).isDirectory()
    } catch {
      return false
    }
  })
  ipcMain.handle('fs:remember', (_e, p: string) => {
    rememberFile(p)
    return true
  })

  ipcMain.handle('fs:list', (_e, dir: string) => fsSvc.listDir(dir))
  ipcMain.handle('workspace:files', (_e, dir: string) => fsSvc.workspaceFiles(dir))
  ipcMain.handle('workspace:search', (_e, dir: string, query: string) => fsSvc.searchWorkspace(dir, query))
  ipcMain.handle('fs:read', (_e, p: string) => fsSvc.readFileSmart(p))
  ipcMain.handle('fs:write', async (_e, p: string, text: string) => {
    await fsSvc.writeFileSmart(p, text)
    return true
  })
  ipcMain.handle('fs:create', (_e, parent: string, name: string, isDir: boolean) =>
    fsSvc.createEntry(parent, name, isDir)
  )
  ipcMain.handle('fs:rename', (_e, oldPath: string, newName: string) => fsSvc.renameEntry(oldPath, newName))
  // 资源管理器删除：移到系统回收站（新手误删可还原），不做永久删除
  ipcMain.handle('fs:delete', async (_e, target: string) => {
    await shell.trashItem(target)
    return true
  })

  // —— 运行 ——
  ipcMain.handle('run:detect', () => runner.detectCompilers(true))
  ipcMain.handle('run:start', async (_e, file: string) => {
    return runner.startRun(file, emit, (need) => {
      win?.webContents.send('run:no-compiler', { need })
    })
  })
  ipcMain.handle('run:input', (_e, sid: number, line: string) => runner.sendInput(sid, line))
  ipcMain.handle('run:kill', (_e, sid: number) => runner.killRun(sid))

  ipcMain.handle('debug:start', (event, file: string, lines: number[]) => debug.start(file, lines, (state) => {
    if (!event.sender.isDestroyed()) event.sender.send('debug:state', state)
  }))
  ipcMain.handle('debug:action', (_event, id: number, action: Parameters<typeof debug.act>[1]) => debug.act(id, action))
  ipcMain.handle('debug:breakpoint', (_event, id: number, line: number, enabled: boolean) => debug.setBreakpoint(id, line, enabled))
  ipcMain.handle('debug:stop', (_event, id: number) => debug.stop(id))

  cancelChat = registerChat(() => win!)

  // —— 终端 ——
  ipcMain.handle('term:start', (_e, cwd: string | null) => term.startTerm(cwd))
  ipcMain.handle('term:in', (_e, sid: number, data: string) => term.writeTerm(sid, data))
  ipcMain.handle('term:kill', (_e, sid: number) => term.killTerm(sid))
  ipcMain.handle('term:alive', (_e, sid: number) => term.termAlive(sid))
}

import { app, BrowserWindow, ipcMain, dialog, shell, Menu } from 'electron'
import path from 'node:path'
import fs from 'node:fs'
import * as fsSvc from './fs-service'
import * as runner from './runner'
import * as anvil from './anvil-host'
import * as term from './term-host'
import { loadSettings, rememberFolder, rememberFile } from './settings'
import type { OutChunk, ExitInfo } from '../shared/types'

let win: BrowserWindow | null = null

// 单实例：重复双击快捷方式时把已有窗口拉到前台
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (win) {
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
    backgroundColor: '#1b1e26',
    title: 'Anvil IDE',
    icon: path.join(__dirname, '../../resources/icon.ico'),
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#20242e', symbolColor: '#e8eaf0', height: 38 },
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
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
  win.webContents.on('render-process-gone', (_e, details) =>
    console.error(`[main-bc] render-process-gone reason=${details.reason}`))
  // 兜底：ready-to-show 3 秒未到也强制显示，避免窗口永远隐藏
  setTimeout(() => {
    if (win && !win.isVisible() && !win.isDestroyed()) {
      console.error('[main-bc] fallback-show')
      win.show()
    }
  }, 3000)
  term.bindWindow(win)
  // 外部链接交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })
  // ELECTRON_RENDERER_URL 由 electron-vite dev 注入
  const devUrl = process.env.ELECTRON_RENDERER_URL
  if (devUrl) {
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
  __diagBc('whenReady')
  // 自研界面：移除 Electron 默认菜单栏，保留系统窗口按钮（titleBarOverlay）
  Menu.setApplicationMenu(null)
  createWindow()
  __diagBc('window-created')
  registerIpc()
  __diagBc('ipc-registered')
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  anvil.shutdown()
  app.quit()
})

app.on('before-quit', () => {
  anvil.shutdown()
  runner.shutdownAll()
  term.shutdownAll()
})

function registerIpc(): void {
  // —— 文件夹与文件 ——
  ipcMain.handle('folder:open', async () => {
    const r = await dialog.showOpenDialog(win!, { properties: ['openDirectory'] })
    if (r.canceled || !r.filePaths.length) return null
    rememberFolder(r.filePaths[0])
    return r.filePaths[0]
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
        { name: '程序文件', extensions: ['c', 'cpp', 'cc', 'cxx', 'h', 'hpp', 'py', 'java', 'js', 'ts', 'json', 'md', 'txt'] },
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
  ipcMain.handle('fs:read', (_e, p: string) => fsSvc.readFileSmart(p))
  ipcMain.handle('fs:write', async (_e, p: string, text: string) => {
    await fsSvc.writeFileSmart(p, text)
    return true
  })
  ipcMain.handle('fs:create', (_e, parent: string, name: string, isDir: boolean) =>
    fsSvc.createEntry(parent, name, isDir)
  )
  ipcMain.handle('fs:rename', (_e, oldPath: string, newName: string) => fsSvc.renameEntry(oldPath, newName))
  ipcMain.handle('fs:delete', async (_e, target: string) => {
    await fsSvc.deleteEntry(target)
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

  // —— Anvil ——
  ipcMain.handle('anvil:status', () => anvil.status())
  ipcMain.handle('anvil:ensure', () => anvil.ensure())
  ipcMain.handle('anvil:api', (_e, method: string, urlPath: string, body?: unknown) =>
    anvil.request(method, urlPath, body)
  )
  ipcMain.handle('anvil:watch', (_e, woId: string) => {
    anvil.watchWo(woId, win)
    return true
  })
  ipcMain.handle('anvil:unwatch', (_e, woId: string) => {
    anvil.unwatchWo(woId)
    return true
  })

  // —— 终端 ——
  ipcMain.handle('term:start', (_e, cwd: string | null) => term.startTerm(cwd))
  ipcMain.handle('term:in', (_e, sid: number, data: string) => term.writeTerm(sid, data))
  ipcMain.handle('term:kill', (_e, sid: number) => term.killTerm(sid))
  ipcMain.handle('term:alive', (_e, sid: number) => term.termAlive(sid))
}

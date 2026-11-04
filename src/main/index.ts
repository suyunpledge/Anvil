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

function createWindow(): void {
  win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: '#1b1e26',
    title: 'Anvil IDE',
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
  win.on('ready-to-show', () => win?.show())
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

app.whenReady().then(() => {
  // 自研界面：移除 Electron 默认菜单栏，保留系统窗口按钮（titleBarOverlay）
  Menu.setApplicationMenu(null)
  createWindow()
  registerIpc()
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
        { name: '程序文件', extensions: ['c', 'cpp', 'cc', 'cxx', 'h', 'hpp', 'py', 'js', 'ts', 'json', 'md', 'txt'] },
        { name: '所有文件', extensions: ['*'] }
      ]
    })
    if (r.canceled || !r.filePaths.length) return null
    rememberFile(r.filePaths[0])
    return r.filePaths[0]
  })

  ipcMain.handle('fs:isdir', (_e, p: string) => {
    try {
      return fs.statSync(p).isDirectory()
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
  ipcMain.handle('fs:write', (_e, p: string, text: string) => {
    fsSvc.writeFileSmart(p, text)
    return true
  })
  ipcMain.handle('fs:create', (_e, parent: string, name: string, isDir: boolean) =>
    fsSvc.createEntry(parent, name, isDir)
  )
  ipcMain.handle('fs:rename', (_e, oldPath: string, newName: string) => fsSvc.renameEntry(oldPath, newName))
  ipcMain.handle('fs:delete', (_e, target: string) => {
    fsSvc.deleteEntry(target)
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

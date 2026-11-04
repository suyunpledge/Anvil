import { contextBridge, ipcRenderer } from 'electron'
import type {
  AnvilStatus,
  ApiResponse,
  CompilerInfo,
  ExitInfo,
  FsEntry,
  OutChunk,
  ReadResult,
  RunStartResult,
  WoEvent
} from '../shared/types'

const api = {
  openFolder: (): Promise<string | null> => ipcRenderer.invoke('folder:open'),
  openFileDialog: (): Promise<string | null> => ipcRenderer.invoke('file:open'),
  recentFolders: (): Promise<{
    lastFolder: string | null
    recentFolders: string[]
    lastFile: string | null
    recentFiles: string[]
  }> => ipcRenderer.invoke('folder:recent'),
  isDir: (p: string): Promise<boolean> => ipcRenderer.invoke('fs:isdir', p),
  rememberFile: (p: string): Promise<boolean> => ipcRenderer.invoke('fs:remember', p),

  listDir: (dir: string): Promise<FsEntry[]> => ipcRenderer.invoke('fs:list', dir),
  readFile: (p: string): Promise<ReadResult> => ipcRenderer.invoke('fs:read', p),
  writeFile: (p: string, text: string): Promise<boolean> => ipcRenderer.invoke('fs:write', p, text),
  createEntry: (parent: string, name: string, isDir: boolean): Promise<string> =>
    ipcRenderer.invoke('fs:create', parent, name, isDir),
  renameEntry: (oldPath: string, newName: string): Promise<string> =>
    ipcRenderer.invoke('fs:rename', oldPath, newName),
  deleteEntry: (p: string): Promise<boolean> => ipcRenderer.invoke('fs:delete', p),

  detectCompilers: (): Promise<CompilerInfo[]> => ipcRenderer.invoke('run:detect'),
  startRun: (file: string): Promise<RunStartResult> => ipcRenderer.invoke('run:start', file),
  sendInput: (sid: number, line: string): Promise<boolean> => ipcRenderer.invoke('run:input', sid, line),
  killRun: (sid: number): Promise<boolean> => ipcRenderer.invoke('run:kill', sid),

  anvilStatus: (): Promise<AnvilStatus> => ipcRenderer.invoke('anvil:status'),
  anvilEnsure: (): Promise<AnvilStatus> => ipcRenderer.invoke('anvil:ensure'),
  anvilApi: <T = unknown>(method: string, urlPath: string, body?: unknown): Promise<ApiResponse<T>> =>
    ipcRenderer.invoke('anvil:api', method, urlPath, body),
  anvilWatch: (woId: string): Promise<boolean> => ipcRenderer.invoke('anvil:watch', woId),
  anvilUnwatch: (woId: string): Promise<boolean> => ipcRenderer.invoke('anvil:unwatch', woId),

  termStart: (cwd: string | null): Promise<{ sid: number; cwd: string }> =>
    ipcRenderer.invoke('term:start', cwd),
  termWrite: (sid: number, data: string): Promise<boolean> => ipcRenderer.invoke('term:in', sid, data),
  termKill: (sid: number): Promise<boolean> => ipcRenderer.invoke('term:kill', sid),
  termAlive: (sid: number): Promise<boolean> => ipcRenderer.invoke('term:alive', sid),

  onTermOut: (cb: (payload: { sid: number; data: string }) => void) => {
    const l = (_e: unknown, payload: { sid: number; data: string }) => cb(payload)
    ipcRenderer.on('term:out', l)
    return () => ipcRenderer.removeListener('term:out', l)
  },

  onRunOut: (cb: (chunk: OutChunk) => void) => {
    const l = (_e: unknown, chunk: OutChunk) => cb(chunk)
    ipcRenderer.on('run:out', l)
    return () => ipcRenderer.removeListener('run:out', l)
  },
  onRunExit: (cb: (info: ExitInfo) => void) => {
    const l = (_e: unknown, info: ExitInfo) => cb(info)
    ipcRenderer.on('run:exit', l)
    return () => ipcRenderer.removeListener('run:exit', l)
  },
  onRunNoCompiler: (cb: (info: { need: string }) => void) => {
    const l = (_e: unknown, info: { need: string }) => cb(info)
    ipcRenderer.on('run:no-compiler', l)
    return () => ipcRenderer.removeListener('run:no-compiler', l)
  },
  onAnvilEvent: (cb: (payload: { woId: string; ev: WoEvent }) => void) => {
    const l = (_e: unknown, payload: { woId: string; ev: WoEvent }) => cb(payload)
    ipcRenderer.on('anvil:event', l)
    return () => ipcRenderer.removeListener('anvil:event', l)
  }
}

contextBridge.exposeInMainWorld('ide', api)

export type IdeApi = typeof api

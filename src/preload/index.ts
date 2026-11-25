import { contextBridge, ipcRenderer } from 'electron'
import type {
  CompilerInfo,
  DebugAction,
  DebugSnapshot,
  ExitInfo,
  FsEntry,
  OutChunk,
  ReadResult,
  RunStartResult,
  WorkspaceFile,
  WorkspaceMatch,
  WorkspaceResults
} from '../shared/types'

const api = {
  takePendingFiles: (): Promise<string[]> => ipcRenderer.invoke('file:take-pending'),
  onPendingFiles: (cb: () => void) => {
    ipcRenderer.on('file:pending', cb)
    return () => ipcRenderer.removeListener('file:pending', cb)
  },
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
  workspaceFiles: (dir: string): Promise<WorkspaceResults<WorkspaceFile>> =>
    ipcRenderer.invoke('workspace:files', dir),
  searchWorkspace: (dir: string, query: string): Promise<WorkspaceResults<WorkspaceMatch>> =>
    ipcRenderer.invoke('workspace:search', dir, query),
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

  debugStart: (file: string, lines: number[]): Promise<number> => ipcRenderer.invoke('debug:start', file, lines),
  debugAction: (id: number, action: DebugAction): Promise<void> => ipcRenderer.invoke('debug:action', id, action),
  debugBreakpoint: (id: number, line: number, enabled: boolean): Promise<void> =>
    ipcRenderer.invoke('debug:breakpoint', id, line, enabled),
  debugStop: (id: number): Promise<void> => ipcRenderer.invoke('debug:stop', id),
  onDebugState: (cb: (state: DebugSnapshot) => void) => {
    const listener = (_event: unknown, state: DebugSnapshot) => cb(state)
    ipcRenderer.on('debug:state', listener)
    return () => ipcRenderer.removeListener('debug:state', listener)
  },

  chatModels: (): Promise<string[]> => ipcRenderer.invoke('chat:models'),
  chatRelease: (): Promise<void> => ipcRenderer.invoke('chat:release'),
  chatSelect: (suggested?: string): Promise<{ file: string; undo: boolean } | null> => ipcRenderer.invoke('chat:select', suggested),
  chatSend: (model: string, prompt: string): Promise<{
    needsFile: boolean; answer: string; tools: string[]; patch: { id: string; old: string; replacement: string } | null
  }> => ipcRenderer.invoke('chat:send', model, prompt),
  chatApply: (id: string): Promise<{ text: string; undo: boolean }> => ipcRenderer.invoke('chat:apply', id),
  chatUndo: (): Promise<{ text: string; undo: boolean }> => ipcRenderer.invoke('chat:undo'),
  chatDiscard: (): Promise<void> => ipcRenderer.invoke('chat:discard'),
  chatCancel: (): Promise<void> => ipcRenderer.invoke('chat:cancel'),
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
  }
}

contextBridge.exposeInMainWorld('ide', api)

export type IdeApi = typeof api

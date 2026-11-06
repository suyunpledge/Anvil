// 主进程 ↔ 渲染进程 的 IPC 契约类型

export interface FsEntry {
  name: string
  path: string
  isDir: boolean
  size: number
}

export interface ReadResult {
  text: string
  encoding: 'utf-8' | 'gbk' | 'latin1'
}

export interface CompilerInfo {
  kind: 'gcc' | 'g++' | 'python' | 'node' | 'java'
  path: string
  version: string
  /** Java 编译器配套的 java 可执行文件。 */
  runtimePath?: string
}

export type OutChunk = { sid: number; stream: 'stdout' | 'stderr'; text: string }
export type ExitInfo = { sid: number; code: number | null; signal?: string }

export interface AnvilStatus {
  running: boolean
  startedByUs: boolean
  health: Record<string, unknown> | null
  token: string | null
  error: string | null
  adapters: Array<{ key: string; label: string; bad?: boolean }>
  integrations: Record<string, unknown> | null
}

export interface ApiResponse<T = unknown> {
  status: number
  data: T | null
  error?: string
}

export interface RunStartResult {
  sid: number
  compileNeeded: boolean
  compiler: CompilerInfo | null
  reason?: string
}

// Anvil SSE 事件（宽松类型：不同 kind 载荷不同，渲染层按 kind 分支）
export interface WoEvent {
  kind: string
  wo_id?: string
  [key: string]: unknown
}

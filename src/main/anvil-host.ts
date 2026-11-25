import { spawn, type ChildProcess } from 'node:child_process'
import fs from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import type { AnvilStatus, ApiResponse } from '../shared/types'
import { loadSettings } from './settings'
import { BackendClient, type BackendLocation } from './anvil-client'
import { WorkOrderSubscription } from './anvil-events'
import { stopProcess } from './process-host'

type Win = import('electron').BrowserWindow | null
type StartResult = { ok: boolean; error?: string }

/** 生命周期、协议传输和事件订阅分层；并发调用共享进行中的启动与探测。 */
export class AnvilHost {
  readonly client: BackendClient
  private child: ChildProcess | null = null
  private starting: Promise<StartResult> | null = null
  private checking: Promise<AnvilStatus> | null = null
  private refreshing: Promise<void> | null = null
  private stop = new AbortController()
  private adapters: AnvilStatus['adapters'] = []
  private integrations: Record<string, unknown> | null = null
  private metaAt = 0
  private watches = new Map<string, WorkOrderSubscription>()

  constructor(private settings: () => BackendLocation = loadSettings,
    private spawnBackend: typeof spawn = spawn, private startupMs = 90000) {
    this.client = new BackendClient(settings)
  }

  request<T = unknown>(method: string, urlPath: string, body?: unknown, timeoutMs = 15000): Promise<ApiResponse<T>> {
    return this.client.request<T>(method, urlPath, body, timeoutMs)
  }

  private async probe(timeout = 3000): Promise<Record<string, unknown> | null> {
    const response = await this.request<Record<string, unknown>>('GET', '/health', undefined, timeout)
    const data = response.data
    return response.status === 200 && data?.ok === true && 'wos' in data && 'core' in data ? data : null
  }

  async health(): Promise<boolean> { return Boolean(await this.probe()) }

  startBackend(): Promise<StartResult> {
    if (!this.starting) this.starting = this.launch().finally(() => { this.starting = null })
    return this.starting
  }

  private async launch(): Promise<StartResult> {
    if (this.stop.signal.aborted) return { ok: false, error: '宿主已关闭' }
    if (await this.health()) return { ok: true }
    if (this.child) return { ok: false, error: '上一次启动的后端进程尚未退出' }
    const settings = this.settings()
    let stderr = ''
    let failure = ''
    let proc: ChildProcess | null = null
    try {
      await fs.access(path.join(settings.anvilRepo, 'anvil.py'))
      const python = path.join(settings.anvilRepo, '.venv', 'Scripts', 'python.exe')
      await fs.access(python)
      if (this.stop.signal.aborted) return { ok: false, error: '宿主已关闭' }
      proc = this.spawnBackend(python, ['anvil.py', '--port', String(settings.anvilPort)], {
        cwd: settings.anvilRepo, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
      })
      this.child = proc
      proc.stdout?.resume()
      proc.stderr?.on('data', (chunk) => { stderr = (stderr + String(chunk)).slice(-4000) })
      proc.on('error', (error) => { failure = String(error); if (this.child === proc) this.child = null })
      proc.on('exit', (code) => {
        failure ||= `Anvil 已退出（${code}）${stderr ? '：' + stderr : ''}`
        if (this.child === proc) this.child = null
        this.metaAt = 0
      })
      const deadline = Date.now() + this.startupMs
      while (Date.now() < deadline && !failure && !this.stop.signal.aborted) {
        if (await this.probe(Math.min(3000, Math.max(1, deadline - Date.now())))) {
          this.metaAt = 0; return { ok: true }
        }
        if (failure) break
        await delay(Math.min(300, Math.max(0, deadline - Date.now())), undefined, { signal: this.stop.signal })
      }
      stopProcess(proc)
      return { ok: false, error: failure || (this.stop.signal.aborted ? '宿主已关闭' : '等待 Anvil 启动超时') }
    } catch (error) {
      stopProcess(proc)
      return { ok: false, error: `Anvil 启动失败：${String(error)}` }
    }
  }

  refreshMeta(): Promise<void> {
    if (!this.refreshing) this.refreshing = this.loadMeta().finally(() => { this.refreshing = null })
    return this.refreshing
  }

  private async loadMeta(): Promise<void> {
    const [presets, integrations] = await Promise.all([
      this.request<Record<string, unknown>>('GET', '/presets'), this.request<Record<string, unknown>>('GET', '/integrations')
    ])
    this.adapters = []
    const available = integrations.status === 200 && Array.isArray(integrations.data?.available_models)
      ? new Set(integrations.data.available_models as string[]) : null
    if (presets.status === 200 && presets.data) {
      const raw = presets.data.profiles || presets.data.presets || presets.data
      const unusable = presets.data.unusable
      this.adapters = (Array.isArray(raw) ? raw : []).filter((item) => typeof item?.key === 'string').map((item) => ({
        key: item.key, label: item.label || item.key,
        bad: Boolean(item.bad || (available && typeof item.model === 'string' && !available.has(item.model))
          || integrations.data?.ollama === false || (Array.isArray(unusable) ? unusable.includes(item.model)
          : unusable && typeof unusable === 'object' && item.model in unusable))
      }))
      const preferred = presets.data.default
      this.adapters.sort((a, b) => Number(b.key === preferred) - Number(a.key === preferred))
    }
    this.integrations = integrations.status === 200 ? integrations.data : null
    this.metaAt = presets.status === 200 && integrations.status === 200 ? Date.now() : 0
  }

  status(): Promise<AnvilStatus> {
    if (!this.checking) this.checking = this.readStatus().finally(() => { this.checking = null })
    return this.checking
  }

  private async readStatus(): Promise<AnvilStatus> {
    const health = await this.probe()
    if (health && Date.now() - this.metaAt > 30000) await this.refreshMeta()
    if (!health) { this.metaAt = 0; this.adapters = []; this.integrations = null }
    return { running: Boolean(health), startedByUs: Boolean(this.child), health,
      token: await this.client.token(), error: null, adapters: this.adapters, integrations: this.integrations }
  }

  async ensure(): Promise<AnvilStatus> {
    const start = await this.startBackend()
    const result = await this.status()
    return result.running ? result : { ...result, error: start.error || '启动失败' }
  }

  watchWo(woId: string, win: Win): void {
    this.unwatchWo(woId)
    if (this.stop.signal.aborted || !win || win.isDestroyed()) return
    const subscription = new WorkOrderSubscription(this.client, woId,
      (ev) => { if (!win.isDestroyed()) win.webContents.send('anvil:event', { woId, ev }) },
      () => { if (this.watches.get(woId) === subscription) this.watches.delete(woId) })
    this.watches.set(woId, subscription)
  }

  unwatchWo(woId: string): void { this.watches.get(woId)?.close() }

  shutdown(): void {
    this.stop.abort()
    for (const subscription of this.watches.values()) subscription.close()
    this.client.close()
    stopProcess(this.child)
  }
}

const host = new AnvilHost()
export const request = host.request.bind(host)
export const health = host.health.bind(host)
export const startBackend = host.startBackend.bind(host)
export const refreshMeta = host.refreshMeta.bind(host)
export const status = host.status.bind(host)
export const ensure = host.ensure.bind(host)
export const watchWo = host.watchWo.bind(host)
export const unwatchWo = host.unwatchWo.bind(host)
export const shutdown = host.shutdown.bind(host)

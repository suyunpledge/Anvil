import { spawn, ChildProcess } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import type { AnvilStatus, ApiResponse } from '../shared/types'
import { loadSettings } from './settings'

// —— Anvil 后端宿主：健康检查、按需拉起 anvil.py、带令牌的 API 代理、SSE 转发 ——

type Win = import('electron').BrowserWindow | null

let startedByUs = false
let child: ChildProcess | null = null
let tokenCache: string | null = null
let adaptersCache: AnvilStatus['adapters'] = []
let integrationsCache: Record<string, unknown> | null = null

function base(): string {
  const s = loadSettings()
  return `http://127.0.0.1:${s.anvilPort}`
}

function token(): string | null {
  if (tokenCache) return tokenCache
  try {
    const s = loadSettings()
    const p = path.join(s.anvilRepo, '.runtime', 'service-token')
    if (fs.existsSync(p)) {
      tokenCache = fs.readFileSync(p, 'utf-8').trim()
      return tokenCache
    }
  } catch {
    /* 读不到令牌就走无令牌请求，后端会返回 401 */
  }
  return null
}

export function request<T = unknown>(
  method: string,
  urlPath: string,
  body?: unknown,
  timeoutMs = 15000
): Promise<ApiResponse<T>> {
  return new Promise((resolve) => {
    const payload = body === undefined ? null : JSON.stringify(body)
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Cache-Control': 'no-store'
    }
    if (payload) headers['Content-Type'] = 'application/json'
    if (token()) {
      headers['X-Local-Ide-Token'] = token() as string
      headers.Authorization = `Bearer ${token()}`
    }
    const req = http.request(
      base() + urlPath,
      { method, headers, timeout: timeoutMs },
      (res) => {
        let raw = ''
        res.setEncoding('utf-8')
        res.on('data', (d) => (raw += d))
        res.on('end', () => {
          let data: unknown = null
          try {
            data = raw ? JSON.parse(raw) : null
          } catch {
            data = raw
          }
          resolve({ status: res.statusCode ?? 0, data: data as T })
        })
      }
    )
    req.on('timeout', () => {
      req.destroy()
      resolve({ status: 0, data: null, error: 'timeout' })
    })
    req.on('error', (e) => resolve({ status: 0, data: null, error: String(e) }))
    if (payload) req.write(payload)
    req.end()
  })
}

export async function health(): Promise<boolean> {
  const r = await request('GET', '/health', undefined, 3000)
  return r.status === 200
}

export async function startBackend(): Promise<{ ok: boolean; error?: string }> {
  if (await health()) return { ok: true }
  const s = loadSettings()
  const py = path.join(s.anvilRepo, '.venv', 'Scripts', 'python.exe')
  const pythonExe = fs.existsSync(py) ? py : 'python'
  if (!fs.existsSync(path.join(s.anvilRepo, 'anvil.py'))) {
    return { ok: false, error: `Anvil 仓库不存在：${s.anvilRepo}` }
  }
  return new Promise((resolve) => {
    try {
      child = spawn(pythonExe, ['anvil.py'], {
        cwd: s.anvilRepo,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
      })
      startedByUs = true
      child.stderr?.on('data', (b) => console.error('[anvil]', String(b).trim()))
      child.on('error', (e) => resolve({ ok: false, error: String(e) }))
      child.on('exit', () => {
        child = null
      })
      // 轮询健康，最多 90 秒（本地模型冷启动可能较慢）
      const t0 = Date.now()
      const timer = setInterval(async () => {
        if (await health()) {
          clearInterval(timer)
          resolve({ ok: true })
        } else if (Date.now() - t0 > 90000) {
          clearInterval(timer)
          resolve({ ok: false, error: '等待 Anvil 启动超时（90 秒）' })
        }
      }, 1200)
    } catch (e) {
      return { ok: false, error: String(e) }
    }
  })
}

export async function refreshMeta(): Promise<void> {
  const [presets, integ] = await Promise.all([
    request<{ presets?: Array<{ key: string; label?: string; bad?: boolean }> }>('GET', '/presets'),
    request('GET', '/integrations')
  ])
  if (presets.status === 200 && presets.data) {
    const raw = presets.data.presets || (presets.data as unknown as Array<{ key: string; label?: string; bad?: boolean }>)
    adaptersCache = (Array.isArray(raw) ? raw : []).map((a) => ({
      key: a.key,
      label: a.label || a.key,
      bad: a.bad
    }))
  }
  if (integ.status === 200) integrationsCache = integ.data as Record<string, unknown>
}

export async function status(): Promise<AnvilStatus> {
  const running = await health()
  if (running && (adaptersCache.length === 0 || !integrationsCache)) await refreshMeta()
  const h = running ? ((await request('GET', '/health')).data as Record<string, unknown>) : null
  return {
    running,
    startedByUs,
    health: h,
    token: token(),
    error: null,
    adapters: adaptersCache,
    integrations: integrationsCache
  }
}

export async function ensure(): Promise<AnvilStatus> {
  const st = await startBackend()
  if (!st.ok && !(await health())) {
    return {
      running: false,
      startedByUs,
      health: null,
      token: token(),
      error: st.error || '启动失败',
      adapters: [],
      integrations: null
    }
  }
  await refreshMeta()
  return status()
}

// —— SSE 订阅转发 ——

interface Watch {
  woId: string
  req: http.ClientRequest
}

const watches = new Map<string, Watch>()

export function watchWo(woId: string, win: Win): void {
  unwatchWo(woId)
  const headers: Record<string, string> = {
    Accept: 'text/event-stream',
    'Cache-Control': 'no-store'
  }
  if (token()) {
    headers['X-Local-Ide-Token'] = token() as string
    headers.Authorization = `Bearer ${token()}`
  }
  const req = http.request(base() + `/wo/${woId}/events`, { method: 'GET', headers }, (res) => {
    res.setEncoding('utf-8')
    let buf = ''
    res.on('data', (chunk) => {
      buf += chunk
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const dataLines = frame
          .split('\n')
          .filter((l) => l.startsWith('data:'))
          .map((l) => l.slice(5).trim())
        if (!dataLines.length) continue
        let ev: unknown = null
        try {
          ev = JSON.parse(dataLines.join('\n'))
        } catch {
          ev = { kind: 'raw', text: dataLines.join('\n') }
        }
        if (win && !win.isDestroyed()) win.webContents.send('anvil:event', { woId, ev })
      }
    })
    res.on('end', () => {
      if (win && !win.isDestroyed()) win.webContents.send('anvil:event', { woId, ev: { kind: 'stream_end' } })
      watches.delete(woId)
    })
  })
  req.on('error', () => {
    watches.delete(woId)
  })
  req.end()
  watches.set(woId, { woId, req })
}

export function unwatchWo(woId: string): void {
  const w = watches.get(woId)
  if (w) {
    try {
      w.req.destroy()
    } catch {
      /* 已断开 */
    }
    watches.delete(woId)
  }
}

export function shutdown(): void {
  for (const id of [...watches.keys()]) unwatchWo(id)
  if (child && startedByUs) {
    try {
      child.kill()
    } catch {
      /* 已退出 */
    }
    child = null
  }
}

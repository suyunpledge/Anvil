import http from 'node:http'
import fs from 'node:fs/promises'
import path from 'node:path'
import type { ApiResponse } from '../shared/types'

export interface BackendLocation { anvilRepo: string; anvilPort: number }

/** API 共用连接池，SSE 独立连接；令牌每次异步读取，兼容后端重启。 */
export class BackendClient {
  private agent = new http.Agent({ keepAlive: true, maxSockets: 8, maxFreeSockets: 2 })
  private pending = new Set<http.ClientRequest>()
  private closed = false
  constructor(readonly location: () => BackendLocation) {}

  url(urlPath: string): string {
    if (!urlPath.startsWith('/') || urlPath.startsWith('//') || /[\r\n#]/.test(urlPath)) throw new Error('无效的 Anvil API 路径')
    const port = this.location().anvilPort
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('无效的 Anvil 端口')
    return `http://127.0.0.1:${port}${urlPath}`
  }

  async token(): Promise<string | null> {
    const repo = this.location().anvilRepo
    const runtime = process.env.LOCAL_IDE_RUNTIME || path.join(repo, '.runtime')
    try { return (await fs.readFile(path.resolve(repo, runtime, 'service-token'), 'utf8')).trim() || null }
    catch { return null }
  }

  async headers(): Promise<Record<string, string>> {
    const token = await this.token()
    return token ? { 'X-Local-Ide-Token': token } : {}
  }

  async request<T = unknown>(method: string, urlPath: string, body?: unknown, timeoutMs = 15000): Promise<ApiResponse<T>> {
    try {
      const url = this.url(urlPath)
      const payload = body === undefined ? undefined : JSON.stringify(body)
      const headers = { Accept: 'application/json', 'Cache-Control': 'no-store', ...(await this.headers()) }
      if (this.closed) return { status: 0, data: null, error: '宿主已关闭' }
      return await new Promise<ApiResponse<T>>((resolve) => {
        let settled = false
        let timer: ReturnType<typeof setTimeout>
        const finish = (result: ApiResponse<T>) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          this.pending.delete(req)
          resolve(result)
        }
        const req = http.request(url, {
          method, agent: this.agent,
          headers: payload === undefined ? headers : { ...headers, 'Content-Type': 'application/json' }
        }, (res) => {
          const chunks: Buffer[] = []
          let bytes = 0
          res.on('data', (chunk: Buffer) => {
            bytes += chunk.length
            if (bytes > 16 * 1024 * 1024) req.destroy(new Error('Anvil 响应超过 16 MiB'))
            else chunks.push(chunk)
          })
          res.on('error', (error) => finish({ status: 0, data: null, error: String(error) }))
          res.on('end', () => {
            const raw = Buffer.concat(chunks).toString('utf8')
            let data: unknown = raw || null
            try { data = raw ? JSON.parse(raw) : null } catch { /* 保留非 JSON 错误响应 */ }
            finish({ status: res.statusCode || 0, data: data as T })
          })
        })
        this.pending.add(req)
        timer = setTimeout(() => req.destroy(new Error('Anvil 请求超时')), timeoutMs)
        req.on('error', (error) => finish({ status: 0, data: null, error: String(error) }))
        req.end(payload)
      })
    } catch (error) { return { status: 0, data: null, error: String(error) } }
  }

  close(): void {
    this.closed = true
    for (const req of this.pending) req.destroy(new Error('宿主已关闭'))
    this.agent.destroy()
  }
}

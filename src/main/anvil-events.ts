import http from 'node:http'
import type { BackendClient } from './anvil-client'

/** 主进程完成续读和去重，渲染层仍接收原有 { woId, ev } 契约。 */
export class WorkOrderSubscription {
  private req: http.ClientRequest | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private closed = false
  private cursor = 0
  private failures = 0
  constructor(private client: BackendClient, readonly woId: string,
    private emit: (event: Record<string, unknown>) => void, private onClose: () => void,
    private retryMs = 250) { void this.connect() }

  private async connect(): Promise<void> {
    if (this.closed) return
    try {
      const headers = await this.client.headers()
      if (this.closed) return
      if (this.cursor) headers['Last-Event-ID'] = String(this.cursor)
      let ended = false
      const lost = () => {
        if (ended || this.closed) return
        ended = true
        req.destroy()
        if (++this.failures > 8) return this.finish('事件连接连续中断，请重新连接工单')
        this.retryTimer = setTimeout(() => {
          this.retryTimer = null
          void this.connect()
        }, Math.min(this.retryMs * 2 ** (this.failures - 1), 5000))
      }
      const req = http.request(this.client.url(`/wo/${encodeURIComponent(this.woId)}/events`), {
        agent: false, headers: { ...headers, Accept: 'text/event-stream', 'Cache-Control': 'no-store' }
      }, (res) => {
        if (res.statusCode !== 200) {
          res.resume()
          if ([400, 403, 404].includes(res.statusCode || 0)) this.finish(`事件接口返回 ${res.statusCode}`)
          else lost()
          return
        }
        res.setEncoding('utf8')
        let buffer = ''
        let lines: string[] = []
        let frameBytes = 0
        const frame = () => {
          const data: string[] = []
          let id: number | null = null
          for (const line of lines) {
            if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
            if (line.startsWith('id:') && /^\d+$/.test(line.slice(3).trim())) id = Number(line.slice(3).trim())
          }
          lines = []
          frameBytes = 0
          if (!data.length) return
          const text = data.join('\n')
          if (text === '[DONE]') { ended = true; this.finish(); return }
          let ev: Record<string, unknown>
          try {
            const parsed: unknown = JSON.parse(text)
            ev = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
              ? parsed as Record<string, unknown> : { kind: 'raw', text }
          } catch { ev = { kind: 'raw', text } }
          if (ev.kind === 'snapshot' && ev.reset === true) this.cursor = 0
          if (id !== null && Number.isSafeInteger(id)) {
            if (id <= this.cursor) return
            this.cursor = id
            this.failures = 0
          }
          this.emit(ev)
        }
        res.on('data', (chunk: string) => {
          if (this.closed || ended) return
          buffer += chunk
          frameBytes += Buffer.byteLength(chunk)
          if (frameBytes > 16 * 1024 * 1024) { this.finish('事件帧超过 16 MiB'); return }
          let newline: number
          while (!this.closed && (newline = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, newline).replace(/\r$/, '')
            buffer = buffer.slice(newline + 1)
            if (!line) frame()
            else lines.push(line)
          }
        })
        res.on('error', lost)
        res.on('end', lost)
        res.on('close', lost)
      })
      this.req = req
      req.setTimeout(45000, lost)
      req.on('error', lost)
      req.end()
    } catch (error) { this.finish(String(error)) }
  }

  private finish(error?: string): void {
    if (this.closed) return
    this.emit({ kind: 'stream_end', ...(error ? { error } : {}) })
    this.close()
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.req?.destroy()
    this.onClose()
  }
}

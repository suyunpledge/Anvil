/** 短暂合并连续输出，限制单次 IPC 大小；退出前必须 flush。 */
export class OutputBuffer {
  private chunks: Array<{ stream: 'stdout' | 'stderr'; text: string }> = []
  private size = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  constructor(private send: (stream: 'stdout' | 'stderr', text: string) => void) {}
  write(stream: 'stdout' | 'stderr', text: string): void {
    if (!text) return
    for (let offset = 0; offset < text.length;) {
      let end = Math.min(offset + 32768, text.length)
      if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1]) && /[\uDC00-\uDFFF]/.test(text[end])) end--
      const piece = text.slice(offset, end)
      offset = end
      if (this.size + piece.length > 32768) this.flush()
      const last = this.chunks[this.chunks.length - 1]
      if (last?.stream === stream) last.text += piece
      else this.chunks.push({ stream, text: piece })
      this.size += piece.length
      if (this.size >= 32768) this.flush()
    }
    if (this.size && !this.timer) this.timer = setTimeout(() => this.flush(), 16)
  }
  flush(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    const chunks = this.chunks
    this.chunks = []; this.size = 0
    for (const chunk of chunks) this.send(chunk.stream, chunk.text)
  }
}

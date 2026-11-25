import fs from 'node:fs/promises'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { withFileWrite } from './fs-service'

const limit = 128 * 1024
const hash = (text: string) => createHash('sha256').update(text).digest('hex')
const key = (file: string) => process.platform === 'win32' ? path.resolve(file).toLowerCase() : path.resolve(file)
const extensions = /\.(?:c|cpp|cc|cxx|h|hpp|py|js|mjs|cjs|ts|cts|mts|tsx|jsx|kt|kts|java|go|rs|css|html|json|md|txt|toml|yaml|yml)$/i

export async function snapshot(file: string): Promise<string> {
  if (!path.isAbsolute(file) || !extensions.test(file)) throw new Error('请选择普通代码或文本文件')
  for (let part = path.resolve(file); part !== path.dirname(part); part = path.dirname(part)) {
    if ((await fs.lstat(part)).isSymbolicLink()) throw new Error('授权路径不能经过链接')
  }
  const handle = await fs.open(file, 'r')
  try {
    const stat = await handle.stat()
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > limit) throw new Error('仅支持 128 KiB 以内、没有硬链接的普通文件')
    const raw = await handle.readFile()
    const text = raw.toString('utf8')
    if (raw.length > limit || text.includes('\0') || !Buffer.from(text).equals(raw)) throw new Error('AI 修改仅支持 UTF-8 文本')
    return text
  } finally { await handle.close() }
}

interface Entry { before: string; after: string }
interface Journal { file: string; entries: Entry[] }

async function atomic(file: string, content: string, mode: number, check?: () => Promise<void>): Promise<void> {
  const temp = path.join(path.dirname(file), '.anvil-' + randomUUID() + '.tmp')
  const handle = await fs.open(temp, 'wx', mode)
  try {
    try { await handle.writeFile(content); await handle.sync() } finally { await handle.close() }
    await check?.()
    await fs.rename(temp, file)
  } finally { await fs.unlink(temp).catch(() => {}) }
}

export class EditHistory {
  constructor(private directory: string) {}
  private journal(file: string): string { return path.join(this.directory, hash(key(file)) + '.json') }
  private async load(file: string): Promise<Journal> {
    try {
      const data = JSON.parse(await fs.readFile(this.journal(file), 'utf8')) as Journal
      if (key(data.file) !== key(file) || !Array.isArray(data.entries) || !data.entries.every(e => typeof e.before === 'string' && typeof e.after === 'string'))
        throw new Error('撤销记录损坏')
      return data
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e
      return { file, entries: [] }
    }
  }
  private async save(journal: Journal): Promise<void> {
    await fs.mkdir(this.directory, { recursive: true })
    await atomic(this.journal(journal.file), JSON.stringify(journal), 0o600)
  }
  // A prepared entry whose preimage is still on disk was never committed, or was already undone.
  private async reconciled(file: string, current: string): Promise<Journal> {
    const journal = await this.load(file)
    const last = journal.entries.at(-1)
    if (last && last.before === current && last.after !== hash(current)) journal.entries.pop()
    return journal
  }
  async canUndo(file: string): Promise<boolean> {
    const current = await snapshot(file)
    return (await this.reconciled(file, current)).entries.at(-1)?.after === hash(current)
  }
  async change(file: string, before: string, next?: string): Promise<string> {
    let result = ''
    await withFileWrite(file, async () => {
      const check = async () => {
        if (await snapshot(file) !== before) throw new Error('目标文件已变化，请重新预览')
      }
      await check()
      const journal = await this.reconciled(file, before)
      const mode = (await fs.stat(file)).mode
      if (next === undefined) {
        const entry = journal.entries.at(-1)
        if (!entry || entry.after !== hash(before)) throw new Error('文件已变化或没有可撤销修改')
        result = entry.before
        await atomic(file, result, mode, check)
        journal.entries.pop()
        await this.save(journal)
      } else {
        if (Buffer.byteLength(next) > limit || next === before || next.includes('\0')) throw new Error('修改为空或超出文件限制')
        journal.entries.push({ before, after: hash(next) })
        journal.entries = journal.entries.slice(-20)
        await this.save(journal)
        await atomic(file, next, mode, check)
        result = next
      }
    })
    return result
  }
}

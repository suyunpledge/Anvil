import fs from 'node:fs'
import path from 'node:path'
import type { FsEntry, ReadResult } from '../shared/types'

const names = new Intl.Collator('zh-Hans-CN')
const writes = new Map<string, Promise<void>>()
const fileKey = (p: string) => process.platform === 'win32' ? path.resolve(p).toLowerCase() : path.resolve(p)

// —— 文件树与文件读写（含 GBK 兜底解码） ——

export async function listDir(dir: string): Promise<FsEntry[]> {
  const entries = await fs.promises.readdir(dir, { withFileTypes: true })
  const out: FsEntry[] = []
  const visible = entries.filter(({ name }) => !name.startsWith('.') || name === '.gitignore')
  // 大目录不会同步阻塞 Electron 主循环，也不一次排入数万个 stat。
  for (let start = 0; start < visible.length; start += 16) {
    await Promise.all(visible.slice(start, start + 16).map(async (entry) => {
      const full = path.join(dir, entry.name)
      try {
        if (entry.isDirectory()) out.push({ name: entry.name, path: full, isDir: true, size: 0 })
        else {
          const st = await fs.promises.stat(full)
          out.push({ name: entry.name, path: full, isDir: st.isDirectory(), size: st.isFile() ? st.size : 0 })
        }
      } catch { /* 条目已消失或不可访问 */ }
    }))
  }
  out.sort((a, b) => (a.isDir === b.isDir ? names.compare(a.name, b.name) : a.isDir ? -1 : 1))
  return out
}

function decodeBuffer(buf: Buffer): ReadResult {
  const utf8 = new TextDecoder('utf-8', { fatal: false }).decode(buf)
  // 严格校验：合法 UTF-8 解码应能无损往返
  const roundTrip = Buffer.from(utf8, 'utf-8')
  if (roundTrip.equals(buf)) return { text: utf8, encoding: 'utf-8' }
  try {
    const gbk = new TextDecoder('gbk').decode(buf)
    if (!gbk.includes('\uFFFD')) return { text: gbk, encoding: 'gbk' }
  } catch {
    /* Chromium 不支持 gbk 时走 latin1 */
  }
  return { text: utf8, encoding: 'utf-8' }
}

export async function readFileSmart(p: string): Promise<ReadResult> {
  await writes.get(fileKey(p))
  const buf = await fs.promises.readFile(p)
  if (buf.length === 0) return { text: '', encoding: 'utf-8' }
  return decodeBuffer(buf)
}

export async function writeFileSmart(p: string, text: string): Promise<void> {
  // v1 统一写 UTF-8：运行控制台也按 UTF-8 解码，与 GCC 默认 exec-charset 一致。
  // 原文件是 GBK 时首编译后即完成迁移；中文内容在 UTF-8 下无损。
  const key = fileKey(p)
  const pending = (writes.get(key) || Promise.resolve()).catch(() => {}).then(() => fs.promises.writeFile(p, text, 'utf-8'))
  writes.set(key, pending)
  try { await pending } finally { if (writes.get(key) === pending) writes.delete(key) }
}

export async function createEntry(parent: string, name: string, isDir: boolean): Promise<string> {
  const full = path.join(parent, name)
  if (isDir) await fs.promises.mkdir(full)
  else await fs.promises.writeFile(full, '', { encoding: 'utf-8', flag: 'wx' })
  return full
}

export async function renameEntry(oldPath: string, newName: string): Promise<string> {
  const parent = path.dirname(oldPath)
  const full = path.join(parent, newName)
  try {
    await fs.promises.lstat(full)
    throw new Error('目标名称已存在：' + newName)
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error }
  await fs.promises.rename(oldPath, full)
  return full
}

export async function deleteEntry(target: string): Promise<void> {
  const st = await fs.promises.lstat(target)
  if (st.isDirectory()) await fs.promises.rm(target, { recursive: true, force: true })
  else await fs.promises.unlink(target)
}

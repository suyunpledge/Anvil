import fs from 'node:fs'
import path from 'node:path'
import type { FsEntry, ReadResult, WorkspaceFile, WorkspaceMatch, WorkspaceResults } from '../shared/types'

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
  const utf8 = new TextDecoder('utf-8', { ignoreBOM: true }).decode(buf)
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
  const canonicalPath = await fs.promises.realpath(p)
  await writes.get(fileKey(canonicalPath))
  const buf = await fs.promises.readFile(canonicalPath)
  return { ...decodeBuffer(buf), canonicalPath }
}

export async function writeFileSmart(p: string, text: string): Promise<void> {
  // v1 统一写 UTF-8：运行控制台也按 UTF-8 解码，与 GCC 默认 exec-charset 一致。
  // 原文件是 GBK 时首编译后即完成迁移；中文内容在 UTF-8 下无损。
  await withFileWrite(p, () => fs.promises.writeFile(p, text, 'utf-8'))
}

export async function withFileWrite(p: string, write: () => Promise<void>): Promise<void> {
  const key = fileKey(p)
  const pending = (writes.get(key) || Promise.resolve()).catch(() => {}).then(write)
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

const ignoredFolders = new Set(['node_modules', 'dist', 'out', 'build', 'venv', '__pycache__'])
const workspaceEntryLimit = 12000
const searchFileBytes = 512 * 1024
const searchByteBudget = 64 * 1024 * 1024
const searchResultLimit = 100

async function* walkWorkspace(root: string): AsyncGenerator<WorkspaceFile, boolean> {
  const dirs = [root]
  let visited = 0
  for (let i = 0; i < dirs.length; i += 1) {
    let dir: fs.Dir
    try {
      dir = await fs.promises.opendir(dirs[i])
    } catch (error) {
      if (i === 0) throw error
      continue
    }
    for await (const entry of dir) {
      if (++visited > workspaceEntryLimit) return true
      if (entry.name.startsWith('.') && entry.name !== '.gitignore') continue
      const full = path.join(dirs[i], entry.name)
      if (entry.isDirectory() && !ignoredFolders.has(entry.name.toLowerCase())) dirs.push(full)
      if (entry.isFile()) yield { path: full, relative: path.relative(root, full) }
    }
  }
  return false
}

export async function workspaceFiles(root: string): Promise<WorkspaceResults<WorkspaceFile>> {
  const tree = walkWorkspace(root)
  const items: WorkspaceFile[] = []
  for (let entry = await tree.next(); ; entry = await tree.next()) {
    if (entry.done) return { items, limited: entry.value }
    items.push(entry.value)
  }
}

export async function searchWorkspace(root: string, query: string): Promise<WorkspaceResults<WorkspaceMatch>> {
  const items: WorkspaceMatch[] = []
  const needle = query.trim()
  if (!needle || needle.length > 200) return { items, limited: false }
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'iu')
  const tree = walkWorkspace(root)
  let bytes = 0
  try {
    for (let entry = await tree.next(); ; entry = await tree.next()) {
      if (entry.done) return { items, limited: entry.value }
      const file = entry.value
      const buffer = await readSearchFile(file.path).catch(() => null)
      if (!buffer) continue
      bytes += buffer.length
      if (bytes > searchByteBudget) return { items, limited: true }
      if (buffer.includes(0)) continue
      const lines = decodeBuffer(buffer).text.split(/\r\n|\n|\r/)
      for (let i = 0; i < lines.length; i += 1) {
        const match = pattern.exec(lines[i])
        if (!match) continue
        if (items.length === searchResultLimit) return { items, limited: true }
        const start = Math.max(0, match.index - 45)
        const preview = lines[i].slice(start, start + 180)
        items.push({ ...file, line: i + 1, column: match.index + 1, preview: start ? '…' + preview : preview })
      }
    }
  } finally {
    await tree.return(false)
  }
}

async function readSearchFile(file: string): Promise<Buffer | null> {
  await writes.get(fileKey(file))
  const stat = await fs.promises.lstat(file)
  if (!stat.isFile() || !stat.size || stat.size > searchFileBytes) return null
  const handle = await fs.promises.open(file, 'r')
  try {
    const buffer = Buffer.allocUnsafe(stat.size)
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0)
    return buffer.subarray(0, bytesRead)
  } finally {
    await handle.close()
  }
}

import fs from 'node:fs/promises'
import { realpathSync } from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import type { WorkChoice, WorkPreviewRow } from '../shared/types'

export function previewChoices(rows: WorkPreviewRow[], target?: number | 'conflicts', choice?: WorkChoice) {
  const choices = new Map(rows.map(row => [row.fileId, row.choice]))
  if (target === undefined && choice === undefined) return choices
  if (target === 'conflicts') {
    if (choice !== 'skip' && choice !== 'rename') throw new Error('批量处理仅支持跳过或自动重命名')
    for (const row of rows) if (row.conflict) choices.set(row.fileId, choice)
  } else {
    const row = rows.find(item => item.fileId === target)
    if (!row?.reason || !choice || !['move','rename','skip'].includes(choice) || (choice === 'move' && !row.canKeep))
      throw new Error('此文件的冲突选项无效')
    choices.set(row.fileId, choice)
  }
  return choices
}

const missing = (e: unknown) => (e as NodeJS.ErrnoException).code === 'ENOENT'
async function stat(file: string) {
  try { return await fs.lstat(file, { bigint: true }) } catch (e) { if (!missing(e)) throw e }
}
type Stat = NonNullable<Awaited<ReturnType<typeof stat>>>
const identity = (s: Stat) => [s.dev, s.ino].join(':')
const stamp = (s: Stat) => [identity(s), s.size, s.mtimeNs].join(':')
const same = (a: string, b: string) => path.relative(a, b) === ''
const under = (p: string, dir: string) => {
  const rel = path.relative(dir, p)
  return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel))
}
function leaf(s: unknown): s is string {
  return typeof s === 'string' && s.length > 0 && s.length <= 240 && !/^[. ]|[. ]$|[\\/:*?"<>|\x00-\x1f]/u.test(s)
    && !/^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i.test(s)
}
async function directory(p: string) {
  const s = await stat(p)
  if (!s?.isDirectory() || s.isSymbolicLink() || !same(await fs.realpath(p), p)) throw new Error('目录位置发生变化或包含链接，请重新授权')
  return identity(s)
}
// Old undo journals contain only the basename; new entries retain their source directory.
export interface WorkFile { id: number; name: string; stamp: string; relative?: string; parentId?: string }
export const sourcePath = (file: WorkFile) => file.relative ?? file.name
export function relativeFolder(value: string) {
  if (value === '.') return '.'
  const parts = value.split(/[\\/]/)
  if (!parts.every(leaf)) throw new Error('目录必须是授权范围内的相对路径')
  return parts.join('/')
}
export const destinationPath = (m: Move) => m.folder === '.' ? (m.toName ?? m.file.name) : m.folder + '/' + (m.toName ?? m.file.name)
export interface Inventory { root: string; identity: string; files: WorkFile[] }
export interface Move { file: WorkFile; folder: string; toName?: string }
const pathKey = (p: string) => process.platform === 'win32' ? p.toLowerCase() : p
interface Transaction { rootId: string; moves: Move[]; created: Record<string, string>; state: 'preparing' | 'applied' | 'undoing' }
interface Journal { root: string; transactions: Transaction[] }

export class WorkFiles {
  private protectedDirs: string[]
  constructor(private storage: string, protectedDirs: string[] = []) {
    this.protectedDirs = [storage, ...protectedDirs].flatMap(dir => {
      try { return [dir, realpathSync.native(dir)] } catch { return [dir] }
    })
  }
  private allowed(p: string) {
    if (this.protectedDirs.some(dir => under(p, dir)) || same(p, path.parse(p).root))
      throw new Error('请选择自己的工作目录')
  }
  async authorize(root: string) {
    this.allowed(root)
    return directory(root)
  }
  async select(selected: string) {
    // Windows dialogs and TEMP may return an 8.3 spelling of an ordinary path.
    // Check the selected chain for junctions before expanding that spelling.
    for (let p = path.resolve(selected); p !== path.dirname(p); p = path.dirname(p)) {
      if ((await fs.lstat(p)).isSymbolicLink()) throw new Error('授权目录不能经过链接')
    }
    const root = await fs.realpath(selected)
    return { root, identity: await this.authorize(root) }
  }
  async inventory(root: string, signal?: AbortSignal): Promise<Inventory> {
    this.allowed(root)
    const rootId = await directory(root)
    const files: WorkFile[] = []
    const queue = ['']
    let visited = 0
    for (let i = 0; i < queue.length; i++) {
      signal?.throwIfAborted()
      const rel = queue[i], full = path.join(root, rel)
      const parentId = await directory(full)
      const dir = await fs.opendir(full)
      for await (const entry of dir) {
        signal?.throwIfAborted()
        if (++visited > 40000) throw new Error('目录树超过 40000 项，请选择较小的子目录')
        if (!leaf(entry.name) || entry.isSymbolicLink() || /^(?:desktop\.ini|thumbs\.db)$/i.test(entry.name)) continue
        const relative = rel ? rel + '/' + entry.name : entry.name
        const entryPath = path.join(root, relative)
        if (this.protectedDirs.some(dir => under(entryPath, dir))) continue
        if (entry.isDirectory()) { queue.push(relative); continue }
        if (!entry.isFile()) continue
        const s = await stat(entryPath)
        if (s?.isFile() && s.nlink === 1n) files.push({ id: 0, name: entry.name, relative, parentId, stamp: stamp(s) })
      }
    }
    files.sort((a, b) => sourcePath(a).localeCompare(sourcePath(b), 'zh-CN')).forEach((f, i) => { f.id = i + 1 })
    return { root, identity: rootId, files }
  }
  private journalPath(root: string) {
    const key = process.platform === 'win32' ? root.toLowerCase() : root
    return path.join(this.storage, createHash('sha256').update(key).digest('hex') + '.json')
  }
  private async journal(root: string): Promise<Journal> {
    try {
      const j = JSON.parse(await fs.readFile(this.journalPath(root), 'utf8')) as Journal
      if (!same(j.root, root) || !Array.isArray(j.transactions)) throw new Error('整理记录损坏')
      return j
    } catch (e) { if (!missing(e)) throw e; return { root, transactions: [] } }
  }
  private async save(j: Journal) {
    await fs.mkdir(this.storage, { recursive: true })
    const file = this.journalPath(j.root), tmp = file + '.' + randomUUID()
    const h = await fs.open(tmp, 'wx', 0o600)
    try {
      try { await h.writeFile(JSON.stringify(j)); await h.sync() } finally { await h.close() }
      await fs.rename(tmp, file)
    } finally { await fs.unlink(tmp).catch(() => {}) }
  }
  async canUndo(root: string) { return (await this.journal(root)).transactions.length > 0 }
  private paths(root: string, move: Move) {
    const relative = sourcePath(move.file)
    if (!leaf(move.file.name) || !relative.split('/').every(leaf) || path.posix.basename(relative) !== move.file.name)
      throw new Error('源文件路径无效')
    const name = move.toName ?? move.file.name
    if (!leaf(name)) throw new Error('目标文件名无效')
    const dir = path.join(root, relativeFolder(move.folder)), src = path.join(root, relative), dst = path.join(dir, name)
    this.allowed(root); this.allowed(src); this.allowed(dst)
    return { dir, src, dst }
  }
  // Check each existing prefix. A missing final component can still sit underneath a junction.
  private async folders(root: string, dir: string, onMissing?: (p: string) => Promise<void>) {
    let current = root, id = await directory(root)
    for (const part of path.relative(root, dir).split(path.sep).filter(Boolean)) {
      if (!leaf(part)) throw new Error('目录超出授权范围')
      current = path.join(current, part)
      this.allowed(current)
      if (!await stat(current)) {
        if (!onMissing) return undefined
        await onMissing(current)
      }
      id = await directory(current)
    }
    return id
  }
  private async sourceParent(root: string, src: string, file: WorkFile) {
    const id = await this.folders(root, path.dirname(src))
    if (!id || (file.parentId && file.parentId !== id)) throw new Error('源目录已变化：' + sourcePath(file))
  }
  private async inspect(inv: Inventory, moves: Move[], choices?: Map<number,WorkChoice>) {
    if (await directory(inv.root) !== inv.identity) throw new Error('授权目录已被替换')
    const ids = new Set<number>()
    const entries = []
    const directories = new Set<string>()
    if (moves.length > 120) throw new Error('本次超过 120 个文件，请缩小整理范围')
    if (!moves.length || moves.length > inv.files.length) throw new Error('没有可执行的文件清单')
    for (const m of moves) {
      if (ids.has(m.file.id) || !inv.files.includes(m.file)) throw new Error('文件编号无效或重复')
      ids.add(m.file.id)
      const { dir, src, dst } = this.paths(inv.root, m)
      if (choices?.get(m.file.id) !== 'skip') {
        await this.sourceParent(inv.root, src, m.file)
        const s = await stat(src)
        if (!s?.isFile() || s.nlink !== 1n || stamp(s) !== m.file.stamp) throw new Error('源文件已变化：' + sourcePath(m.file))
      }
      await this.folders(inv.root, dir)
      for (let p = dir; !same(p, inv.root); p = path.dirname(p)) directories.add(pathKey(p))
      entries.push({ move:m, dir, key:pathKey(dst), exists:Boolean(await stat(dst)) })
    }
    return { entries, directories }
  }
  async preflight(inv: Inventory, moves: Move[]) {
    const {entries, directories} = await this.inspect(inv,moves)
    const seen = new Set<string>()
    for (const {move,key,exists} of entries) {
      if (exists || seen.has(key) || directories.has(key)) throw new Error('目标同名冲突：' + destinationPath(move))
      seen.add(key)
    }
  }
  async review(inv: Inventory, proposals: Move[], choices = new Map<number, WorkChoice>()) {
    const {entries, directories} = await this.inspect(inv,proposals,choices)
    const reserved = new Set(directories)
    const counts = new Map<string,number>(), owners = new Map<string,number>()
    for (const {move,key,exists} of entries) {
      counts.set(key,(counts.get(key) ?? 0)+1)
      reserved.add(key)
      const choice = choices.get(move.file.id)
      if (!exists && !directories.has(key) && choice !== 'skip' && choice !== 'rename' && !owners.has(key))
        owners.set(key,move.file.id)
    }
    const rows: WorkPreviewRow[] = [], moves: Move[] = []
    const nextSuffix = new Map<string, number>()
    for (const {move,dir,key,exists} of entries) {
      const fileId = move.file.id, requested = destinationPath(move)
      const reason = exists ? '目标已存在' : directories.has(key) ? '目标与目录同名' : counts.get(key)! > 1 ? '批内同名' : ''
      const canKeep = !exists && !directories.has(key) && (!owners.has(key) || owners.get(key) === fileId)
      let choice = choices.get(fileId) ?? 'move'
      if (choice === 'move' && !canKeep) choice = 'skip'
      let resolved = move
      if (choice === 'rename') {
        const name = move.file.name
        const ext = /\.tar\.(?:gz|bz2|xz|zst)$/i.exec(name)?.[0] ?? path.extname(name)
        const stem = name.slice(0,name.length-ext.length)
        for (let n = nextSuffix.get(key) ?? 1; ; n++) {
          const suffix = ' ('+n+')'+ext
          if (suffix.length >= 240) throw new Error('扩展名过长，无法自动重命名：'+name)
          const toName = stem.slice(0,240-suffix.length).replace(/[\uD800-\uDBFF]$/,'') + suffix
          const full = path.join(dir,toName)
          if (reserved.has(pathKey(full)) || await stat(full)) continue
          reserved.add(pathKey(full))
          nextSuffix.set(key,n+1)
          resolved = { ...move, toName }
          break
        }
      }
      if (choice !== 'skip') moves.push(resolved)
      rows.push({fileId, from:sourcePath(move.file), requested, to:choice === 'skip' ? null : destinationPath(resolved),
        choice, conflict:Boolean(reason), reason:reason || (choice !== 'move' ? '原名现可用' : ''), canKeep})
    }
    return {rows,moves}
  }
  async apply(inv: Inventory, moves: Move[]) {
    await this.preflight(inv, moves)
    const j = await this.journal(inv.root)
    if (j.transactions.at(-1) && j.transactions.at(-1)?.state !== 'applied') throw new Error('请先撤销上次中断的整理')
    const t: Transaction = { rootId: inv.identity, moves, created: {}, state: 'preparing' }
    j.transactions.push(t)
    await this.save(j)
    try {
      for (const m of moves) {
        const { dir, src, dst } = this.paths(inv.root, m)
        await this.preflight(inv, [m])
        await this.folders(inv.root, dir, async p => {
          await fs.mkdir(p)
          const relative = path.relative(inv.root, p).split(path.sep).join('/')
          Object.defineProperty(t.created, relative, { value: await directory(p), enumerable: true, configurable: true })
          await this.save(j)
        })
        await directory(dir)
        // Exclusive link creation cannot overwrite a competing destination. The journal
        // also handles an interruption between link and unlink on the same volume.
        await fs.link(src, dst)
        await this.unlinkTwin(src, dst, m.file.stamp)
      }
      t.state = 'applied'
      await this.save(j)
    } catch (cause) {
      try { await this.restore(j, t) } catch { throw new Error('整理中断，请检查冲突并点击撤销。' + String(cause)) }
      throw cause
    }
  }
  private async unlinkTwin(remove: string, keep: string, expected: string) {
    const [a, b] = await Promise.all([stat(remove), stat(keep)])
    if (!a?.isFile() || !b?.isFile() || a.nlink !== 2n || b.nlink !== 2n || stamp(a) !== expected || stamp(b) !== expected)
      throw new Error('文件位置或内容已变化')
    await fs.unlink(remove)
  }
  private async restore(j: Journal, t: Transaction) {
    for (const m of [...t.moves].reverse()) {
      if (await directory(j.root) !== t.rootId) throw new Error('授权目录已被替换')
      const { dir, src, dst } = this.paths(j.root, m)
      await this.sourceParent(j.root, src, m.file)
      const a = await stat(src)
      if (a?.isFile() && a.nlink === 1n && stamp(a) === m.file.stamp) continue
      if (!await this.folders(j.root, dir)) throw new Error('目标目录已变化')
      const b = await stat(dst)
      if (!b?.isFile() || stamp(b) !== m.file.stamp) throw new Error('整理后的文件已变化：' + m.file.name)
      if (!a) {
        if (b.nlink !== 1n) throw new Error('文件链接已变化')
        await fs.link(dst, src)
      }
      await this.unlinkTwin(dst, src, m.file.stamp)
    }
    for (const [name, id] of Object.entries(t.created).sort(([a], [b]) => b.split('/').length - a.split('/').length)) {
      if (!name.split('/').every(leaf)) throw new Error('整理记录中的目录无效')
      const dir = path.join(j.root, name)
      this.allowed(dir)
      if (!await this.folders(j.root, path.dirname(dir))) continue
      const s = await stat(dir)
      if (s?.isDirectory() && !s.isSymbolicLink() && identity(s) === id) {
        try { await fs.rmdir(dir) } catch (e) { if (!['ENOTEMPTY', 'EEXIST', 'ENOENT'].includes((e as NodeJS.ErrnoException).code ?? '')) throw e }
      }
    }
    j.transactions.pop()
    await this.save(j)
  }
  async undo(root: string, openFiles: string[] = []) {
    this.allowed(root)
    const j = await this.journal(root), t = j.transactions.at(-1)
    if (!t) throw new Error('没有可撤销的整理')
    this.checkOpen(root, t.moves, openFiles)
    // Reject known conflicts before moving the first file during normal undo.
    if (t.state === 'applied') {
      if (await directory(root) !== t.rootId) throw new Error('授权目录已被替换')
      for (const m of t.moves) {
        const { dir, src, dst } = this.paths(root, m)
        await this.sourceParent(root, src, m.file)
        if (!await this.folders(root, dir)) throw new Error('目标目录已变化')
        const s = await stat(dst)
        if (await stat(src) || !s?.isFile() || s.nlink !== 1n || stamp(s) !== m.file.stamp) throw new Error('撤销冲突：' + m.file.name)
      }
      t.state = 'undoing'
      await this.save(j)
    }
    await this.restore(j, t)
    return j.transactions.length > 0
  }
  checkOpen(root: string, moves: Move[], openFiles: string[]) {
    for (const m of moves) {
      const { src, dst } = this.paths(root, m)
      if (openFiles.some(file => same(file, src) || same(file, dst))) throw new Error('请先关闭正在编辑的文件：' + m.file.name)
    }
  }
}

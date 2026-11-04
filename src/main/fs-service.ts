import fs from 'node:fs'
import path from 'node:path'
import type { FsEntry, ReadResult } from '../shared/types'

// —— 文件树与文件读写（含 GBK 兜底解码） ——

export function listDir(dir: string): FsEntry[] {
  const names = fs.readdirSync(dir)
  const out: FsEntry[] = []
  for (const name of names) {
    if (name.startsWith('.') && name !== '.gitignore') continue
    const full = path.join(dir, name)
    try {
      const st = fs.statSync(full)
      out.push({ name, path: full, isDir: st.isDirectory(), size: st.isFile() ? st.size : 0 })
    } catch {
      /* 无权限或已消失的条目直接跳过 */
    }
  }
  out.sort((a, b) => (a.isDir === b.isDir ? a.name.localeCompare(b.name, 'zh-Hans-CN') : a.isDir ? -1 : 1))
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

export function readFileSmart(p: string): ReadResult {
  const buf = fs.readFileSync(p)
  if (buf.length === 0) return { text: '', encoding: 'utf-8' }
  return decodeBuffer(buf)
}

export function writeFileSmart(p: string, text: string): void {
  // v1 统一写 UTF-8：运行控制台也按 UTF-8 解码，与 GCC 默认 exec-charset 一致。
  // 原文件是 GBK 时首编译后即完成迁移；中文内容在 UTF-8 下无损。
  void p
  fs.writeFileSync(p, text, 'utf-8')
}

export function createEntry(parent: string, name: string, isDir: boolean): string {
  const full = path.join(parent, name)
  if (fs.existsSync(full)) throw new Error('已存在同名文件或文件夹：' + name)
  if (isDir) fs.mkdirSync(full)
  else fs.writeFileSync(full, '', 'utf-8')
  return full
}

export function renameEntry(oldPath: string, newName: string): string {
  const parent = path.dirname(oldPath)
  const full = path.join(parent, newName)
  if (fs.existsSync(full)) throw new Error('目标名称已存在：' + newName)
  fs.renameSync(oldPath, full)
  return full
}

export function deleteEntry(target: string): void {
  const st = fs.statSync(target)
  if (st.isDirectory()) fs.rmSync(target, { recursive: true, force: true })
  else fs.unlinkSync(target)
}

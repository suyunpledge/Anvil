import { bus, state, $, el } from './core'
import { openFile } from './editor'
import type { FsEntry } from '../../shared/types'

// —— 文件树：懒加载目录，双击/单击打开文件 ——

const expanded = new Set<string>()
let activeFile: string | null = null

function iconFor(e: FsEntry): string {
  if (e.isDir) return '📁'
  const ext = e.name.split('.').pop()?.toLowerCase() ?? ''
  if (['c', 'cpp', 'cc', 'cxx', 'h', 'hpp'].includes(ext)) return '📘'
  if (ext === 'py') return '🐍'
  if (['js', 'ts', 'mjs'].includes(ext)) return '🟨'
  if (['md', 'txt'].includes(ext)) return '📄'
  if (['json', 'toml', 'ini'].includes(ext)) return '⚙️'
  return '📄'
}

async function loadChildren(dir: string, container: HTMLElement): Promise<void> {
  container.innerHTML = ''
  let entries: FsEntry[] = []
  try {
    entries = await window.ide.listDir(dir)
  } catch (err) {
    container.append(el('div', 'hint', '读取失败：' + String(err)))
    return
  }
  for (const e of entries) {
    const row = el('div', 'tree-item')
    row.dataset.path = e.path
    row.append(el('span', 'ico', iconFor(e)))
    const name = el('span', '', e.name)
    name.style.overflow = 'hidden'
    name.style.textOverflow = 'ellipsis'
    row.append(name)
    row.addEventListener('click', async () => {
      if (e.isDir) {
        if (expanded.has(e.path)) {
          expanded.delete(e.path)
          row.nextElementSibling?.remove()
          row.querySelector('.ico')!.textContent = '📁'
        } else {
          expanded.add(e.path)
          row.querySelector('.ico')!.textContent = '📂'
          const kids = el('div', 'kids')
          row.after(kids)
          await loadChildren(e.path, kids)
        }
      } else {
        await openFile(e.path)
      }
      refreshActive()
    })
    // 文件夹默认展开第一层
    if (e.isDir && expanded.has(e.path)) {
      row.querySelector('.ico')!.textContent = '📂'
      const kids = el('div', 'kids')
      row.after(kids)
      void loadChildren(e.path, kids)
    }
    container.append(row)
  }
}

export async function openFolder(folder: string | null): Promise<void> {
  if (!folder) return
  state.folder = folder
  $('title-folder').textContent = folder
  expanded.clear()
  // 打开新文件夹时只展开根目录第一层
  try {
    const entries = await window.ide.listDir(folder)
    for (const e of entries) if (e.isDir) expanded.add(e.path)
  } catch {
    /* 根目录读取失败也能展示错误 */
  }
  await loadChildren(folder, $('file-tree'))
}

export function initExplorer(): void {
  bus.on('tree:active', (p) => {
    activeFile = typeof p === 'string' ? p : null
    refreshActive()
  })
}

function refreshActive(): void {
  document.querySelectorAll('.tree-item').forEach((n) => {
    const p = (n as HTMLElement).dataset.path
    n.classList.toggle('active', p === activeFile)
  })
}

import { bus, state, $, el } from './core'
import { openFile } from './editor'
import type { FsEntry } from '../../shared/types'

// —— 文件树：懒加载目录，双击/单击打开文件 ——

const expanded = new Set<string>()
const pending = new WeakMap<HTMLElement, symbol>()
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
  const request = Symbol()
  pending.set(container, request)
  let entries: FsEntry[] = []
  try {
    entries = await window.ide.listDir(dir)
  } catch (err) {
    if (pending.get(container) === request) container.replaceChildren(el('div', 'hint', '读取失败：' + String(err)))
    return
  }
  if (pending.get(container) !== request) return
  container.replaceChildren()
  for (const e of entries) {
    const row = el('div', 'tree-item')
    row.dataset.path = e.path
    row.classList.toggle('active', !e.isDir && e.path === activeFile)
    row.append(el('span', 'ico', iconFor(e)))
    const name = el('span', '', e.name)
    name.style.overflow = 'hidden'
    name.style.textOverflow = 'ellipsis'
    row.append(name)
    container.append(row)
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
  }
}

export async function openFolder(folder: string | null): Promise<void> {
  if (!folder) return
  if (state.folder !== folder) {
    expanded.clear()
    $('file-tree').replaceChildren()
  }
  state.folder = folder
  $('title-folder').textContent = folder
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

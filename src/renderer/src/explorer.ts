import { bus, state, $, el } from './core'
import { openFile } from './editor'
import { icon, fileBadge } from './icons'
import type { FsEntry } from '../../shared/types'

// —— 文件树：懒加载目录，双击/单击打开文件 ——

const expanded = new Set<string>()
const pending = new WeakMap<HTMLElement, symbol>()
let activeFile: string | null = null

// 文件夹展开 / 收起：箭头旋转 + 图标换成打开的文件夹
function setOpen(row: HTMLElement, e: FsEntry, open: boolean): void {
  row.classList.toggle('open', open)
  row.querySelector('.ico')!.innerHTML = fileBadge(e.name, e.isDir, open)
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
    const twisty = el('span', 'tw')
    if (e.isDir) twisty.innerHTML = icon('chevronRight')
    const ico = el('span', 'ico')
    ico.innerHTML = fileBadge(e.name, e.isDir)
    row.append(twisty, ico)
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
          setOpen(row, e, false)
        } else {
          expanded.add(e.path)
          setOpen(row, e, true)
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
      setOpen(row, e, true)
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

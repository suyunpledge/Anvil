import { bus, state, $, el, showFeedback } from './core'
import { openFile, tabsUnder, closeTabs, reopenMoved, isUnder, isDirty, copyText, relativePath } from './editor'
import { icon, fileBadge } from './icons'
import { showMenu, type MenuItem } from './menu'
import { askText, badName } from './dialogs'
import type { FsEntry } from '../../shared/types'

// —— 资源管理器：懒加载文件树 + 右键菜单（新建 / 重命名 / 删除 / 复制路径）——

const expanded = new Set<string>()
const pending = new WeakMap<HTMLElement, symbol>()
let activeFile: string | null = null
let selected: FsEntry | null = null

// 文件夹展开 / 收起：箭头旋转 + 图标换成打开的文件夹
function setOpen(row: HTMLElement, e: FsEntry, open: boolean): void {
  row.classList.toggle('open', open)
  row.querySelector('.ico')!.innerHTML = fileBadge(e.name, e.isDir, open)
}

function select(row: HTMLElement | null, e: FsEntry | null): void {
  selected = e
  document.querySelectorAll('.tree-item.selected').forEach((n) => n.classList.remove('selected'))
  row?.classList.add('selected')
}

async function toggleDir(row: HTMLElement, e: FsEntry): Promise<void> {
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
  if (!entries.length && container.id === 'file-tree') {
    container.append(el('div', 'hint', '这个文件夹是空的。点上方的「新建文件」按钮创建第一个文件。'))
  }
  for (const e of entries) {
    const row = el('div', 'tree-item')
    row.dataset.path = e.path
    row.tabIndex = -1
    row.classList.toggle('active', !e.isDir && e.path === activeFile)
    row.classList.toggle('selected', e.path === selected?.path)
    const twisty = el('span', 'tw')
    if (e.isDir) twisty.innerHTML = icon('chevronRight')
    const ico = el('span', 'ico')
    ico.innerHTML = fileBadge(e.name, e.isDir)
    row.append(twisty, ico)
    const name = el('span', 'tree-name', e.name)
    row.append(name)
    row.title = e.path
    container.append(row)
    row.addEventListener('click', async () => {
      select(row, e)
      if (e.isDir) await toggleDir(row, e)
      else await openFile(e.path)
      refreshActive()
    })
    row.addEventListener('contextmenu', (ev) => {
      ev.preventDefault()
      ev.stopPropagation()
      select(row, e)
      showMenu(itemMenu(e), { x: ev.clientX, y: ev.clientY }, { onError: report })
    })
    if (e.isDir && expanded.has(e.path)) {
      setOpen(row, e, true)
      const kids = el('div', 'kids')
      row.after(kids)
      void loadChildren(e.path, kids)
    }
  }
}

const report = (error: unknown): void => showFeedback(error instanceof Error ? error.message : String(error))
const parentOf = (p: string): string => p.replace(/[\\/][^\\/]+[\\/]?$/, '')

/** 新建文件 / 文件夹时的目标目录：选中的是文件夹就放里面，是文件就放同级 */
function targetDir(): string | null {
  if (!state.folder) return null
  if (!selected || !isUnder(selected.path, state.folder)) return state.folder
  return selected.isDir ? selected.path : parentOf(selected.path)
}

export async function newEntry(isDir: boolean, dir = targetDir()): Promise<void> {
  if (!dir) {
    showFeedback('请先打开一个文件夹')
    return
  }
  const name = await askText({
    title: isDir ? '新建文件夹' : '新建文件',
    icon: isDir ? 'folderPlus' : 'filePlus',
    detail: dir,
    label: isDir ? '文件夹名' : '文件名',
    placeholder: isDir ? '例如 homework' : '例如 hello.c 或 main.py',
    ok: '创建',
    validate: badName
  })
  if (!name) return
  const full = await window.ide.createEntry(dir, name, isDir).catch((error: unknown) => {
    throw new Error(String(error).includes('EEXIST') ? `「${name}」已经存在` : '创建失败：' + String(error))
  })
  if (dir !== state.folder) expanded.add(dir)
  await refreshTree()
  if (!isDir) await openFile(full)
  showFeedback((isDir ? '已创建文件夹 ' : '已创建 ') + name)
}

async function renameEntry(e: FsEntry): Promise<void> {
  if (tabsUnder(e.path).some(isDirty)) {
    showFeedback(`「${e.name}」里有未保存的修改，请先保存（Ctrl+S）再重命名`)
    return
  }
  const name = await askText({
    title: e.isDir ? '重命名文件夹' : '重命名文件',
    detail: e.path,
    label: '新名称',
    value: e.name,
    selectStem: !e.isDir,
    ok: '重命名',
    validate: (v) => v === e.name ? '名称没有变化' : badName(v)
  })
  if (!name) return
  const full = await window.ide.renameEntry(e.path, name)
  // 展开状态跟着改名
  for (const p of [...expanded]) {
    if (isUnder(p, e.path)) {
      expanded.delete(p)
      expanded.add(full + p.slice(e.path.length))
    }
  }
  if (selected?.path === e.path) selected = { ...e, name, path: full }
  await refreshTree()
  await reopenMoved(e.path, full)
  showFeedback(`已重命名为 ${name}`)
}

async function deleteEntry(e: FsEntry): Promise<void> {
  const what = e.isDir ? `文件夹「${e.name}」及其中的全部内容` : `「${e.name}」`
  if (!confirm(`确定删除${what}吗？\n\n会移到系统回收站，需要时可以从回收站还原。`)) return
  const open = tabsUnder(e.path)
  if (open.length && !closeTabs(open)) return
  await window.ide.deleteEntry(e.path)
  for (const p of [...expanded]) if (isUnder(p, e.path)) expanded.delete(p)
  if (selected && isUnder(selected.path, e.path)) selected = null
  await refreshTree()
  showFeedback(`已删除 ${e.name}（在回收站里）`)
}

function itemMenu(e: FsEntry): MenuItem[] {
  const dir = e.isDir ? e.path : parentOf(e.path)
  return [
    ...(e.isDir ? [] : [
      { label: '打开', icon: 'file', run: () => openFile(e.path) },
      { label: '打开并运行', icon: 'play', shortcut: 'F5', run: async () => { await openFile(e.path); $('btn-run').click() } },
      '-' as const
    ]),
    { label: '新建文件…', icon: 'filePlus', run: () => newEntry(false, dir) },
    { label: '新建文件夹…', icon: 'folderPlus', run: () => newEntry(true, dir) },
    '-',
    { label: '重命名…', icon: 'edit', shortcut: 'F2', run: () => renameEntry(e) },
    { label: '删除', icon: 'trash', shortcut: 'Delete', danger: true, run: () => deleteEntry(e) },
    '-',
    { label: '复制路径', icon: 'copy', run: () => copyText(e.path) },
    { label: '复制相对路径', run: () => copyText(relativePath(e.path)) },
    ...(e.isDir ? [{ label: '在项目中搜索', icon: 'search', run: () => bus.emit('pane:show', 'search') }] : [])
  ]
}

function rootMenu(): MenuItem[] {
  return [
    { label: '新建文件…', icon: 'filePlus', enabled: () => Boolean(state.folder), run: () => newEntry(false, state.folder) },
    { label: '新建文件夹…', icon: 'folderPlus', enabled: () => Boolean(state.folder), run: () => newEntry(true, state.folder) },
    '-',
    { label: '刷新', icon: 'refresh', enabled: () => Boolean(state.folder), run: () => refreshTree() },
    { label: '全部折叠', icon: 'collapse', enabled: () => expanded.size > 0, run: () => collapseAll() },
    '-',
    { label: '复制文件夹路径', icon: 'copy', enabled: () => Boolean(state.folder), run: () => copyText(state.folder ?? '') }
  ]
}

export async function refreshTree(): Promise<void> {
  if (state.folder) await loadChildren(state.folder, $('file-tree'))
  refreshActive()
}

export async function collapseAll(): Promise<void> {
  expanded.clear()
  await refreshTree()
}

export async function openFolder(folder: string | null): Promise<void> {
  if (!folder) return
  if (state.folder !== folder) {
    expanded.clear()
    selected = null
    $('file-tree').replaceChildren()
  }
  state.folder = folder
  $('title-folder').textContent = folder
  const name = folder.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? folder
  $('explorer-root').textContent = name
  $('explorer-root').title = folder
  document.getElementById('pane-files')?.classList.add('has-folder')
  await loadChildren(folder, $('file-tree'))
  bus.emit('folder:opened', folder)
}

export function initExplorer(): void {
  bus.on('tree:active', (p) => {
    activeFile = typeof p === 'string' ? p : null
    refreshActive()
  })
  $('ex-new-file').addEventListener('click', () => void newEntry(false).catch(report))
  $('ex-new-folder').addEventListener('click', () => void newEntry(true).catch(report))
  $('ex-refresh').addEventListener('click', () => void refreshTree().catch(report))
  $('ex-collapse').addEventListener('click', () => void collapseAll().catch(report))
  const tree = $('file-tree')
  tree.addEventListener('contextmenu', (ev) => {
    if ((ev.target as HTMLElement).closest('.tree-item')) return
    ev.preventDefault()
    select(null, null)
    showMenu(rootMenu(), { x: ev.clientX, y: ev.clientY }, { onError: report })
  })
  // 点空白处取消选中（新建会放到根目录）
  tree.addEventListener('click', (ev) => { if (!(ev.target as HTMLElement).closest('.tree-item')) select(null, null) })
  // 键盘：F2 重命名，Delete 删除（焦点在侧栏里时）
  $('pane-files').addEventListener('keydown', (ev) => {
    if (!selected || (ev.target as HTMLElement).closest('input, textarea')) return
    if (ev.key === 'F2') { ev.preventDefault(); void renameEntry(selected).catch(report) }
    else if (ev.key === 'Delete') { ev.preventDefault(); void deleteEntry(selected).catch(report) }
  })
}

function refreshActive(): void {
  document.querySelectorAll('.tree-item').forEach((n) => {
    const p = (n as HTMLElement).dataset.path
    n.classList.toggle('active', p === activeFile)
  })
}

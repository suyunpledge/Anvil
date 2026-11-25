import fs from 'node:fs/promises'
import { generate, models, type Message } from './local-chat'
import { app, dialog, ipcMain, type BrowserWindow } from 'electron'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { EditHistory, snapshot } from './chat-files'
import { WorkFiles, previewChoices, type Inventory, type Move } from './work-files'
import type { WorkChoice, WorkPreviewRow } from '../shared/types'
import { planWork } from './work-planner'

type Patch = { id: string; file: string; before: string; after: string; line: number; old: string; replacement: string }
export function registerChat(window: () => BrowserWindow) {
  const history = new EditHistory(path.join(app.getPath('userData'), 'chat-undo'))
  const work = new WorkFiles(path.join(app.getPath('userData'), 'work-undo'),
    [app.getPath('userData'), app.getAppPath(), path.dirname(app.getPath('exe')), process.env.SystemRoot, process.env.ProgramFiles,
      process.env['ProgramFiles(x86)']].filter((p): p is string => Boolean(p)))
  let workRoot = '', rootId = ''
  type Plan = { id: string; inv: Inventory; proposals: Move[]; moves: Move[]; rows: WorkPreviewRow[] }
  let plan: Plan | undefined
  const view = (p: Plan) => ({id:p.id, rows:p.rows})
  async function revise(current: Plan, target?: number | 'conflicts', choice?: WorkChoice) {
    const choices = previewChoices(current.rows, target, choice)
    const next = await work.review(current.inv,current.proposals,choices)
    if (plan !== current) throw new Error('整理提议已过期')
    if (JSON.stringify(next.rows) !== JSON.stringify(current.rows))
      plan = { ...current, ...next, id:randomUUID() }
    return plan!
  }
  let file = ''
  let transcript: Message[] = []
  let draft: Patch | undefined
  let controller: AbortController | undefined
  let busy = false
  let generation = 0
  function reset(): void {
    generation++
    controller?.abort()
    file = ''
    workRoot = rootId = ''
    plan = undefined
    draft = undefined
    transcript = []
  }
  async function exclusive<T>(fn: () => Promise<T>): Promise<T> {
    if (busy) throw new Error('请等待当前操作完成')
    busy = true
    try { return await fn() } finally { busy = false }
  }
  ipcMain.handle('chat:models', models)
  ipcMain.handle('work:select', () => exclusive(async () => {
    const selecting = generation
    const choice = await dialog.showOpenDialog(window(), { title: '授权 Work 查找与整理此目录及其子目录', properties: ['openDirectory'] })
    if (choice.canceled) return null
    const { root, identity } = await work.select(choice.filePaths[0])
    const undo = await work.canUndo(root)
    if (selecting !== generation) return null
    reset()
    workRoot = root; rootId = identity
    return { file: root, undo }
  }))
  ipcMain.handle('work:send', (_e, model: string, prompt: string) => exclusive(async () => {
    if (!workRoot || plan || typeof prompt !== 'string') throw new Error('请先授权目录并处理已有提议')
    controller = new AbortController()
    const timeout = setTimeout(() => controller?.abort(), 150000)
    try {
      const inv = await work.inventory(workRoot, controller.signal)
      if (inv.identity !== rootId) throw new Error('目录已被替换，请重新授权')
      const result = await planWork(model, prompt, inv, controller.signal, transcript)
      const review = result.moves.length ? await work.review(inv,result.moves) : null
      controller.signal.throwIfAborted()
      plan = review ? { id: randomUUID(), inv, proposals:result.moves, ...review } : undefined
      transcript = [...transcript, { role: 'user' as const, content: prompt }, { role: 'assistant' as const, content: result.answer.slice(0, 1000) }].slice(-4)
      return { answer: result.answer, preview:plan ? view(plan) : null }
    } finally { clearTimeout(timeout); controller = undefined }
  }))
  ipcMain.handle('work:choice', (_e, id: string, target: number | 'conflicts', choice: WorkChoice) => exclusive(async () => {
    if (!plan || id !== plan.id) throw new Error('整理提议已过期')
    return view(await revise(plan,target,choice))
  }))
  ipcMain.handle('work:apply', (_e, id: string, open: string[]) => exclusive(async () => {
    if (!plan || id !== plan.id || plan.inv.root !== workRoot) throw new Error('整理提议已过期')
    const current = await revise(plan)
    if (current.id !== id) return {preview:view(current), moved:0, skipped:0}
    const moved = current.moves.length, skipped = current.rows.length - moved
    if (moved) {
      work.checkOpen(workRoot, current.moves, open)
      await work.apply(current.inv, current.moves)
    }
    transcript.push({ role: 'user', content: '工具结果：已移动 '+moved+' 个，跳过 '+skipped+' 个。' })
    plan = undefined
    return {preview:null, moved, skipped}
  }))
  ipcMain.handle('work:undo', (_e, open: string[]) => exclusive(async () => {
    if (!workRoot || plan || rootId !== await work.authorize(workRoot)) throw new Error('请先授权目录并处理当前提议')
    const undo = await work.undo(workRoot, open)
    transcript.push({ role: 'user', content: '工具结果：上次整理已撤销。' })
    return undo
  }))
  ipcMain.handle('work:canUndo', () => exclusive(async () => Boolean(workRoot) && work.canUndo(workRoot)))
  ipcMain.handle('chat:select', (_e, suggested?: string) => exclusive(async () => {
    const selecting = generation
    const choice = await dialog.showOpenDialog(window(), { title: '授权 AI 读取并提议修改这个文件', defaultPath: typeof suggested === 'string' && path.isAbsolute(suggested) ? suggested : undefined, properties: ['openFile'] })
    if (choice.canceled) return null
    const target = choice.filePaths[0]
    await snapshot(target)
    const canonical = await fs.realpath(target)
    const profileRelative = path.relative(await fs.realpath(app.getPath('userData')), canonical)
    if (!profileRelative.startsWith('..' + path.sep) && profileRelative !== '..' && !path.isAbsolute(profileRelative))
      throw new Error('IDE 的内部数据文件不能授权给 AI')
    const undo = await history.canUndo(canonical)
    if (selecting !== generation) return null
    reset()
    file = canonical
    transcript = []
    draft = undefined
    return { file, undo }
  }))
  ipcMain.handle('chat:release', () => exclusive(async () => reset()))
  ipcMain.handle('chat:cancel', () => controller?.abort())
  ipcMain.handle('chat:discard', () => exclusive(async () => { draft = undefined; plan = undefined }))
  ipcMain.handle('chat:send', (_e, model: string, prompt: string) => exclusive(async () => {
    if (workRoot || draft) throw new Error('请先退出 Work 或处理上一次修改提议')
    if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 8000) throw new Error('请输入 1 至 8000 个字符')
    controller = new AbortController()
    const timeout = setTimeout(() => controller?.abort(), 150000)
    try {
      const before = file ? await snapshot(file) : ''
      const result = await generate(model, prompt, file, before, transcript, controller.signal)
      controller.signal.throwIfAborted()
      if (result.needsFile) return { needsFile: true, answer: result.answer, tools: [], patch: null }
      draft = result.draft ? { ...result.draft, id: randomUUID(), file, before } : undefined
      transcript = [...transcript, { role: 'user' as const, content: prompt }, { role: 'assistant' as const, content: result.answer }].slice(-8)
      return { needsFile: false, answer: result.answer, tools: result.tools, patch: draft ? { id: draft.id, old: draft.old, replacement: draft.replacement, line: draft.line } : null }
    } finally { clearTimeout(timeout); controller = undefined }
  }))
  ipcMain.handle('chat:apply', (_e, id: string) => exclusive(async () => {
    if (!draft || draft.id !== id || draft.file !== file) throw new Error('提议已过期')
    const text = await history.change(file, draft.before, draft.after)
    draft = undefined
    transcript.push({ role: 'user', content: '工具结果：用户已应用修改。' })
    return { text, undo: true }
  }))
  ipcMain.handle('chat:undo', () => exclusive(async () => {
    if (!file || draft) throw new Error('请先选择目标文件并处理当前提议')
    const text = await history.change(file, await snapshot(file))
    transcript.push({ role: 'user', content: '工具结果：用户已撤销上次修改。' })
    return { text, undo: await history.canUndo(file) }
  }))
  return reset
}

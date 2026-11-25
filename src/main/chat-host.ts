import fs from 'node:fs/promises'
import { generate, models, type Message } from './local-chat'
import { app, dialog, ipcMain, type BrowserWindow } from 'electron'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { EditHistory, snapshot } from './chat-files'

type Patch = { id: string; file: string; before: string; after: string; old: string; replacement: string }
export function registerChat(window: () => BrowserWindow) {
  const history = new EditHistory(path.join(app.getPath('userData'), 'chat-undo'))
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
    draft = undefined
    transcript = []
  }
  async function exclusive<T>(fn: () => Promise<T>): Promise<T> {
    if (busy) throw new Error('请等待当前操作完成')
    busy = true
    try { return await fn() } finally { busy = false }
  }
  ipcMain.handle('chat:models', models)
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
    file = canonical
    transcript = []
    draft = undefined
    return { file, undo }
  }))
  ipcMain.handle('chat:release', () => exclusive(async () => reset()))
  ipcMain.handle('chat:cancel', () => controller?.abort())
  ipcMain.handle('chat:discard', () => exclusive(async () => { draft = undefined }))
  ipcMain.handle('chat:send', (_e, model: string, prompt: string) => exclusive(async () => {
    if (draft) throw new Error('请先处理上一次修改提议')
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
      return { needsFile: false, answer: result.answer, tools: result.tools, patch: draft ? { id: draft.id, old: draft.old, replacement: draft.replacement } : null }
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

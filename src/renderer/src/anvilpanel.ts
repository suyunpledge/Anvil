import { $, el, state } from './core'
import { openFolder } from './explorer'
import { activeTab, openEditorFiles, updateFromChat } from './editor'
import type { WorkChoice, WorkPreview } from '../../shared/types'

export function initAnvilPanel(): void {
  const pane = $('pane-anvil')
  pane.innerHTML = '<div class="chat-layout"><div class="chat-config"><label class="chat-mode-label">模式<select id="chat-mode"><option value="code">代码</option><option value="work">Work</option></select></label><label>本地模型<select id="chat-model"></select></label><button class="mini-btn" id="chat-refresh">刷新模型</button></div><div class="chat-target"><button class="mini-btn" id="chat-select">选择授权文件</button><span id="chat-file">仅聊天</span><button class="mini-btn" id="chat-release" hidden>解除授权</button></div><div id="chat-log" role="log" aria-live="polite"></div><section id="chat-preview" hidden><strong>待确认修改</strong><p id="chat-preview-file"></p><div id="work-moves" hidden></div><label id="chat-old-label">原文</label><pre id="chat-old"></pre><label id="chat-new-label">替换为</label><pre id="chat-new"></pre><button class="tb-btn" id="chat-apply">应用修改</button> <button class="tb-btn" id="chat-discard">放弃</button></section><form id="chat-form"><textarea id="chat-input" aria-label="聊天消息" placeholder="询问代码，或描述要修改的内容" maxlength="8000"></textarea><div class="chat-actions"><button class="tb-btn" id="chat-send">发送</button><button class="tb-btn" type="button" id="chat-cancel" hidden>取消生成</button><button class="mini-btn" type="button" id="chat-undo">撤销上次修改</button></div></form><p id="chat-status" role="status"></p></div>'
  const button = (id: string) => $(id) as HTMLButtonElement
  const select = $('chat-model') as HTMLSelectElement
  const input = $('chat-input') as HTMLTextAreaElement
  let file = '', patch = '', undo = false, busy = false, generating = false
  const mode = $('chat-mode') as HTMLSelectElement
  let work = false
  let moveCount = 0
  function controls(): void {
    mode.disabled = busy || Boolean(patch)
    button('chat-select').disabled = busy || Boolean(patch)
    button('chat-select').textContent = work ? '授权目录' : '选择授权文件'
    input.placeholder = work ? '例如：把采购目录及子目录里的 PDF 移到「归档/采购」' : '询问代码，或描述要修改的内容'
    input.maxLength = work ? 4000 : 8000
    for (const id of ['chat-old','chat-new','chat-old-label','chat-new-label']) $(id).hidden = work
    $('work-moves').hidden = !work
    button('chat-apply').textContent = work ? (moveCount ? '确认移动（'+moveCount+'）' : '确认跳过') : '应用修改'
    for (const item of $('work-moves').querySelectorAll('select')) item.disabled = busy
    button('chat-undo').textContent = work ? '撤销上次整理' : '撤销上次修改'
    button('chat-release').hidden = !file
    button('chat-release').disabled = busy
    select.disabled = busy
    for (const id of ['chat-refresh','chat-apply','chat-discard']) button(id).disabled = busy
    button('chat-send').disabled = busy || !select.value || Boolean(patch)
    button('chat-undo').disabled = busy || !undo || Boolean(patch)
    button('chat-cancel').hidden = !generating
    $('chat-preview').hidden = !patch
  }
  function say(label: string, content: string): void {
    const box = $('chat-log')
    const item = el('article', 'chat-message')
    item.append(el('strong', '', label), el('p', '', content))
    box.append(item)
    if (box.childElementCount > 60) box.firstElementChild?.remove()
    box.scrollTop = box.scrollHeight
  }
  async function run(status: string, fn: () => Promise<void>): Promise<void> {
    if (busy) return
    busy = true
    controls()
    $('chat-status').textContent = status
    try { await fn(); $('chat-status').textContent = '' }
    catch (e) { $('chat-status').textContent = String(e).replace(/^Error: Error invoking remote method '[^']+': (?:Error: )?/, '') }
    finally { busy = generating = false; controls() }
  }
  async function refresh(): Promise<void> {
    const previous = select.value
    const names = await window.ide.chatModels()
    select.replaceChildren(...names.map(name => new Option(name, name)))
    if (names.includes(previous)) select.value = previous
    $('st-anvil').textContent = names.length ? 'AI：本地模型就绪' : 'AI：未安装聊天模型'
    $('anvil-dot').textContent = '● 本地 AI'
    $('anvil-dot').className = 'anvil-dot ' + (names.length ? 'on' : 'off')
    if (!names.length) throw new Error('请在 Ollama 中安装本地聊天模型后刷新')
  }
  function showWorkPreview(preview: WorkPreview | null) {
    patch = preview?.id ?? ''
    const rows = preview?.rows ?? []
    moveCount = rows.filter(row=>row.to !== null).length
    $('chat-preview-file').textContent = file + ' · 移动 ' + moveCount + ' 项 · 跳过 ' + (rows.length-moveCount) + ' 项'
    const box = $('work-moves')
    box.replaceChildren()
    if (rows.some(row => row.conflict)) {
      const label = el('label','work-choice','应用到所有同名冲突')
      const bulk = document.createElement('select')
      bulk.id = 'work-bulk-choice'
      bulk.add(new Option('选择处理方式',''))
      bulk.options[0].disabled = true
      bulk.add(new Option('全部跳过','skip'))
      bulk.add(new Option('全部自动重命名','rename'))
      bulk.value = ''
      bulk.onchange = () => {
        const choice = bulk.value
        bulk.value = ''
        if (choice !== 'skip' && choice !== 'rename') return
        void run('正在更新预览…', async () => {
          showWorkPreview(await window.ide.workChoice(patch, 'conflicts', choice))
          box.querySelector<HTMLSelectElement>('#work-bulk-choice')?.focus()
        })
      }
      label.append(bulk)
      box.append(label)
    }
    for (const row of rows) {
      const item = el('div','work-move-row')
      item.append(el('div','work-path',row.from + ' → ' + (row.to ?? row.requested)))
      if (row.reason) {
        const label = el('label','work-choice',row.reason)
        const select = document.createElement('select')
        select.setAttribute('aria-label',row.from + ' 的同名处理方式')
        if (row.canKeep) select.add(new Option('按原名移动','move'))
        select.add(new Option('跳过（留在原处）','skip'))
        select.add(new Option('自动重命名','rename'))
        select.value = row.choice
        select.onchange = () => {
          const choice = select.value as WorkChoice
          select.value = row.choice
          void run('正在更新预览…',async () => {
            showWorkPreview(await window.ide.workChoice(patch,row.fileId,choice))
            const next = box.querySelectorAll('select')
            Array.from(next).find(s=>s.getAttribute('aria-label') === select.getAttribute('aria-label'))?.focus()
          })
        }
        label.append(select)
        item.append(label)
      }
      box.append(item)
    }
  }
  const loadModels = () => run('正在连接本地 Ollama…', async () => {
    try { await refresh() } catch (error) {
      select.replaceChildren()
      $('st-anvil').textContent = 'AI：模型未就绪'
      $('anvil-dot').className = 'anvil-dot off'
      throw new Error('请检查本机 Ollama 服务和模型，然后点「刷新模型」。' + String(error))
    }
  })
  $('chat-refresh').onclick = () => void loadModels()
  async function chooseFile(): Promise<boolean> {
    const target = work ? await window.ide.workSelect() : await window.ide.chatSelect(activeTab()?.path)
    if (!target) return false
    file = target.file; undo = target.undo; patch = ''
    $('chat-file').textContent = file
    $('chat-file').title = file
    $('chat-log').replaceChildren()
    return true
  }
  $('chat-select').onclick = () => void run('选择目标…', async () => { await chooseFile() })
  async function release() {
    await window.ide.chatRelease()
    file = patch = ''
    undo = false
    $('chat-file').textContent = work ? '尚未授权目录' : '仅聊天'
    $('chat-file').title = ''
    $('chat-log').replaceChildren()
  }
  $('chat-release').onclick = () => void run('', release)
  mode.onchange = () => void run('', async () => {
    work = mode.value === 'work'
    await release()
    input.value = ''
  })
  $('chat-form').onsubmit = event => {
    event.preventDefault()
    const prompt = input.value.trim()
    if (!prompt || button('chat-send').disabled) return
    generating = true
    void run('本地模型处理中…', async () => {
      if (work) {
        if (!file && !await chooseFile()) return
        say('你', prompt)
        const result = await window.ide.workSend(select.value, prompt)
        say('助手', result.answer)
        showWorkPreview(result.preview)
        if (input.value.trim() === prompt) input.value = ''
        return
      }
      say('你', prompt)
      let result = await window.ide.chatSend(select.value, prompt)
      if (result.needsFile) {
        generating = false
        controls()
        $('chat-status').textContent = '选择并授权后，将继续这条请求…'
        if (!await chooseFile()) {
          say('文件', '已取消选择。请求保留在输入框，可再次发送。')
          return
        }
        say('你', prompt)
        generating = true
        controls()
        $('chat-status').textContent = '正在读取授权文件并处理请求…'
        result = await window.ide.chatSend(select.value, prompt)
      }
      if (input.value.trim() === prompt) input.value = ''
      say('助手', result.answer)
      if (result.tools.length) say('工具', result.tools.join(' → '))
      if (result.patch) {
        patch = result.patch.id
        $('chat-preview-file').textContent = file + ' · 第 ' + result.patch.line + ' 行'
        $('chat-old').textContent = result.patch.old
        $('chat-new').textContent = result.patch.replacement
      }
    })
  }
  input.onkeydown = event => {
    if (event.ctrlKey && event.key === 'Enter' && !event.isComposing) {
      event.preventDefault()
      ;($('chat-form') as HTMLFormElement).requestSubmit()
    }
  }
  $('chat-cancel').onclick = () => void window.ide.chatCancel().catch(e => { $('chat-status').textContent = String(e) })
  $('chat-apply').onclick = () => void run('正在应用修改…', async () => {
    if (work) {
      const result = await window.ide.workApply(patch, openEditorFiles()).catch(async error => {
        undo = await window.ide.workCanUndo()
        throw error
      })
      if (result.preview) {
        showWorkPreview(result.preview)
        say('文件','目标位置已变化，请核对更新后的预览并再次确认。')
        return
      }
      patch = ''; undo = undo || result.moved > 0
      say('文件', '已移动 ' + result.moved + ' 项，跳过 ' + result.skipped + ' 项。' + (result.moved ? '可一次撤销本批移动。' : ''))
      await openFolder(state.folder)
      return
    }
    undo = await updateFromChat(file, () => window.ide.chatApply(patch))
    patch = ''
    say('文件', '已应用修改，可以撤销。')
  })
  $('chat-discard').onclick = () => void run('', async () => {
    await window.ide.chatDiscard()
    patch = ''
    say('文件', '已放弃修改。')
  })
  $('chat-undo').onclick = () => void run('正在恢复原文…', async () => {
    if (work) {
      undo = await window.ide.workUndo(openEditorFiles())
      say('文件', '已恢复上次整理的文件位置。')
      await openFolder(state.folder)
      return
    }
    undo = await updateFromChat(file, () => window.ide.chatUndo())
    say('文件', '已撤销上次 AI 修改。')
  })
  for (const tab of document.querySelectorAll<HTMLButtonElement>('.side-tab')) {
    tab.onclick = () => {
      for (const item of document.querySelectorAll('.side-tab')) item.classList.toggle('active', item === tab)
      for (const item of document.querySelectorAll('.side-pane')) item.classList.toggle('active', item.id === 'pane-' + tab.dataset.pane)
      $('sidebar').classList.toggle('show-chat', tab.dataset.pane === 'anvil')
    }
  }
  controls()
  void loadModels()
}

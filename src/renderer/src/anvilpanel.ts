import { $, el } from './core'
import { activeTab, updateFromChat } from './editor'
import { icon } from './icons'

export function initAnvilPanel(): void {
  const pane = $('pane-anvil')
  pane.innerHTML = '<div class="chat-layout"><div class="chat-config"><label>本地模型<select id="chat-model"></select></label><button class="mini-btn" id="chat-refresh">刷新模型</button></div><div class="chat-target"><button class="mini-btn" id="chat-select">选择授权文件</button><span id="chat-file">仅聊天</span><button class="mini-btn" id="chat-release" hidden>解除授权</button></div><div id="chat-log" role="log" aria-live="polite"></div><section id="chat-preview" hidden><strong>待确认修改</strong><p id="chat-preview-file"></p><label>原文</label><pre id="chat-old"></pre><label>替换为</label><pre id="chat-new"></pre><button class="tb-btn" id="chat-apply">应用修改</button> <button class="tb-btn" id="chat-discard">放弃</button></section><form id="chat-form"><textarea id="chat-input" aria-label="聊天消息" placeholder="询问代码，或描述要修改的内容" maxlength="8000"></textarea><div class="chat-actions"><button class="tb-btn" id="chat-send">发送</button><button class="tb-btn" type="button" id="chat-cancel" hidden>取消生成</button><button class="mini-btn" type="button" id="chat-undo">撤销上次修改</button></div></form><p id="chat-status" role="status"></p></div>'
  // 按钮前加图标（文字和 id 不变）
  for (const [id, name] of [['chat-refresh', 'refresh'], ['chat-select', 'link'], ['chat-release', 'unlink'],
    ['chat-send', 'send'], ['chat-cancel', 'stop'], ['chat-undo', 'undo'], ['chat-apply', 'check'], ['chat-discard', 'x']]) {
    $(id).insertAdjacentHTML('afterbegin', icon(name))
  }
  const button = (id: string) => $(id) as HTMLButtonElement
  const select = $('chat-model') as HTMLSelectElement
  const input = $('chat-input') as HTMLTextAreaElement
  let file = '', patch = '', undo = false, busy = false, generating = false
  function controls(): void {
    button('chat-release').hidden = !file
    button('chat-release').disabled = busy
    select.disabled = busy
    for (const id of ['chat-refresh','chat-select','chat-apply','chat-discard']) button(id).disabled = busy
    button('chat-send').disabled = busy || !select.value || Boolean(patch)
    button('chat-undo').disabled = busy || !undo || Boolean(patch)
    button('chat-cancel').hidden = !generating
    $('chat-preview').hidden = !patch
  }
  function say(label: string, content: string): void {
    const box = $('chat-log')
    const item = el('article', 'chat-message')
    // 按角色区分气泡样式：你 / 助手 / 工具调用 / 文件操作记录
    item.dataset.role = ({ 你: 'you', 助手: 'bot', 工具: 'tool', 文件: 'file' } as Record<string, string>)[label] ?? 'bot'
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
    $('anvil-dot').textContent = '本地 AI'
    $('anvil-dot').className = 'anvil-dot ' + (names.length ? 'on' : 'off')
    if (!names.length) throw new Error('请在 Ollama 中安装本地聊天模型后刷新')
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
    const target = await window.ide.chatSelect(activeTab()?.path)
    if (!target) return false
    file = target.file; undo = target.undo; patch = ''
    $('chat-file').textContent = file
    $('chat-file').title = file
    $('chat-log').replaceChildren()
    return true
  }
  $('chat-select').onclick = () => void run('选择目标…', async () => { await chooseFile() })
  $('chat-release').onclick = () => void run('', async () => {
    await window.ide.chatRelease()
    file = patch = ''
    undo = false
    $('chat-file').textContent = '仅聊天'
    $('chat-file').title = ''
    $('chat-log').replaceChildren()
  })
  $('chat-form').onsubmit = event => {
    event.preventDefault()
    const prompt = input.value.trim()
    if (!prompt || button('chat-send').disabled) return
    generating = true
    void run('本地模型处理中…', async () => {
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
        $('chat-preview-file').textContent = file
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

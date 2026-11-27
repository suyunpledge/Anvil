import { $, el, state, bus } from './core'
import { openFolder } from './explorer'
import { activeTab, openEditorFiles, updateFromChat } from './editor'
import { icon, fileBadge } from './icons'
import type { WorkChoice, WorkPreview } from '../../shared/types'

// —— 左侧 AI 面板：「代码助手」与「Work 文件整理」两种模式 ——
// 模式状态以隐藏的 #chat-mode 下拉框为准（界面测试直接改它）；顶部两张模式卡片只是它的可视化开关。

const INTRO = {
  code: {
    title: '代码助手',
    desc: '让本地模型读懂、检查或修改你的代码。授权一个代码文件后，它可以读取、搜索并提出修改；每处修改都先给你预览，确认后才写入，可以撤销。不授权文件也可以直接提问。',
    steps: ['选择授权文件（可选）', '描述你的问题或要改什么', '核对预览，点「应用修改」'],
    examples: ['这个文件是做什么的？按步骤解释一下', '帮我检查有没有 bug，并说明在第几行', '把 average 函数改成保留两位小数']
  },
  work: {
    title: 'Work 文件整理',
    desc: '授权一个文件夹后，用一句话让本地模型查找或批量移动里面的文件（包括子文件夹）。每次移动前都会逐项列出预览，确认后才执行，整批移动可以一键撤销。',
    steps: ['点「授权目录」选择文件夹', '说出要找什么或怎么整理', '核对预览，点「确认移动」'],
    examples: ['找出所有子目录中的 PDF 文件', '把采购目录及子目录里的 PDF 移到「归档/采购」', '把这里的文件按图片、文档、代码分类整理']
  }
} as const

export function initAnvilPanel(): void {
  const pane = $('pane-anvil')
  pane.innerHTML =
    '<div class="chat-layout">' +
      '<div class="mode-switch" role="tablist" aria-label="AI 模式">' +
        `<button type="button" class="mode-btn" data-mode="code" role="tab">${icon('fileCode')}<span><b>代码助手</b><small>读写当前代码文件</small></span></button>` +
        `<button type="button" class="mode-btn" data-mode="work" role="tab">${icon('folders')}<span><b>Work 整理</b><small>批量查找、移动文件</small></span></button>` +
      '</div>' +
      '<select id="chat-mode" class="sr-only" aria-label="模式" tabindex="-1"><option value="code">代码</option><option value="work">Work</option></select>' +
      '<div class="chat-config"><label>本地模型<select id="chat-model"></select></label>' +
        `<button class="mini-btn icon-only" id="chat-refresh" title="刷新模型列表" aria-label="刷新模型">${icon('refresh')}</button></div>` +
      '<div class="chat-target">' +
        `<span class="target-ico" id="target-ico">${icon('file')}</span>` +
        '<div class="target-text"><small id="target-kind">授权文件</small><span id="chat-file">仅聊天</span></div>' +
        `<button class="mini-btn" id="chat-select">${icon('link')}<span>选择授权文件</span></button>` +
        `<button class="mini-btn icon-only" id="chat-release" title="解除授权" aria-label="解除授权" hidden>${icon('unlink')}</button>` +
      '</div>' +
      '<div id="chat-intro"></div>' +
      '<div id="chat-log" role="log" aria-live="polite"></div>' +
      '<section id="chat-preview" hidden>' +
        '<header class="preview-head"><strong id="preview-title">待确认修改</strong><div id="work-summary" hidden></div></header>' +
        '<p id="chat-preview-file"></p>' +
        '<div id="work-moves" hidden></div>' +
        '<label id="chat-old-label">原文</label><pre id="chat-old"></pre>' +
        '<label id="chat-new-label">替换为</label><pre id="chat-new"></pre>' +
        `<div class="preview-actions"><button class="tb-btn" id="chat-apply">${icon('check')}<span>应用修改</span></button>` +
        `<button class="tb-btn" id="chat-discard">${icon('x')}<span>放弃</span></button></div>` +
      '</section>' +
      '<form id="chat-form">' +
        '<textarea id="chat-input" aria-label="聊天消息" placeholder="询问代码，或描述要修改的内容" maxlength="8000" rows="2"></textarea>' +
        '<div class="chat-actions">' +
          `<button class="tb-btn" id="chat-send">${icon('send')}<span>发送</span></button>` +
          `<button class="tb-btn" type="button" id="chat-cancel" hidden>${icon('stop')}<span>取消生成</span></button>` +
          '<span class="chat-hint">Enter 发送 · Shift+Enter 换行</span>' +
          `<button class="mini-btn ghost" type="button" id="chat-undo">${icon('undo')}<span>撤销上次修改</span></button>` +
        '</div>' +
      '</form>' +
      '<p id="chat-status" role="status"></p>' +
    '</div>'
  const button = (id: string) => $(id) as HTMLButtonElement
  // 带图标按钮只换文字，图标保留（按钮的 textContent 仍然只有文字）
  const setLabel = (id: string, text: string) => {
    const b = $(id)
    const span = b.querySelector('span')
    if (span) span.textContent = text
    else b.textContent = text
  }
  const select = $('chat-model') as HTMLSelectElement
  const input = $('chat-input') as HTMLTextAreaElement
  let file = '', patch = '', undo = false, busy = false, generating = false
  const mode = $('chat-mode') as HTMLSelectElement
  let work = false
  let moveCount = 0
  function controls(): void {
    mode.disabled = busy || Boolean(patch)
    for (const b of pane.querySelectorAll<HTMLButtonElement>('.mode-btn')) {
      const on = b.dataset.mode === mode.value
      b.classList.toggle('active', on)
      b.setAttribute('aria-selected', String(on))
      b.disabled = mode.disabled && !on
    }
    pane.classList.toggle('work', work)
    pane.classList.toggle('busy', busy)
    button('chat-select').disabled = busy || Boolean(patch)
    setLabel('chat-select', work ? (file ? '更换目录' : '授权目录') : (file ? '更换文件' : '选择授权文件'))
    $('target-kind').textContent = work ? '授权目录' : '授权文件'
    $('target-ico').innerHTML = icon(work ? 'folder' : 'file')
    $('target-ico').classList.toggle('on', Boolean(file))
    input.placeholder = work ? '例如：把采购目录及子目录里的 PDF 移到「归档/采购」' : '询问代码，或描述要修改的内容'
    input.maxLength = work ? 4000 : 8000
    for (const id of ['chat-old','chat-new','chat-old-label','chat-new-label']) $(id).hidden = work
    $('work-moves').hidden = !work
    $('work-summary').hidden = !work
    $('preview-title').textContent = work ? '整理预览' : '待确认修改'
    setLabel('chat-apply', work ? (moveCount ? '确认移动（'+moveCount+'）' : '确认跳过') : '应用修改')
    for (const item of $('work-moves').querySelectorAll('select')) item.disabled = busy
    setLabel('chat-undo', work ? '撤销上次整理' : '撤销上次修改')
    button('chat-release').hidden = !file
    button('chat-release').disabled = busy
    select.disabled = busy
    for (const id of ['chat-refresh','chat-apply','chat-discard']) button(id).disabled = busy
    button('chat-send').disabled = busy || !select.value || Boolean(patch)
    button('chat-undo').disabled = busy || !undo || Boolean(patch)
    button('chat-cancel').hidden = !generating
    button('chat-send').hidden = generating
    $('chat-preview').hidden = !patch
    $('chat-intro').hidden = $('chat-log').childElementCount > 0 || Boolean(patch)
    renderSideTabs()
  }
  // 引导卡片：说明 + 三步 + 可点的示例（点了只填进输入框，不直接发送）
  function renderIntro(): void {
    const info = INTRO[work ? 'work' : 'code']
    const box = $('chat-intro')
    box.replaceChildren()
    const head = el('div', 'intro-head')
    head.innerHTML = icon(work ? 'folders' : 'sparkles')
    head.append(el('b', '', info.title))
    const steps = el('ol', 'intro-steps')
    for (const s of info.steps) steps.append(el('li', '', s))
    const examples = el('div', 'intro-examples')
    examples.append(el('small', '', '试试这样说'))
    for (const text of info.examples) {
      const chip = el('button', 'intro-chip', text) as HTMLButtonElement
      chip.type = 'button'
      chip.onclick = () => { input.value = text; autoGrow(); input.focus() }
      examples.append(chip)
    }
    box.append(head, el('p', 'intro-desc', info.desc), steps, examples)
  }
  function say(label: string, content: string): void {
    const box = $('chat-log')
    const item = el('article', 'chat-message')
    // 按角色区分气泡样式：你 / 助手 / 工具调用 / 文件操作记录
    item.dataset.role = ({ 你: 'you', 助手: 'bot', 工具: 'tool', 文件: 'file' } as Record<string, string>)[label] ?? 'bot'
    item.append(el('strong', '', label), el('p', '', content))
    box.append(item)
    if (box.childElementCount > 60) box.firstElementChild?.remove()
    $('chat-intro').hidden = true
    box.scrollTop = box.scrollHeight
  }
  async function run(status: string, fn: () => Promise<void>): Promise<void> {
    if (busy) return
    busy = true
    controls()
    $('chat-status').textContent = status
    $('chat-status').classList.remove('error')
    try { await fn(); $('chat-status').textContent = '' }
    catch (e) {
      $('chat-status').textContent = String(e).replace(/^Error: Error invoking remote method '[^']+': (?:Error: )?/, '')
      $('chat-status').classList.add('error')
    }
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
  function showWorkPreview(preview: WorkPreview | null) {
    patch = preview?.id ?? ''
    const rows = preview?.rows ?? []
    moveCount = rows.filter(row=>row.to !== null).length
    const conflicts = rows.filter(row => row.conflict).length
    $('chat-preview-file').textContent = file + ' · 移动 ' + moveCount + ' 项 · 跳过 ' + (rows.length-moveCount) + ' 项'
    const summary = $('work-summary')
    summary.replaceChildren(el('span', 'pill move', '移动 ' + moveCount), el('span', 'pill skip', '跳过 ' + (rows.length - moveCount)))
    if (conflicts) summary.append(el('span', 'pill conflict', '同名 ' + conflicts))
    const box = $('work-moves')
    box.replaceChildren()
    if (rows.some(row => row.conflict)) {
      const label = el('label','work-choice bulk','应用到所有同名冲突')
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
      item.dataset.state = row.to === null ? 'skip' : row.conflict ? 'conflict' : 'move'
      const line = el('div', 'work-line')
      line.insertAdjacentHTML('beforeend', fileBadge(row.from.split('/').pop() ?? row.from))
      // .work-path 只放纯文字「来源 → 目标」（界面测试按这个格式读取）
      line.append(el('div','work-path',row.from + ' → ' + (row.to ?? row.requested)))
      line.append(el('span', 'work-state', row.to === null ? '跳过' : row.choice === 'rename' ? '重命名' : '移动'))
      item.append(line)
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
    autoGrow()
    renderIntro()
  })
  // 模式卡片 → 驱动隐藏下拉框
  function setMode(next: 'code' | 'work'): void {
    if (mode.value === next || mode.disabled) return
    mode.value = next
    mode.dispatchEvent(new Event('change'))
  }
  for (const b of pane.querySelectorAll<HTMLButtonElement>('.mode-btn')) {
    b.onclick = () => setMode(b.dataset.mode as 'code' | 'work')
  }
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
        if (input.value.trim() === prompt) { input.value = ''; autoGrow() }
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
      if (input.value.trim() === prompt) { input.value = ''; autoGrow() }
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
  // Enter 发送、Shift+Enter 换行（Ctrl+Enter 同样发送）；输入法组字时不发送
  function autoGrow(): void {
    input.style.height = 'auto'
    input.style.height = Math.min(200, input.scrollHeight + 2) + 'px'
  }
  input.oninput = autoGrow
  input.onkeydown = event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
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

  // —— 侧栏标签：「AI 聊天」打开代码助手；「Work」打开面板并切到 Work 模式 ——
  function showPane(name: string): void {
    $('app').classList.remove('no-side')
    for (const item of document.querySelectorAll('.side-pane')) item.classList.toggle('active', item.id === 'pane-' + name)
    $('sidebar').classList.toggle('show-chat', name === 'anvil')
    renderSideTabs()
    bus.emit('pane:shown', name)
  }
  bus.on('pane:show', (name) => showPane(typeof name === 'string' ? name : 'files'))
  function renderSideTabs(): void {
    const anvilOpen = pane.classList.contains('active')
    const hasWorkTab = Boolean(document.querySelector('.side-tab[data-mode="work"]'))
    for (const tab of document.querySelectorAll<HTMLElement>('.side-tab')) {
      let on: boolean
      if (tab.dataset.pane !== 'anvil') on = document.getElementById('pane-' + tab.dataset.pane)?.classList.contains('active') ?? false
      else if (tab.dataset.mode === 'work') on = anvilOpen && mode.value === 'work'
      else on = anvilOpen && (mode.value !== 'work' || !hasWorkTab)
      tab.classList.toggle('active', on)
    }
  }
  function openMode(next: 'code' | 'work'): void {
    showPane('anvil')
    setMode(next)
    setTimeout(() => input.focus(), 30)
  }
  for (const tab of document.querySelectorAll<HTMLButtonElement>('.side-tab')) {
    tab.onclick = () => {
      // 活动栏：再点一次当前图标 → 收起侧栏（与 VS Code 相同）
      const app = $('app')
      if (tab.classList.contains('active') && !app.classList.contains('no-side')) {
        app.classList.add('no-side')
        return
      }
      if (tab.dataset.mode === 'work') openMode('work')
      else if (tab.dataset.pane === 'anvil' && mode.value === 'work') openMode('code')
      else showPane(tab.dataset.pane ?? 'files')
    }
  }
  bus.on('ai:open', (m) => openMode(m === 'work' ? 'work' : 'code'))
  document.getElementById('btn-work')?.addEventListener('click', () => openMode('work'))
  document.getElementById('w-work')?.addEventListener('click', () => openMode('work'))

  renderIntro()
  controls()
  void loadModels()
}

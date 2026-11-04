import { bus, $, el } from './core'
import { state } from './core'
import type { AnvilStatus, WoEvent } from '../../shared/types'

// —— Anvil 侧栏：本地模型编码代理（工单创建 / 列表 / 实时事件 / diff 审阅 / 确认落盘） ——

interface Wo {
  wo_id: string
  status?: string
  task?: string
}

let status: AnvilStatus | null = null
let woList: Wo[] = []
let selected: string | null = null
const evLines = new Map<string, Array<{ html: string }>>()

const EV_LABEL: Record<string, string> = {
  step: '步骤', tool: '工具', model: '模型调用', gate: '关卡', status: '状态',
  context: '上下文', hr_retrieve: '记忆检索', applied: '已应用', confirm: '待确认',
  reject: '已拒绝', err: '错误', vision_result: '视觉结果', adapter_result: '适配器',
  snapshot: '快照', tidy: '整理', runtime: '运行时', gap: '缺口', event: '事件',
  visual_query: '视觉查询', vision_verification_result: '视觉复核', stream_end: '事件流结束',
  raw: '原始'
}

function statusClass(st?: string): string {
  if (!st) return 'st-running'
  if (st === 'done') return 'st-done'
  if (st === 'awaiting_confirm') return 'st-await'
  if (['failed', 'rejected', 'cancelled'].includes(st)) return 'st-bad'
  return 'st-running'
}

function statusText(st?: string): string {
  const map: Record<string, string> = {
    running: '运行中', done: '完成', awaiting_confirm: '待确认', failed: '失败',
    rejected: '已拒绝', cancelled: '已取消', queued: '排队中'
  }
  return map[st ?? ''] ?? st ?? '…'
}

export async function initAnvilPanel(): Promise<void> {
  buildSkeleton()
  // 异步探测状态，不阻塞界面
  void refreshStatus().then(() => {
    if (status?.running) void loadList()
  })

  document.querySelectorAll('.side-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.side-tab').forEach((b) => b.classList.remove('active'))
      document.querySelectorAll('.side-pane').forEach((p) => p.classList.remove('active'))
      btn.classList.add('active')
      const pane = btn.getAttribute('data-pane')
      $(`pane-${pane}`).classList.add('active')
    })
  })

  window.ide.onAnvilEvent(({ woId, ev }) => {
    const arr = evLines.get(woId) ?? []
    arr.push(formatEv(ev))
    evLines.set(woId, arr)
    if (woId === selected) renderDetailEvents()
    if (ev.kind === 'status' || ev.kind === 'confirm' || ev.kind === 'reject' || ev.kind === 'applied') {
      void loadList()
    }
  })
}

function buildSkeleton(): void {
  const pane = $('pane-anvil')
  pane.innerHTML = ''
  const head = el('div', 'anv-head')
  head.append(el('span', 'anv-title', 'Anvil 本地助手'))
  const stateEl = el('span', 'anv-state', '检测中…')
  stateEl.id = 'anv-state'
  head.append(stateEl)
  pane.append(head)

  const models = el('div', 'anv-models')
  models.id = 'anv-models'
  models.textContent = '本地模型服务状态检测中…'
  pane.append(models)

  const form = el('div', 'anv-form')
  const fTask = el('div', 'anv-field')
  const lTask = el('label', '', '任务描述（想让 AI 做什么）')
  const tTask = document.createElement('textarea')
  tTask.id = 'anv-task'
  tTask.placeholder = '例如：修复 main.c 里的排序函数，输入 5 个数输出从小到大'
  fTask.append(lTask, tTask)

  const fTarget = el('div', 'anv-field')
  const lTarget = el('label', '', '目标文件（当前打开的文件）')
  const iTarget = document.createElement('input')
  iTarget.type = 'text'
  iTarget.id = 'anv-target'
  iTarget.readOnly = true
  iTarget.placeholder = '（先在编辑器打开目标文件）'
  fTarget.append(lTarget, iTarget)

  const fAdapter = el('div', 'anv-field')
  const lAdapter = el('label', '', '适配器')
  const sAdapter = document.createElement('select')
  sAdapter.id = 'anv-adapter'
  sAdapter.innerHTML = '<option value="">（连接 Anvil 后获取）</option>'
  fAdapter.append(lAdapter, sAdapter)

  const btn = el('button', 'anv-create', '创建工单，交给 Anvil') as HTMLButtonElement
  btn.id = 'anv-create'
  btn.disabled = true
  form.append(fTask, fTarget, fAdapter, btn)
  pane.append(form)

  const list = el('div', 'anv-list')
  list.id = 'anv-list'
  pane.append(list)

  const detail = el('div', 'wo-detail')
  detail.id = 'wo-detail'
  detail.style.display = 'none'
  pane.append(detail)

  btn.addEventListener('click', () => void createWo())
  bus.on('tab:active', () => syncTarget())
}

function syncTarget(): void {
  const input = document.getElementById('anv-target') as HTMLInputElement | null
  if (input && state.openFile) {
    input.value = state.openFile.split(/[\\/]/).pop() ?? ''
    input.title = state.openFile
  }
}

async function refreshStatus(): Promise<void> {
  const st = await window.ide.anvilStatus()
  status = st
  const dot = $('anvil-dot')
  const stateEl = document.getElementById('anv-state')
  const models = document.getElementById('anv-models')
  dot.classList.toggle('on', st.running)
  dot.classList.toggle('off', !st.running)
  $('st-anvil').textContent = st.running ? `Anvil：已连接 :8099` : 'Anvil：未连接'
  if (stateEl) {
    stateEl.textContent = st.running ? '已连接' : '未连接'
    stateEl.classList.toggle('on', st.running)
  }
  const adapterSel = document.getElementById('anv-adapter') as HTMLSelectElement | null
  if (adapterSel && st.adapters.length) {
    adapterSel.innerHTML = ''
    for (const a of st.adapters) {
      const opt = document.createElement('option')
      opt.value = a.key
      opt.textContent = a.label + (a.bad ? '（不可用）' : '')
      adapterSel.append(opt)
    }
  }
  const createBtn = document.getElementById('anv-create') as HTMLButtonElement | null
  if (createBtn) createBtn.disabled = !st.running
  if (models) {
    if (st.running) {
      const integ = st.integrations as { models?: Record<string, unknown>; ready?: Record<string, boolean> } | null
      models.textContent = integ
        ? `模型服务就绪：代码 qwen3.5 · 视觉 qwen2.5vl · 记忆 bge-m3（全部本地 Ollama）`
        : '已连接'
    } else {
      models.textContent = st.error
        ? `连接失败：${st.error}`
        : 'Anvil 未运行。点击下方「启动 Anvil」自动拉起本地服务。'
      const startBtn = el('button', 'anv-create', '▶ 启动 Anvil（本地服务）') as HTMLButtonElement
      startBtn.style.marginTop = '6px'
      startBtn.addEventListener('click', async () => {
        startBtn.disabled = true
        startBtn.textContent = '启动中…（最长 90 秒）'
        const st2 = await window.ide.anvilEnsure()
        status = st2
        await refreshStatus()
        if (st2.running) void loadList()
      })
      models.append(document.createElement('br'), startBtn)
    }
  }
}

async function loadList(): Promise<void> {
  const r = await window.ide.anvilApi<{ work_orders?: Wo[]; items?: Wo[] } | Wo[]>('GET', '/wo')
  if (r.status !== 200 || !r.data) return
  const raw = r.data as { work_orders?: Wo[]; items?: Wo[] } | Wo[]
  woList = Array.isArray(raw) ? raw : raw.work_orders || raw.items || []
  renderList()
}

function renderList(): void {
  const list = document.getElementById('anv-list')
  if (!list) return
  list.innerHTML = ''
  list.append(el('div', 'anv-sec', '工单'))
  if (!woList.length) {
    list.append(el('div', 'hint', '还没有工单。写好任务描述后点「创建工单」。'))
    return
  }
  for (const wo of woList.slice(0, 12)) {
    const item = el('div', 'wo-item' + (wo.wo_id === selected ? ' active' : ''))
    item.append(el('span', 'wo-id', short(wo.wo_id)))
    item.append(el('span', `wo-st ${statusClass(wo.status)}`, statusText(wo.status)))
    item.title = wo.task || wo.wo_id
    item.addEventListener('click', () => void selectWo(wo.wo_id))
    list.append(item)
  }
}

function short(id: string): string {
  return id.length > 14 ? id.slice(0, 8) + '…' : id
}

async function createWo(): Promise<void> {
  const task = (document.getElementById('anv-task') as HTMLTextAreaElement).value.trim()
  const adapter = (document.getElementById('anv-adapter') as HTMLSelectElement).value
  if (!task) {
    alert('请先填写任务描述')
    return
  }
  if (!state.folder && !state.openFile) {
    alert('请先打开一个文件（可以拖进窗口，或点「打开文件」）')
    return
  }
  // 单文件场景：工作目录回退到文件所在目录
  const workdir = state.folder || (state.openFile as string).replace(/[\\/][^\\/]+$/, '')
  const btn = document.getElementById('anv-create') as HTMLButtonElement
  btn.disabled = true
  btn.textContent = '创建中…'
  const r = await window.ide.anvilApi<{ wo_id?: string }>('POST', '/wo', {
    kind: 'code',
    task,
    workdir,
    target: state.openFile,
    test_path: '',
    adapter,
    visual_tasks: false,
    require_confirm: true
  })
  btn.textContent = '创建工单，交给 Anvil'
  btn.disabled = false
  if (r.status === 200 && r.data?.wo_id) {
    ;(document.getElementById('anv-task') as HTMLTextAreaElement).value = ''
    await loadList()
    void selectWo(r.data.wo_id)
  } else {
    alert('建单失败：' + (r.error || JSON.stringify(r.data)?.slice(0, 200) || `HTTP ${r.status}`))
  }
}

async function selectWo(id: string): Promise<void> {
  selected = id
  renderList()
  const detail = document.getElementById('wo-detail')
  if (detail) {
    detail.style.display = ''
    detail.innerHTML = ''
    detail.append(el('div', 'anv-sec', `工单 ${short(id)} · 过程记录`))
  }
  await window.ide.anvilWatch(id)
  void refreshWoDetail()
}

function formatEv(ev: WoEvent): { html: string } {
  const label = EV_LABEL[ev.kind] ?? ev.kind
  let extra = ''
  const e = ev as Record<string, unknown>
  if (e.gate) extra += ` ${String(e.gate)}`
  if (e.signal) extra += ` ${String(e.signal)}`
  if (e.name) extra += ` ${String(e.name)}`
  if (e.status) extra += ` → ${statusText(String(e.status))}`
  if (e.detail || e.message) extra += ` ${String(e.detail ?? e.message)}`
  if (e.issues && Array.isArray(e.issues) && e.issues.length) extra += ` ${String(e.issues[0])}`
  return { html: `<span class="t">[${label}]</span>${escape(extra)}` }
}

function escape(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function renderDetailEvents(): void {
  const detail = document.getElementById('wo-detail')
  if (!detail || !selected) return
  const box = document.getElementById('wo-events')
  if (!box) return
  box.innerHTML = ''
  for (const line of evLines.get(selected) ?? []) {
    const d = el('div', 'ev-line')
    d.innerHTML = line.html
    box.append(d)
  }
  box.scrollTop = box.scrollHeight
}

async function refreshWoDetail(): Promise<void> {
  if (!selected) return
  const r = await window.ide.anvilApi<{ status?: string; staged?: Record<string, unknown> }>('GET', `/wo/${selected}`)
  const detail = document.getElementById('wo-detail')
  if (!detail) return
  if (r.status !== 200 || !r.data) return
  const st = r.data.status ?? ''
  let box = document.getElementById('wo-events')
  if (!box) {
    box = el('div')
    box.id = 'wo-events'
    detail.append(box)
  }
  // staged diff 区
  let diffBox = document.getElementById('wo-diff')
  if (!diffBox) {
    diffBox = el('div', 'diff-box')
    diffBox.id = 'wo-diff'
    detail.append(diffBox)
  }
  if (st === 'awaiting_confirm') {
    const sr = await window.ide.anvilApi<{ staged?: Record<string, string>; diff?: string }>('GET', `/wo/${selected}/staged`)
    const diff = sr.data?.diff ?? (sr.data?.staged ? JSON.stringify(sr.data.staged, null, 1) : '')
    renderDiff(diffBox as HTMLElement, typeof diff === 'string' ? diff : JSON.stringify(diff))
    if (!document.getElementById('wo-actions')) {
      const actions = el('div', 'anv-actions')
      actions.id = 'wo-actions'
      const ok = el('button', 'mini-btn ok', '✔ 确认落盘')
      const no = el('button', 'mini-btn no', '✘ 拒绝')
      ok.addEventListener('click', () => void decide(true))
      no.addEventListener('click', () => void decide(false))
      actions.append(ok, no)
      detail.append(actions)
    }
  }
}

function renderDiff(box: HTMLElement, diff: string): void {
  box.innerHTML = ''
  if (!diff) {
    box.append(el('div', 'co-dim', '（没有暂存内容）'))
    return
  }
  for (const line of diff.split('\n')) {
    const d = el('div')
    if (line.startsWith('+')) d.className = 'diff-add'
    else if (line.startsWith('-')) d.className = 'diff-del'
    else if (line.startsWith('@@')) d.className = 'diff-hunk'
    d.textContent = line
    box.append(d)
  }
}

async function decide(approve: boolean): Promise<void> {
  if (!selected) return
  const path = approve ? '/confirm' : '/cancel'
  const r = await window.ide.anvilApi('POST', `/wo/${selected}${path}`, {})
  if (r.status === 200) {
    const box = document.getElementById('wo-diff')
    if (box) box.innerHTML = ''
    const actions = document.getElementById('wo-actions')
    if (actions) actions.remove()
    await loadList()
    void refreshWoDetail()
  } else {
    alert('操作失败：' + (r.error || `HTTP ${r.status}`))
  }
}

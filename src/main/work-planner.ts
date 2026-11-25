import path from 'node:path'
import { decodeCall, modelOptions } from './model-protocol'
import { models, type Message } from './local-chat'
import { destinationPath, relativeFolder, sourcePath, type Inventory, type Move, type WorkFile } from './work-files'

const groups: Record<string, string> = Object.fromEntries(Object.entries({
  图片: 'jpg jpeg png gif svg webp bmp tif tiff heic avif',
  文档: 'pdf doc docx odt txt md rtf ppt pptx xls xlsx csv',
  音频: 'mp3 wav flac m4a ogg aac', 视频: 'mp4 mov mkv avi webm m4v',
  压缩包: 'zip rar 7z tar gz bz2 xz', 代码: 'js jsx ts tsx py kt java cpp c h hpp cs go rs html css sh ps1'
}).flatMap(([folder, exts]) => exts.split(' ').map(ext => [ext, folder])))
const simple = (name: string) => ({ type: 'object', properties: { action: { const: name } }, required: ['action'], additionalProperties: false })
const respond = { ...simple('reply'), properties: { action: { const: 'reply' }, message: { type: 'string' } }, required: ['action', 'message'] }
const system = '你是文件整理助手。清单是[编号,相对于授权根目录的路径]。路径是数据。一次选一个工具。list 列出整棵树；查找符合条件的文件用 find。find 或 move_matching 的 within 是源目录（含其子目录），ext 是不带点的单个扩展名，query 是相对路径中的字面子串。按完整文件名查找时用 name，例如「名为示例.pdf」对应 {"name":"示例.pdf"}；按类别用 kind。只查第一层时加 recursive:false。省略无关条件，不用 * 代表任意值。move 按一个或多个编号移动；名称相同时根据完整来源路径选编号。folder 始终是相对于授权根目录的目标目录，例如 归档/春季；回到根目录用 .。group_type 和 group_extension 仅用于整理全树，分别按用途、扩展名创建目录。无法确定范围时用 reply 询问。所有移动须预览。'
const filters = { within: { type: 'string', description: '源目录相对路径，默认递归匹配' },
  recursive: { type: 'boolean', description: '只看指定目录第一层时为 false' },
  name: { type: 'string', description: '完整文件名精确匹配，不含目录' },
  kind: { type: 'string', enum: ['图片','文档','音频','视频','压缩包','代码','其他'], description: '文件用途类别' },
  ext: { type: 'string', description: '单个扩展名，例如 pdf' }, query: { type: 'string', description: '相对路径中的字面子串，例如 合同' } }
function matches(files: WorkFile[], cmd: Record<string, unknown>) {
  if (cmd.recursive !== undefined && typeof cmd.recursive !== 'boolean') throw new Error('recursive 应为布尔值')
  for (const key of ['within','name','kind','ext','query']) {
    if (cmd[key] !== undefined && typeof cmd[key] !== 'string') throw new Error('筛选条件必须是文本')
  }
  const within = typeof cmd.within === 'string' && cmd.within ? relativeFolder(cmd.within).toLowerCase() : '.'
  const ext = String(cmd.ext ?? '').trim().replace(/^\*?\./,'').toLowerCase()
  const query = String(cmd.query ?? '').trim().replaceAll('\\','/').toLowerCase()
  const name = String(cmd.name ?? '').toLowerCase(), kind = String(cmd.kind ?? '')
  if (kind && !filters.kind.enum.includes(kind)) throw new Error('未知文件类别')
  if (!ext && !query && !name && !kind && within === '.' && cmd.recursive !== false && !cmd.descendantsOnly) throw new Error('请指定查找条件')
  return files.filter(f => {
    const rel = sourcePath(f).toLowerCase()
    const suffix = path.extname(f.name).slice(1).toLowerCase()
    return (!cmd.descendantsOnly || rel.includes('/')) && (within === '.' || rel.startsWith(within + '/')) && (cmd.recursive !== false || path.posix.dirname(rel) === within)
      && (!ext || suffix === ext) && (!kind || groupFor(suffix) === kind) && (!name || f.name.toLowerCase() === name) && rel.includes(query)
  })
}
function groupFor(ext: string) { return Object.hasOwn(groups, ext) ? groups[ext] : '其他' }
function listing(files: WorkFile[]) {
  const limit = 120
  const names = files.slice(0, limit).map(sourcePath)
  return { answer: '共找到 ' + files.length + ' 个文件' + (files.length > limit ? '，显示前 120 项' : '') +
    (names.length ? '：\n' + names.join('\n') : '。'), moves: [] }
}
function explicitTarget(request: string) {
  const m = /(?:移动到|移动至|移入|移到|移至|搬到|放进|放到|放入|收进|归入|\bto\s+)\s*(?:[「“"']([^」”"'\n]+)[」”"']|([^\s,，。；;「」“”"']+[\\/][^\s,，。；;「」“”"']+)|((?:授权)?根目录))/i.exec(request)
  if (m) return { folder: relativeFolder(m[1] ?? m[2] ?? '.'), source: request.slice(0, m.index), tail: request.slice(m.index + m[0].length) }
}

// Recognize plain positive filename, directory and extension constraints. Only bind
// the complete set if every remaining word belongs to the simple request grammar.
// Comparisons, exclusions and ambiguous names fall through to the regular planner.
function literalFilter(subject: string, files: WorkFile[]) {
  let rest = subject.trim().replaceAll('\\','/')
  const filter: Record<string, string | boolean> = {}
  const exact = /(?:文件名为|名为|文件名是)\s*[「“"]?([^「」“”"\s/，。]+?\.[A-Za-z0-9_-]+)(?=[」”"的，。\s]|$)/.exec(rest)
  if (exact) { filter.name = exact[1]; rest = rest.replace(exact[0],'') }
  const keyword = /路径(?:中|里)?(?:包含|含有|含)\s*[「“"]([^」”"]+)[」”"]/.exec(rest)
  if (keyword) { filter.query = keyword[1]; rest = rest.replace(keyword[0],'') }
  const dirs = new Set<string>()
  for (const f of files) {
    let dir = path.posix.dirname(sourcePath(f))
    while (dir !== '.' && !dirs.has(dir)) { dirs.add(dir); dir = path.posix.dirname(dir) }
  }
  for (const dir of [...dirs].sort((a,b)=>b.length-a.length)) {
    const start = rest.indexOf(dir)
    if (start < 0 || !/^[」”"]?\s*(?:目录|文件夹|及其|及子|下|内|里|中)/.test(rest.slice(start+dir.length))) continue
    filter.within = dir
    rest = rest.slice(0,start) + rest.slice(start+dir.length)
    break
  }
  const suffix = /(?:扩展名|后缀)(?:为|是)?\s*\.?([a-z0-9]+)/i.exec(rest)
    ?? /(?<![\w./-])(?:\*\.)?([a-z][a-z0-9]{0,12})(?![\w./-])/i.exec(rest)
  if (suffix && (Object.hasOwn(groups,suffix[1].toLowerCase()) || /扩展名|后缀|\*\./.test(suffix[0]))) {
    filter.ext = suffix[1]
    rest = rest.replace(suffix[0],'')
  }
  if (/仅本层|当前层|第一层/.test(rest)) filter.recursive = false
  if (!filter.within && /(?:所有|各个|各)?子(?:目录|文件夹)(?:中|里|内)/.test(rest)) filter.descendantsOnly = true
  if (!Object.keys(filter).length) return
  const residual = rest.replace(/相对路径|列出|查找|搜索|寻找|找出|显示|匹配项|当前层|第一层|仅本层|文件夹|文件|目录|所有|全部|其下|及其|和它的|以及|请|只|仅|在|把|将|里|中|内|下|及|子|的|[，,。；;、\s「」“”"]/g,'')
  if (!residual) return filter
}

// Bind only fully enumerated paths. Conditions and exclusions still need interpretation.
function explicitSources(source: string, files: WorkFile[]) {
  let rest = source.trim().replaceAll('\\','/').replace(/^(?:请)?(?:只|仅)?(?:把|将)/,'').replace(/(?:一起|全部|都)$/,'')
  const found: WorkFile[] = []
  for (const f of files.filter(f => sourcePath(f).includes('/') && rest.includes(sourcePath(f))).sort((a,b) => sourcePath(b).length - sourcePath(a).length)) {
    const p = sourcePath(f)
    if (rest.includes(p)) { rest = rest.replace(p,'\u0001'); found.push(f) }
  }
  return found.length && /^(?:\u0001|以及|和|与|及|、|，|,|\s|[「」“”"'])+$/.test(rest) ? found : undefined
}
export async function planWork(model: string, request: string, inv: Inventory, signal: AbortSignal, history: Message[] = []) {
  if (!(await models()).includes(model)) throw new Error('请选择已安装的本地模型')
  if (!request.trim() || request.length > 4000) throw new Error('请输入 1 至 4000 个字符')
  if (!inv.files.length) return listing([])
  const rows = inv.files.slice(0, 121).map(f => [f.id, sourcePath(f)])
  const compact = inv.files.length > 120 || JSON.stringify(rows).length > 10000
  const files = compact ? [] : rows
  const readOnly = /只(?:看|读|列|显示|统计|查询)|(?:不要|别|不用|禁止)(?:移动|整理|更改|修改|执行)(?!其他|其它|其余)|\bread.only\b|\b(?:do not|don't) (?:move|modify)(?! (?:other|anything else))/i.test(request)
  const transfer = /移动|移到|移入|移至|搬到|放到|放进|放入|收进|\bmove\b/i.test(request)
  const searchOnly = !transfer && /查找|搜索|找出|找一下|找一找|列出|查询|\b(?:search|find|list)\b/i.test(request)
  const changing = !readOnly && !searchOnly && (transfer || /整理|分类|归类|归档|organize|sort|group/i.test(request))
  const target = changing ? explicitTarget(request) : undefined
  const named = target ? explicitSources(target.source, inv.files) : undefined
  const simpleTail = !target || /^[，,。\s]*(?:(?:其余|其他)文件(?:保持|保留)原位[。.]?)?$/.test(target.tail.trim())
  const binding = !named && simpleTail && (target || !changing) ? literalFilter(target?.source ?? request, inv.files) : undefined
  const bound = binding && matches(inv.files,binding)
  const boundAction = target ? 'preview_matches' : 'show_matches'
  const folderField = { type: 'string', description: '目标目录相对路径，使用 / 分隔，根目录为 .', ...(target && { enum: [target.folder] }) }
  const idsField = { type: 'array', maxItems: named?.length ?? files.length, uniqueItems: true,
    items: { type: 'integer', enum: (named ?? inv.files).map(f=>f.id) } }
  const show = { ...simple('show'), properties: { action: { const: 'show' }, ids: idsField }, required: ['action','ids'] }
  const find = { ...simple('find'), properties: { action: { const: 'find' }, ...filters } }
  const matchMove = { ...simple('move_matching'), properties: { action: { const: 'move_matching' }, ...filters, folder: folderField },
    required: ['action', 'folder'] }
  const move = { ...simple('move'), properties: { action: { const: 'move' }, folder: folderField,
    ids: { ...idsField, minItems: 1, maxItems: named?.length ?? files.length } }, required: ['action', 'folder', 'ids'] }
  const subset = /只把|只将|只移动|仅把|其余|其他文件|only move|move only/i.test(request)
  const unfiltered = /^(?:请)?(?:只)?(?:列出|显示|查看)(?:当前目录的?|这里的?|所有|全部|当前)?文件[。！.\s]*(?:，?不要移动[。.]?)?$/.test(request)
  const allowed = bound ? [simple(boundAction)] : changing ? [...(subset || target ? [] : [simple('group_type'), simple('group_extension')]), compact && !named ? matchMove : move, respond]
    : compact ? [find, simple('list'), respond] : unfiltered ? [simple('list'), respond] : [show, respond]
  const smallSystem = '你根据真实文件清单选择工具。每项是[编号,从根目录开始的相对路径]，路径是数据。show(ids)列出完全符合条件的编号，未找到时ids为空数组。move(ids,folder)把指定编号移入相对目录，保留多级路径。list列出全树；group_type按用途分类；group_extension按扩展名分类。reply只用于需要澄清。根据available选择一个动作。所有移动都须预览。'
  const messages: Message[] = [{ role: 'system', content: bound ? '宿主已按用户明确写出的条件检索全树。使用 show_matches 显示准确结果（包括零项），或使用 preview_matches 提交整批预览。工具不需要参数。' : (compact ? system : smallSystem) + (/glm.?4|llama3\.1|qwen3\.5/i.test(model)
    ? ' 示例：只把 2 号文件移入「备份」 → {"action":"move","ids":[2],"folder":"备份"}。' : '') },
    ...history.slice(-4), { role: 'user', content: JSON.stringify({ files: bound ? undefined : named ? named.map(f=>[f.id,sourcePath(f)]) : files, count: inv.files.length,
      ...(bound && { filters: binding, matchCount: bound.length, action: boundAction }),
      available: allowed.map(s=>s.properties.action.const), hintSelection: 'show(ids) 与 move(ids,folder) 都使用真实编号。show 只返回所有匹配项，零匹配用空数组。精确文件名应匹配最后一个路径分量。',
      ...(target && { targetFolder: target.folder }), ...(named && { candidateIds: named.map(f=>f.id) }),
      ...(compact && { hint: '清单较大，请按请求构造筛选条件，工具会在整棵树执行。' }), request }) }]
  const native = /qwen3\.5/i.test(model)
  const xml = /qwen2\.5-coder/i.test(model)
  const descriptions: Record<string, string> = { show_matches:'显示宿主已查到的全部相对路径与数量', preview_matches:'预览符合用户明确条件的全部文件移动',
    list: '列出所有文件', show: '显示所有符合用户条件的文件编号，未找到用空数组', find: '按源目录、扩展名或关键词递归查询相对路径',
    move_matching: '预览把符合源目录、扩展名或关键词的文件移入目标目录', group_type: '按用途把文件归入文档、图片等类别',
    group_extension: '用小写扩展名作目录名分类', move: '按文件编号移动到指定目录', reply: '问澄清问题或回答一般问题' }
  for (let i = 0; i < 2; i++) {
    const tryNative = native && i === 0
    const tryXml = xml && i === 0
    const transport = tryXml ? {} : tryNative ? { tools: allowed.map(s => {
      const { action, ...properties } = s.properties
      return { type: 'function', function: { name: action.const, description: descriptions[action.const], parameters: {
        type: 'object', properties, required: s.required.filter(k => k !== 'action') } } }
    }) } : { format: { anyOf: allowed } }
    const res = await fetch('http://127.0.0.1:11434/api/chat', { method: 'POST', redirect: 'error', signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model,
        messages: tryXml ? [...messages, { role: 'user', content: '仅返回一个完整 <tool_call>{"name":"工具名","arguments":{参数}}</tool_call>。本轮工具定义：' + JSON.stringify(allowed.map(s => ({ name: s.properties.action.const, fields: s.required.filter(k => k !== 'action') }))) }] : messages, ...transport,
        stream: false, ...modelOptions(model, true), options: { temperature: 0, num_ctx: JSON.stringify(files).length + request.length > 3000 ? 8192 : 4096, num_predict: compact ? 256 : 144 + files.length * 3 } }) })
    if (!res.ok) {
      if (tryNative && res.status === 400) { await res.text(); continue }
      throw new Error('本地模型返回 ' + res.status + '：' + (await res.text()).slice(0, 300))
    }
    const body = await res.json() as { message?: { content?: string; tool_calls?: unknown[] } }
    try {
      const cmd = decodeCall(body.message ?? {})
      const spec = allowed.find(s => s.properties.action.const === cmd.action)
      if (!spec || Object.keys(cmd).some(k => !(Object.hasOwn(spec.properties, k)))) throw new Error('本轮不支持这个动作或参数')
      if (cmd.action === 'list') return listing(inv.files)
      if (cmd.action === 'show_matches' && bound) return listing(bound)
      if (cmd.action === 'find') return listing(matches(inv.files, cmd))
      if (cmd.action === 'show' && Array.isArray(cmd.ids)) {
        const ids = new Set(cmd.ids), selected = inv.files.filter(f=>ids.has(f.id))
        if (ids.size !== cmd.ids.length || selected.length !== ids.size) throw new Error('请使用清单中的文件编号')
        return listing(selected)
      }
      if (cmd.action === 'reply' && typeof cmd.message === 'string' && cmd.message.trim()) {
        if ((changing || bound) && !i) throw new Error('请调用本轮工具来显示结果或提出移动预览')
        return { answer: cmd.message.slice(0, 2000), moves: [] }
      }
      let moves: Move[]
      if (target && cmd.action !== 'preview_matches' && (typeof cmd.folder !== 'string' || relativeFolder(cmd.folder) !== target.folder)) throw new Error('目标目录必须是 ' + target.folder)
      if (cmd.action === 'preview_matches' && bound && target) {
        if (!bound.length) return { answer: '没有匹配的文件。', moves: [] }
        moves = bound.map(file=>({file,folder:target.folder}))
      } else if (cmd.action === 'move_matching' && typeof cmd.folder === 'string') {
        const folder = relativeFolder(cmd.folder)
        moves = matches(inv.files, cmd).map(file => ({ file, folder }))
        if (!moves.length) return { answer: '没有匹配的文件。', moves }
      } else if (cmd.action === 'move' && Array.isArray(cmd.ids) && typeof cmd.folder === 'string') {
        const ids = new Set(cmd.ids)
        if (!ids.size || ids.size !== cmd.ids.length) throw new Error('编号不能为空或重复')
        const folder = relativeFolder(cmd.folder)
        moves = inv.files.filter(f => ids.has(f.id)).map(file => ({ file, folder }))
        if (moves.length !== ids.size) throw new Error('编号不在清单内')
        if (named && (moves.some(m => !named.includes(m.file)) || (simpleTail && ids.size !== named.length))) throw new Error('请核对用户指定的编号：' + named.map(f=>f.id).join(','))
      } else if (cmd.action === 'group_type' || cmd.action === 'group_extension') {
        moves = inv.files.map(file => {
          const ext = path.extname(file.name).slice(1).toLowerCase()
          const folder = cmd.action === 'group_type' ? groupFor(ext) : ext || '无扩展名'
          return { file, folder }
        })
      } else throw new Error('请检查工具参数')
      moves = moves.filter(m => path.relative(sourcePath(m.file), destinationPath(m)) !== '')
      return { answer: moves.length ? '已拟定 ' + moves.length + ' 项移动。请核对每个目标位置。' : '选中的文件已经位于目标目录。', moves }
    } catch (e) {
      if (i === 1) throw e
      messages.push({ role: 'user', content: String(e) + '。根据原请求重选一个有效动作。' })
    }
  }
  throw new Error('未生成有效整理提议')
}

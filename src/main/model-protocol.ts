// Model output is data. Only single, whole-message calls enter the action validator.
type Bag = Record<string, unknown>
function object(value: unknown): Bag {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('工具调用需要一个对象')
  return value as Bag
}
function only(data: Bag, keys: string[]): void {
  if (Object.keys(data).some(k => !keys.includes(k))) throw new Error('工具参数含额外字段')
}
export function decodeCall(message: { content?: string; tool_calls?: unknown[] }): Bag {
  let value: unknown
  if (message.tool_calls?.length) {
    if (message.tool_calls.length !== 1 || message.content?.trim()) throw new Error('请单独返回一次工具调用')
    const fn = object(object(message.tool_calls[0]).function)
    only(fn, ['name', 'arguments', 'index'])
    value = { name: fn.name, arguments: fn.arguments }
  } else {
    let raw = (message.content ?? '').trim()
    const fence = /^`{3}(?:json|xml)?\s*\n([\s\S]*?)\n`{3}$/i.exec(raw)
    if (fence) raw = fence[1].trim()
    const xml = /^<(tool_call|tools)>\s*([\s\S]*?)\s*<\/\1>$/.exec(raw)
    if (xml) raw = xml[2]
    value = JSON.parse(raw)
    if (xml && Array.isArray(value) && value.length === 1) value = value[0]
  }
  let call = object(value)
  if ('name' in call) {
    only(call, ['name', 'arguments'])
    if (typeof call.name !== 'string') throw new Error('工具名无效')
    const args = object(typeof call.arguments === 'string' ? JSON.parse(call.arguments) : call.arguments)
    if ('action' in args) throw new Error('工具名冲突')
    call = { ...args, action: call.name }
  }
  if (call.action === 'apply_patch' || (call.action === 'replace' && 'patch' in call)) {
    only(call, ['action', 'patch', 'message'])
    const patch = object(call.patch)
    only(patch, ['old_string', 'new_string'])
    return { action: 'replace', old: patch.old_string, replacement: patch.new_string, message: call.message }
  }
  return call
}

export function modelOptions(model: string, work = false) {
  return /qwen3(?:\.5)?[:/-]/i.test(model) || (work && /mythos-v2/i.test(model)) ? { think: false } : {}
}
export function protocolHint(model: string): string {
  if (/glm.?4/i.test(model)) return '只输出一条 JSON。示例：查找标识符 → {"action":"find","old":"limit"}；将 limit = 2 改为 3 → {"action":"replace","old":"limit = 2","replacement":"limit = 3"}。'
  if (/mythos|llama3\.1/i.test(model)) return '一次只选一个动作。先定位指定对象；编辑时 old 与 replacement 只写必要原文，其他对象不可重复修改。'
  return ''
}

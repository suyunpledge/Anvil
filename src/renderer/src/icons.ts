// 界面图标：统一的 24×24 线条图标（内联 SVG，离线可用），以及文件类型徽标与品牌标志。
// 用法：HTML 里写 <i data-ic="名字"></i>，启动时 hydrateIcons() 换成 SVG；代码里用 icon('名字')。

const P: Record<string, string> = {
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  folderOpen: '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
  folders: '<path d="M20 17a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3.9a2 2 0 0 1-1.69-.9l-.81-1.2a2 2 0 0 0-1.67-.9H8a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2Z"/><path d="M2 8v11a2 2 0 0 0 2 2h14"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  fileCode: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="m10 13-2 2 2 2M14 17l2-2-2-2"/>',
  filePlus: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M12 18v-6M9 15h6"/>',
  files: '<path d="M20 7h-3a2 2 0 0 1-2-2V2"/><path d="M9 18a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h7l4 4v10a2 2 0 0 1-2 2Z"/><path d="M3 7.6v12.8A1.6 1.6 0 0 0 4.6 22h9.8"/>',
  save: '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>',
  search: '<circle cx="11" cy="11" r="7.5"/><path d="m20.5 20.5-4.2-4.2"/>',
  play: '<path d="M7 4.6v14.8a1 1 0 0 0 1.52.85l11.86-7.4a1 1 0 0 0 0-1.7L8.52 3.75A1 1 0 0 0 7 4.6Z" fill="currentColor" stroke="none"/>',
  stop: '<rect x="5.5" y="5.5" width="13" height="13" rx="2.5" fill="currentColor" stroke="none"/>',
  sparkles: '<path d="M9.94 15.5a2 2 0 0 0-1.44-1.44l-6.13-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.13a.5.5 0 0 1 .96 0l1.58 6.13a2 2 0 0 0 1.44 1.44l6.13 1.58a.5.5 0 0 1 0 .96l-6.13 1.58a2 2 0 0 0-1.44 1.44l-1.58 6.13a.5.5 0 0 1-.96 0z"/><path d="M20 3v4M22 5h-4"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.14L4 21l1.86-5.4A8 8 0 1 1 21 12Z"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  terminal: '<rect x="2.5" y="4" width="19" height="16" rx="2.5"/><path d="m7 9 3 3-3 3M12.5 15H17"/>',
  output: '<path d="M4 6h16M4 12h11M4 18h7"/>',
  trash: '<path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><path d="M10 11v6M14 11v6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  unlink: '<path d="m18.84 12.25 1.72-1.71a5 5 0 0 0-7.07-7.07l-1.72 1.71M5.17 11.75l-1.71 1.71a5 5 0 0 0 7.07 7.07l1.71-1.71M8 2v3M2 8h3M16 22v-3M22 16h-3"/>',
  cpu: '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>',
  bug: '<path d="m8 2 1.88 1.88M14.12 3.88 16 2M9 7.13v-1a3 3 0 1 1 6 0v1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6M12 20v-9M6.53 9C4.6 8.8 3 7.1 3 5M6 13H2M3 21c0-2.1 1.7-3.9 3.8-4M20.97 5c0 2.1-1.6 3.8-3.5 4M22 13h-4M17.2 17c2.1.1 3.8 1.9 3.8 4"/>',
  // 调试控制
  continue: '<path d="M5 4v16"/><path d="M9.5 5.3v13.4a.8.8 0 0 0 1.22.68l10.4-6.7a.8.8 0 0 0 0-1.36L10.72 4.6A.8.8 0 0 0 9.5 5.3Z" fill="currentColor" stroke="none"/>',
  stepOver: '<path d="M3 13a9 9 0 0 1 17.2-3.6"/><path d="M21 4v5.5h-5.5"/><circle cx="12" cy="19" r="2" fill="currentColor" stroke="none"/>',
  stepInto: '<path d="M12 3v11"/><path d="m7.5 9.5 4.5 4.5 4.5-4.5"/><circle cx="12" cy="19.5" r="2" fill="currentColor" stroke="none"/>',
  stepOut: '<path d="M12 15V4"/><path d="m7.5 8.5 4.5-4.5 4.5 4.5"/><circle cx="12" cy="19.5" r="2" fill="currentColor" stroke="none"/>',
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.8"/><path d="M17 13.5v7M13.5 17h7"/>',
  command: '<path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"/>',
  zap: '<path d="M13 2 4.5 13.5H12L11 22l8.5-11.5H12L13 2Z"/>'
}

export function icon(name: string, cls = ''): string {
  return `<svg class="ic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] ?? ''}</svg>`
}

/** 把页面里所有 <i data-ic="名字"> 换成对应的 SVG（可重复调用）。 */
export function hydrateIcons(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>('i[data-ic]').forEach((node) => {
    const name = node.dataset.ic ?? ''
    if (node.dataset.done === name) return
    node.innerHTML = name === 'brand' ? brandMark() : icon(name)
    node.dataset.done = name
  })
}

/** 品牌标志：与应用图标同造型的铁砧 + 锻造火花（矢量版）。 */
export function brandMark(): string {
  return `<svg class="brand-mark" viewBox="0 0 64 64" aria-hidden="true">
  <defs>
    <linearGradient id="bm-bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2c3452"/><stop offset="1" stop-color="#0f121c"/></linearGradient>
    <linearGradient id="bm-metal" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f6f8fc"/><stop offset="1" stop-color="#8690a6"/></linearGradient>
    <linearGradient id="bm-spark" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffec96"/><stop offset="1" stop-color="#ff7828"/></linearGradient>
    <radialGradient id="bm-glow"><stop offset="0" stop-color="#ffaa3c" stop-opacity=".75"/><stop offset="1" stop-color="#ffaa3c" stop-opacity="0"/></radialGradient>
  </defs>
  <rect x="2.5" y="2.5" width="59" height="59" rx="14" fill="url(#bm-bg)" stroke="#8a9bd0" stroke-opacity=".28"/>
  <circle cx="37.5" cy="18" r="11" fill="url(#bm-glow)"/>
  <path d="M21 25.1h27.9l1.9.5v2.9l-2.5 1.9H21l-4.8-1-5.1-2.4v-.8l5.1-.7z M27.5 30.4h14.7l-3.1 6.5h-8.5z M24.5 36.6h20.7v3.4H24.5z M23.2 39.5h23.3l1.9 4.3H21.3z M21 43.2h27.7v2.1H21z" fill="url(#bm-metal)"/>
  <path d="M37.5 10.5l1.6 5.9 5.9 1.6-5.9 1.6-1.6 5.9-1.6-5.9-5.9-1.6 5.9-1.6z" fill="url(#bm-spark)"/>
  <circle cx="47" cy="14.6" r="1.1" fill="#ffc86a"/><circle cx="29.3" cy="14.2" r=".8" fill="#ffc86a"/>
</svg>`
}

const EXT_BADGE: Record<string, [string, string]> = {
  c: ['c', 'C'], h: ['c', 'H'], cpp: ['cpp', '++'], cc: ['cpp', '++'], cxx: ['cpp', '++'], hpp: ['cpp', 'H'],
  py: ['py', 'Py'], js: ['js', 'JS'], mjs: ['js', 'JS'], cjs: ['js', 'JS'], ts: ['ts', 'TS'], mts: ['ts', 'TS'], cts: ['ts', 'TS'],
  tsx: ['ts', 'TS'], jsx: ['js', 'JS'], java: ['java', 'J'], kt: ['kt', 'K'], kts: ['kt', 'K'],
  html: ['html', '<>'], htm: ['html', '<>'], css: ['css', '#'], json: ['json', '{}'], md: ['md', 'M↓'], txt: ['txt', 'T'],
  toml: ['cfg', '⚙'], ini: ['cfg', '⚙'], yml: ['cfg', 'Y'], yaml: ['cfg', 'Y'], bat: ['sh', '>_'], cmd: ['sh', '>_'],
  ps1: ['sh', '>_'], sh: ['sh', '$'], go: ['go', 'Go'], rs: ['rs', 'Rs'], csv: ['txt', ','], exe: ['bin', '▸']
}
const IMG = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'bmp'])

/** 文件类型徽标（HTML 字符串，内容只来自上面的常量表）。 */
export function fileBadge(name: string, isDir = false, open = false): string {
  if (isDir) return `<span class="fi dir${open ? ' open' : ''}">${icon(open ? 'folderOpen' : 'folder')}</span>`
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
  if (IMG.has(ext)) return `<span class="fi img">▣</span>`
  const [cls, label] = EXT_BADGE[ext] ?? ['other', '']
  return `<span class="fi ${cls}">${label || icon('file')}</span>`
}

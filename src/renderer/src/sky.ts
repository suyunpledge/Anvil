// —— 星空极光背景：铺在整个窗口最底层，各面板半透明透出来 ——
// 组成：夜空渐变（CSS）+ 静态星星（canvas，按窗口大小绘制一次）+ 少量闪烁星（CSS 动画）
//      + 三条模糊极光带（CSS，只动 transform，走 GPU 合成）+ 偶尔一颗流星。
// 关闭设置「星空极光背景」或系统开启「减少动画」时，动画全部停止。

let built = false
let meteorTimer: ReturnType<typeof setTimeout> | undefined

function drawStars(canvas: HTMLCanvasElement): void {
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = window.innerWidth
  const h = window.innerHeight
  canvas.width = Math.round(w * dpr)
  canvas.height = Math.round(h * dpr)
  canvas.style.width = w + 'px'
  canvas.style.height = h + 'px'
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)
  ctx.clearRect(0, 0, w, h)
  // 伪随机（固定种子）：每次重绘星星位置一致，缩放窗口时不会「跳」
  let seed = 20241127
  const rand = (): number => ((seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296)
  const count = Math.round((w * h) / 2600)
  const tints = ['255,255,255', '200,220,255', '255,236,210', '190,255,240', '225,205,255']
  for (let i = 0; i < count; i++) {
    const x = rand() * w
    // 越往上星星越多（下方被极光和地平线的光晕盖住）
    const y = Math.pow(rand(), 1.35) * h
    const big = rand() > 0.965
    const r = big ? 0.9 + rand() * 0.9 : 0.25 + rand() * 0.6
    const alpha = big ? 0.75 + rand() * 0.25 : 0.25 + rand() * 0.55
    const tint = tints[Math.floor(rand() * tints.length)]
    ctx.beginPath()
    ctx.fillStyle = `rgba(${tint},${alpha.toFixed(2)})`
    ctx.arc(x, y, r, 0, Math.PI * 2)
    ctx.fill()
    if (big) {
      // 亮星带一圈柔光
      const glow = ctx.createRadialGradient(x, y, 0, x, y, r * 5)
      glow.addColorStop(0, `rgba(${tint},0.28)`)
      glow.addColorStop(1, `rgba(${tint},0)`)
      ctx.fillStyle = glow
      ctx.beginPath()
      ctx.arc(x, y, r * 5, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

function build(): void {
  if (built) return
  built = true
  const sky = document.createElement('div')
  sky.id = 'sky'
  sky.setAttribute('aria-hidden', 'true')
  const canvas = document.createElement('canvas')
  canvas.className = 'sky-stars'
  sky.append(canvas)
  for (let i = 1; i <= 3; i++) {
    const band = document.createElement('div')
    band.className = 'aurora-band a' + i
    sky.append(band)
  }
  // 闪烁星：位置随机，动画延迟错开
  const twinkles = document.createElement('div')
  twinkles.className = 'sky-twinkles'
  for (let i = 0; i < 26; i++) {
    const s = document.createElement('i')
    s.style.left = (Math.random() * 100).toFixed(2) + '%'
    s.style.top = (Math.pow(Math.random(), 1.4) * 70).toFixed(2) + '%'
    s.style.animationDelay = (-Math.random() * 6).toFixed(2) + 's'
    s.style.animationDuration = (3 + Math.random() * 4).toFixed(2) + 's'
    twinkles.append(s)
  }
  sky.append(twinkles)
  const meteor = document.createElement('div')
  meteor.className = 'meteor'
  sky.append(meteor)
  document.body.prepend(sky)
  drawStars(canvas)
  let resizeTimer: ReturnType<typeof setTimeout> | undefined
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => drawStars(canvas), 200)
  })
}

// 流星：每隔 9~20 秒从上方随机位置划过一次
function scheduleMeteor(): void {
  clearTimeout(meteorTimer)
  meteorTimer = setTimeout(() => {
    const meteor = document.querySelector<HTMLElement>('#sky .meteor')
    if (meteor && document.body.classList.contains('aurora') && !document.hidden) {
      meteor.style.left = (25 + Math.random() * 65).toFixed(1) + '%'
      meteor.style.top = (Math.random() * 30).toFixed(1) + '%'
      meteor.classList.remove('go')
      void meteor.offsetWidth
      meteor.classList.add('go')
    }
    scheduleMeteor()
  }, 9000 + Math.random() * 11000)
}

export function setSky(on: boolean): void {
  if (on) build()
  document.body.classList.toggle('aurora', on)
  // 右上角系统窗口按钮的底色（与 ui.css 里两种标题栏颜色对应）
  try { window.ide.setTitleBarColor?.(on ? '#080c1e' : '#151922') } catch { /* 浏览器预览没有这个接口 */ }
  if (on) scheduleMeteor()
  else clearTimeout(meteorTimer)
}

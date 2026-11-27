import { $, el, bus, state, showFeedback } from './core'
import { openFile } from './editor'
import { openFolder } from './explorer'
import { icon, fileBadge, hydrateIcons } from './icons'
import { badName } from './dialogs'

// —— 开始页（对标 VS 2022 启动窗口）：左边「最近使用」，右边「开始使用」；以及「新建项目」模板 ——

interface Template {
  id: string
  title: string
  desc: string
  badge: string
  file: string
  defaultName: string
  body: string
}

const TEMPLATES: Template[] = [
  {
    id: 'c', title: 'C 控制台程序', desc: '最经典的入门：printf 输出、scanf 读入', badge: 'main.c', file: 'main.c', defaultName: 'HelloC',
    body: '#include <stdio.h>\n\nint main(void) {\n    printf("你好，铁砧！\\n");\n\n    // TODO: 在这里写你的代码，按 F5 运行\n    int a, b;\n    printf("输入两个整数：");\n    scanf("%d %d", &a, &b);\n    printf("它们的和是 %d\\n", a + b);\n    return 0;\n}\n'
  },
  {
    id: 'cpp', title: 'C++ 控制台程序', desc: 'iostream 输入输出，适合算法和课程作业', badge: 'main.cpp', file: 'main.cpp', defaultName: 'HelloCpp',
    body: '#include <iostream>\nusing namespace std;\n\nint main() {\n    cout << "你好，铁砧！" << endl;\n\n    // TODO: 在这里写你的代码，按 F5 运行\n    int a, b;\n    cout << "输入两个整数：";\n    cin >> a >> b;\n    cout << "它们的和是 " << a + b << endl;\n    return 0;\n}\n'
  },
  {
    id: 'python', title: 'Python 程序', desc: '语法最友好，适合入门和小工具', badge: 'main.py', file: 'main.py', defaultName: 'HelloPython',
    body: 'def main():\n    print("你好，铁砧！")\n\n    # TODO: 在这里写你的代码，按 F5 运行\n    name = input("你叫什么名字？")\n    print(f"很高兴认识你，{name}！")\n\n\nif __name__ == "__main__":\n    main()\n'
  },
  {
    id: 'java', title: 'Java 程序', desc: '面向对象入门，类名需与文件名 Main 一致', badge: 'Main.java', file: 'Main.java', defaultName: 'HelloJava',
    body: 'import java.util.Scanner;\n\npublic class Main {\n    public static void main(String[] args) {\n        System.out.println("你好，铁砧！");\n\n        // TODO: 在这里写你的代码，按 F5 运行\n        Scanner sc = new Scanner(System.in);\n        System.out.print("输入两个整数：");\n        int a = sc.nextInt(), b = sc.nextInt();\n        System.out.println("它们的和是 " + (a + b));\n    }\n}\n'
  },
  {
    id: 'js', title: 'JavaScript（Node.js）', desc: '网页同款语言，在本机直接运行', badge: 'main.js', file: 'main.js', defaultName: 'HelloJS',
    body: '// 你好，铁砧！按 F5 运行\nfunction greet(name) {\n  return `你好，${name}！`\n}\n\nconsole.log(greet("铁砧"))\n\n// TODO: 在这里写你的代码\n'
  }
]

const LOCATION_KEY = 'anvil.newproj.location'
const parentOf = (p: string): string => p.replace(/[\\/]+$/, '').replace(/[\\/][^\\/]+$/, '')

export function newProjectDialog(): void {
  const dialog = document.createElement('dialog')
  dialog.className = 'ask-dialog newproj'
  document.body.append(dialog)
  dialog.addEventListener('close', () => setTimeout(() => dialog.remove(), 0))

  let chosen = TEMPLATES[0]
  let nameTouched = false
  const head = el('div', 'ask-head')
  const title = el('h2', 'ask-title')
  title.innerHTML = icon('project')
  title.append('新建项目')
  const close = el('button', 'mini-btn icon-only') as HTMLButtonElement
  close.type = 'button'
  close.setAttribute('aria-label', '关闭')
  close.innerHTML = icon('x')
  close.addEventListener('click', () => dialog.close())
  head.append(title, close)

  const grid = el('div', 'np-grid')
  grid.setAttribute('role', 'radiogroup')
  const cards = TEMPLATES.map((t) => {
    const card = el('button', 'np-card') as HTMLButtonElement
    card.type = 'button'
    card.setAttribute('role', 'radio')
    card.insertAdjacentHTML('beforeend', fileBadge(t.file))
    const text = el('span', 'np-text')
    text.append(el('b', '', t.title), el('small', '', t.desc))
    card.append(text, el('code', 'np-file', t.badge))
    card.addEventListener('click', () => pick(t))
    card.addEventListener('dblclick', () => void create())
    grid.append(card)
    return card
  })

  const form = el('form', 'np-form') as HTMLFormElement
  const nameLabel = el('label', '', '项目名称') as HTMLLabelElement
  const name = el('input') as HTMLInputElement
  name.id = 'np-name'
  nameLabel.htmlFor = name.id
  name.spellcheck = false
  const locLabel = el('label', '', '位置（项目会建成这里面的一个新文件夹）') as HTMLLabelElement
  const locRow = el('div', 'np-loc')
  const loc = el('input') as HTMLInputElement
  loc.id = 'np-location'
  locLabel.htmlFor = loc.id
  loc.spellcheck = false
  loc.placeholder = '点「浏览…」选择一个文件夹'
  loc.value = localStorage.getItem(LOCATION_KEY) ?? (state.folder ? parentOf(state.folder) : '')
  const browse = el('button', 'tb-btn', '浏览…') as HTMLButtonElement
  browse.type = 'button'
  locRow.append(loc, browse)
  const preview = el('p', 'np-preview')
  const error = el('p', 'ask-error')
  error.setAttribute('role', 'alert')
  const actions = el('div', 'ask-actions')
  const cancel = el('button', 'tb-btn', '取消') as HTMLButtonElement
  cancel.type = 'button'
  cancel.addEventListener('click', () => dialog.close())
  const ok = el('button', 'tb-btn ask-ok', '创建并打开') as HTMLButtonElement
  ok.type = 'submit'
  actions.append(cancel, ok)
  form.append(nameLabel, name, locLabel, locRow, preview, error, actions)

  const body = el('div', 'np-body')
  const left = el('section', 'np-left')
  left.append(el('h3', '', '选择模板'), grid)
  const right = el('section', 'np-right')
  right.append(el('h3', '', '配置项目'), form)
  body.append(left, right)
  dialog.append(head, body)

  function updatePreview(): void {
    error.textContent = ''
    const base = loc.value.trim().replace(/[\\/]+$/, '')
    preview.replaceChildren()
    if (!base || !name.value.trim()) {
      preview.textContent = '填写名称和位置后，这里会显示将要创建的文件。'
      return
    }
    preview.append('将创建：', el('code', '', `${base}\\${name.value.trim()}\\${chosen.file}`))
  }
  function pick(t: Template): void {
    chosen = t
    cards.forEach((c, i) => {
      c.classList.toggle('active', TEMPLATES[i] === t)
      c.setAttribute('aria-checked', String(TEMPLATES[i] === t))
    })
    if (!nameTouched) name.value = t.defaultName
    updatePreview()
  }
  name.addEventListener('input', () => { nameTouched = true; updatePreview() })
  loc.addEventListener('input', updatePreview)
  browse.addEventListener('click', async () => {
    const folder = await window.ide.pickFolder('选择新项目的存放位置')
    if (folder) { loc.value = folder; updatePreview() }
  })

  async function create(): Promise<void> {
    const project = name.value.trim()
    const base = loc.value.trim().replace(/[\\/]+$/, '')
    const problem = !project ? '请给项目起个名字' : badName(project) ?? (!base ? '请选择项目存放的位置' : null)
    if (problem) { error.textContent = problem; return }
    ok.disabled = true
    try {
      let dir: string
      try {
        dir = await window.ide.createEntry(base, project, true)
      } catch (e) {
        const text = String(e)
        throw new Error(text.includes('EEXIST') ? `「${project}」文件夹已经存在，换个名字吧` : text.includes('ENOENT') ? '位置不存在，请重新选择' : '创建失败：' + text)
      }
      const file = await window.ide.createEntry(dir, chosen.file, false)
      await window.ide.writeFile(file, chosen.body)
      localStorage.setItem(LOCATION_KEY, base)
      await window.ide.rememberFolder(dir)
      dialog.close()
      await openFolder(dir)
      await openFile(file)
      await window.ide.rememberFile(file)
      bus.emit('pane:show', 'files')
      showFeedback(`项目「${project}」已创建，按 F5 运行试试`)
    } catch (e) {
      error.textContent = e instanceof Error ? e.message : String(e)
    } finally {
      ok.disabled = false
    }
  }
  form.addEventListener('submit', (e) => { e.preventDefault(); void create() })

  pick(TEMPLATES[0])
  dialog.showModal()
  cards[0].focus()
}

// —— 开始页的「最近使用」：可筛选，文件和文件夹混排，显示完整路径 ——
export function renderRecents(importPath: (p: string) => Promise<void>): void {
  const box = $('w-recent')
  const filter = document.getElementById('w-filter') as HTMLInputElement | null
  void window.ide.recentFolders().then((r) => {
    const items = [
      ...r.recentFolders.map((p) => ({ p, dir: true })),
      ...r.recentFiles.filter((p) => !/\.(docx?|xlsx?|pptx?|pdf)$/i.test(p)).map((p) => ({ p, dir: false }))
    ]
    const draw = (): void => {
      const q = filter?.value.trim().toLowerCase() ?? ''
      box.replaceChildren()
      const shown = items.filter((i) => !q || i.p.toLowerCase().includes(q)).slice(0, 10)
      if (!shown.length) {
        box.append(el('div', 'w-empty', items.length ? '没有匹配的最近项目' : '还没有最近使用的内容。新建一个项目或打开文件夹开始吧。'))
        return
      }
      for (const { p, dir } of shown) {
        const item = el('button', 'w-recent-item') as HTMLButtonElement
        item.type = 'button'
        const label = p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? p
        item.insertAdjacentHTML('beforeend', fileBadge(label, dir))
        const text = el('span', 'w-recent-text')
        text.append(el('b', '', label), el('small', '', p))
        item.append(text, el('span', 'tag', dir ? '文件夹' : '文件'))
        item.title = p
        item.addEventListener('click', () => void (dir ? openFolder(p).then(() => window.ide.rememberFolder(p)) : importPath(p)).catch((e) => showFeedback(String(e))))
        box.append(item)
      }
    }
    if (filter && !filter.dataset.bound) {
      filter.dataset.bound = '1'
      filter.addEventListener('input', draw)
    }
    draw()
  })
}

export function initStartPage(): void {
  $('w-newproj').addEventListener('click', newProjectDialog)
  $('w-ai').addEventListener('click', () => bus.emit('ai:open', 'code'))
  hydrateIcons($('welcome'))
}

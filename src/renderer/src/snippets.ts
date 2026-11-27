import * as monaco from 'monaco-editor'
import { editor, activeTab } from './editor'
import { showMenu } from './menu'
import { showFeedback } from './core'

// —— 代码片段（对标 VS「插入代码片段」Ctrl+K Ctrl+X）：输入前缀后从补全列表选，或用菜单插入 ——
// 片段语法：${1:占位} 按 Tab 依次跳转，$0 为最终光标位置。

interface Snippet { prefix: string; label: string; body: string }
type Kind = 'c' | 'cpp' | 'python' | 'java' | 'js'

const SNIPPETS: Record<Kind, Snippet[]> = {
  c: [
    { prefix: 'main', label: 'main 函数（完整程序）', body: '#include <stdio.h>\n\nint main(void) {\n\t$0\n\treturn 0;\n}' },
    { prefix: 'inc', label: '#include <stdio.h>', body: '#include <${1:stdio.h}>' },
    { prefix: 'printf', label: 'printf 输出', body: 'printf("${1:%d}\\n", ${2:x});' },
    { prefix: 'scanf', label: 'scanf 读入', body: 'scanf("${1:%d}", &${2:x});' },
    { prefix: 'for', label: 'for 循环', body: 'for (int ${1:i} = 0; ${1:i} < ${2:n}; ${1:i}++) {\n\t$0\n}' },
    { prefix: 'while', label: 'while 循环', body: 'while (${1:条件}) {\n\t$0\n}' },
    { prefix: 'do', label: 'do-while 循环', body: 'do {\n\t$0\n} while (${1:条件});' },
    { prefix: 'if', label: 'if 判断', body: 'if (${1:条件}) {\n\t$0\n}' },
    { prefix: 'ifelse', label: 'if-else 判断', body: 'if (${1:条件}) {\n\t$2\n} else {\n\t$0\n}' },
    { prefix: 'switch', label: 'switch 分支', body: 'switch (${1:x}) {\ncase ${2:1}:\n\t$0\n\tbreak;\ndefault:\n\tbreak;\n}' },
    { prefix: 'func', label: '函数', body: '${1:int} ${2:name}(${3:int x}) {\n\t$0\n}' },
    { prefix: 'struct', label: '结构体', body: 'struct ${1:Name} {\n\t${2:int x;}\n};' },
    { prefix: 'arr', label: '遍历数组', body: 'for (int ${1:i} = 0; ${1:i} < ${2:n}; ${1:i}++) {\n\tprintf("%d ", ${3:a}[${1:i}]);\n}\nprintf("\\n");' }
  ],
  cpp: [
    { prefix: 'main', label: 'main 函数（完整程序）', body: '#include <iostream>\nusing namespace std;\n\nint main() {\n\t$0\n\treturn 0;\n}' },
    { prefix: 'inc', label: '#include <iostream>', body: '#include <${1:iostream}>' },
    { prefix: 'cout', label: 'cout 输出', body: 'cout << ${1:x} << endl;' },
    { prefix: 'cin', label: 'cin 读入', body: 'cin >> ${1:x};' },
    { prefix: 'for', label: 'for 循环', body: 'for (int ${1:i} = 0; ${1:i} < ${2:n}; ${1:i}++) {\n\t$0\n}' },
    { prefix: 'forr', label: '范围 for（遍历容器）', body: 'for (auto& ${1:x} : ${2:v}) {\n\t$0\n}' },
    { prefix: 'while', label: 'while 循环', body: 'while (${1:条件}) {\n\t$0\n}' },
    { prefix: 'if', label: 'if 判断', body: 'if (${1:条件}) {\n\t$0\n}' },
    { prefix: 'ifelse', label: 'if-else 判断', body: 'if (${1:条件}) {\n\t$2\n} else {\n\t$0\n}' },
    { prefix: 'func', label: '函数', body: '${1:int} ${2:name}(${3:int x}) {\n\t$0\n}' },
    { prefix: 'class', label: '类', body: 'class ${1:Name} {\npublic:\n\t${1:Name}() {}\n\t$0\nprivate:\n};' },
    { prefix: 'vector', label: 'vector 容器', body: 'vector<${1:int}> ${2:v};' },
    { prefix: 'struct', label: '结构体', body: 'struct ${1:Name} {\n\t${2:int x;}\n};' }
  ],
  python: [
    { prefix: 'main', label: 'if __name__ == "__main__"', body: 'def main():\n\t$0\n\n\nif __name__ == "__main__":\n\tmain()' },
    { prefix: 'def', label: '函数', body: 'def ${1:name}(${2:x}):\n\t${0:pass}' },
    { prefix: 'class', label: '类', body: 'class ${1:Name}:\n\tdef __init__(self${2:, x}):\n\t\t${0:pass}' },
    { prefix: 'for', label: 'for 循环（range）', body: 'for ${1:i} in range(${2:n}):\n\t$0' },
    { prefix: 'fori', label: 'for 循环（遍历列表）', body: 'for ${1:x} in ${2:items}:\n\t$0' },
    { prefix: 'while', label: 'while 循环', body: 'while ${1:条件}:\n\t$0' },
    { prefix: 'if', label: 'if 判断', body: 'if ${1:条件}:\n\t$0' },
    { prefix: 'ifelse', label: 'if-else 判断', body: 'if ${1:条件}:\n\t$2\nelse:\n\t$0' },
    { prefix: 'try', label: 'try-except', body: 'try:\n\t$1\nexcept ${2:Exception} as e:\n\t${0:print(e)}' },
    { prefix: 'input', label: '读入一个整数', body: '${1:n} = int(input(${2:"请输入："}))' },
    { prefix: 'inputs', label: '读入一行多个整数', body: '${1:a} = list(map(int, input().split()))' },
    { prefix: 'with', label: '读写文件', body: 'with open(${1:"data.txt"}, ${2:"r"}, encoding="utf-8") as f:\n\t${0:text = f.read()}' }
  ],
  java: [
    { prefix: 'main', label: 'main 方法', body: 'public static void main(String[] args) {\n\t$0\n}' },
    { prefix: 'sout', label: 'System.out.println', body: 'System.out.println(${1:x});' },
    { prefix: 'scanner', label: 'Scanner 读入', body: 'Scanner ${1:sc} = new Scanner(System.in);\nint ${2:n} = ${1:sc}.nextInt();' },
    { prefix: 'class', label: '类', body: 'public class ${1:Name} {\n\t$0\n}' },
    { prefix: 'for', label: 'for 循环', body: 'for (int ${1:i} = 0; ${1:i} < ${2:n}; ${1:i}++) {\n\t$0\n}' },
    { prefix: 'fore', label: '增强 for（遍历）', body: 'for (${1:int} ${2:x} : ${3:arr}) {\n\t$0\n}' },
    { prefix: 'while', label: 'while 循环', body: 'while (${1:条件}) {\n\t$0\n}' },
    { prefix: 'if', label: 'if 判断', body: 'if (${1:条件}) {\n\t$0\n}' },
    { prefix: 'ifelse', label: 'if-else 判断', body: 'if (${1:条件}) {\n\t$2\n} else {\n\t$0\n}' },
    { prefix: 'method', label: '方法', body: '${1:public} ${2:static} ${3:int} ${4:name}(${5:int x}) {\n\t$0\n}' }
  ],
  js: [
    { prefix: 'log', label: 'console.log', body: 'console.log(${1:x})' },
    { prefix: 'func', label: '函数', body: 'function ${1:name}(${2:x}) {\n\t$0\n}' },
    { prefix: 'arrow', label: '箭头函数', body: 'const ${1:name} = (${2:x}) => {\n\t$0\n}' },
    { prefix: 'for', label: 'for 循环', body: 'for (let ${1:i} = 0; ${1:i} < ${2:n}; ${1:i}++) {\n\t$0\n}' },
    { prefix: 'forof', label: 'for…of 遍历', body: 'for (const ${1:x} of ${2:items}) {\n\t$0\n}' },
    { prefix: 'if', label: 'if 判断', body: 'if (${1:条件}) {\n\t$0\n}' },
    { prefix: 'ifelse', label: 'if-else 判断', body: 'if (${1:条件}) {\n\t$2\n} else {\n\t$0\n}' }
  ]
}

function kindOf(model: monaco.editor.ITextModel): Kind | null {
  const ext = model.uri.path.split('.').pop()?.toLowerCase() ?? ''
  if (ext === 'c' || ext === 'h') return 'c'
  if (['cpp', 'cc', 'cxx', 'hpp'].includes(ext)) return 'cpp'
  if (ext === 'py') return 'python'
  if (ext === 'java') return 'java'
  if (['js', 'mjs', 'cjs', 'ts', 'mts', 'cts', 'tsx'].includes(ext)) return 'js'
  return null
}

export function snippetsFor(model: monaco.editor.ITextModel | null): Snippet[] {
  const kind = model ? kindOf(model) : null
  return kind ? SNIPPETS[kind] : []
}

/** 弹出片段菜单，选中后在光标处展开（带 Tab 占位跳转） */
export function insertSnippetMenu(): void {
  const model = editor.getModel()
  const list = snippetsFor(model)
  if (!activeTab() || !list.length) {
    showFeedback('当前文件类型没有可用的代码片段（支持 C / C++ / Python / Java / JavaScript）')
    return
  }
  const pos = editor.getPosition()
  const coords = pos ? editor.getScrolledVisiblePosition(pos) : null
  const box = editor.getDomNode()!.getBoundingClientRect()
  const at = coords ? { x: box.left + coords.left, y: box.top + coords.top + coords.height + 2 } : { x: box.left + 60, y: box.top + 40 }
  showMenu(list.map((s) => ({
    label: s.label,
    shortcut: s.prefix,
    run: () => {
      editor.focus()
      const controller = editor.getContribution('snippetController2') as unknown as { insert(text: string): void } | null
      controller?.insert(s.body)
    }
  })), at)
}

export function initSnippets(): void {
  for (const language of ['cpp', 'python', 'java', 'javascript', 'typescript']) {
    monaco.languages.registerCompletionItemProvider(language, {
      provideCompletionItems(model, position) {
        const word = model.getWordUntilPosition(position)
        const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn)
        return {
          suggestions: snippetsFor(model).map((s) => ({
            label: { label: s.prefix, description: s.label },
            kind: monaco.languages.CompletionItemKind.Snippet,
            detail: '代码片段 · ' + s.label,
            documentation: { value: '```\n' + s.body.replace(/\$\{\d+:([^}]*)\}/g, '$1').replace(/\$\d/g, '') + '\n```' },
            insertText: s.body,
            insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
            range,
            sortText: '~' + s.prefix
          }))
        }
      }
    })
  }
}

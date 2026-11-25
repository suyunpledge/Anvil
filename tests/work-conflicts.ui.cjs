// Run after packaging: node_modules/electron/dist/electron.exe tests/work-conflicts.ui.cjs
// Set ANVIL_UI_MODELS to a comma-separated list to override the test models.
const {app, BrowserWindow, dialog} = require('electron')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')

const models = (process.env.ANVIL_UI_MODELS ??
  'anvil-qwen3.5:9b,fableforge-ai/mythos-v2-8b:q4_k_m,fableforge-ai/mythos-v2-8b:latest').split(',').map(tag=>tag.trim()).filter(Boolean)
assert.ok(models.length, '请提供本机模型标签')
const area = fs.mkdtempSync(path.join(os.tmpdir(), 'anvil-conflict-ui-'))
const profile = path.join(area,'profile')
fs.mkdirSync(profile)
app.setPath('userData',profile)
const archive = path.resolve('dist/win-unpacked/resources/app.asar')
app.setAppPath(archive)
let root
dialog.showOpenDialog = async (_window, options) => {
  assert.ok(options.properties.includes('openDirectory'))
  return {canceled:false,filePaths:[root]}
}
require(path.join(archive,'out/main/index.js'))

app.whenReady().then(async () => {
  const wc = BrowserWindow.getAllWindows()[0].webContents
  const ui = (fn,arg) => wc.executeJavaScript('('+fn+')('+JSON.stringify(arg ?? null)+')',true)
  const wait = async fn => {
    const end = Date.now()+155000
    while(Date.now()<end) {
      if(await ui(fn)) return
      await new Promise(resolve=>setTimeout(resolve,120))
    }
    throw Error('UI timeout: '+await ui(()=>document.querySelector('#chat-status').textContent))
  }
  const click = id => ui(id=>document.getElementById(id).click(),id)
  const changeChoice = async (selector,value) => {
    await ui(([selector,value])=>{
      const select=document.querySelector(selector)
      select.value=value
      select.dispatchEvent(new Event('change'))
    },[selector,value])
    await wait(()=>!document.querySelector('#chat-apply').disabled)
  }
  const rowChoices = () => ui(()=>[...document.querySelectorAll('.work-move-row select')].map(s=>s.value))
  const submit = request=>ui(request=>{
    document.querySelector('#chat-input').value=request
    document.querySelector('#chat-form').requestSubmit()
  },request)
  const read = p=>fs.readFileSync(path.join(root,p),'utf8')
  const exists = p=>fs.existsSync(path.join(root,p))
  try {
    await wait(()=>document.querySelector('#chat-model')?.options.length)
    await ui(()=>{
      document.querySelector('[data-pane=anvil]').click()
      const mode=document.querySelector('#chat-mode')
      mode.value='work';mode.dispatchEvent(new Event('change'))
    })
    await wait(()=>document.querySelector('#chat-file').textContent==='尚未授权目录' && !document.querySelector('#chat-select').disabled)
    for(const [index,model] of models.entries()){
      root = path.join(area,'授权目录'+index)
      const fixtures=['甲/合同.pdf','乙/合同.pdf','丙/清单.txt','归档/合同.pdf','归档/合同 (1).pdf']
      for(const p of fixtures){
        const full=path.join(root,p)
        fs.mkdirSync(path.dirname(full),{recursive:true})
        fs.writeFileSync(full,p)
      }
      await ui(model=>{document.querySelector('#chat-model').value=model},model)
      await click('chat-select')
      await wait(()=>document.querySelector('#chat-release').hidden===false && !document.querySelector('#chat-send').disabled)
      await submit('把甲/合同.pdf、乙/合同.pdf和丙/清单.txt一起移入「归档」。')
      await wait(()=>!document.querySelector('#chat-preview').hidden && !document.querySelector('#chat-apply').disabled)
      const rowA='select[aria-label="甲/合同.pdf 的同名处理方式"]'
      assert.deepEqual(await rowChoices(),['skip','skip'])
      await changeChoice('#work-bulk-choice','rename')
      assert.deepEqual(await rowChoices(),['rename','rename'])
      const paths=await ui(()=>[...document.querySelectorAll('.work-path')].map(item=>item.textContent.split(' → ')[1]).sort())
      assert.deepEqual(paths,['归档/合同 (2).pdf','归档/合同 (3).pdf','归档/清单.txt'].sort())
      await changeChoice(rowA,'skip')
      assert.deepEqual((await rowChoices()).sort(),['rename','skip'])
      await changeChoice('#work-bulk-choice','skip')
      assert.deepEqual(await rowChoices(),['skip','skip'])
      assert.equal(await ui(()=>document.querySelector('#chat-apply').textContent),'确认移动（1）')
      await changeChoice(rowA,'rename')
      assert.deepEqual((await rowChoices()).sort(),['rename','skip'])
      await wait(()=>document.querySelector('#work-moves').textContent.includes('甲/合同.pdf → 归档/合同 (2).pdf') && !document.querySelector('#chat-apply').disabled)
      for(const p of fixtures) assert.equal(read(p),p)
      fs.writeFileSync(path.join(root,'归档/合同 (2).pdf'),'外部新增')
      await click('chat-apply')
      await wait(()=>document.querySelector('#work-moves').textContent.includes('甲/合同.pdf → 归档/合同 (3).pdf') && !document.querySelector('#chat-apply').disabled)
      for(const p of fixtures) assert.equal(read(p),p)
      assert.ok(!exists('归档/清单.txt'))
      await click('chat-apply')
      await wait(()=>document.querySelector('#chat-preview').hidden && !document.querySelector('#chat-undo').disabled)
      assert.equal(read('归档/合同 (3).pdf'),'甲/合同.pdf')
      assert.equal(read('归档/清单.txt'),'丙/清单.txt')
      assert.equal(read('乙/合同.pdf'),'乙/合同.pdf')
      assert.ok(!exists('甲/合同.pdf') && !exists('丙/清单.txt'))
      // A no-op confirmation must preserve the previous undo entry.
      await submit('只把乙/合同.pdf移入「归档」。')
      await wait(()=>!document.querySelector('#chat-preview').hidden && document.querySelector('#chat-apply').textContent==='确认跳过' && !document.querySelector('#chat-apply').disabled)
      await click('chat-apply')
      await wait(()=>document.querySelector('#chat-preview').hidden && !document.querySelector('#chat-undo').disabled)
      await click('chat-undo')
      await wait(()=>document.querySelector('#chat-undo').disabled && !document.querySelector('#chat-send').disabled)
      for(const p of fixtures) assert.equal(read(p),p)
      assert.equal(read('归档/合同 (2).pdf'),'外部新增')
      assert.ok(!exists('归档/合同 (3).pdf') && !exists('归档/清单.txt'))
      await click('chat-release')
      await wait(()=>document.querySelector('#chat-file').textContent==='尚未授权目录' && !document.querySelector('#chat-select').disabled)
      console.log('PASS packaged bulk choices, individual overrides, refreshed preview and batch undo: '+model)
    }
    app.exit(0)
  }catch(error){console.error(error);app.exit(1)}
})

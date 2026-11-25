// npm run bench:work -- <installed Ollama tags ...>
require('tsx/cjs')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const assert = require('node:assert/strict')
const { WorkFiles, destinationPath, sourcePath, previewChoices } = require('../src/main/work-files.ts')
const { planWork } = require('../src/main/work-planner.ts')

const files = ['cover.JPG', 'scan.png', '账单.pdf', '会议.txt', 'main.ts']
const cases = [
  ['list', '只列出当前目录的文件，不要移动。', []],
  ['types', '把这里的所有文件按用途分类整理，图片、文档、代码各进对应目录。', ['图片/cover.JPG','图片/scan.png','文档/账单.pdf','文档/会议.txt','代码/main.ts']],
  ['subset', '只把两张图片放入「待审核」，其余文件保持原位。', ['待审核/cover.JPG','待审核/scan.png']],
  ['named', '把会议.txt 和 cover.JPG 移入「周报附件」，不要移动其他文件。', ['周报附件/会议.txt','周报附件/cover.JPG']],
  ['ext', '把所有文件按扩展名整理，每种扩展名各建一个小写目录。', ['jpg/cover.JPG','png/scan.png','pdf/账单.pdf','txt/会议.txt','ts/main.ts']]
]
const nested = ['采购/春季/合同.pdf','采购/秋季/报价.PDF','财务/合同.pdf','资料/合同.txt','资料/封面.png','已归档/保留.txt','历史/合同.pdf.bak']
const recursiveCases = [
  { name:'find-duplicates', request:'查找所有子目录中名为合同.pdf的文件，列出相对路径。', found:[nested[0],nested[2]] },
  { name:'find-within', request:'只列出采购目录及其子目录中的 PDF 文件。', found:nested.slice(0,2) },
  { name:'no-match', request:'查找所有子目录中扩展名为 woff 的文件。', found:[] },
  { name:'path-keyword', request:'查找路径里包含“合同”的文件，列出所有匹配项。', found:[nested[0],nested[2],nested[3],nested[6]] },
  { name:'scope-move', request:'把采购及其子文件夹里的所有 PDF 移入「汇总/采购件」，其他文件保留原位。',
    from:nested.slice(0,2), folder:'汇总/采购件' },
  { name:'pick-duplicate', request:'只把财务/合同.pdf移入「已归档/签署」，保留采购目录的同名文件。',
    from:[nested[2]],folder:'已归档/签署' },
  { name:'collision', request:'把采购/春季/合同.pdf和财务/合同.pdf一起移入「签署/完成」。', collision:true },
  { name:'large-scope', large:true, request:'在采购目录和它的子目录中查找所有扩展名为 pdf 的文件。', found:nested.slice(0,2) },
  { name:'large-move', large:true, request:'把采购目录及其子目录中扩展名为 pdf 的文件移入存档/单据。', from:nested.slice(0,2),folder:'存档/单据' },
]
const conflictRequest='把采购/春季/合同.pdf、财务/合同.pdf和采购/秋季/报价.PDF一起移入「签署/完成」。'
for(const policy of ['skip','rename','skip-override','rename-override'])
  recursiveCases.push({name:'existing-'+policy, policy, request:conflictRequest})
async function snapshotTree(root) {
  const entries = {}
  async function visit(rel) {
    for (const e of await fs.readdir(path.join(root,rel),{withFileTypes:true})) {
      const p = rel ? rel+'/'+e.name : e.name
      entries[p] = e.isDirectory() ? null : await fs.readFile(path.join(root,p),'base64')
      if (e.isDirectory()) await visit(p)
    }
  }
  await visit('')
  return entries
}
async function recursiveCase(host,storage,root,model,c) {
  const before = await snapshotTree(root)
  const inv = await host.inventory(root)
  const {answer,moves} = await planWork(model,c.request,inv,AbortSignal.timeout(150000))
  if ('found' in c) {
    assert.equal(moves.length,0)
    assert.deepEqual(answer.split('\n').slice(1).sort(),[...c.found].sort(),answer)
    if (!c.found.length) assert.match(answer,/0 个文件/)
  } else if (c.collision) {
    assert.deepEqual(moves.map(m=>sourcePath(m.file)).sort(),[nested[0],nested[2]].sort())
    assert.ok(moves.every(m=>m.folder==='签署/完成'))
    const first=await host.review(inv,moves)
    assert.deepEqual(first.rows.map(r=>r.choice).sort(),['move','skip'])
    const losing=first.rows.find(r=>r.choice==='skip')
    const changed=await host.review(inv,moves,new Map([[losing.fileId,'rename']]))
    assert.equal(changed.rows.find(r=>r.fileId===losing.fileId).to,'签署/完成/合同 (1).pdf')
    assert.deepEqual(await snapshotTree(root),before)
    await host.apply(inv,changed.moves)
    for(const r of changed.rows) assert.equal(await fs.readFile(path.join(root,r.to),'base64'),before[r.from])
    await new WorkFiles(storage).undo(root)
  } else if (c.policy) {
    assert.deepEqual(moves.map(m=>sourcePath(m.file)).sort(),nested.slice(0,3).sort())
    assert.ok(moves.every(m=>m.folder==='签署/完成'))
    const first=await host.review(inv,moves)
    const conflicts=first.rows.filter(r=>r.conflict)
    assert.equal(conflicts.length,2)
    assert.ok(conflicts.every(r=>r.choice==='skip' && !r.canKeep))
    const [bulk,override]=c.policy.split('-')
    let review=await host.review(inv,moves,previewChoices(first.rows,'conflicts',bulk))
    assert.ok(review.rows.every(row=>row.choice===(row.conflict ? bulk : 'move')))
    if(override) {
      const choice=bulk==='skip'?'rename':'skip'
      review=await host.review(inv,moves,previewChoices(review.rows,conflicts[0].fileId,choice))
      assert.equal(review.rows.find(row=>row.fileId===conflicts[0].fileId).choice,choice)
    }
    const n=override?1:bulk==='skip'?0:2
    assert.equal(review.moves.length,n+1)
    assert.deepEqual(review.rows.filter(r=>r.choice==='rename').map(r=>r.to).sort(),
      ['签署/完成/合同 (2).pdf','签署/完成/合同 (3).pdf'].slice(0,n))
    assert.deepEqual(await snapshotTree(root),before)
    await host.apply(inv,review.moves)
    for(const r of review.rows){
      assert.equal(await fs.readFile(path.join(root,r.to??r.from),'base64'),before[r.from])
      if(r.to) await assert.rejects(fs.stat(path.join(root,r.from)),{code:'ENOENT'})
    }
    for(const p of ['签署/完成/合同.pdf','签署/完成/合同 (1).pdf'])
      assert.equal(await fs.readFile(path.join(root,p),'base64'),before[p])
    await new WorkFiles(storage).undo(root)
  } else {
    const preview=moves.map(m=>[sourcePath(m.file),destinationPath(m)]).sort()
    assert.deepEqual(preview,c.from.map(p=>[p,c.folder+'/'+path.posix.basename(p)]).sort())
    await host.preflight(inv,moves)
    assert.deepEqual(await snapshotTree(root),before)
    await host.apply(inv,moves)
    for (const [src,dst] of preview) {
      assert.equal(await fs.readFile(path.join(root,dst),'base64'),before[src])
      await assert.rejects(fs.stat(path.join(root,src)),{code:'ENOENT'})
    }
    await new WorkFiles(storage).undo(root)
  }
  assert.deepEqual(await snapshotTree(root),before)
}
async function main() {
  const tags = process.argv.slice(2)
  assert.ok(tags.length, '请传入本地模型标签')
  const fetchLocal = globalThis.fetch
  const formats = []
  globalThis.fetch = async (url, options) => {
    const res = await fetchLocal(url, options)
    if (url.endsWith('/chat') && res.ok) {
      const msg = (await res.clone().json()).message
      formats.push(msg?.tool_calls?.length ? 'native' : /^\s*<(tool_call|tools)>/.test(msg?.content) ? 'xml' :
        /^\s*\{/.test(msg?.content) ? 'json' : 'text')
    }
    return res
  }
  const area = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'anvil-work-live-')))
  try {
    const root = path.join(area,'incoming')
    await fs.mkdir(root)
    for (const f of files) await fs.writeFile(path.join(root,f), 'fixture:'+f)
    const host = new WorkFiles(path.join(area,'history'))
    const tree=path.join(area,'tree')
    const large=path.join(area,'large-tree')
    const conflict=path.join(area,'conflict-tree')
    for (const dir of [tree,large,conflict]) for (const p of nested) {
      await fs.mkdir(path.dirname(path.join(dir,p)),{recursive:true})
      await fs.writeFile(path.join(dir,p),'fixture:'+p)
    }
    await fs.mkdir(path.join(conflict,'签署/完成'),{recursive:true})
    for(const name of ['合同.pdf','合同 (1).pdf']) await fs.writeFile(path.join(conflict,'签署/完成',name),'original:'+name)
    await fs.mkdir(path.join(large,'其他'))
    for(let i=0;i<125;i++) await fs.writeFile(path.join(large,'其他',i+'.txt'),'filler')
    for (const model of tags) for (const c of recursiveCases) {
      const start=Date.now()
      formats.length=0
      try {
        await recursiveCase(host,path.join(area,'history'),c.policy ? conflict : c.large ? large : tree,model,c)
        console.log(JSON.stringify({model,name:c.name,pass:true,formats,seconds:(Date.now()-start)/1000}))
      } catch(e) {process.exitCode=1;console.log(JSON.stringify({model,name:c.name,pass:false,formats,error:e.message,seconds:(Date.now()-start)/1000}))}
    }
    for (const model of tags) for (const [name, request, expected] of cases) {
      const start = Date.now()
      formats.length = 0
      try {
        const inventory = await host.inventory(root)
        const {moves, answer} = await planWork(model,request,inventory,AbortSignal.timeout(150000))
        const actual = moves.map(m=>m.folder+'/'+m.file.name).sort()
        assert.deepEqual(actual,[...expected].sort())
        if (name === 'list') for (const f of files) assert.ok(answer.includes(f),answer)
        if (moves.length) {
          await host.apply(inventory,moves)
          for (const m of moves) assert.equal(await fs.readFile(path.join(root,m.folder,m.file.name),'utf8'),'fixture:'+m.file.name)
          await new WorkFiles(path.join(area,'history')).undo(root)
        }
        assert.deepEqual((await fs.readdir(root)).sort(),[...files].sort())
        console.log(JSON.stringify({model,name,pass:true,formats,seconds:(Date.now()-start)/1000}))
      } catch(e) {process.exitCode=1; console.log(JSON.stringify({model,name,pass:false,formats,error:e.message,seconds:(Date.now()-start)/1000}))}
    }
  } finally { await fs.rm(area,{recursive:true,force:true}) }
}
main().catch(e=>{console.error(e);process.exitCode=1})

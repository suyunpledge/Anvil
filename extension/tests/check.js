#!/usr/bin/env node
// check.js —— 交付件 D 的静态检查（不需要 VS Code 宿主、不需要 npm install）。
//
// 它检查的是「扩展的契约完整性」——这类问题在装进编辑器之前就能发现：
//   1. extension.js 语法可解析（node --check 级别）
//   2. package.json 里声明的每条命令，extension.js 里都真的注册了
//   3. 代码里用到的配置键，package.json 里都声明了（否则读出来是 undefined）
//   4. webview 的动态内容都过了转义函数（防止把文件内容当 HTML 注入）
//   5. 主线入口与激活事件配置正确
//
// 跑法：node extension/tests/check.js

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const here = __dirname;
const extDir = path.dirname(here);
const pkgPath = path.join(extDir, 'package.json');
const jsPath = path.join(extDir, 'extension.js');

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log('  [PASS] ' + name);
  } catch (e) {
    failures++;
    console.log('  [FAIL] ' + name + '  —— ' + e.message);
  }
}

console.log('检查对象：' + extDir + '\n');

const pkgRaw = fs.readFileSync(pkgPath, 'utf8');
const pkg = JSON.parse(pkgRaw);
const js = fs.readFileSync(jsPath, 'utf8');

console.log('— 1. 语法 —');
check('package.json 是合法 JSON', () => JSON.parse(pkgRaw));
check('extension.js 语法可解析（不执行）', () => {
  new vm.Script(js, { filename: jsPath });
});

console.log('— 2. 命令声明 ↔ 注册 —');
const declared = (pkg.contributes.commands || []).map((c) => c.command);
check('至少声明了 6 条命令', () => assert(declared.length >= 6, '实际 ' + declared.length));
for (const id of declared) {
  check('已注册 ' + id, () => assert(js.includes("'" + id + "'"), 'extension.js 里找不到该命令 id'));
}
const registered = (js.match(/\['localIde\.[A-Za-z]+',/g) || []).length;
check('注册数与声明数一致', () => assert.strictEqual(registered, declared.length,
  '声明 ' + declared.length + ' / 注册 ' + registered));

console.log('— 3. 配置键声明 ↔ 使用 —');
const cfgProps = Object.keys((pkg.contributes.configuration || {}).properties || {})
  .map((k) => k.replace('localIde.', ''));
const used = [];
for (const m of js.matchAll(/c\.get\('([A-Za-z]+)'\)/g)) used.push(m[1]);
check('至少用到了 4 个配置键', () => assert(used.length >= 4, '实际 ' + used.length));
for (const key of new Set(used)) {
  check('配置键已声明：' + key, () =>
    assert(cfgProps.includes(key), 'package.json 未声明 ' + key + '（读了会是 undefined）'));
}
for (const key of cfgProps) {
  check('配置键有默认值：' + key, () => {
    const p = pkg.contributes.configuration.properties['localIde.' + key];
    assert(p && p.default !== undefined, '缺少 default');
  });
}

console.log('— 4. 视图与入口 —');
check('入口指向存在的文件', () => {
  const main = path.join(extDir, pkg.main.replace(/^\.\//, ''));
  assert(fs.existsSync(main), '找不到 ' + main);
});
check('activitybar 视图容器已声明', () =>
  assert((pkg.contributes.viewsContainers || {}).activitybar, '缺 viewsContainers.activitybar'));
check('views 指向 localIdeContainer', () => {
  const v = (pkg.contributes.views || {}).localIdeContainer;
  assert(v && v.length && v[0].id === 'localIde.panel', '视图 id 不对');
  assert(v[0].type === 'webview', '面板应为 webview');
});
check('图标文件存在', () => {
  const icon = pkg.contributes.viewsContainers.activitybar[0].icon;
  assert(fs.existsSync(path.join(extDir, icon)), '找不到 ' + icon);
});
check('选中即解释已挂到右键菜单', () => {
  const m = (pkg.contributes.menus || {})['editor/context'] || [];
  assert(m.some((x) => x.command === 'localIde.explainSelection'), '未挂菜单');
});
check('激活事件不为空', () => assert((pkg.activationEvents || []).length > 0));

console.log('— 5. 安全：动态内容必须过转义 —');
check('存在 esc() 转义函数', () => assert(/esc\s*\(s\)\s*\{/.test(js), '找不到 esc 定义'));
check('存在集中生成 HTML 片段的辅助函数', () => {
  for (const fn of ['badge(', 'moveList(', 'preBlock(']) {
    assert(js.includes(fn), '缺少 ' + fn);
  }
});
// 只检查 render() 返回的那段 HTML 模板：那里才是真正拼 HTML 的地方
const htmlStart = js.indexOf('return `<!DOCTYPE html>');
const htmlEnd = js.indexOf('</body></html>`', htmlStart);
assert(htmlStart > 0 && htmlEnd > htmlStart, '定位不到 render() 的 HTML 模板');
const htmlTpl = js.slice(htmlStart, htmlEnd);
const allowed = [/^this\.esc\(/, /^gates\b/, /^moves\b/, /^diff\b/, /^woLine\b/, /^gatesLine\b/,
  /^auditBtn\b/, /^awaiting/, /^viol\b/, /^this\.log\.slice/];
const raw = [];
for (const m of htmlTpl.matchAll(/\$\{([^}]*)\}/g)) {
  const expr = m[1].trim();
  if (allowed.some((rx) => rx.test(expr))) continue;
  raw.push(expr);
}
check('render() 的 HTML 里没有未转义的服务端数据', () =>
  assert(raw.length === 0, '发现可疑插值：' + raw.slice(0, 4).join(' ; ')));
check('辅助函数与预拼片段内部都调了 esc()', () => {
  const needEsc = { badge: 'badge(', moveList: 'moveList(', preBlock: 'preBlock(',
                    woLine: 'woLine =' };
  for (const [name, needle] of Object.entries(needEsc)) {
    const at = js.indexOf(needle);
    assert(at > 0, '找不到 ' + name + ' 的定义/调用');
    const seg = js.slice(at, at + 400);
    assert(seg.includes('this.esc('), name + ' 内部没有走 esc()');
  }
  // 纯常量片段：必须不包含任何插值
  for (const [name, needle] of Object.entries({ gatesLine: 'gatesLine =',
                                                auditBtn: 'auditBtn =' })) {
    const at = js.indexOf(needle);
    assert(at > 0, '找不到 ' + name);
    const seg = js.slice(at, js.indexOf(';', at));
    assert(!seg.includes('${'), name + ' 是纯常量片段，不应含插值');
  }
});
check('webview 使用标准消息通道', () =>
  assert(js.includes('acquireVsCodeApi'), 'webview 未使用标准消息通道'));

console.log('— 6. 不该出现的东西 —');
check('扩展不做模型调用以外的外部网络', () => {
  const bad = ['api.openai.com', 'anthropic.com', 'https://api.', 'googleapis.com'];
  for (const b of bad) {
    assert(!js.includes(b), '扩展里出现了外部服务地址：' + b);
  }
});
check('只有 gateway/service 两个后端地址来源', () => {
  const urls = [...js.matchAll(/https?:\/\/[a-zA-Z0-9.:_-]+/g)].map((m) => m[0]);
  for (const u of urls) {
    assert(/127\.0\.0\.1|localhost/.test(u), '出现了非本机地址：' + u);
  }
});
check('没有把凭据写进代码', () => {
  assert(!/sk-[A-Za-z0-9]{16,}/.test(js), 'extension.js 里像是有密钥');
  assert(!/Bearer\s+[A-Za-z0-9._-]{20,}/.test(js), 'extension.js 里像是有令牌');
});

console.log('\n合计：' + (failures === 0 ? '全部通过 ✓' : failures + ' 项未通过 ✗'));
process.exit(failures === 0 ? 0 : 1);

// extension.js —— 交付件 D：VS Code 扩展（侧栏工作台 + 选中即解释 + diff 审阅）
//
// 为什么写成**纯 JS、不带构建步骤**：
//   1) 避免 npm install（本机网络按流量计费，不该为了一次构建下载一堆依赖）；
//   2) VS Code 直接加载 JS，clone 下来就能 F5 调试，交给别人改的门槛最低；
//   3) HTTP 只用内置 http 模块，不依赖 fetch 在扩展宿主里的可用性差异。
//
// 与后端的分工：扩展**不做任何模型调用、不碰文件**。它只做三件事——
// 发工单、看事件、在 diff 里让人确认。所有判断都在服务端（gate 才是真相源）。

const vscode = require('vscode');
const http = require('http');
const https = require('https');
const fs = require('fs');
const os = require('os');
const path = require('path');

// ============================================================
// HTTP 小工具（不带依赖）
// ============================================================
function request(url, { method = 'GET', body = null, timeout = 600000 } = {}) {
  return new Promise((resolve, reject) => {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return reject(new Error('地址不合法：' + url));
    }
    const mod = u.protocol === 'https:' ? https : http;
    const payload = body ? Buffer.from(JSON.stringify(body), 'utf8') : null;
    const req = mod.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + (u.search || ''),
        method,
        headers: Object.assign(
          { Accept: 'application/json' },
          payload ? { 'Content-Type': 'application/json', 'Content-Length': payload.length } : {}
        )
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let json = null;
          try {
            json = text ? JSON.parse(text) : null;
          } catch (e) {
            /* 非 JSON 就把原文带回去 */
          }
          resolve({ status: res.statusCode, json, text });
        });
      }
    );
    req.on('error', (e) => reject(e));
    req.setTimeout(timeout, () => {
      req.destroy(new Error('请求超时'));
    });
    if (payload) req.write(payload);
    req.end();
  });
}

// SSE：逐块解析 `data:` 行
function openEvents(url, onEvent, onEnd) {
  const u = new URL(url);
  const mod = u.protocol === 'https:' ? https : http;
  const req = mod.request(
    { hostname: u.hostname, port: u.port, path: u.pathname + (u.search || ''), method: 'GET',
      headers: { Accept: 'text/event-stream' } },
    (res) => {
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => {
        buf += chunk;
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of raw.split('\n')) {
            if (line.startsWith('data:')) {
              const payload = line.slice(5).trim();
              if (payload === '[DONE]') continue;
              try {
                onEvent(JSON.parse(payload));
              } catch (e) {
                /* 忽略解析不了的分片 */
              }
            }
          }
        }
      });
      res.on('end', () => onEnd && onEnd());
    }
  );
  req.on('error', () => onEnd && onEnd());
  req.end();
  return () => req.destroy();
}

function cfg() {
  const c = vscode.workspace.getConfiguration('localIde');
  return {
    gateway: c.get('gatewayUrl') || 'http://127.0.0.1:8080',
    service: c.get('serviceUrl') || 'http://127.0.0.1:8090',
    adapter: c.get('defaultAdapter') || 'mythos',
    autoApply: !!c.get('autoApply'),
    token: c.get('serviceToken') || ''
  };
}

// ★ 服务端写操作需要令牌（外部审查 #2：不能让任何本机程序都能驱动模型改文件）。
//   令牌由服务启动时生成，写到 <项目>/.runtime/service-token；
//   这里缓存一份，401 时清缓存重读（服务重启会换新令牌）。
let TOKEN_CACHE = null;
function loadToken() {
  const { token } = cfg();
  if (token) return token;               // 设置里手填的优先
  if (TOKEN_CACHE) return TOKEN_CACHE;
  const roots = (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
  const cands = [];
  for (const r of roots) {
    cands.push(path.join(r, '.runtime', 'service-token'));
    cands.push(path.join(r, '..', 'local-model-ide-app', '.runtime', 'service-token'));
  }
  for (const p of cands) {
    try {
      const v = fs.readFileSync(p, 'utf8').trim();
      if (v) {
        TOKEN_CACHE = v;
        return v;
      }
    } catch (e) {
      /* 试下一个 */
    }
  }
  return '';
}

function tokenHeader() {
  const t = loadToken();
  return t ? { 'X-Local-Ide-Token': t } : {};
}

// 带令牌请求；401 时清缓存重试一次
async function authed(url, opts) {
  const o = Object.assign({}, opts || {});
  o.headers = Object.assign({}, (opts && opts.headers) || {}, tokenHeader());
  let r = await request(url, o);
  if (r.status === 401) {
    TOKEN_CACHE = null;
    o.headers = Object.assign({}, o.headers, tokenHeader());
    r = await request(url, o);
  }
  return r;
}

// ============================================================
// 面板（webview）
// ============================================================
class Panel {
  constructor() {
    this.view = null;
    this.log = [];
    this.state = { wo: null, status: 'idle', gates: [], violations: [], diff: '', moves: [] };
    this._closeSse = null;
  }

  attach(webviewView) {
    this.view = webviewView.webview;
    this.view.options = { enableScripts: true };
    this.view.html = this.render();
    this.view.onDidReceiveMessage((m) => this.onMessage(m));
  }

  push(line) {
    const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    this.log.push(`[${stamp}] ${line}`);
    if (this.log.length > 300) this.log.shift();
    this.refresh();
  }

  refresh() {
    if (this.view) this.view.html = this.render();
  }

  esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // 预编译 HTML 片段：集中在这里生成，静态检查就能确认「服务端数据只经由 esc() 进 HTML」
  badge(g) {
    const cls = g.signal === 'ok' ? 'ok' : 'bad';
    const word = g.signal === 'ok' ? '通过' : '未过';
    return `<span class="badge ${cls}">${this.esc(g.gate)} ${word}</span>`;
  }

  moveList(moves) {
    return moves.slice(0, 20).map((m) => `<li>${this.esc(m.src)} → ${this.esc(m.dst)}</li>`).join('');
  }

  preBlock(text, cls, limit) {
    if (!text) return '';
    return `<pre class="${cls}">${this.esc(text).slice(0, limit)}</pre>`;
  }

  render() {
    const c = cfg();
    const s = this.state;
    const gates = s.gates.map((g) => this.badge(g)).join('');
    const viol = s.violations.length
      ? `<div class="warn">违规 ${s.violations.length} 项：${this.esc(
          s.violations.map((v) => v.rule).join(', ')
        )}</div>`
      : '';
    const moves = this.moveList(s.moves || []);
    const diff = this.preBlock(s.diff, 'diff', 4000);
    const awaiting = s.status === 'awaiting_confirm';
    // ★ 预先拼好的 HTML 片段：全部经由 esc()，静态检查会逐条确认。
    //   （如果直接把数据往模板里插，检查器会报“未转义的插值”。）
    const woLine = s.wo ? '　工单 <code>' + this.esc(s.wo) + '</code>' : '';
    const gatesLine = gates || '<span class="hint">尚未运行</span>';
    const auditBtn = '<button class="ghost" id="audit">查看调用审计</button>';
    return `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body{font-family:var(--vscode-font-family);font-size:12.5px;color:var(--vscode-foreground);padding:10px 12px}
  h3{margin:14px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;opacity:.7;font-weight:600}
  label{display:block;margin:8px 0 3px;opacity:.85}
  input,select,textarea{width:100%;box-sizing:border-box;background:var(--vscode-input-background);color:var(--vscode-input-foreground);border:1px solid var(--vscode-input-border,#6666);border-radius:3px;padding:4px 6px;font-family:inherit;font-size:12px}
  textarea{min-height:56px;resize:vertical}
  button{margin:8px 6px 0 0;padding:4px 12px;background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:0;border-radius:3px;cursor:pointer;font-size:12px}
  button.ghost{background:var(--vscode-button-secondaryBackground,#4444);color:var(--vscode-button-secondaryForeground,#ddd)}
  button:disabled{opacity:.45;cursor:default}
  .row{display:flex;gap:8px}.row>*{flex:1}
  .badge{display:inline-block;padding:1px 7px;margin:2px 4px 2px 0;border-radius:2px;border:1px solid #8886;font-size:11px}
  .badge.ok{border-color:#4a6b3f;color:#4a6b3f}
  .badge.bad{border-color:#8c2f22;color:#8c2f22}
  .warn{margin:8px 0;padding:6px 8px;border-left:3px solid #b04a20;background:#b04a2018}
  .st{margin:8px 0;padding:6px 8px;border:1px solid #8884;border-radius:3px}
  pre.diff{white-space:pre-wrap;word-break:break-all;max-height:260px;overflow:auto;background:var(--vscode-textCodeBlock-background,#0002);padding:6px;border-radius:3px;font-size:11.5px}
  pre.log{white-space:pre-wrap;max-height:200px;overflow:auto;opacity:.8;font-size:11px;margin:6px 0 0}
  ul{margin:6px 0;padding-left:18px}
  .hint{opacity:.65;font-size:11px;margin-top:6px;line-height:1.5}
</style></head><body>
<div class="st">
  <b>状态</b>：${this.esc(s.status)}${woLine}
  <div style="margin-top:6px">${gatesLine}</div>${viol}
</div>

<h3>新建工单</h3>
<label>类型</label>
<select id="kind">
  <option value="code">code —— 实现/修改代码（五节点 + 三 gate）</option>
  <option value="tidy">tidy —— 整理文件夹（方案闸 + 复核闸）</option>
</select>
<label>任务描述</label>
<textarea id="task" placeholder="例：在 calc.py 中实现 average(nums)：返回平均值，空列表返回 0.0。"></textarea>
<div class="row">
  <div><label>目标文件（code）</label><input id="target" placeholder="calc.py"></div>
  <div><label>测试文件（code）</label><input id="test" placeholder="test_calc.py"></div>
</div>
<div class="row">
  <div><label>目录（tidy，逗号分隔）</label><input id="dirs" placeholder="文档,图片,压缩包"></div>
  <div><label>适配层</label><input id="adapter" value="${this.esc(c.adapter)}"></div>
</div>
<label><input type="checkbox" id="semantic" style="width:auto"> tidy 用本地嵌入给内容归类建议</label>
<button id="go">建单并运行</button>
<button class="ghost" id="check">检查服务</button>

${
  awaiting
    ? `<h3>待确认（未点确认不会写文件）</h3>
       ${moves ? `<ul>${moves}</ul>` : ''}
       ${diff}
       <button id="review">打开 diff 审阅</button>
       <button id="confirm">确认落盘</button>
       <button class="ghost" id="cancel">放弃</button>
       <div class="hint">autoApply 关闭时，工单完成后文件仍保持原样，直到你点确认。</div>`
    : `<h3>操作</h3><button class="ghost" id="review">查看暂存 diff</button>
       ${auditBtn}
       <button class="ghost" id="cancel">取消当前工单</button>`
}

<pre class="log">${this.esc(this.log.slice(-40).join('\n'))}</pre>

<script>
  const vscode = acquireVsCodeApi();
  const send = (kind, extra) => vscode.postMessage(Object.assign({ kind }, extra || {}));
  const on = (id, kind) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', () => {
      if (kind === 'run') {
        send('run', {
          woKind: document.getElementById('kind').value,
          task: document.getElementById('task').value,
          target: document.getElementById('target').value,
          test: document.getElementById('test').value,
          dirs: document.getElementById('dirs').value,
          adapter: document.getElementById('adapter').value,
          semantic: document.getElementById('semantic').checked
        });
      } else send(kind);
    });
  };
  on('go', 'run'); on('check', 'check'); on('review', 'review');
  on('confirm', 'confirm'); on('cancel', 'cancel'); on('audit', 'audit');
</script>
</body></html>`;
  }

  onMessage(m) {
    if (m.kind === 'run') return Commands.run(m);
    if (m.kind === 'check') return Commands.check();
    if (m.kind === 'review') return Commands.reviewDiff();
    if (m.kind === 'confirm') return Commands.confirmApply();
    if (m.kind === 'cancel') return Commands.cancelOrder();
    if (m.kind === 'audit') return Commands.showAudit();
  }

  // ---- 事件接入 ----
  watch(woId) {
    if (this._closeSse) this._closeSse();
    const { service } = cfg();
    this.state.wo = woId;
    this.state.status = 'running';
    this.state.gates = [];
    this.state.violations = [];
    this.state.diff = '';
    this.state.moves = [];
    this.refresh();
    this._closeSse = openEvents(`${service}/wo/${woId}/events`, (ev) => {
      if (ev.kind === 'gate') {
        this.state.gates.push({ gate: ev.gate, signal: ev.signal });
        if (ev.issues && ev.issues.length) this.push(`${ev.gate}: ${ev.issues[0]}`);
      } else if (ev.kind === 'violation') {
        this.state.violations.push({ rule: ev.rule });
        this.push(`违规：${ev.rule} ${ev.detail || ''}`);
      } else if (ev.kind === 'status') {
        this.state.status = ev.status;
        if (ev.diff) this.state.diff = ev.diff;
        // ★ 外部审查 #5：原来这里写的是 this.state.moves = this.state.moves（空赋值），
        //   tidy 的待确认界面因此永远看不到具体移动方案。现在收服务端发来的清单。
        if (ev.plan && ev.plan.length) this.state.moves = ev.plan;
        this.push(`状态：${ev.status}${ev.needs_confirm ? '（等待你确认）' : ''}`);
      } else if (ev.kind === 'progress') {
        this.push(ev.message || '');
      } else if (ev.kind === 'error') {
        this.push(`错误：${ev.message}`);
      } else if (ev.kind === 'applied') {
        this.push(ev.path ? `已落盘：${ev.path}` : `已移动 ${ev.moved} 个文件`);
      } else if (ev.kind === 'done') {
        this.push('—— 工单结束 ——');
      }
      this.refresh();
    });
  }
}

// ============================================================
// 命令实现
// ============================================================
const PanelSingleton = new Panel();

const Commands = {
  async run(m) {
    const { service, adapter } = cfg();
    const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
    if (!folder) return vscode.window.showWarningMessage('请先打开一个文件夹作为工作目录');
    const root = folder.uri.fsPath;
    let body;
    if (m.woKind === 'tidy') {
      body = {
        kind: 'tidy',
        task: m.task || '按类型整理这个目录',
        workdir: root,
        dirs: String(m.dirs || '')
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        semantic: !!m.semantic,
        adapter: m.adapter || adapter,
        require_confirm: !cfg().autoApply
      };
    } else {
      if (!m.task) return vscode.window.showWarningMessage('请填写任务描述');
      body = {
        kind: 'code',
        task: m.task,
        workdir: root,
        target: m.target || '',
        test_path: m.test || '',
        adapter: m.adapter || adapter,
        require_confirm: !cfg().autoApply
      };
      if (!body.target) return vscode.window.showWarningMessage('code 工单需要目标文件');
    }
    try {
      const r = await authed(`${service}/wo`, { method: 'POST', body });
      if (r.status >= 400 || !r.json || !r.json.wo_id) {
        return vscode.window.showErrorMessage(`建单失败：${r.text || r.status}`);
      }
      PanelSingleton.push(`建单成功：${r.json.wo_id}`);
      PanelSingleton.watch(r.json.wo_id);
    } catch (e) {
      vscode.window.showErrorMessage(`连不上工单服务（${service}）：${e.message}`);
    }
  },

  async check() {
    const { gateway, service } = cfg();
    const lines = [];
    for (const [name, url] of [['网关', `${gateway}/health`], ['工单服务', `${service}/health`]]) {
      try {
        const r = await request(url, { timeout: 15000 });
        lines.push(`${name}: ${r.status} ${r.text ? r.text.slice(0, 160) : ''}`);
      } catch (e) {
        lines.push(`${name}: 连不上（${e.message}）`);
      }
    }
    PanelSingleton.push(lines.join(' ｜ '));
    vscode.window.showInformationMessage(lines[0]);
  },

  async explainSelection() {
    const ed = vscode.window.activeTextEditor;
    if (!ed) return;
    const sel = ed.document.getText(ed.selection);
    if (!sel.trim()) return vscode.window.showWarningMessage('先选中一段代码');
    const { gateway, adapter } = cfg();
    // 说明：解释走网关的对话路径（本地模型、无工具、不落盘）
    const body = {
      model: adapter === 'qwen-coder' ? 'qwen2.5-coder:7b' : undefined,
      _task: 'chat',
      messages: [
        { role: 'system', content: '你是代码解释器。用中文简洁解释这段代码做什么、有什么风险。不要提改法。' },
        { role: 'user', content: '```\n' + sel.slice(0, 4000) + '\n```' }
      ]
    };
    try {
      const r = await request(`${gateway}/v1/chat/completions`, { method: 'POST', body });
      const out = (r.json && r.json.choices && r.json.choices[0].message.content) || r.text;
      const doc = await vscode.workspace.openTextDocument({ content: out, language: 'markdown' });
      await vscode.window.showTextDocument(doc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
    } catch (e) {
      vscode.window.showErrorMessage(`解释失败（网关 ${gateway}）：${e.message}`);
    }
  },

  async reviewDiff() {
    const wo = PanelSingleton.state.wo;
    if (!wo) return vscode.window.showWarningMessage('还没有工单');
    const { service } = cfg();
    try {
      const r = await authed(`${service}/wo/${wo}/staged`, { timeout: 30000 });
      if (!r.json || !r.json.content) return vscode.window.showWarningMessage('这个工单没有暂存内容');
      // 服务端只回文件名（减少本机路径暴露）；真实文件从工作区里按名找
      const filename = r.json.filename || '';
      const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
      let real = '';
      if (filename && folder) {
        const uri = await vscode.workspace.findFiles(filename, null, 1);
        if (uri && uri.length) real = uri[0].fsPath;
      }
      if (!real) {
        return vscode.window.showWarningMessage(
          `在当前工作区找不到 ${filename || '目标文件'}；暂存内容已在日志里，可从服务端查看`
        );
      }
      // 把暂存内容写到临时文件，用 VS Code 原生 diff 打开（只读审阅，不碰真实文件）
      const tmp = path.join(os.tmpdir(), `local-ide-staged-${wo}${path.extname(real) || '.txt'}`);
      fs.writeFileSync(tmp, r.json.content, 'utf8');
      await vscode.commands.executeCommand(
        'vscode.diff',
        vscode.Uri.file(real),
        vscode.Uri.file(tmp),
        `暂存改动 ↔ 当前文件（${path.basename(real)}）`
      );
    } catch (e) {
      vscode.window.showErrorMessage(`取暂存失败：${e.message}`);
    }
  },

  async confirmApply() {
    const wo = PanelSingleton.state.wo;
    if (!wo) return vscode.window.showWarningMessage('还没有工单');
    const { service } = cfg();
    const ok = await vscode.window.showWarningMessage(
      '确认把暂存改动写入真实文件？', { modal: true }, '确认落盘'
    );
    if (ok !== '确认落盘') return;
    try {
      const r = await authed(`${service}/wo/${wo}/confirm`, { method: 'POST', body: {}, timeout: 120000 });
      if (r.status >= 400) {
        return vscode.window.showErrorMessage(`落盘被拒：${(r.json && r.json.error) || r.text}`);
      }
      PanelSingleton.push(`已落盘：${(r.json && r.json.path) || (r.json && r.json.moved) + ' 个文件'}`);
      vscode.window.showInformationMessage('已落盘');
    } catch (e) {
      vscode.window.showErrorMessage(`落盘失败：${e.message}`);
    }
  },

  async cancelOrder() {
    const wo = PanelSingleton.state.wo;
    if (!wo) return;
    const { service } = cfg();
    try {
      await authed(`${service}/wo/${wo}/cancel`, { method: 'POST', body: {} });
      PanelSingleton.push('已请求取消');
    } catch (e) {
      vscode.window.showErrorMessage(`取消失败：${e.message}`);
    }
  },

  async showAudit() {
    const { gateway } = cfg();
    try {
      const r = await request(`${gateway}/audit?limit=200`, { timeout: 30000 });
      const entries = (r.json && r.json.entries) || [];
      const rows = entries.map((e) =>
        `${e.ts}  ${e.model || ''}  ${e.kind}  ${e.sec}s  画像=${e.profile || '-'}` +
        `${e.stripped_think ? '  [摘掉think]' : ''}${e.task ? '  task=' + e.task : ''}`
      );
      const doc = await vscode.workspace.openTextDocument({
        content: `本地模型调用审计（${gateway}）\n共 ${entries.length} 条\n\n` + rows.join('\n'),
        language: 'plaintext'
      });
      await vscode.window.showTextDocument(doc, { preview: true });
    } catch (e) {
      vscode.window.showErrorMessage(`取审计失败：${e.message}`);
    }
  },

  async newWorkOrder() {
    const task = await vscode.window.showInputBox({ prompt: '任务描述（实现/修改什么）' });
    if (!task) return;
    const target = await vscode.window.showInputBox({ prompt: '目标文件（相对工作目录）' });
    if (!target) return;
    const testPath = (await vscode.window.showInputBox({ prompt: '测试文件（可留空）' })) || '';
    return Commands.run({ woKind: 'code', task, target, test: testPath, adapter: cfg().adapter });
  },

  async tidyFolder() {
    const task = await vscode.window.showInputBox({
      prompt: '怎么整理（例：按类型把文件归到 文档/图片/压缩包）',
      value: '按类型整理这个目录：文档归 文档，图片归 图片，压缩包归 压缩包。不确定的不要动。'
    });
    if (!task) return;
    const dirs = await vscode.window.showInputBox({
      prompt: '允许归入的目录（逗号分隔）',
      value: '文档,图片,压缩包'
    });
    return Commands.run({ woKind: 'tidy', task, dirs: dirs || '', adapter: cfg().adapter });
  }
};

// ============================================================
// 激活
// ============================================================
function activate(context) {
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('localIde.panel', {
      resolveWebviewView(view) {
        PanelSingleton.attach(view);
      }
    })
  );
  for (const [id, fn] of [
    ['localIde.newWorkOrder', Commands.newWorkOrder],
    ['localIde.tidyFolder', Commands.tidyFolder],
    ['localIde.explainSelection', Commands.explainSelection],
    ['localIde.reviewDiff', Commands.reviewDiff],
    ['localIde.confirmApply', Commands.confirmApply],
    ['localIde.cancelOrder', Commands.cancelOrder],
    ['localIde.showAudit', Commands.showAudit],
    ['localIde.checkServices', Commands.check]
  ]) {
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  }
  console.log('Local Model IDE 已激活');
}

function deactivate() {
  if (PanelSingleton._closeSse) PanelSingleton._closeSse();
}

module.exports = { activate, deactivate };
// 逻辑单测用（不需要 VS Code 宿主）
module.exports.__test__ = { request, openEvents, tokenHeader, authed };

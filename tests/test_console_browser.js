/**
 * 真实浏览器冒烟：用 Chrome DevTools Protocol 打开 PrismHarness 页面，
 * 抓 console 报错 / 未捕获异常 / 失败请求，并在真 DOM 里驱动一次团队面板渲染。
 *
 * 需要 server 已在 127.0.0.1:8765 运行（脚本会自己起；起不来就跳过）。
 * 运行：node tests/test_console_browser.js
 */
'use strict';
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const APP_URL = process.env.PH_URL || 'http://127.0.0.1:8765';
const ROOT = path.join(__dirname, '..');
const PY = process.env.PH_PYTHON || 'python';

const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  path.join(os.homedir(), 'AppData\\Local\\Google\\Chrome\\Application\\chrome.exe'),
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];

const sleep = ms => new Promise(r => setTimeout(r, ms));

function findBrowser() {
  for (const p of CHROME_CANDIDATES) if (fs.existsSync(p)) return p;
  return null;
}

async function waitForServer(timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(APP_URL + '/api/flags');
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
    await sleep(500);
  }
  return false;
}

function cdp(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    const listeners = [];
    ws.addEventListener('open', () => resolve({
      send(method, params) {
        return new Promise((res, rej) => {
          const mid = ++id;
          pending.set(mid, { res, rej });
          ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
        });
      },
      on(fn) { listeners.push(fn); },
      close() { try { ws.close(); } catch (e) {} },
    }));
    ws.addEventListener('error', e => reject(new Error('CDP 连接失败: ' + (e.message || e))));
    ws.addEventListener('message', ev => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.id && pending.has(msg.id)) {
        const { res, rej } = pending.get(msg.id);
        pending.delete(msg.id);
        msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
      } else if (msg.method) {
        listeners.forEach(fn => fn(msg));
      }
    });
  });
}

async function main() {
  const browser = findBrowser();
  if (!browser) {
    console.log('⚠ 本机没找到 Chrome/Edge，跳过浏览器冒烟。');
    process.exit(0);
  }

  // 1) 确保 server 在跑（不在就自己起一个，结束后关掉）
  let serverProc = null;
  if (!(await waitForServer(1500))) {
    console.log('· 启动 server…');
    serverProc = spawn(PY, ['server.py'], { cwd: ROOT, stdio: 'ignore', detached: false });
    if (!(await waitForServer(30000))) {
      console.log('✗ server 没起来，跳过浏览器冒烟。');
      console.log('  多半是 ' + PY + ' 没装 agentscope（要用 launch.bat 那个 conda 环境）。试试：');
      console.log('  PH_PYTHON="<conda环境的python.exe路径>" node tests/test_console_browser.js');
      console.log('  或先手动跑起 server（launch.bat），再执行本测试。');
      try { serverProc.kill(); } catch (e) {}
      process.exit(0);
    }
  }

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ph-chrome-'));
  const port = 9333;
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=' + port, '--remote-allow-origins=*',
    '--user-data-dir=' + profile, 'about:blank',
  ], { stdio: 'ignore' });

  const cleanup = () => {
    try { proc.kill(); } catch (e) {}
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
    if (serverProc) { try { serverProc.kill(); } catch (e) {} }
  };
  process.on('exit', cleanup);

  try {
    // 2) 找到 page target
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
      await sleep(250);
      try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        target = list.find(t => t.type === 'page');
      } catch (e) { /* 还没起来 */ }
    }
    if (!target) throw new Error('连不上 DevTools（浏览器没起来？）');

    const client = await cdp(target.webSocketDebuggerUrl);
    const problems = [];
    client.on(msg => {
      if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params.exceptionDetails;
        problems.push('未捕获异常: ' + (d.exception && d.exception.description || d.text));
      } else if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) {
        problems.push('console.' + msg.params.type + ': ' +
          msg.params.args.map(a => a.value || a.description || a.type).join(' '));
      } else if (msg.method === 'Log.entryAdded' && ['error', 'warning'].includes(msg.params.entry.level)) {
        problems.push('log.' + msg.params.entry.level + ': ' + msg.params.entry.text);
      } else if (msg.method === 'Network.loadingFailed' && !msg.params.canceled) {
        problems.push('请求失败: ' + msg.params.errorText);
      }
    });

    await client.send('Runtime.enable');
    await client.send('Log.enable');
    await client.send('Page.enable');
    await client.send('Network.enable');

    const loaded = new Promise(res => client.on(m => { if (m.method === 'Page.loadEventFired') res(); }));
    await client.send('Page.navigate', { url: APP_URL });
    await loaded;
    await sleep(1200);   // 让首屏的异步 fetch（会话/配置）跑完

    const evalJs = async expr => {
      const r = await client.send('Runtime.evaluate', {
        expression: expr, returnByValue: true, awaitPromise: true,
      });
      if (r.exceptionDetails) {
        throw new Error('页面求值抛错: ' + (r.exceptionDetails.exception || {}).description);
      }
      return r.result.value;
    };

    const out = {};
    out.title = await evalJs('document.title');

    // 团队菜单能加载出全部团队（走真实 /api/teams），且菜单里不含输入框、条目不被裁掉
    out.teams = await evalJs(`(async () => {
      await renderTeamMenu();
      const menu = document.getElementById('teamMenu');
      menu.classList.add('open');              // 量布局前必须可见（开关走 .open 类，不写内联 display）
      const list = document.getElementById('teamMenuList');
      const items = list.querySelectorAll('.team-menu-item');
      const api = await (await fetch('/api/teams')).json();
      const m = menu.getBoundingClientRect(), l = list.getBoundingClientRect();
      // 列表若被父级裁切，底部条目就永远够不着（表现为「菜单里只有 8 个团队」）
      const clipped = l.bottom > m.bottom + 1;
      const canScroll = list.scrollHeight > list.clientHeight + 1;
      list.scrollTop = list.scrollHeight;      // 滚到底，看最后一项是否真的露出来
      const last = items[items.length - 1];
      const lb = last.getBoundingClientRect();
      const lastReachable = lb.height > 4 && lb.bottom <= l.bottom + 1 && lb.top >= l.top - 1;
      const res = {
        count: items.length,
        apiCount: (api.teams || []).length,
        menuInputs: menu.querySelectorAll('input,textarea,select').length,
        clipped, canScroll, lastReachable,
        menuH: Math.round(m.height), listH: Math.round(l.height), scrollH: list.scrollHeight,
      };
      list.scrollTop = 0;
      closeTeamMenu();                         // 量完复原，别影响后续步骤
      return res;
    })()`);

    // 关键：输入区此前有「聊天输入框 + 团队任务输入框」两个框，现在只应剩一个
    out.inputs = await evalJs(`(() => {
      const area = document.querySelector('.chat-input-area');
      return {
        total: area.querySelectorAll('input,textarea,select').length,
        textareas: area.querySelectorAll('textarea').length,
      };
    })()`);

    // 在真 DOM 里驱动一次三阶段管线（6+1+1 位成员，足以触发跑完自动收起），
    // 检查面板真的渲染出来且样式生效
    out.panel = await evalJs(`(() => {
      const asstId = addAssistantMessage('', true);
      resetTeamWidget();
      const evs = [
        {phase:'start', stage:'pipeline'},
        {phase:'stage_start', stage:'s1', label:'🔭 三端扫描', index:0, total:3},
      ];
      ['龚几端','徐秋端','季数端','甲','乙','丙'].forEach((m,i) => {
        evs.push({phase:'member_start', stage:'s1', member:m, role:'扫描', color:'#abcdef'});
        evs.push({phase:'member_done', stage:'s1', member:m, bytes:1000+i, summary:m+'的摘要'});
      });
      evs.push({phase:'stage_done', stage:'s1'});
      evs.push({phase:'stage_start', stage:'s2', label:'🔗 因果验证', index:1, total:3});
      evs.push({phase:'member_start', stage:'s2', member:'阴果验', color:'#fedcba'});
      evs.push({phase:'member_error', stage:'s2', member:'阴果验', error:'超时'});
      evs.push({phase:'stage_done', stage:'s2'});
      evs.push({phase:'stage_start', stage:'s3', label:'🎯 评级输出', index:2, total:3});
      evs.push({phase:'member_start', stage:'s3', member:'平定级', color:'#0af'});
      evs.push({phase:'member_done', stage:'s3', member:'平定级', bytes:999, summary:'评级报告'});
      evs.push({phase:'stage_done', stage:'s3'});
      evs.push({phase:'done'});
      evs.forEach(e => handleTeamEvent(e, asstId));
      const panel = document.querySelector('.team-panel');
      if (!panel) return { found: false };
      const toggle = panel.querySelector('.tp-toggle');
      return {
        found: true,
        text: panel.textContent.replace(/\\s+/g,' ').trim().slice(0, 120),
        hasToggle: !!toggle,
        toggleClickable: toggle ? getComputedStyle(toggle).cursor === 'pointer' : false,
        chipCount: panel.querySelectorAll('.tp-step').length,
        chipDone: panel.querySelectorAll('.tp-step.done').length,
        rowsWhenCollapsed: panel.querySelectorAll('.tp-row').length,
        statusBar: (document.getElementById('statusBadge')||{}).textContent,
        panelBg: getComputedStyle(panel).backgroundColor,
      };
    })()`);

    // 点「展开成员」→ 成员行与展开摘要都应出现（8 位成员）
    out.expanded = await evalJs(`(() => {
      const panel = document.querySelector('.team-panel');
      panel.querySelector('.tp-toggle').click();
      const rows = panel.querySelectorAll('.tp-row');
      const firstRow = rows[0];
      if (firstRow) firstRow.click();
      const expandedBox = panel.querySelector('.tp-expand');
      return {
        rows: panel.querySelectorAll('.tp-row').length,
        hasArrow: !!panel.querySelector('.tp-arrow'),
        summaryShown: !!expandedBox,
        summaryText: expandedBox ? expandedBox.textContent.slice(0, 40) : '',
      };
    })()`);

    // 3) 结果
    const checks = [
      ['页面标题正确', out.title && out.title.includes('PrismHarness')],
      ['团队菜单与 /api/teams 一致且不少于 15 个',
        out.teams && out.teams.count === out.teams.apiCount && out.teams.count >= 15],
      ['团队菜单是覆盖层、里面没有输入框', out.teams && out.teams.menuInputs === 0],
      ['团队菜单条目没被裁掉（最后一个也能滚到）',
        out.teams && !out.teams.clipped && out.teams.lastReachable],
      ['聊天输入区只剩一个输入框', out.inputs && out.inputs.total === 1],
      ['团队面板渲染出来了', out.panel && out.panel.found],
      ['面板显示类型与阶段 3/3', out.panel.text.includes('多阶段管线') && out.panel.text.includes('阶段 3/3')],
      ['3 个阶段 chip 全部已完成', out.panel.chipCount === 3 && out.panel.chipDone === 3],
      ['跑完自动收起成员明细', out.panel.rowsWhenCollapsed === 0 && out.panel.text.includes('成员明细已收起')],
      ['收起按钮可点', out.panel.hasToggle && out.panel.toggleClickable],
      ['面板背景样式生效', out.panel.panelBg && out.panel.panelBg !== 'rgba(0, 0, 0, 0)'],
      ['状态栏显示当前阶段', /阶段 3\/3/.test(out.panel.statusBar || '')],
      ['点击展开后出现 8 行成员', out.expanded.rows === 8],
      ['成员行可展开看摘要', out.expanded.hasArrow && out.expanded.summaryShown],
      ['浏览器无 console 报错/异常', problems.length === 0],
    ];

    // 4) 可选：真实点「🎭 团队」跑一次（会真调模型，PH_LIVE=1 才启用）
    if (process.env.PH_LIVE === '1') {
      console.log('\n· 真实跑一次团队（点 🎭 → 选评审团 → 运行）…');
      // 先清掉上面合成用例留下的面板，避免后面 querySelector 抓到旧的那个
      await evalJs(`(() => {
        document.querySelectorAll('.team-panel').forEach(el => {
          const msg = el.closest('.msg');
          if (msg) msg.remove(); else el.remove();
        });
        return document.querySelectorAll('.team-panel').length;
      })()`);
      await evalJs(`document.getElementById('teamQuickBtn').click(); true`);
      await sleep(700);
      const picked = await evalJs(`(() => {
        const items = [...document.querySelectorAll('#teamMenuList .team-menu-item')];
        const hit = items.find(el => (el.querySelector('.tmi-name')||{}).textContent === '评审团') || items[0];
        hit.click();
        document.getElementById('userInput').value = '一句话：要不要做这个功能？';
        return document.getElementById('teamModeName').textContent;
      })()`);
      console.log('  选中团队:', picked);

      // 团队模式条应出现，且输入区仍然只有一个输入框
      const modeUi = await evalJs(`(() => {
        const bar = document.getElementById('teamModeBar');
        const area = document.querySelector('.chat-input-area');
        const menu = document.getElementById('teamMenu');
        return {
          barVisible: getComputedStyle(bar).display !== 'none',
          chip: bar.textContent.replace(/\\s+/g,' ').trim(),
          placeholder: document.getElementById('userInput').placeholder,
          btnText: document.getElementById('sendBtn').textContent,
          inputCount: area.querySelectorAll('input,textarea,select').length,
          menuClosed: getComputedStyle(menu).display === 'none',
        };
      })()`);
      checks.push(['选团队后出现模式条（只剩一个输入框）', modeUi.barVisible && modeUi.inputCount === 1]);
      checks.push(['输入框切换成任务提示', /任务/.test(modeUi.placeholder)]);
      checks.push(['按钮变成「运行」', /运行/.test(modeUi.btnText)]);
      checks.push(['选完自动关闭菜单', modeUi.menuClosed]);
      console.log('  模式条:', modeUi.chip);
      console.log('  输入框提示:', modeUi.placeholder, '| 按钮:', modeUi.btnText, '| 输入控件数:', modeUi.inputCount);

      // 真的按回车触发运行（走 onInputKey 这条真实路径）
      await evalJs(`(() => {
        document.getElementById('userInput')
          .dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
        return true;
      })()`);
      await sleep(500);
      const afterSend = await evalJs(`(() => ({
        modeExited: getComputedStyle(document.getElementById('teamModeBar')).display === 'none',
        btn: document.getElementById('sendBtn').textContent,
      }))()`);
      checks.push(['回车后自动退出团队模式', afterSend.modeExited && /发送/.test(afterSend.btn)]);

      const t0 = Date.now();
      let done = false;
      while (Date.now() - t0 < 240000) {
        await sleep(3000);
        const st = await evalJs(`(() => {
          const bubbles = document.querySelectorAll('#chatMessages .msg.assistant .bubble');
          const last = bubbles[bubbles.length - 1];
          return {
            status: (document.getElementById('statusBadge')||{}).textContent || '',
            text: last ? last.textContent.trim() : '',
          };
        })()`);
        if ((st.status.includes('完成') || st.status.includes('已停止')) && st.text) { done = true; break; }
      }

      const live = await evalJs(`(() => {
        const panels = document.querySelectorAll('.team-panel');
        const panel = panels[panels.length - 1];          // 最后一次运行的面板
        const bubbles = document.querySelectorAll('#chatMessages .msg.assistant .bubble');
        const lastBubble = bubbles[bubbles.length - 1];
        return {
          panelCount: panels.length,
          panelText: panel ? panel.textContent.replace(/\\s+/g,' ').trim().slice(0,160) : '',
          memberRows: panel ? panel.querySelectorAll('.tp-row').length : 0,
          reportText: lastBubble ? lastBubble.textContent.trim() : '',
        };
      })()`);

      checks.push(['真实运行：只出现一个团队面板（未抓到残留）', live.panelCount === 1]);
      checks.push(['真实运行：面板是「并行圆桌」且含耗时',
        live.panelText.includes('并行圆桌') && /耗时 \d+s/.test(live.panelText)]);
      checks.push(['真实运行：面板含 2 位成员', live.memberRows === 2]);
      checks.push(['真实运行：产出最终报告', done && live.reportText.length > 40]);
      checks.push(['真实运行：过程无 console 报错', problems.length === 0]);
      console.log('  面板:', live.panelText);
      console.log('  成员行数:', live.memberRows, '| 报告长度:', live.reportText.length, '| 状态:', done ? '完成' : '超时');
      console.log('  报告开头:', live.reportText.replace(/\s+/g, ' ').slice(0, 160));
    }

    console.log('真实浏览器冒烟（' + path.basename(browser) + '）');
    console.log('  标题:', out.title);
    console.log('  团队下拉:', JSON.stringify(out.teams));
    console.log('  面板文本:', out.panel.text);
    console.log('  状态栏:', out.panel.statusBar);
    console.log('');
    let failed = 0;
    for (const [name, ok] of checks) { if (!ok) failed++; console.log('  ' + (ok ? '✓ ' : '✗ ') + name); }
    if (problems.length) {
      console.log('\n  浏览器报错/警告:');
      problems.slice(0, 20).forEach(p => console.log('    · ' + p));
    }
    console.log('\n通过 ' + (checks.length - failed) + ' 项，失败 ' + failed + ' 项');

    client.close();
    cleanup();
    process.exit(failed ? 1 : 0);
  } catch (e) {
    console.log('✗ 浏览器冒烟没能跑完: ' + e.message);
    cleanup();
    process.exit(2);
  }
}

main();

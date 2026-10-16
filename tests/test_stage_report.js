/**
 * 阶段产出卡片（圆桌的「风险管理师评估」）的无头回归测试。
 *
 * 从 index.html 抽出三段 JS——markdown 渲染、报告卡片、团队面板——配一套最小 DOM 桩，
 * 驱动 renderStageReport，断言生成的卡片。
 *
 * 背景：风控阶段跑完、内容也喂给了决策，但正文从没发到界面；用户只看到
 * 「✅ 风险管理师评估风险…」后面空无一物。现在它单独成卡，排在团队面板与
 * 最终报告之间。
 *
 * 运行：node tests/test_stage_report.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const INDEX = path.join(__dirname, '..', 'index.html');
if (!fs.existsSync(INDEX)) {
  console.error('找不到 index.html：' + INDEX);
  process.exit(2);
}
const html = fs.readFileSync(INDEX, 'utf8');
const script = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).join('\n');

// 从 startAnchor 切到 endAnchor；endFrom 指定 endAnchor 从哪个位置往后找
// （同一段里可能多次出现同一个收尾锚点）。
function slice(startAnchor, endAnchor, endFrom) {
  const a = script.indexOf(startAnchor);
  if (a < 0) throw new Error('找不到起点：' + startAnchor);
  const b = script.indexOf(endAnchor, endFrom != null ? endFrom : a);
  if (b < 0) throw new Error('找不到终点：' + endAnchor);
  return script.slice(a, b);
}

// 三段拼起来：escapeHtml/renderMarkdown → 报告标记与卡片 → 团队面板（含 renderStageReport）
const mdSrc = slice('function escapeHtml', '\nfunction ', script.indexOf('return out.join'));
const cardSrc = slice('const REPORT_TAG_SRC', 'function addAssistantMessage');
const widgetSrc = slice('let teamWidgetEl = null;', '\nfunction ',
  script.indexOf('box.innerHTML = html;'));

// ---- 最小 DOM 桩 ----
const made = [];
const parent = { id: 'asst-1', inserted: [], insertBefore(el) { this.inserted.push(el); }, appendChild(el) { this.inserted.push(el); } };
const bubble = { id: 'asst-1-bubble' };
globalThis.document = {
  getElementById: id => (id === 'asst-1' ? parent : (id === 'asst-1-bubble' ? bubble : null)),
  createElement: () => {
    const el = { className: '', innerHTML: '', dataset: {}, style: {},
      addEventListener() {}, insertBefore() {}, querySelector() { return null; } };
    made.push(el);
    return el;
  },
};
globalThis.scrollChat = () => {};
globalThis.setStatus = () => {};
globalThis.escapeHtml = s => (!s ? '' : String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));
globalThis.escapeAttr = globalThis.escapeHtml;

// eslint-disable-next-line no-eval
eval(mdSrc + '\n' + cardSrc + '\n' + widgetSrc +
  '\n;globalThis.STAGE = renderStageReport;globalThis.ROUTE = handleTeamEvent;' +
  '\n;globalThis.CLEAR = () => { Object.keys(stageReportEls).forEach(k => delete stageReportEls[k]); };');

const results = [];
const check = (name, cond, extra) => { results.push([name, !!cond, extra]); };

const riskText = '1. **整体风险等级：高**\n\n| 约束项 | 数值 |\n|--------|------|\n| 仓位上限 | 55% |\n\n[风险评估报告]';

// ---- 1. 带标记的阶段正文渲染成卡片 ----
STAGE('asst-1', { phase: 'stage_report', stage: 'risk', label: '风险管理师评估', text: riskText });
const el = made[made.length - 1];
check('生成了阶段产出元素', !!el && el.className === 'stage-report', el && el.className);
check('内部是可折叠报告卡片', el.innerHTML.includes('report-card') && el.innerHTML.includes('report-head'));
check('卡片标题取正文里的标记', el.innerHTML.includes('风险评估报告'), el.innerHTML.slice(0, 120));
check('正文里的标记已去掉', !el.innerHTML.includes('[风险评估报告]'));
check('表格被渲染成 <table>', el.innerHTML.includes('<table>') && el.innerHTML.includes('<td>55%</td>'));
check('插在最终报告（气泡）之前', parent.inserted[0] === el);

// ---- 2. 没带标记时退回用 label 当标题 ----
made.length = 0;
parent.inserted.length = 0;
STAGE('asst-1', { phase: 'stage_report', stage: 'audit', label: '合规审查', text: '结论：无异常。' });
const el2 = made[made.length - 1];
check('无标记时用 label 当标题', el2.innerHTML.includes('合规审查'), el2.innerHTML.slice(0, 120));
check('无标记时正文原样渲染', el2.innerHTML.includes('结论：无异常。'));
check('无标记时不硬套标记样式', !el2.innerHTML.includes('[阶段报告]'));

// ---- 3. 同一阶段重复到达时复用同一节点（不堆叠） ----
const before = made.length;
STAGE('asst-1', { phase: 'stage_report', stage: 'audit', label: '合规审查', text: '结论：有 1 处异常。' });
check('同阶段不重复创建节点', made.length === before, made.length + ' vs ' + before);
check('同阶段内容被更新', el2.innerHTML.includes('有 1 处异常'));

// ---- 4. 空正文不产生节点 ----
const n = made.length;
STAGE('asst-1', { phase: 'stage_report', stage: 'empty', label: '空', text: '' });
STAGE('asst-1', { phase: 'stage_report', stage: 'empty2' });
STAGE('asst-1', null);
check('空正文 / 缺 ev 都不建节点', made.length === n, made.length + ' vs ' + n);

// ---- 5. 事件路由：stage_report 要经 handleTeamEvent 落到卡片上 ----
// 后端发的是 {type:'team', phase:'stage_report', stage, label, text}，
// 前端按 phase 分派。这条链路断了的话，卡片逻辑再对也不会被触发。
CLEAR();                 // 上面的直调已登记过 asst-1::risk，清掉才能看出路由是否新建
made.length = 0;
parent.inserted.length = 0;
ROUTE({ phase: 'start', stage: 'roundtable', team: 'T' }, 'asst-1');
ROUTE({ phase: 'stage_start', stage: 'analysis', label: '分析', index: 0, total: 3 }, 'asst-1');
ROUTE({ phase: 'stage_report', stage: 'risk', label: '风险管理师评估', text: riskText }, 'asst-1');
const routed = made.filter(e => e.className === 'stage-report');
check('handleTeamEvent 把 stage_report 落到阶段卡片上', routed.length === 1, String(routed.length));
check('路由后的卡片内容正确',
  routed.length === 1 && routed[0].innerHTML.includes('风险评估报告')
  && routed[0].innerHTML.includes('<table>'),
  routed.length ? routed[0].innerHTML.slice(0, 100) : '(无)');

// ---- 汇总 ----
let failed = 0;
console.log('阶段产出卡片测试：');
for (const [name, ok, extra] of results) {
  if (!ok) failed++;
  console.log('  ' + (ok ? '✓ ' : '✗ ') + name + (ok || !extra ? '' : '  -> ' + extra));
}
console.log('\n通过 ' + (results.length - failed) + ' 项，失败 ' + failed + ' 项');
process.exit(failed ? 1 : 0);

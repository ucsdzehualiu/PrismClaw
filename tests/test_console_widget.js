/**
 * 团队面板（console 里的 🎭 实时小组件）的无头回归测试。
 *
 * 不需要浏览器：从 index.html 里抽出团队面板那段 JS，配一套最小 DOM 桩，
 * 喂一串真实的 SSE 事件序列，断言渲染出来的 HTML。
 *
 * 运行：node tests/test_console_widget.js
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

// 抽出团队面板那一段（从状态声明到 renderTeamWidget 结束）
const i = script.indexOf('let teamWidgetEl = null;');
const j = script.indexOf('function renderTeamWidget(asstId) {');
if (i < 0 || j < 0) {
  console.error('index.html 里找不到团队面板代码段（是不是改名/删掉了？）');
  process.exit(2);
}
const k = script.indexOf('\nfunction ', script.indexOf('box.innerHTML = html;', j));
const widgetSrc = script.slice(i, k < 0 ? undefined : k);

// ---- 最小 DOM / 全局桩 ----
const statuses = [];
globalThis.escapeHtml = s => (!s ? '' : String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));
globalThis.escapeAttr = s => escapeHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
globalThis.setStatus = (_type, text) => { statuses.push(text); };
globalThis.scrollChat = () => {};
const host = { innerHTML: '', firstChild: null, insertBefore() {} };
globalThis.document = {
  getElementById: id => (id === 'asst-1' ? host : null),
  createElement: () => ({
    className: '', innerHTML: '', dataset: {}, style: {},
    addEventListener() {}, insertBefore() {}, querySelector() { return null; },
  }),
};

// eslint-disable-next-line no-eval
eval(widgetSrc + '\n' +
  ';globalThis.H = handleTeamEvent;' +
  'globalThis.W = () => teamWidgetEl;' +
  'globalThis.SET_COLLAPSED = v => { teamCollapsed = v; };' +
  'globalThis.RERENDER = () => renderTeamWidget("asst-1");');

// ---- 断言小工具 ----
const results = [];
const check = (name, cond) => { results.push([name, !!cond]); };

// ---- 场景一：三阶段管线，跑完自动收起 ----
const emit = ev => H(ev, 'asst-1');
emit({ phase: 'start', stage: 'pipeline', team: '相信光么' });
emit({ phase: 'stage_start', stage: 's1', label: '🔭 三端扫描', index: 0, total: 3 });
['甲', '乙', '丙', '丁', '戊', '己'].forEach((m, n) => {
  emit({ phase: 'member_start', stage: 's1', member: m, role: 'r', color: '#abc' });
  emit({ phase: 'member_done', stage: 's1', member: m, bytes: 100 + n, summary: m + '摘要' });
});
emit({ phase: 'stage_done', stage: 's1' });
emit({ phase: 'stage_start', stage: 's2', label: '🔗 因果验证', index: 1, total: 3 });
emit({ phase: 'member_start', stage: 's2', member: '阴果验', color: '#def' });
emit({ phase: 'member_error', stage: 's2', member: '阴果验', error: '超时' });
emit({ phase: 'stage_done', stage: 's2' });
emit({ phase: 'stage_start', stage: 's3', label: '🎯 评级输出', index: 2, total: 3 });
emit({ phase: 'member_start', stage: 's3', member: '平定级', color: '#0af' });
emit({ phase: 'member_done', stage: 's3', member: '平定级', bytes: 999, summary: '评级报告' });
emit({ phase: 'stage_done', stage: 's3' });
emit({ phase: 'done' });

const collapsed = W().innerHTML;
check('标题显示团队类型', collapsed.includes('多阶段管线'));
check('标题显示团队名', collapsed.includes('相信光么'));
check('阶段进度用服务端总数（3/3）', collapsed.includes('阶段 3/3'));
check('显示耗时', /耗时 \d+s/.test(collapsed));
check('三个阶段各一个 chip', (collapsed.match(/class="tp-step[ ">]/g) || []).length === 3);
check('跑完自动收起成员明细', collapsed.includes('成员明细已收起'));
check('收起统计正确（✅7 ❌1）', collapsed.includes('✅ 7') && collapsed.includes('❌ 1'));
check('收起时不渲染成员行', !collapsed.includes('data-member='));
check('显示完成提示', collapsed.includes('最终报告见下方'));
check('状态栏随阶段推进 1/3 → 3/3',
  /阶段 1\/3/.test(statuses.join(' ')) && /阶段 3\/3/.test(statuses.join(' ')));

// ---- 场景二：点「展开成员」后明细恢复 ----
SET_COLLAPSED(false);
RERENDER();
const expanded = W().innerHTML;
check('展开后 8 行成员明细', (expanded.match(/data-member=/g) || []).length === 8);
check('展开后不再显示收起提示', !expanded.includes('成员明细已收起'));
check('失败成员行高亮', expanded.includes('tp-row error'));
check('成员产出可展开查看', expanded.includes('tp-arrow'));

// ---- 场景三：同一成员出现在两个阶段时用阶段名区分 ----
emit({ phase: 'start', stage: 'pipeline' });
emit({ phase: 'member_start', stage: 's1', member: '谭溯源', color: '#111' });
emit({ phase: 'stage_start', stage: 's1', label: '初调', index: 0, total: 2 });
emit({ phase: 'stage_start', stage: 's2', label: '逐章', index: 1, total: 2 });
emit({ phase: 'member_start', stage: 's2', member: '谭溯源', color: '#111' });
const dup = W().innerHTML;
check('同名成员跨阶段各占一行', (dup.match(/data-member=/g) || []).length === 2);
check('同名行补阶段名区分', dup.includes('谭溯源（初调）') && dup.includes('谭溯源（逐章）'));

// ---- 场景四：成员名里的引号不能截断属性 ----
emit({ phase: 'start', stage: 'parallel' });
emit({ phase: 'member_start', member: 'a"b<c>', color: '#222' });
const quoted = W().innerHTML;
check('成员名引号被转义（不破坏属性）', quoted.includes('&quot;') && !quoted.includes('data-member="a"b'));

// ---- 汇总 ----
let failed = 0;
console.log('团队面板渲染测试：');
for (const [name, ok] of results) {
  if (!ok) failed++;
  console.log('  ' + (ok ? '✓ ' : '✗ ') + name);
}
console.log('\n通过 ' + (results.length - failed) + ' 项，失败 ' + failed + ' 项');
process.exit(failed ? 1 : 0);

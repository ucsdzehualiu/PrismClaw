/**
 * 报告卡片的无头回归测试。
 *
 * 不需要浏览器：从 index.html 里抽出「报告标记识别 + 卡片渲染」那段 JS，
 * 只把 renderMarkdown / escapeHtml 换成桩，断言识别结果。
 *
 * 背景：各团队的产出标记措辞并不统一（[最终投资决策]、[最终交易决策]、
 * [交易员决策]、[新闻分析报告]…），而判定原先是一张写死的白名单——
 * 「[最终投资决策]」不在表里，于是最终决策一直以纯文本出现，没有卡片。
 *
 * 运行：node tests/test_report_card.js
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

const i = script.indexOf('const REPORT_TAG_SRC');
const j = script.indexOf('function addAssistantMessage');
if (i < 0 || j < 0) {
  console.error('index.html 里找不到报告卡片代码段（REPORT_TAG_SRC / addAssistantMessage 改名了？）');
  process.exit(2);
}
const src = script.slice(i, j);

// ---- 最小桩 ----
globalThis.escapeHtml = s => (!s ? '' : String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'));
globalThis.renderMarkdown = t => '<md>' + t + '</md>';

// eslint-disable-next-line no-eval
eval(src + '\n;globalThis.IS_REPORT = isReportText;' +
  'globalThis.TAG_OF = reportTagOf;' +
  'globalThis.BODY = reportBodyOf;' +
  'globalThis.RENDER = renderAssistantContent;');

const results = [];
const check = (name, cond) => { results.push([name, !!cond]); };

// ---- 各团队实际在用的产出标记，全部要渲染成卡片 ----
// 这几个名字都是从 teams/*.json 的 prompt 里抄出来的
for (const tag of ['[最终投资决策]', '[最终决策报告]', '[最终交易决策]',
  '[交易员决策]', '[风险评估报告]', '[新闻分析报告]', '[情绪分析报告]']) {
  const body = '## 结论\n\nHOLD（持有观望）\n\n' + tag;
  check(tag + ' 渲染成卡片', IS_REPORT(body) && RENDER(body).includes('report-card'));
}

// ---- 标题取的是实际标记，不是写死的「报告」 ----
check('卡片标题用实际标记', TAG_OF('结论\n\n[最终投资决策]') === '最终投资决策',
  TAG_OF('结论\n\n[最终投资决策]'));

// ---- 不该误判成卡片 ----
check('普通回答不套卡片', !RENDER('看好 AI 周期，建议逢低分批建仓。').includes('report-card'));
check('不含决策/报告的方括号不误判',
  !IS_REPORT('[已取消]') && !IS_REPORT('[待确认]') && !IS_REPORT('[1/3]'));
check('空内容不崩', RENDER('') === '<md></md>' && !IS_REPORT(null));

// ---- 报告尾部附带的「跑满迭代上限」提示不能破坏卡片识别 ----
const withWarn = '## 结论\n\nHOLD\n\n[最终投资决策]\n\n---\n\n' +
  '⚠️ **以下 1 位成员跑满迭代上限**，其分析为强制收尾产出、可能不完整：\n- 华尔街斗牛士';
check('附带超限提示的报告仍渲染成卡片',
  IS_REPORT(withWarn) && RENDER(withWarn).includes('report-card'));

// ---- 成员面板那一行给的是「正在分析中」之类的普通文本，不该套卡片 ----
check('团队面板的中间摘要不套卡片', !IS_REPORT('⏳ 分析中… 已完成 3，排队 16'));

// ---- 产出标记是机器标记，不该出现在卡片正文里 ----
// 用户看到的「好像没写完」，就是报告正文末尾孤零零挂着的一行 [最终决策报告]。
const realReport = '**1. 结论**  \nHOLD（信心：中低）\n\n**2. 理由**  \n分歧在于是否主动降仓。\n\n' +
  '- **入场**：科创50企稳（分批回补）  \n- **止损**：跌破前低清仓  \n\n[最终决策报告]';
check('正文里的产出标记被去掉', !RENDER(realReport).includes('[最终决策报告]'));
check('去掉标记后正文内容仍在', RENDER(realReport).includes('HOLD（信心：中低）')
  && RENDER(realReport).includes('跌破前低清仓'));
check('标记前的硬换行（行尾两空格）不被误删',
  BODY(realReport).includes('**1. 结论**  \n'), JSON.stringify(BODY(realReport).slice(0, 20)));
check('去掉标记后不留多余空行', !/\n{3,}/.test(BODY(realReport)), JSON.stringify(BODY(realReport).slice(-30)));

// ---- 被反引号包着的标记：不能剩下空反引号 ----
// prompt 常写成 `[最终投资决策]`。只删方括号会留下两个空反引号「 `` 」，
// 在卡片里就像一句没写完的残句——用户报的「好像没写完」正是这个。
const ticked = '结论：HOLD\n\n---\n\n`[最终投资决策]`\n\n**决策：HOLD**';
check('反引号包着的标记被整段去掉', !BODY(ticked, '最终投资决策').includes('`'),
  JSON.stringify(BODY(ticked, '最终投资决策')));
check('不残留空反引号', !BODY(ticked, '最终投资决策').includes('``'));
check('去标记后正文完好', BODY(ticked, '最终投资决策').includes('**决策：HOLD**'));

// ---- 卡片正文的首尾装饰 ----
check('开头的 --- 分隔线被去掉',
  BODY('---\n\n正文', null) === '正文', JSON.stringify(BODY('---\n\n正文', null)));
check('结尾的 --- 分隔线被去掉',
  BODY('正文\n\n---', null) === '正文', JSON.stringify(BODY('正文\n\n---', null)));
check('与卡片标题重复的一级标题被去掉',
  BODY('# 📊 最终投资决策报告\n\n正文', '最终投资决策') === '正文',
  JSON.stringify(BODY('# 📊 最终投资决策报告\n\n正文', '最终投资决策')));
check('不重复的一级标题保留',
  BODY('# 别的标题\n\n正文', '最终投资决策').includes('别的标题'),
  JSON.stringify(BODY('# 别的标题\n\n正文', '最终投资决策')));

// ---- 一条消息里拼进多个标记时，用最后一个当标题 ----
// 圆桌会把风险评估和最终决策拼进同一条消息，最后一行才是这份内容的定性。
const combined = '1. 风险等级：高\n\n[风险评估报告]\n\n---\n\n1. 结论：HOLD\n\n[最终决策报告]';
check('多个标记时取最后一个当标题', TAG_OF(combined) === '最终决策报告', TAG_OF(combined));
check('多个标记的正文全部去掉',
  !BODY(combined).includes('[风险评估报告]') && !BODY(combined).includes('[最终决策报告]'));
check('多标记仍渲染成一张卡片', RENDER(combined).includes('report-card'));

// ---- 汇总 ----
let failed = 0;
console.log('报告卡片渲染测试：');
for (const [name, ok] of results) {
  if (!ok) failed++;
  console.log('  ' + (ok ? '✓ ' : '✗ ') + name);
}
console.log('\n通过 ' + (results.length - failed) + ' 项，失败 ' + failed + ' 项');
process.exit(failed ? 1 : 0);

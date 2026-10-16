/**
 * markdown 渲染器的无头回归测试。
 *
 * 从 index.html 抽出 escapeHtml + renderMarkdown 直接跑，不用浏览器。
 *
 * 背景：旧渲染器只认代码块 / 粗体 / ## / ###。团队报告里最吃重的**表格**，
 * 以及 # 标题、> 引用、--- 分隔线，全部原样漏成记号——报告越结构化，卡片里
 * 看着越像一堆没渲染的文本。
 *
 * 运行：node tests/test_markdown.js
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

const i = script.indexOf('function escapeHtml');
const j = script.indexOf('function renderMarkdown');
if (i < 0 || j < 0) {
  console.error('index.html 里找不到 escapeHtml / renderMarkdown');
  process.exit(2);
}
const k = script.indexOf('\nfunction ', script.indexOf('return out.join', j));
const src = script.slice(i, k < 0 ? undefined : k);

// eslint-disable-next-line no-eval
eval(src + '\n;globalThis.MD = renderMarkdown;');

const results = [];
const check = (name, cond, extra) => { results.push([name, !!cond, extra]); };

// ---- 一份贴近真实团队报告的样本 ----
const sample = [
  '---',
  '# 📊 最终投资决策报告',
  '',
  '**决策日期**：2026年10月16日  ',
  '**决策者**：投资组合经理',
  '',
  '---',
  '',
  '## 第一步：信号汇总',
  '',
  '| 类别 | 数量 | 占比 |',
  '|------|------|------|',
  '| 🟢 看多 | 12 | 63% |',
  '| 🔴 看空 | 1 | 5% |',
  '',
  '```',
  '加权得分 = 12 × 0.72 = 8.64',
  '```',
  '',
  '> 方向看多，时机等待；不割肉优质股，不急于重仓。',
  '',
  '- 不割肉优质股',
  '- 不急于重仓',
  '',
  '1. 第一步',
  '2. 第二步',
].join('\n');
const out = MD(sample);

// ---- 表格（旧渲染器完全没有这个能力） ----
check('表格渲染成 <table>', out.includes('<table>') && out.includes('</table>'));
check('表头进 <th>', out.includes('<th>类别</th>') && out.includes('<th>占比</th>'));
check('数据行进 <td>', out.includes('<td>12</td>') && out.includes('<td>63%</td>'));
check('表格行数正确（2 行数据）', (out.match(/<tr>/g) || []).length === 3,
  String((out.match(/<tr>/g) || []).length));
check('表格包在可横向滚动的容器里', out.includes('md-table'));
check('分隔行 |---| 不会漏成文本', !out.includes('|---') && !out.includes('|------'));

// ---- 标题 / 分隔线 / 引用 ----
check('# 渲染成 h2', out.includes('<h2>📊 最终投资决策报告</h2>'));
check('## 渲染成 h3（保持原有层级）', out.includes('<h3>第一步：信号汇总</h3>'));
check('--- 渲染成 <hr>', (out.match(/<hr>/g) || []).length === 2,
  String((out.match(/<hr>/g) || []).length));
check('> 渲染成 <blockquote>', out.includes('<blockquote>方向看多，时机等待；不割肉优质股，不急于重仓。</blockquote>'));
check('引用不再漏成 &gt;', !out.includes('&gt; 方向看多'));

// ---- 代码块 / 列表 / 行内 ----
check('围栏代码块渲染成 pre>code',
  out.includes('<pre><code>加权得分 = 12 × 0.72 = 8.64</code></pre>'));
check('无序列表', out.includes('<ul><li>不割肉优质股</li><li>不急于重仓</li></ul>'));
check('有序列表', out.includes('<ol><li>第一步</li><li>第二步</li></ol>'));
check('粗体', out.includes('<strong>决策日期</strong>'));
check('行内代码', MD('用 `run_shell` 跑').includes('<code>run_shell</code>'));
check('斜体', MD('这是 *强调* 文字').includes('<em>强调</em>'));

// ---- 别把普通星号当斜体 ----
// 报告里到处是 "12 × 0.72"、"a * b * c" 这种算式，误判成斜体会把句子切碎。
check('算式里的星号不误判成斜体', !MD('10 * 5 * 2').includes('<em>'), MD('10 * 5 * 2'));

// ---- 安全：先转义再拼标签，注入不进去 ----
check('HTML 标签被转义', !MD('<img src=x onerror=alert(1)>').includes('<img'));
check('script 被转义', !MD('<script>alert(1)</script>').includes('<script'));
check('表格单元格里的 HTML 同样被转义',
  !MD('| a |\n|---|\n| <b>x</b> |').includes('<b>'), MD('| a |\n|---|\n| <b>x</b> |'));

// ---- 边界 ----
check('空输入不崩', MD('') === '' && MD(null) === '');
const unclosed = MD('```\ncode here\n没有收尾围栏');
check('未闭合的代码围栏不卡死且保留内容',
  unclosed.includes('<pre>') && unclosed.includes('code here'), unclosed);
check('只有分隔线时不产出内容块', MD('---') === '<hr>', MD('---'));

// ---- 汇总 ----
let failed = 0;
console.log('markdown 渲染测试：');
for (const [name, ok, extra] of results) {
  if (!ok) failed++;
  console.log('  ' + (ok ? '✓ ' : '✗ ') + name + (ok || !extra ? '' : '  -> ' + extra));
}
console.log('\n通过 ' + (results.length - failed) + ' 项，失败 ' + failed + ' 项');
process.exit(failed ? 1 : 0);

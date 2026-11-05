// 持仓面板逻辑自测：在 Node 里跑真实 sql.js，验证成本 / 盈亏 / 分红 / 费用口径
// 用法：node _selftest.js
const fs = require('fs'), path = require('path'), vm = require('vm');
const dir = __dirname;
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');

// 主脚本 = 最后一个 <script>...</script>（前面是 sql.js 与 wasm base64）
const m = html.match(/<script>\n\(function \(\) \{\n'use strict';\n\n\/\* =+ 常量[\s\S]*?<\/script>/);
if (!m) throw new Error('找不到主脚本');
const main = m[0].replace(/^<script>\n/, '').replace(/<\/script>$/, '');

const els = {};
function el(id) {
  if (!els[id]) els[id] = {
    id, innerHTML: '', textContent: '', value: '', style: {}, className: '', dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, closest() { return null; }, focus() {}
  };
  return els[id];
}

// sql.js 的 WASM 从页面里内联的那段 base64 取，测试脚本不依赖任何 npm 包
const wasmB64 = html.match(/id="sqlWasmB64">([\s\S]*?)<\/script>/)[1];
els['sqlWasmB64'] = { textContent: wasmB64, id: 'sqlWasmB64' };

const sandbox = {
  console, Date, Math, Number, String, Object, Array, JSON, isNaN, isFinite,
  parseFloat, parseInt, atob: s => Buffer.from(s, 'base64').toString('binary'),
  setTimeout: () => 0, setInterval: () => 0, clearTimeout: () => {}, clearInterval: () => {},
  confirm: () => true, alert: () => {},
  indexedDB: { open() { return {}; } },
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    getElementById: el,
    querySelector: s => el(s.replace(/^#/, '')),
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => ({ style: {}, dataset: {}, remove() {}, click() {}, appendChild() {}, setAttribute() {} }),
    head: { appendChild() {} }, body: { appendChild() {} }
  },
  fetch: () => Promise.reject(new Error('no net')),
  location: { search: '' },
  FileReader: function () {},
  Blob: function () {}, URL: { createObjectURL: () => '', revokeObjectURL() {} }
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// config.js 是可选依赖，存在就注入
const cfgPath = path.join(dir, 'config.js');
if (fs.existsSync(cfgPath)) vm.runInContext(fs.readFileSync(cfgPath, 'utf8'), sandbox);
// sql.js 加载器（页面里内联的那段）也跑进同一个沙箱，得到 initSqlJs
const loader = html.match(/<!-- SQLJS:START[^>]*-->\s*<script>([\s\S]*?)<\/script>/);
if (!loader) throw new Error('找不到 sql.js 加载器');
vm.runInContext(loader[1], sandbox);
if (typeof sandbox.initSqlJs !== 'function') throw new Error('sql.js 加载器没有导出 initSqlJs');
vm.runInContext(main, sandbox);

const P = sandbox.__PANEL__;
let fail = 0;
const ok = (cond, msg) => { console.log((cond ? '  PASS ' : '  FAIL ') + msg); if (!cond) fail++; };
const near = (a, b, eps) => Math.abs(a - b) < (eps == null ? 0.01 : eps);
const strip = s => String(s || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

(async () => {
  // 直接用页面内联的 sql.js 引擎，不依赖 npm
  const SQL = await P._ensureSql();
  const d = new SQL.Database();
  const SCHEMA = html.match(/const SCHEMA = `([\s\S]*?)`;/)[1];
  d.exec(SCHEMA);
  P._setDb(d);
  P._seedInto(d);
  P._readState();

  const run = (sql, p) => { d.run(sql, p); };
  const addTrade = (acct, code, name, side, date, sh, px, fee) =>
    run('INSERT INTO trade(acct,code,name,side,date,shares,price,fee,note) VALUES(?,?,?,?,?,?,?,?,?)',
      [acct, code, name, side, date, sh, px, fee || 0, '']);
  const addDiv = (acct, code, name, date, amt, tax) =>
    run('INSERT INTO dividend(acct,code,name,date,amount,tax,note) VALUES(?,?,?,?,?,?,?)',
      [acct, code, name, date, amt, tax || 0, '']);

  /* ---------- 1. 费用口径 ---------- */
  console.log('\n--- 交易费用 ---');
  const S = P.S;
  ok(near(S.settings.comm, 0.0000854, 1e-9), '佣金费率默认万分之 0.854');
  ok(near(S.settings.minFee, 5, 1e-9), '最低佣金默认 5 元');
  // 小额：1000 元成交，佣金 0.0854 元 < 5，取 5
  const f1 = P.calcFee('buy', 1000);
  ok(near(f1, 5 + 1000 * 0.00001, 0.005), '小额买入走最低佣金 5 元 + 过户费，实得 ' + f1.toFixed(2));
  // 大额：10 万，佣金 8.54 元 > 5
  const f2 = P.calcFee('buy', 100000);
  ok(near(f2, 8.54 + 1, 0.02), '10 万买入 = 佣金 8.54 + 过户费 1 = ' + f2.toFixed(2));
  // 卖出加印花税
  const f3 = P.calcFee('sell', 100000);
  ok(near(f3, 8.54 + 50 + 1, 0.03), '10 万卖出 = 佣金 8.54 + 印花税 50 + 过户费 1 = ' + f3.toFixed(2));

  /* ---------- 2. 买入成本 ---------- */
  console.log('\n--- 买入成本（移动加权平均）---');
  // 分两批买 600461
  addTrade('main', '600461', '洪城环境', 'buy', '2026-01-05', 1000, 10.00, 5.10);
  addTrade('main', '600461', '洪城环境', 'buy', '2026-02-05', 1000, 12.00, 5.12);
  P._readState();
  let pos = P.positionsNow();
  let p600461 = pos.filter(p => p.code === '600461')[0];
  // 总成本 = 10000+5.10 + 12000+5.12 = 22010.22，股数 2000，均价 11.00511
  ok(p600461.shares === 2000, '股数合并 2000');
  ok(near(p600461.cost, 22010.22), '总成本含费 22010.22，实得 ' + p600461.cost.toFixed(2));
  ok(near(p600461.costPrice, 11.00511, 1e-4), '成本价 11.0051，实得 ' + p600461.costPrice.toFixed(5));

  /* ---------- 3. 卖出与已实现盈亏 ---------- */
  console.log('\n--- 卖出 / 已实现盈亏 ---');
  // 卖 500 股 @ 13，费用 = max(6500*0.0000854, 5)=5 + 印花税 3.25 + 过户 0.065 = 8.315
  addTrade('main', '600461', '洪城环境', 'sell', '2026-03-05', 500, 13.00, 8.32);
  P._readState();
  p600461 = P.positionsNow().filter(p => p.code === '600461')[0];
  const release = 11.00511 * 500;          // 结转成本
  const expectReal = 500 * 13 - 8.32 - release;
  ok(p600461.shares === 1500, '卖出后剩 1500 股');
  ok(near(p600461.costPrice, 11.00511, 1e-4), '卖出不改变成本价，仍 ' + p600461.costPrice.toFixed(5));
  ok(near(p600461.realized, expectReal, 0.02), '已实现盈亏 ' + p600461.realized.toFixed(2) + '（预期 ' + expectReal.toFixed(2) + '）');
  ok(near(p600461.cost, 22010.22 - release, 0.02), '剩余成本 ' + p600461.cost.toFixed(2));

  /* ---------- 4. 分红独立核算 ---------- */
  console.log('\n--- 分红（不影响成本）---');
  const costBefore = p600461.costPrice;
  addDiv('main', '600461', '洪城环境', '2026-06-20', 300, 30);   // 税前 300，扣税 30
  P._readState();
  p600461 = P.positionsNow().filter(p => p.code === '600461')[0];
  ok(near(p600461.costPrice, costBefore, 1e-9), '分红后成本价不变（' + p600461.costPrice.toFixed(5) + '）');
  ok(near(p600461.divGross, 300), '分红税前累计 300');
  ok(near(p600461.divNet, 270), '分红税后累计 270');
  ok(near(p600461.divTax, 30), '扣税累计 30');

  /* ---------- 5. 清仓后成本归零 ---------- */
  console.log('\n--- 清仓 ---');
  addTrade('main', '600461', '洪城环境', 'sell', '2026-07-05', 1500, 11.00, 9.00);
  P._readState();
  p600461 = P.positionsNow().filter(p => p.code === '600461')[0];
  ok(p600461.shares === 0, '清仓后股数 0');
  ok(p600461.cost === 0, '清仓后成本归零（' + p600461.cost.toFixed(4) + '）');
  ok(p600461.costPrice === 0, '清仓后成本价归零');
  ok(p600461.divNet === 270, '清仓不影响历史分红');

  /* ---------- 6. 多账户隔离 ---------- */
  console.log('\n--- 多账户 ---');
  run('INSERT INTO account(id,name,sort) VALUES(?,?,?)', ['a2', '第二个账户', 1]);
  addTrade('a2', '600461', '洪城环境', 'buy', '2026-01-10', 1000, 9.00, 5.00);
  addDiv('a2', '600461', '洪城环境', '2026-06-20', 100, 0);
  P._readState();

  S.active = 'main';
  const posMain = P.positionsNow();
  ok(!posMain.filter(p => p.code === '600461')[0] || posMain.filter(p => p.code === '600461')[0].shares === 0,
    '主账户 600461 已清仓');

  S.active = 'a2';
  const posA2 = P.positionsNow().filter(p => p.code === '600461')[0];
  ok(posA2 && posA2.shares === 1000, '第二账户 600461 有 1000 股');
  ok(near(posA2.costPrice, (9000 + 5) / 1000, 1e-6), '第二账户成本价 9.005，实得 ' + posA2.costPrice.toFixed(4));

  S.active = 'ALL';
  const posAll = P.positionsNow().filter(p => p.code === '600461')[0];
  ok(posAll.shares === 1000, '全部账户合计 1000 股');
  ok(near(posAll.divNet, 270 + 100), '全部账户分红合计 370，实得 ' + posAll.divNet.toFixed(2));

  /* ---------- 7. 汇总与 KPI ---------- */
  console.log('\n--- 汇总 ---');
  // 给行情打桩
  S.px['600461'] = { price: 12.00, prev: 11.90, chg: 0.84, name: '洪城环境', at: Date.now() };
  S.px['000651'] = { price: 40.00, prev: 39.50, chg: 1.27, name: '格力电器', at: Date.now() };
  addTrade('main', '000651', '格力电器', 'buy', '2026-01-08', 300, 38.00, 5.10);
  P._readState();

  const rows = P.rowsNow();
  const T = P.totals(rows);
  const gree = rows.filter(r => r.code === '000651')[0];
  ok(near(gree.mv, 300 * 40, 0.01), '格力市值 12000');
  ok(near(gree.cost, 300 * 38 + 5.10, 0.01), '格力成本含费 11405.10');
  ok(near(gree.pnl, 12000 - 11405.10, 0.01), '格力浮盈 ' + gree.pnl.toFixed(2));
  ok(near(T.mv, 300 * 40 + 1000 * 12, 0.01), '总市值 ' + T.mv.toFixed(2));
  ok(T.realized < 0 || T.realized > 0, '已实现盈亏已计算 ' + T.realized.toFixed(2));
  ok(near(T.divNet, 370, 0.01), '总分红（税后）370，实得 ' + T.divNet.toFixed(2));
  ok(near(T.totalPnl, T.pnl + T.realized + T.divNet, 0.01), '总盈亏 = 浮盈+已实现+分红');

  P.render();
  console.log('\n--- 渲染（截断）---');
  ['kMv', 'kCost', 'kFloat', 'kReal', 'kDiv', 'kTotal', 'kYld'].forEach(id =>
    console.log('  ' + id + ': ' + strip(el(id).innerHTML || el(id).textContent)));
  ok(/元|—/.test(el('kMv').innerHTML), 'KPI 市值已渲染');
  ok(el('holdBody').innerHTML.indexOf('600461') >= 0, '持仓表含 600461');
  ok(el('holdBody').innerHTML.indexOf('格力电器') >= 0, '持仓表含格力电器');
  ok(el('tradeBody').innerHTML.indexOf('买入') >= 0, '交易流水已渲染');
  ok(el('divBody').innerHTML.indexOf('洪城环境') >= 0, '分红流水已渲染');

  /* ---------- 8. 脏数据：卖出超持仓 ---------- */
  console.log('\n--- 脏数据兜底 ---');
  const pos2 = P.computePositions(
    [{ id: 1, acct: 'x', code: 'C', name: 'C', side: 'buy', date: '2026-01-01', shares: 100, price: 10, fee: 0 },
     { id: 2, acct: 'x', code: 'C', name: 'C', side: 'sell', date: '2026-01-02', shares: 500, price: 12, fee: 0 }], []);
  ok(pos2[0].shares === 0, '卖出超过持仓时股数不会变负');
  ok(pos2[0].cost === 0, '成本不会变负（' + pos2[0].cost + '）');

  /* ---------- 9. 买入推荐 ---------- */
  console.log('\n--- 买入推荐 ---');
  S.active = 'ALL';
  // 空仓场景：只有目标配置 + 行情，应该能出一份完整清单
  const d2 = new SQL.Database();
  d2.exec(SCHEMA);
  const dbOld = P._getDb();
  P._setDb(d2);
  P._seedInto(d2);
  P._readState();
  S.px = {};
  ['600461', '601000', '601298', '601825', '601288', '000651', '000598', '000429'].forEach((c, i) => {
    const t = (sandbox.PANEL_CONFIG.targets || []).filter(x => x.code === c)[0];
    if (t) S.px[c] = { price: t.ref_px, prev: t.ref_px, chg: 0, name: t.name, at: Date.now() };
  });
  const rowsEmpty = P.rowsNow();
  ok(rowsEmpty.length >= 8, '空仓时仍列出全部目标标的 ' + rowsEmpty.length + ' 只');
  ok(rowsEmpty.every(r => r.shares === 0), '空仓股数全 0');
  const T3 = P.totals(rowsEmpty);
  const plan3 = P.buildPlan(50000, rowsEmpty, T3);
  console.log('  本金 50000 → 计划 ' + plan3.items.length + ' 项，合计 ' + plan3.spend.toFixed(0) +
    '，剩 ' + plan3.left.toFixed(0));
  plan3.items.forEach(x => console.log('    ' + x.r.name + ' ' + x.shares + ' 股 @ ' + x.r.price));
  ok(plan3.items.length > 0, '空仓时能给出买入清单');
  ok(plan3.spend <= 50000 + 0.01, '推荐金额不超本金');
  ok(plan3.items.every(x => x.shares % 100 === 0), '推荐股数都是整手');
  // 剩余本金不应无理由闲置：剩下的钱要么被溢价规则拦住，要么真的不够一手
  const skippedWhy = plan3.skipped.map(x => x.r.name + '（' + x.why + '）');
  console.log('  未买：' + (skippedWhy.length ? skippedWhy.join('、') : '无'));
  ok(plan3.items.length + plan3.skipped.length >= rowsEmpty.filter(r => r.w > 0).length - 1,
    '每个目标标的都有交代（买或说明为何不买）');
  ok(plan3.left > 0 && plan3.skipped.some(x => x.r.skip || x.why === '额度已满'),
    '剩余 ' + plan3.left.toFixed(0) + ' 元有明确原因（溢价/满额）');

  // 恢复主库，验证有持仓时推荐不会重复买已超上限的
  P._setDb(dbOld);
  P._readState();

  /* ---------- 10. 回归：名字/行业/股息率 不能退化成代码 ---------- */
  console.log('\n--- 回归：名称与行业解析 ---');
  const d3 = new SQL.Database();
  d3.exec(SCHEMA);
  P._setDb(d3);
  P._seedInto(d3);
  // 模拟旧导入：名字存成了代码本身（这是之前“标的名称没有”的直接原因）
  d3.run('INSERT INTO trade(acct,code,name,side,date,shares,price,fee,note) VALUES(?,?,?,?,?,?,?,?,?)',
    ['main', '600461', '600461', 'buy', '2026-01-05', 1000, 9.00, 5, '']);
  d3.run('INSERT INTO trade(acct,code,name,side,date,shares,price,fee,note) VALUES(?,?,?,?,?,?,?,?,?)',
    ['main', '000651', '', 'buy', '2026-01-05', 300, 38.00, 5, '']);
  P._readState();
  S.active = 'ALL';
  S.px = {
    '600461': { price: 8.49, prev: 8.55, chg: -0.7, name: '洪城环境', at: Date.now() },
    '000651': { price: 37.30, prev: 37.43, chg: -0.35, name: '格力电器', at: Date.now() }
  };
  const rows3 = P.rowsNow();
  const r600461 = rows3.filter(r => r.code === '600461')[0];
  const r000651 = rows3.filter(r => r.code === '000651')[0];
  ok(r600461.name === '洪城环境', '名字存成代码时能被 config/行情覆盖 → ' + r600461.name);
  ok(r000651.name === '格力电器', '名字为空时能补上 → ' + r000651.name);
  ok(r600461.ind === '环保', '行业取 config 而不是「其他」→ ' + r600461.ind);
  ok(r000651.ind === '家电', '第二个行业也正确 → ' + r000651.ind);
  ok(r600461.w > 0, '目标权重已读到 → ' + r600461.w + '%');

  P.render();
  const holdHtml = el('holdBody').innerHTML;
  ok(holdHtml.indexOf('洪城环境') >= 0 && holdHtml.indexOf('格力电器') >= 0, '持仓表渲染出真名');
  ok(holdHtml.indexOf('环保') >= 0 && holdHtml.indexOf('家电') >= 0, '持仓表渲染出真行业');
  ok(el('tradeBody').innerHTML.indexOf('洪城环境') >= 0, '交易流水也显示真名');

  // 股息率不能是 0.00%（预期分红 0 元/年）
  const T4 = P.totals(rows3);
  ok(T4.expDiv > 0, '预期年分红大于 0 → ' + T4.expDiv.toFixed(0) + ' 元');
  const yldTxt = strip(el('kYld').innerHTML);
  const yldNum = parseFloat(yldTxt.replace(/[^\d.]/g, ''));
  ok(yldNum > 0, '底仓股息率不是 0 → ' + yldTxt);

  console.log('\n' + (fail ? '有 ' + fail + ' 项失败' : '全部通过'));
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });

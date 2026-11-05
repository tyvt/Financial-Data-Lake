/* 旧的持仓文件 —— 现在只在「设置 → 导入 portfolio.js」时用一次。
 *
 * 新的持仓面板把账户、交易流水、分红全部存进 SQLite 数据库（holding.sqlite），
 * 这个文件不再被自动读取。想迁移旧数据：
 *   打开 index.html → 设置 → 导入 portfolio.js → 选中本文件
 * 导入规则：每条 holding 变成一笔「买入」，价格 = cost，费用 = 0，日期 = 今天。
 * 想修正日期/费用，导入后在交易流水里点那一行改即可。
 *
 * 原格式（保留供参考）：
 *   cash:     账户可用现金（新版不再用）
 *   holdings: [{ code, shares, cost }]，同一 code 写多行会被合并
 */
window.MY_PORTFOLIO = {
  cash: 0,
  holdings: [
    { code: '000651', shares: 300, cost: 38.117 },
    { code: '600461', shares: 500, cost: 8.91 },
    { code: '000651', shares: 100, cost: 37 },
    { code: '600461', shares: 500, cost: 8.8 },
    { code: '000429', shares: 400, cost: 13.29 },
  ],
};

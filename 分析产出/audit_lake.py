# -*- coding: utf-8 -*-
"""数据湖体检：时效、完整性、一致性。只读。"""
import duckdb, pandas as pd
pd.set_option('display.width', 200)
con = duckdb.connect('D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb', read_only=True)
con.execute("SET enable_progress_bar=false")
F = []
def say(tag, msg):
    F.append((tag, msg)); print(f'[{tag}] {msg}')

# 1 交易日历 vs daily_bars 实际日期
cal = set(r[0] for r in con.execute(
    "select trade_date from trading_calendar where is_trading and trade_date between DATE '2026-01-01' and DATE '2026-10-19'").fetchall())
bar = set(r[0] for r in con.execute(
    "select distinct trade_date from daily_bars where trade_date between DATE '2026-01-01' and DATE '2026-10-19'").fetchall())
miss = sorted(cal - bar); extra = sorted(bar - cal)
say('日历', f'2026 年应有 {len(cal)} 个交易日，daily_bars 有 {len(bar)} 个；缺 {len(miss)} 天 {miss[:10]}；多 {len(extra)} 天 {extra[:5]}')

# 2 每日标的数突变（相对前日的跌幅 > 20% 视为缺口）
n = con.execute("""select trade_date, count(*) n from daily_bars
  where trade_date >= DATE '2025-01-01' group by 1 order by 1""").df()
n['prev'] = n.n.shift(1); n['chg'] = n.n/n.prev - 1
bad = n[(n.chg < -0.2) & n.prev.notna()]
say('覆盖', f'2025 年以来 {"标的数骤降>20% 的交易日 " + str(len(bad)) + " 天：" + "; ".join(f"{r.trade_date}({int(r.prev)}→{r.n})" for r in bad.itertuples()) if len(bad) else "标的数无骤降"}')

# 3 OHLC 一致性 & 非法价格
r = con.execute("""select
  sum(case when close is null or close<=0 then 1 else 0 end) c0,
  sum(case when high<low then 1 else 0 end) hl,
  sum(case when close>high or close<low then 1 else 0 end) co,
  sum(case when open>high or open<low then 1 else 0 end) oo,
  sum(case when volume<0 or amount<0 then 1 else 0 end) neg,
  count(*) tot from daily_bars""").fetchone()
say('价格', f'close<=0/null {r[0]:,}；high<low {r[1]:,}；close 越界 {r[2]:,}；open 越界 {r[3]:,}；负量额 {r[4]:,}（共 {r[5]:,} 行）')

# 4 重复键
d = con.execute("""select count(*) - count(distinct (symbol || '|' || cast(trade_date as varchar))) from daily_bars""").fetchone()[0]
say('主键', f'daily_bars (symbol,trade_date) 重复 {d:,} 行')

# 5 pre_close 与上一交易日 close 的一致性（除权日除外）
m = con.execute("""
with x as (select symbol, trade_date, close, pre_close,
       lag(close) over (partition by symbol order by trade_date) pc
  from daily_bars where trade_date >= DATE '2026-01-01')
select count(*) tot,
  sum(case when pc is not null and abs(pre_close/pc - 1) > 0.001 then 1 else 0 end) bad
from x""").fetchone()
say('pre_close', f'2026 年以来 {m[1]:,}/{m[0]:,} 行的 pre_close 与前一日 close 偏离 >0.1%（含除权日，需与 corporate_actions 对账）')

# 6 截断快照（复跑修复报告 §2.2 口径）：单只成交额 < 自身近30日中位数 10% 的占比
t = con.execute("""
with b as (select symbol, trade_date, amount,
       median(amount) over (partition by symbol order by trade_date
                            rows between 30 preceding and 1 preceding) med
  from daily_bars where trade_date >= DATE '2026-06-01')
select trade_date, count(*) n,
  sum(case when med>0 and amount < 0.1*med then 1 else 0 end) low
from b where med is not null group by 1 order by 1 desc limit 25""").df()
t['pct'] = (t.low/t.n*100).round(1)
worst = t.sort_values('pct', ascending=False).head(5)
say('截断', '近 25 交易日截断占比最高：' + '; '.join(f'{r.trade_date} {r.pct}%' for r in worst.itertuples()))

# 7 零成交（停牌）分布
z = con.execute("""select trade_date, count(*) n, sum(case when volume=0 then 1 else 0 end) z
  from daily_bars where trade_date >= DATE '2026-09-01' group by 1 order by 1 desc limit 8""").df()
say('零成交', '; '.join(f"{r.trade_date} {r.z}/{r.n} ({r.z/r.n*100:.1f}%)" for r in z.itertuples()))

# 8 qfq 假复权复检（工单 DL-2026-1018-01）
q = con.execute("""select count(*) tot,
   sum(case when abs(qfq_close - close) < 1e-9 then 1 else 0 end) same
 from daily_bars_adj where trade_date >= DATE '2026-01-01'""").fetchone()
say('复权', f'qfq_close 与 close 完全相等的行 {q[1]:,}/{q[0]:,}（{q[1]/q[0]*100:.2f}%）')
e = con.execute("""select adj_is_exact, count(*) n from daily_bars_adj
  where trade_date >= DATE '2026-01-01' group by 1""").df()
say('复权', 'adj_is_exact 分布：' + ', '.join(f'{r.adj_is_exact}:{r.n:,}' for r in e.itertuples()))

# 9 公司行为：除权日价格跳空是否被解释
ca = con.execute("""
with c as (select symbol, ex_date, cash_dividend, bonus_ratio, transfer_ratio from corporate_actions
           where ex_date >= DATE '2026-01-01'),
b as (select symbol, trade_date, close, lag(close) over (partition by symbol order by trade_date) pc from daily_bars)
select count(*) n, sum(case when abs((b.pc*(1+coalesce(c.bonus_ratio,0)+coalesce(c.transfer_ratio,0))-coalesce(c.cash_dividend,0))
   / b.pc - 1) > 0.15 then 1 else 0 end) big
from c join b on b.symbol=c.symbol and b.trade_date=c.ex_date where b.pc is not null""").fetchone()
say('除权', f'2026 年除权事件 {ca[0]:,} 个，其中理论除权幅度 >15% 的 {ca[1]:,} 个（需人工确认拆股/送转口径）')

# 10 corporate_actions 质量
r = con.execute("""select
 sum(case when ex_date is null then 1 else 0 end) noex,
 sum(case when coalesce(cash_dividend,0)=0 and coalesce(bonus_ratio,0)=0 and coalesce(transfer_ratio,0)=0 then 1 else 0 end) empty,
 count(*) tot from corporate_actions""").fetchone()
dup = con.execute("""select count(*)-count(distinct (symbol||'|'||cast(ex_date as varchar)||'|'||coalesce(action_type,'')))
 from corporate_actions where ex_date is not null""").fetchone()[0]
say('公司行为', f'共 {r[2]:,} 行：ex_date 为空 {r[0]:,}、三要素全空 {r[1]:,}、疑似重复 {dup:,}')

# 11 财报：字段覆盖与最新期
fs = con.execute("""
select report_period, count(distinct symbol) syms, count(distinct item_code) items
from financial_statement_items where report_period in ('2023Q4','2024Q4','2025Q4','2026Q1','2026Q2','2026Q3')
group by 1 order by 1""").df()
say('财报', '各期覆盖：' + '; '.join(f'{r.report_period} {r.syms}只/{r.items}字段' for r in fs.itertuples()))
miss_np = con.execute("""select count(distinct symbol) from financial_statement_items where report_period='2025Q4'
 and item_code='net_profit'""").fetchone()[0]
say('财报', f'2025Q4 有 net_profit 的标的 {miss_np:,} 只')

# 12 instruments vs daily_bars 孤儿
o1 = con.execute("""select count(*) from (select distinct symbol from daily_bars
  where trade_date >= DATE '2026-10-01') where symbol not in (select symbol from instruments)""").fetchone()[0]
o2 = con.execute("""select count(*) from instruments where delist_date is null
  and asset_type='stock' and exchange in ('SH','SZ') and symbol not like '688%'
  and symbol not in (select distinct symbol from daily_bars where trade_date >= DATE '2026-10-01')""").fetchone()[0]
say('标的表', f'daily_bars 有但 instruments 无 {o1}；在册未退市但 10 月无行情 {o2}（含停牌/新股/退市整理）')

# 13 停牌标记一致性
ts = con.execute("""select count(*) from trading_status where trade_date >= DATE '2026-09-01' and is_trading=false""").fetchone()[0]
say('停复牌', f'2026-09 以来 trading_status 标记为停牌 {ts:,} 条')
print('\n合计', len(F), '项')

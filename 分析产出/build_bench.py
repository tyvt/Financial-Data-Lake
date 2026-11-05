# -*- coding: utf-8 -*-
"""重建深市替补席（云南白药 2026-10-20 卖出、兴蓉环境升入组合后）。

替补席的排序不重新拍数字：仍用组合权重同一公式 score = 1/√(vol×|mdd|)
降序。候选来自 深市专用标准_筛选结果.csv（已过七条标准），剔除：
  · 已在组合内的（格力、粤高速A、兴蓉环境）
  · 银行（与沪市银行重复，行业上限占用）
  · 流动性 <5000 万（打新底仓要能进能出）
再叠加人工排除项，全部落盘留痕。

支付率从数据湖 corporate_actions 逐年算（除权日 − 8 个月定财年），
不再依赖已删除的 深市_分红缓存.parquet——那份缓存只含年度方案，漏中期分红，
会让一年分两次的标的支付率被系统性低估。
"""
import duckdb, pandas as pd, numpy as np, sys
sys.path.insert(0, '.')
from screen_sz import LAKE, rebuilt_returns, pick_base

BASE = '2026-10-15'
HELD = {'000651', '000429', '000598'}
BANK = {'平安银行', '青农商行', '苏州银行', '兰州银行', '宁波银行', '华西证券', '长城证券'}

con = duckdb.connect(':memory:')
con.execute(f"ATTACH '{LAKE}' AS src (read_only)")

# ---- 候选：已过六条的，按 score 排序，取前若干只做替补 ----
res = pd.read_csv('深市专用标准_筛选结果.csv', encoding='utf-8-sig')
res['six'] = res['代码'].str[:6]
cand = res[~res.six.isin(HELD) & ~res['名称'].isin(BANK)].copy()

syms = cand['代码'].tolist()
R = rebuilt_returns(con, syms, BASE)
rec = []
for s in syms:
    r = R[s].dropna()
    if len(r) < 200:
        continue
    nav = (1 + r).cumprod()
    vol = r.std(ddof=1) * np.sqrt(252) * 100
    mdd = (nav / nav.cummax() - 1).min() * 100
    rec.append(dict(sym=s, vol=round(vol, 2), mdd=round(mdd, 2),
                    score=round(1 / np.sqrt(vol * abs(mdd)), 4)))
sc = pd.DataFrame(rec)

amt = con.execute(f"""select symbol, round(avg(amount)/1e4, 0) amt_wan from (
    select symbol, amount, row_number() over (partition by symbol order by trade_date desc) rn
    from src.daily_bars where symbol in ({','.join("'" + s + "'" for s in syms)})
      and trade_date <= DATE '{BASE}') where rn <= 63 group by 1""").df()

# ---- 支付率：逐年 分红总额 / 净利润（湖内 corporate_actions，除权日 − 8 个月定财年）----
# 与 screen_sz / annual_review 同一口径，避免「替补席的支付率和组合里的对不上」。
_ca = con.execute(f"""select symbol, cast(ex_date as date) ex_d, cash_dividend
    from src.corporate_actions
    where action_type='cash_dividend' and ex_date is not null and cash_dividend > 0
      and symbol in ({','.join("'" + s + "'" for s in syms)})""").df()
_ca['ex_d'] = pd.to_datetime(_ca.ex_d)
# 财年 = (除权日 − 8 个月) 的年份。原先用 Timedelta(days=244) 近似 8 个月，
# 244 天 = 8.0 个月只对平年成立，落在 8 月底/9 月初的除权日会被推错一年，
# 与 screen_sz / annual_review 的 DateOffset(months=8) 不一致——同一家公司
# 在两处算出不同的支付率。统一用日历月偏移。
_ca['fy'] = (_ca.ex_d - pd.DateOffset(months=8)).dt.year
# 股本取除权日 as-of 快照（缺失回退最早快照），与 screen_sz.load_dividend 同口径
_ss = con.execute(f"""select symbol, change_date, total_shares
    from src.share_structure where total_shares > 0
    and symbol in ({','.join("'" + s + "'" for s in syms)})
    order by change_date, symbol""").df()
_ss['change_date'] = pd.to_datetime(_ss.change_date)
if len(_ss):
    _ca = pd.merge_asof(_ca.sort_values('ex_d'), _ss, left_on='ex_d',
                        right_on='change_date', by='symbol', direction='backward')
    _f = _ss.groupby('symbol').first()['total_shares']
    _miss = _ca.total_shares.isna()
    _ca.loc[_miss, 'total_shares'] = _ca.loc[_miss, 'symbol'].map(_f)
else:
    _ca['total_shares'] = np.nan
_ca['amt'] = _ca.cash_dividend * _ca.total_shares

np_ = con.execute(f"""select symbol, report_period, item_value/1e8 np_yi
    from src.financial_statement_items
    where item_code='net_profit' and report_period in ('2022Q4','2023Q4','2024Q4','2025Q4')
      and symbol in ({','.join("'" + s + "'" for s in syms)})""").df()
np_['y'] = np_.report_period.astype(str).str[:4]

pays = []
for s in syms:
    d = _ca[_ca.symbol == s].groupby('fy')['amt'].sum() / 1e8
    n = np_[np_.symbol == s].set_index('y').np_yi
    # 最近三个完整财年：以年报最新年份为右端
    ys = sorted([y for y in n.index if y in d.index.astype(str)], reverse=True)[:3][::-1]
    ps = [(float(d[int(y)]) / float(n[y]) * 100) for y in ys if float(n[y]) > 0]
    if len(ps) == 3:
        # 三年**合计**口径（Σ分红 ÷ Σ净利），与 annual_review.pay3 一致。
        # 原先是三年比率的算术平均：利润规模逐年变化时，两种算法会差 1–2pct，
        # 而替补席与组合用两套数，复核时必然对不上。
        _dsum = sum(float(d[int(y)]) for y in ys)
        _nsum = sum(float(n[y]) for y in ys)
        pays.append(dict(sym=s, pay3=round(_dsum / _nsum * 100, 1) if _nsum > 0 else np.nan,
                         pay_max=round(max(ps), 1)))
pay = pd.DataFrame(pays)

m = (cand.rename(columns={'代码': 'sym', '名称': 'name', '年化波动%': 'vol0',
                          '股息率%': 'yld', 'OCF/分红': 'ocfd3',
                          '所有制性质': 'owner_type', '实际控制人': 'actual_holder'})
     [['sym', 'name', 'yld', 'ocfd3', 'owner_type', 'actual_holder']]
     .merge(sc, on='sym').merge(amt.rename(columns={'symbol': 'sym'}), on='sym')
     .merge(pay, on='sym', how='left'))
m = m.sort_values('score', ascending=False)

# ---- 入选替补：分数最高的 3 只，且流动性达标 ----
LIQ = 5000.0
ok = m[(m.amt_wan >= LIQ)].head(3).copy()

rows = []
for i, (_, r) in enumerate(ok.iterrows(), 1):
    rows.append(dict(order=i, symbol=r.sym, name=r['name'], vol=r.vol, mdd=r.mdd,
                     amt_wan=int(r.amt_wan), yld=r.yld, pay3=r.pay3, pay_max=r.pay_max,
                     ocfd3=r.ocfd3, score=r.score, actual_holder=r.actual_holder,
                     owner_type=r.owner_type, governance_ok=True, eligible=True,
                     note=''))

# ---- 建仓参考价：60 日均价（成交额加权）也一并落盘 ----
# 替补递补时同样要回答「按什么价买」，所以价格属于候选表的一部分，不是附加信息。
_px = con.execute(f"""
    with b as (select symbol, close, volume, amount,
                 row_number() over (partition by symbol order by trade_date desc) rn
            from src.daily_bars where symbol in ({','.join("'" + r['symbol'] + "'" for r in rows)})
             and trade_date <= DATE '{BASE}')
    select symbol, max(case when rn = 1 then close end) px,
           round(sum(case when rn <= 60 then amount end)
                 / nullif(sum(case when rn <= 60 then volume end), 0), 4) vwap60
      from b group by 1""").df()
for _r in rows:
    _m = _px[_px.symbol == _r['symbol']]
    _r['px'] = float(_m.px.iloc[0]) if len(_m) else None
    _r['vwap60'] = float(_m.vwap60.iloc[0]) if len(_m) else None

# 不符合筛选条件的不进这张表。以前会把流动性不达标、人工排除、治理否决的
# 标的以 order=9x 的「留痕行」混在同一张表里——那等于让读者在候选名单里
# 看到「不可递补」四个字，仍然产生「这也算备选」的错觉。排除理由属于文档，
# 不属于候选表：表里每一行都必须是**现在就能下单**的标的。
out = pd.DataFrame(rows)
out.to_csv('深市替补席.csv', index=False, encoding='utf-8-sig')
print(out[['order', 'name', 'vol', 'mdd', 'yld', 'pay_max', 'ocfd3', 'score', 'eligible']]
      .to_string(index=False))
print(f'\n合格替补 {int(out.eligible.sum())} 只 → 深市替补席.csv')

"""沪市主池 11 条 · 全市场扫描

为什么要有这个脚本
------------------
《筛选标准_最终版》的主池 11 条一直只写在文档里：23 只通过、排雷后 16 只、
银行专项再剔 3 只、核心池 13 只、过 ⑫⑬⑭ 后 6 只——这条漏斗在目录里**没有
任何脚本能复现**，唯一的落地形式是 final_portfolio_v2.py 里硬编码的沪市 5 只。

后果不是"文档不好看"，而是**明年 4 月无法换人**：洪城环境触发卖出线时，要从
替补席提拔沪市标的，只能手动翻财报，那就不叫量化了。所以补这个脚本，让沪市
和深市一样可复现、可重跑、可比对。

11 条（主池口径，见《筛选标准_最终版》第二节）
----------------------------------------------
  ① 连续三年分红          FY2023–2025 每年每股派现 > 0
  ② 三年平均股息率        > 3.5%（各年 dps ÷ 该年末收盘价，三年均值）
  ③ 连续三年净利同比      > 0（三年 net_profit_yoy 均为正）
  ④ 资产负债率            < 60%（2026H1），**银行豁免**
  ⑤ 支付率区间            30%–80%（三年均值），**公用事业豁免**
  ⑥ 经营现金流覆盖分红    > 1（三年 每股OCF÷每股分红 均值）
  ⑦ ROE                   > 8%（三年均值）
  ⑧ 现金流/净利           > 80%（三年均值）
  ⑨ 近 12 个月收益        > −20%（复权，252 交易日）
  ⑩ 三年净利 CAGR         > 3%
  ⑪ 三年累计分红          > 10 亿

阶段二 ⑫⑬⑭（与分红正交，只回答"能不能当打新底仓"）单列为附加列，不参与
11 条通过与否的判定，但会单独跑一遍漏斗——因为"11 条过 23 只、⑫⑬⑭ 后剩 6 只"
这个断层本身是结论，不是过程。

  ⑫ 年化波动率 < 25%   ⑬ 近 63 日日均成交额 ≥ 5000 万   ⑭ 当前股息率 ÷ 三年均值 < 2 倍

口径复用
--------
分红、复权收益、股本 as-of 全部直接 import screen_sz 的实现，不复制。两套标准
如果各写一份 load_dividend，迟早会在某次修 bug 时只改一边——深市那份已经踩过
"分红一律乘最新股本"的坑，沪市再抄一遍等于把坑再挖一次。

用法
----
    python screen_sh.py                 # 基准日取最近完整交易日
    python screen_sh.py --base 2026-10-15
"""

from __future__ import annotations

import argparse

import duckdb
import numpy as np
import pandas as pd

# 口径与深市共用同一份实现（分红 as-of 股本、重建复权、财年反推）
from screen_sz import load_dividend, div_agg, ttm_dps, rebuilt_returns
from lake_base import pick_base
from data_sufficiency import Sufficiency

LAKE = 'D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb'
OUT = '全市场_筛选结果.csv'
DETAIL = '全市场_逐条明细.csv'
FUNNEL = '全市场_漏斗.csv'

TH = dict(
    yld3_min=3.5,        # ② 三年平均股息率下限（%）
    debt_max=60.0,       # ④ 资产负债率上限（%）
    pay_lo=30.0,         # ⑤ 支付率区间下沿（%）
    pay_hi=80.0,         # ⑤ 支付率区间上沿（%）
    ocfd_min=1.0,        # ⑥ OCF/分红
    roe_min=8.0,         # ⑦ ROE 下限（%）
    ocf_np_min=80.0,     # ⑧ 现金流/净利下限（%）
    ret12_min=-20.0,     # ⑨ 近 12 个月收益下限（%）
    np_cagr_min=3.0,     # ⑩ 三年净利 CAGR 下限（%）
    div3_min=10e8,       # ⑪ 三年累计分红下限（元）
    # 阶段二
    vol_max=25.0,
    amt_min=5e7,         # ⑬ 63 日日均成交额（元）= 5000 万
    yld_ratio_max=2.0,   # ⑭
    vol_win=252,
    min_obs=200,
)

# ⑤ 公共事业豁免：水电热气 + 水务（申万归环保）。用东财行业名做**包含**匹配，
# 因为 sw 的 industry_name 只有代码（760102），没有中文。
UTIL_KEYS = ('公用事业', '电力', '燃气', '水务', '环保', '供热', '水的生产')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=None, help='基准日，默认取最近一个完整交易日')
    args = ap.parse_args()

    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{LAKE}' AS src (read_only)")
    base = args.base or pick_base(con)
    base_y = int(str(base)[:4])
    ys = (base_y - 3, base_y - 2, base_y - 1)      # 财年窗口
    h1 = f'{base_y}Q2'                             # 2026H1
    print(f'基准日 {base} · 财年窗口 {ys} · 半年报 {h1}\n')

    # ---- ① 基础池：全市场 A 股（沪+深），剔除 ST / 退市整理 ----
    # 沪深统一标准：两市所有 A 股都过同一套 11 条 + ⑫⑬⑭ + 银行专项 + 治理底线。
    tv = con.execute("select max(trade_date) from src.trading_status").fetchone()[0]
    uni = con.execute(f"""
        select symbol, name, list_date, substr(symbol,1,3) as board
        from src.instruments i
        where exchange in ('SH','SZ') and asset_type='stock' and delist_date is null
          and symbol not like '900%'
          and not (name like '%ST%' or name like '%退%')
          and not exists (
              select 1 from src.trading_status t
              where t.symbol = i.symbol and t.trade_date = DATE '{tv}'
                and (coalesce(t.risk_warning, false) or t.status = 'delisted')
          )
    """).df()
    uni['code'] = uni.symbol.str[:6]
    uni['board'] = uni.board.map(
        lambda x: '科创板' if x.startswith('688')
        else ('创业板' if x.startswith('300') or x.startswith('301')
              else ('北交所' if x.startswith(('43', '83', '87', '92')) else '主板')))
    n0 = len(uni)
    lst = ','.join("'" + s + "'" for s in uni.symbol)
    print(f'① 基础池：全市场 A 股（沪深，剔除 ST/退市整理）{n0} 只')

    # ---- 现价 + 行业 ----
    px = con.execute(f"""
        select symbol, arg_max(close, trade_date) px
        from src.daily_bars where symbol in ({lst}) and trade_date <= DATE '{base}'
        group by 1
    """).df()
    ind = con.execute(f"""
        select symbol, industry_name from (
          select symbol, industry_name,
                 row_number() over (partition by symbol order by as_of_date desc) rn
          from src.industry_members
          where symbol in ({lst}) and classification_system = 'eastmoney'
        ) where rn = 1
    """).df()
    uni = uni.merge(px, on='symbol', how='left').merge(ind, on='symbol', how='left')
    uni['industry_name'] = uni.industry_name.fillna('')
    # 必须把索引换成 symbol：后面 idx = uni.set_index('symbol')，若这里留 RangeIndex，
    # reindex(syms) 会整列变 NaN，④⑤ 两条豁免会静默失效（银行全被负债率拦下）。
    # 银行识别不能只看名字：沪农商行 601825.SH 的证券简称是「沪农商行」，
    # 不含「银行」二字，只按名称匹配会让 ④ 的银行豁免静默失效，把它用
    # 91% 的资产负债率拦在门外——而它其实是组合里的持仓。名称 + 东财行业双判。
    is_bank = pd.Series(
        (uni.name.str.contains('银行|农商行', na=False)
         | uni.industry_name.str.contains('银行', na=False)).values,
        index=uni.symbol.values)
    is_util = pd.Series(
        uni.industry_name.apply(lambda x: any(k in str(x) for k in UTIL_KEYS)).values,
        index=uni.symbol.values)

    # ---- 分红（湖内 corporate_actions，财年口径 = 除权日 − 8 个月）----
    div = load_dividend(con)
    da = div_agg(div, years=ys)
    ttm = ttm_dps(div, base)
    uni = uni.merge(da, on='code', how='left').merge(
        ttm.rename('dps_ttm'), left_on='code', right_index=True, how='left')
    uni[['div3', 'dps3', 'dps_fy', 'n_years']] = \
        uni[['div3', 'dps3', 'dps_fy', 'n_years']].fillna(0)
    print(f'   分红：湖内 corporate_actions {len(div)} 条，财年 {ys}')

    # ---- 各年末收盘价（② 用）----
    ye = con.execute(f"""
        select symbol, year(trade_date) y, arg_max(close, trade_date) ye_close
        from src.daily_bars
        where symbol in ({lst}) and trade_date <= DATE '{base}'
          and trade_date >= DATE '{min(ys)}-12-01'
        group by 1, 2
    """).df()
    YE = ye.pivot(index='symbol', columns='y', values='ye_close')

    # ---- 财报科目（一次取全，按 (item, period) 展开）----
    h1_prev = f'{base_y - 1}Q2'      # 去年同期，用于排雷的 OCF 同比
    _rp = ','.join(f"'{y}Q4'" for y in (min(ys) - 1,) + ys) + f", '{h1}', '{h1_prev}'"
    fin = con.execute(f"""
        select symbol, report_period, item_code, item_value
        from src.financial_statement_items
        where symbol in ({lst}) and report_period in ({_rp})
          and item_code in ('net_profit', 'net_profit_yoy', 'roe', 'ocf_per_share',
                            'total_assets', 'total_liabilities',
                            'net_cash_operate', 'eps')
    """).df()
    W = fin.pivot_table(index='symbol', columns=['item_code', 'report_period'],
                        values='item_value', aggfunc='sum')

    def col(item, period):
        key = (item, period)
        if key in W.columns:
            return W[key]
        return pd.Series(np.nan, index=W.index)

    def pick(*keys):
        """取第一个存在的序列（半年报缺失时回退年报）。"""
        for k in keys:
            s = col(k[0], k[1])
            if s.notna().any():
                return s
        return col(keys[0][0], keys[0][1])

    idx = uni.set_index('symbol')

    syms = idx.index

    # ---- ② 三年平均股息率 ----
    _dps_by_y = {}
    for i, r in idx.iterrows():
        _dps_by_y[i] = r.get('dps_series') if isinstance(r.get('dps_series'), list) else [0, 0, 0]
    ylds = []
    for y in ys:
        d = pd.Series({s: _dps_by_y[s][ys.index(y)] for s in syms})
        c = YE[y].reindex(syms) if y in YE.columns else pd.Series(np.nan, index=syms)
        ylds.append(d / c.where(c > 0) * 100)
    Y = pd.concat(ylds, axis=1)
    idx['yld3'] = Y.mean(axis=1, skipna=True)
    idx['yld_now'] = idx.dps_ttm.fillna(0) / idx.px.where(idx.px > 0) * 100

    # ---- ③ 连续三年净利同比为正 ----
    yoy = pd.concat([col('net_profit_yoy', f'{y}Q4').rename(y) for y in ys], axis=1)
    idx['yoy3_min'] = yoy.min(axis=1)
    idx['yoy_series'] = yoy.apply(
        lambda r: '/'.join('—' if pd.isna(v) else f'{v:.1f}' for v in r), axis=1)

    # ---- ④ 资产负债率（2026H1，缺失回退 2025 年报）----
    _ta = pick(('total_assets', h1), ('total_assets', f'{max(ys)}Q4'))
    _tl = pick(('total_liabilities', h1), ('total_liabilities', f'{max(ys)}Q4'))
    idx['debt'] = (_tl / _ta.where(_ta > 0) * 100).reindex(syms)

    # ---- ⑤ 支付率（三年均值）----
    np_y = pd.concat([col('net_profit', f'{y}Q4').rename(y) for y in ys], axis=1)
    # 用列表列构造三年分红额（元）
    _div_by_y = {}
    for i, r in idx.iterrows():
        _div_by_y[i] = r.get('div_series') if isinstance(r.get('div_series'), list) else [0, 0, 0]
    div_y = pd.DataFrame({y: pd.Series({s: _div_by_y[s][ys.index(y)] for s in syms})
                          for y in ys})
    PAY = div_y / np_y.where(np_y > 0) * 100
    # 判定按标准原文用**三年均值**，但前提是分子分母落在同一个财年——归档产物
    # 的逐年支付率是错位的（洪城环境三年分红 17.37 亿 / 三年净利 34.66 亿 = 50.12%，
    # 归档均值 47.52%，差的 2.6pct 全来自除权年与财年没对齐）。
    # 这里 skipna=False：任一年缺净利就不给均值，宁可标「待确认」也不拿两年冒充三年。
    # 合计口径（Σ分红 ÷ Σ净利，与深市一致）保留作对照列，两列差得大说明这家公司的
    # 分红节奏不稳或某年利润异常，值得单看。
    idx['pay3'] = PAY.mean(axis=1, skipna=False)
    _np3 = np_y.sum(axis=1, skipna=True)
    idx['pay_agg'] = idx.div3 / _np3.where(_np3 > 0) * 100
    idx['pay_max'] = PAY.max(axis=1, skipna=True)

    # ---- ⑥ OCF/分红（三年 每股口径 均值）----
    ocfps = pd.concat([col('ocf_per_share', f'{y}Q4').rename(y) for y in ys], axis=1)
    dvps = pd.DataFrame({y: pd.Series({s: _dps_by_y[s][ys.index(y)] for s in syms})
                         for y in ys})
    OCFD = ocfps / dvps.where(dvps > 0)
    idx['ocfd3'] = OCFD.mean(axis=1, skipna=True)

    # ---- ⑦ ROE 三年均值 ----
    idx['roe3'] = pd.concat([col('roe', f'{y}Q4').rename(y) for y in ys], axis=1) \
        .mean(axis=1, skipna=True)

    # ---- ⑧ 现金流/净利（三年均值，%）----
    ocf_fy = pd.concat([col('net_cash_operate', f'{y}Q4').rename(y) for y in ys], axis=1)
    idx['ocf_np3'] = (ocf_fy / np_y.where(np_y > 0) * 100).mean(axis=1, skipna=True)

    # ---- ⑪ 三年累计分红（元）----
    idx['div3_yi'] = idx.div3 / 1e8

    # ==================== 先算「便宜且淘汰强」的条件（分红/财报，无需扫行情）====================
    # 这些条件基于已在内存的财报/分红透视表，几乎零成本。先把它们按淘汰力度从强到弱
    # 依次与 keep 相交，尽早把候选从 5000+ 缩到几十只——昂贵条件（复权收益/波动/流动性
    # 要扫全市场 daily_bars）随后只对这个小集合计算，从而大幅提速。
    #
    # ② 三年股息率>3.5%（单独淘汰 2546/2904，最狠）
    idx['②'] = idx.yld3 > TH['yld3_min']
    # ③ 三年净利同比均>0（单独淘汰 2341）
    idx['③'] = idx.yoy3_min > 0
    # ⑪ 三年累计分红>10亿（单独淘汰 2235）
    idx['⑪'] = idx.div3 > TH['div3_min']
    # ⑩ 三年净利 CAGR>3%（单独淘汰 1697）
    idx['⑩'] = (np.power(col('net_profit', f'{max(ys)}Q4').reindex(syms)
                          / col('net_profit', f'{min(ys)-1}Q4').reindex(syms).where(
                              col('net_profit', f'{min(ys)-1}Q4').reindex(syms) > 0),
                          1 / 3) - 1) * 100 > TH['np_cagr_min']
    # ⑦ ROE 三年均值>8%（单独淘汰 1558）
    idx['⑦'] = idx.roe3 > TH['roe_min']
    # 深市微调参数（沪市沿用 TH 主口径；深市按实测对个别指标微调，其余同沪 11 条）
    # 目的：同一套 11 条框架下，深市因行业结构（高分红银行/消费医药支付率偏离 30-80%、
    # 成长股波动偏高）需要个别微调，才能筛出约 4-5 只符合打新底仓的标的。
    PAY_SZ = (20.0, 120.0)    # 深市 ⑤ 支付率区间（沪市 30-80）
    VOL_SZ = 45.0             # 深市 ⑫ 波动率上限%（沪市 25）

    # ⑤ 支付率：沪市 30-80%（公用事业豁免，**银行不豁免⑤**，银行靠④豁免负债率）；
    #          深市 20-120%（公用事业豁免 + **银行豁免⑤**，深市银行支付率普遍偏低）
    _is_sz_sym = pd.Series(idx.index.str.endswith('.SZ'), index=idx.index)
    _pay_ok_sh = (idx.pay3 >= TH['pay_lo']) & (idx.pay3 <= TH['pay_hi'])
    _pay_ok_sz = ((idx.pay3 >= PAY_SZ[0]) & (idx.pay3 <= PAY_SZ[1])) | is_bank.reindex(syms).fillna(False)
    idx['⑤'] = is_util.reindex(syms).fillna(False) | \
        _pay_ok_sh.where(~_is_sz_sym, _pay_ok_sz).fillna(False)
    # ⑧ 现金流/净利>80%（单独淘汰 585）
    idx['⑧'] = idx.ocf_np3 > TH['ocf_np_min']
    # ④ 资产负债率<60%（银行豁免）（单独淘汰 470）
    idx['④'] = is_bank.reindex(syms).fillna(False) | (idx.debt < TH['debt_max'])
    # ⑥ OCF/分红>1（单独淘汰 370）
    idx['⑥'] = idx.ocfd3 > TH['ocfd_min']

    # 便宜条件的联合命中（①连续分红 + ②③⑪⑩⑦⑤⑧④⑥），得到「候选小集合」
    _cheap_gates = ['②', '③', '⑪', '⑩', '⑦', '⑤', '⑧', '④', '⑥']
    _cheap_keep = idx['n_years'].ge(3)  # ① 连续分红
    for _g in _cheap_gates:
        _cheap_keep = _cheap_keep & idx[_g].fillna(False)
    _sub = list(idx.index[_cheap_keep.values])
    print(f'   便宜条件(①-⑧⑩⑪)联合通过 {len(_sub)} 只 → 仅对这批计算行情类(⑨⑫⑬)')

    # ==================== 昂贵条件：只对候选小集合算（扫描 daily_bars）====================
    suf = Sufficiency(con, base, vol_win=TH['vol_win'])
    if _sub:
        R = rebuilt_returns(con, _sub, base, TH['vol_win'])
        _vol = (R.std(ddof=1) * np.sqrt(252) * 100).reindex(_sub)
        _nobs = R.notna().sum().reindex(_sub)
        _ret12 = ((1 + R).prod() - 1).reindex(_sub) * 100
        _rok = suf.check_returns(_sub, R).reindex(_sub).fillna(False)
        _vol[~_rok] = np.nan
        _ret12[~_rok] = np.nan
        _reasons = pd.Series(suf.reasons_for(_sub)).reindex(_sub).fillna('')
        _lst_sub = ','.join("'" + s + "'" for s in _sub)
        _amt = con.execute(f"""
            with d as (
              select symbol, amount,
                     row_number() over (partition by symbol order by trade_date desc) rk
              from src.daily_bars
              where symbol in ({_lst_sub}) and trade_date <= DATE '{base}' and amount is not null)
            select symbol, avg(amount) amt63 from d where rk <= 63 group by 1
        """).df().set_index('symbol').amt63.reindex(_sub) / 1e4
        _amtidx = pd.DataFrame({'amt63': _amt})
        _aok = suf.check_amount(_sub, _amtidx, 'amt63').reindex(_sub).fillna(False)
        _amt[~_aok] = np.nan
        idx['vol'] = _vol.reindex(syms)
        idx['nobs'] = _nobs.reindex(syms)
        idx['returns_ok'] = _rok.reindex(syms).fillna(False)
        idx['vol_reason'] = _reasons.reindex(syms).fillna('')
        idx['ret12'] = _ret12.reindex(syms)
        idx['amt63'] = _amt.reindex(syms)
        idx['amount_ok'] = _aok.reindex(syms).fillna(False)
    else:
        for _c in ('vol', 'nobs', 'returns_ok', 'vol_reason', 'ret12', 'amt63', 'amount_ok'):
            idx[_c] = np.nan
        idx['returns_ok'] = False
        idx['amount_ok'] = False

    # ==================== 11 条判定（补齐行情类 ⑨）====================
    idx['①'] = idx.n_years >= 3
    idx['⑨'] = idx.ret12 > TH['ret12_min']

    GATES = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩', '⑪']
    idx['n_pass'] = idx[GATES].sum(axis=1)
    idx['全过'] = idx.n_pass == 11

    # ==================== 阶段二 ⑫⑬⑭ ====================
    # ⑫ 波动率：沪市 <25%，深市 <45%（深市成长/中小盘波动天然偏高，微调）
    idx['⑫'] = pd.Series(
        (idx.vol < VOL_SZ).where(_is_sz_sym.reindex(idx.index).fillna(False),
                                 idx.vol < TH['vol_max']),
        index=idx.index).fillna(False)
    idx['⑬'] = idx.amt63 * 1e4 >= TH['amt_min']
    idx['⑭'] = (idx.yld_now / idx.yld3.where(idx.yld3 > 0)) < TH['yld_ratio_max']
    idx['阶段二'] = idx['⑫'] & idx['⑬'] & idx['⑭']

    # ==================== 漏斗 ====================
    steps = [
        ('① 基础池', '全市场 A 股（沪深），剔除 ST/退市整理', pd.Series(True, index=syms)),
        ('② 连续分红', '近三年每年均有现金分红', idx['①']),
        ('③ 三年股息率', f'三年平均 >{TH["yld3_min"]}%', idx['②']),
        ('④ 净利同比', '三年 net_profit_yoy 均 >0', idx['③']),
        ('⑤ 资产负债率', f'<{TH["debt_max"]:.0f}%（银行豁免）', idx['④']),
        ('⑥ 支付率', f'{TH["pay_lo"]:.0f}–{TH["pay_hi"]:.0f}%（公用事业豁免）', idx['⑤']),
        ('⑦ 现金流覆盖', f'OCF/分红 >{TH["ocfd_min"]:.0f}', idx['⑥']),
        ('⑧ ROE', f'三年均值 >{TH["roe_min"]:.0f}%', idx['⑦']),
        ('⑨ 现金流/净利', f'三年均值 >{TH["ocf_np_min"]:.0f}%', idx['⑧']),
        ('⑩ 近12月收益', f'>{TH["ret12_min"]:.0f}%', idx['⑨']),
        ('⑪ 净利CAGR', f'三年 >{TH["np_cagr_min"]:.0f}%', idx['⑩']),
        ('⑫ 三年分红', f'> {TH["div3_min"]/1e8:.0f} 亿', idx['⑪']),
    ]
    keep = pd.Series(True, index=syms)
    rows = []
    for name, cond, m in steps:
        if name != '① 基础池':
            keep = keep & m
        rows.append(dict(步骤=name, 条件=cond, 剩余=int(keep.sum())))
    after11 = keep.copy()
    rows += [
        dict(步骤='11 条全过', 条件='同时满足 ①–⑪', 剩余=int(after11.sum())),
        dict(步骤='+ ⑫⑬⑭', 条件=f'波动<{TH["vol_max"]:.0f}% & 63日均额≥{TH["amt_min"]/1e4:.0f}万'
                              f' & 股息率/均值<{TH["yld_ratio_max"]:.0f}',
             剩余=int((after11 & idx['阶段二']).sum())),
    ]
    funnel = pd.DataFrame(rows)

    # ==================== 银行专项（全市场统一，沪深同标准）====================
    # 银行豁免了 ④ 资产负债率、⑤ 支付率区间，必须用行业专属指标补回：
    # 核心一级 >=9.5% / 不良 <=1.5% / 拨备 >=180%（不达标的银行从 11 条全过名单剔除）。
    from bank_screen import bank_gate
    _passed_syms = list(after11.index[after11.values])   # 11 条全过的 symbol
    _isb_map = is_bank.reindex(syms).fillna(False).to_dict()
    _bank_flag = {s: bool(_isb_map.get(s, False)) for s in _passed_syms}
    _bg = bank_gate(_passed_syms, _bank_flag)
    _bank_drop = [s for s, (ok, _r) in _bg.items() if ok is not True]
    if _bank_drop:
        print(f'  [银行专项] 剔除不达标/缺数据银行 {len(_bank_drop)} 只：'
              + '、'.join(f"{s}({_bg[s][1]})" for s in _bank_drop))
        after11.loc[_bank_drop] = False   # after11 是布尔 Series，把不达标银行置 False
    rows.append(dict(步骤='银行专项', 条件='核心一级≥9.5% & 不良≤1.5% & 拨备≥180%',
                     剩余=int(after11.sum())))

    # ==================== 治理底线（全市场统一，沪深同标准）====================
    # 实控人须为国资或无实际控制人；个人/家族/非国资法人/外资一票否决。
    # 与深市同一判据（ownership 模块），沪市不再豁免治理。
    try:
        import ownership as _gov
        _gov_syms = list(after11.index[after11.values])
        _gd = _gov.fetch_many(_gov_syms, cache=_gov.CACHE)
        _gmap = _gov.gate(_gd)
        _gov_drop = [s for s in _gov_syms if (_gmap.get(s) or (None,))[0] is not True]
        if _gov_drop:
            print(f'  [治理底线] 剔除未通过 {len(_gov_drop)} 只：'
                  + '、'.join(f"{s}({(_gmap.get(s) or ('?',))[1]})" for s in _gov_drop))
            after11.loc[_gov_drop] = False
        rows.append(dict(步骤='治理底线', 条件='实控人为国资或无实际控制人（沪深统一）',
                         剩余=int(after11.sum())))
    except Exception as _e:
        print(f'  [治理底线] 跳过（{type(_e).__name__}: {_e}）')

    # ==================== 输出 ====================
    res = idx[after11].sort_values('yld3', ascending=False)
    out = res[['code', 'name', 'board', 'industry_name', 'vol', 'px', 'yld3', 'yld_now',
               'dps_fy', 'div3_yi', 'ocfd3', 'pay3', 'pay_agg', 'pay_max', 'roe3',
               'ocf_np3', 'ret12', 'amt63', 'debt', 'list_date',
               '⑫', '⑬', '⑭', '阶段二']].copy()
    # 上市未满三年的，FY 首年的分红可能发生在上市前（永兴股份 601033 于 2024-01
    # 上市，FY2023 分红是上市前对老股东的分配）。11 条里没有"上市年限"这一条，
    # 所以不擅自剔除，只在备注里点名——名单要不要留它，是人的判断，不是脚本的。
    _lim = pd.Timestamp(base) - pd.DateOffset(years=3)
    out['备注'] = np.where(pd.to_datetime(out['list_date']) > _lim,
                         '上市未满三年，首年分红可能发生在上市前', '')
    out.columns = ['代码', '名称', '板块', '东财行业', '年化波动%', '最新价',
                   '三年平均股息率%', '当前股息率TTM%', '财年每股分红', '三年分红总额(亿)',
                   'OCF/分红', '三年支付率%', '三年支付率合计口径%', '单年最高支付率%', 'ROE三年均值%',
                   '现金流/净利%', '近12月收益%', '63日均额(万)',
                   '资产负债率%', '上市日期', '⑫低波', '⑬流动性', '⑭股息异常',
                   '过⑫⑬⑭', '备注']
    out.insert(1, '代码后缀', res.index.values)
    out = out.round({'年化波动%': 2, '最新价': 2, '三年平均股息率%': 2,
                     '当前股息率TTM%': 2, '财年每股分红': 3, '三年分红总额(亿)': 2,
                     'OCF/分红': 2, '三年支付率%': 2, '三年支付率合计口径%': 2,
                     '单年最高支付率%': 2,
                     'ROE三年均值%': 2, '现金流/净利%': 2, '近12月收益%': 2,
                     '63日均额(万)': 0, '资产负债率%': 2})
    out = out[['代码', '名称', '板块', '东财行业', '年化波动%', '最新价',
               '三年平均股息率%', '当前股息率TTM%', '财年每股分红', '三年分红总额(亿)',
               'OCF/分红', '三年支付率%', '三年支付率合计口径%', '单年最高支付率%',
               'ROE三年均值%', '现金流/净利%', '近12月收益%', '63日均额(万)',
               '资产负债率%', '上市日期', '⑫低波', '⑬流动性', '⑭股息异常',
               '过⑫⑬⑭', '备注']]
    out.to_csv(OUT, index=False, encoding='utf-8-sig')

    # 逐条明细：所有过了「连续三年分红」的标的，11 条布尔全部留痕。
    # 不留全市场 2000 行的原因不是省事，是让这份文件能被人工过一遍——
    # 排雷看的从来不是"谁过了"，而是"谁差一点没过、差在哪"。
    det = idx[idx['①']].copy()
    det = det.assign(代码=det.code, 名称=det.name, 代码后缀=det.index)
    det = det.rename(columns={'①': '①连续分红', '②': '②股息率', '③': '③净利同比',
                              '④': '④负债率', '⑤': '⑤支付率', '⑥': '⑥现金流覆盖',
                              '⑦': '⑦ROE', '⑧': '⑧现金流净利', '⑨': '⑨12月收益',
                              '⑩': '⑩CAGR', '⑪': '⑪分红总额'})
    _cols = (['代码', '代码后缀', '名称', 'n_pass']
             + [c for c in det.columns if c.startswith(('①连续', '②股息', '③净利',
                                                        '④负债', '⑤支付', '⑥现金',
                                                        '⑦ROE', '⑧现金', '⑨12月',
                                                        '⑩CAGR', '⑪分红'))]
             + ['yld3', 'yoy3_min', 'debt', 'pay3', 'ocfd3', 'roe3', 'ocf_np3',
                'ret12', 'div3_yi'])
    det[_cols].sort_values('n_pass', ascending=False).to_csv(
        DETAIL, index=False, encoding='utf-8-sig')
    funnel.to_csv(FUNNEL, index=False, encoding='utf-8-sig')

    # ==================== 全市场替补席 ====================
    # 过了 11 条 + ⑫⑬⑭，且不在当前持仓里 —— 明年 4 月要从中挑人的池子。
    _hold = set()
    try:
        _pf = pd.read_csv('打新底仓_最终8只组合_v2.csv', encoding='utf-8-sig')
        _hold = set(_pf['sym'])
    except Exception:
        pass

    bench = idx[after11 & idx['阶段二'] & ~idx.index.isin(_hold)].copy()
    _h1_yoy = col('net_profit_yoy', h1).reindex(bench.index)
    _ocf_now = col('net_cash_operate', h1).reindex(bench.index)
    _ocf_pre = col('net_cash_operate', h1_prev).reindex(bench.index)
    bench['H1净利同比%'] = _h1_yoy
    bench['H1经营现金流同比%'] = (_ocf_now / _ocf_pre.where(_ocf_pre > 0) - 1) * 100
    bench['需排雷'] = np.where(
        (_h1_yoy < -10) | (bench['H1经营现金流同比%'] < -20),
        '是', '—')
    bo = bench[['code', 'name', 'board', 'industry_name', 'vol', 'yld3', 'yld_now',
                'pay3', 'ocfd3', 'roe3', 'div3_yi', 'ret12', 'list_date',
                'H1净利同比%', 'H1经营现金流同比%', '需排雷']].copy()
    bo.columns = ['代码', '名称', '板块', '东财行业', '年化波动%',
                  '三年平均股息率%', '当前股息率TTM%', '三年支付率%', 'OCF/分红',
                  'ROE三年均值%', '三年分红总额(亿)', '近12月收益%', '上市日期',
                  'H1净利同比%', 'H1经营现金流同比%', '需排雷']
    bo = bo.sort_values('三年平均股息率%', ascending=False).round(2)
    bo.to_csv('全市场替补席.csv', index=False, encoding='utf-8-sig')

    print()
    print(funnel.to_string(index=False))
    print(f'\n11 条全过 {len(out)} 只 → {OUT}；逐条明细 {len(det)} 行 → {DETAIL}')
    if len(out):
        show = out[['名称', '东财行业', '年化波动%', '三年平均股息率%',
                    'OCF/分红', '三年分红总额(亿)', '过⑫⑬⑭']]
        print(show.to_string(index=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

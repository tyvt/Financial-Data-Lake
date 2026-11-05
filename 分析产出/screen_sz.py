"""深市打新底仓专用标准 · 全市场扫描

与主池的关系
------------
主池 11 条是沪市口径（股息率、支付率、资产负债率、净利增速……），直接套到
深市会两头错：深市低波高股息标的本来就少，11 条一过只剩个位数；而 11 条里
的「净利增速 > 0」「资产负债率 < 60%」对打新底仓没有解释力——底仓要的是
市值稳定 + 分红可持续，不是成长期权。

所以深市单列一套 7 条：

  1. 基础池   深市 A 股，剔除 ST / 退市整理
  2. 治理底线 实控人为国资或无实际控制人（个人 / 家族 / 外资直接剔除）
  3. 低波底线 年化波动率 < 25%（用重建的复权收益率）
  4. 股息底线 当前股息率 > 2.5%，且近三年连续分红
  5. 现金流   三年累计 OCF ÷ 三年累计分红 > 1
  6. 支付率   三年合计支付率 < 100%（《筛选标准_最终版》4.2 之②）
  7. 豁免     不看净利增速，不看资产负债率

口径
----
· 波动率：日收益标准差 × √252，252 个交易日窗口。收益率用**重建的**复权
  序列，不用 `qfq_close`——后者与 `close` 完全相等，是假复权（工单
  DL-2026-1018-01），直接用会把波动率算低或算高。
  重建公式：r_T = close_T / [(close_{T-1} − 每股现金红利) / (1+送股+转增)] − 1
· 分红：读湖内 `corporate_actions`（逐次除权记录，天然含中期分红），按**除权日**
  归因；股息率主口径取 TTM（近 365 天），另列「最近完整除权年度」作对照。
  不再外抓东财 fhps——湖的主源就是东财，外抓等于二手转发，而且抓的年度方案
  接口会整片丢掉中期分红（云南白药 1.038、格力 2.0、五粮液 2.578）。
· OCF：年报口径 `net_cash_operate`，三年累加。
· 分红总额 = 每 10 股派现 ÷ 10 × **除权日当日股本**（share_structure as-of 快照，
  缺失依次回退「最早快照 → 归母净利÷EPS 反推 → NaN 告警」）。不能用最新股本：
  转增/增发会把历史分红整体放大，同时污染 OCF/分红 与支付率两个门槛。

为什么先做低波再做治理
----------------------
治理要逐只外抓（有延时和重试），全市场 2791 只抓一遍要 1 小时以上。
先用本地数据把范围压到几十只，再定向抓实控人——这也是 `ownership.py`
设计成「传入候选列表」而不是全市场扫描的原因。

用法
----
    python screen_sz.py                 # 用本地缓存的分红数据
    python screen_sz.py --refresh        # 重抓治理核查（分红读湖，不联网）
    python screen_sz.py --no-gov         # 跳过治理（只看财务漏斗）
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

import ownership as gov

LAKE = 'D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb'
OUT = '深市专用标准_筛选结果.csv'
PEND = '深市专用标准_待确认.csv'
FUNNEL = '深市专用标准_漏斗.csv'

TH = dict(
    vol_max=25.0,        # 年化波动率上限（%）
    yld_min=2.5,         # 当前股息率下限（%）
    ocfd_min=1.0,        # 三年累计 OCF / 分红
    pay3_max=100.0,      # 三年合计支付率上限（%），《深市打新底仓专用标准》第 ② 条
    vol_win=252,         # 波动率窗口（交易日）
    min_obs=200,         # 有效收益观测下限，不足则不出波动率
    div_years=(2023, 2024, 2025),
)

PERIODS = ['20231231', '20241231', '20251231', '20250630', '20260630']


# ==================== 分红 ====================
def load_dividend(con) -> pd.DataFrame:
    """从湖内 `corporate_actions` 读全市场分红（除权日口径）。

    为什么不再外抓东财：`corporate_actions` 的主源就是东财，外抓等于同一份
    数据经二手转发一遍，而且抓的是「年度方案」接口——会整片丢掉中期分红
    （云南白药 2026-09-24 的 1.038、格力 2026-08-27 的 2.0、五粮液 2026 中期
    的 2.578 都不在年度方案里）。湖内是逐次除权的全量记录，天然含中期。

    代价：湖里没有「报告期」字段，用除权年直接归因会把两年的分红串到一年
    （五粮液一个自然年里既有 FY2024 年报方案、又有 FY2025 中期方案，叠加成
    8.32 元 / 71.30 元 = 11.67%，而它一年真实派现只有 5.16 元 = 7.23%）。
    解法是**从除权日反推财年**：

        财年 = (除权日 − 8 个月) 的年份

    年报分红次年 3–8 月除权、当年中期 9–12 月除权、跨年中期次年 1–2 月除权，
    三种情形减 8 个月后都落回自己的财年（详见 annual_review.load_dividend
    里的逐例验算）。这样 `dps_fy` 才是真的「上一财年分红」。

    输出列与原 fetch_dividend 保持一致（period / code / name / per10 /
    shares / ex_date / progress），下游 div_agg / ttm_dps 不用改。
    """
    ca = con.execute("""
        select symbol, ex_date, cash_dividend
        from src.corporate_actions
        where action_type = 'cash_dividend'
          and ex_date is not null and cash_dividend > 0
          and symbol not like '%.BJ'
    """).df()
    ca['ex_date'] = pd.to_datetime(ca.ex_date)

    # 股本必须取**除权日当日**的快照（as-of），不能取最新一期。
    # 原实现一律乘 symbol 最新 total_shares，等于拿今天的股本去乘三年前的分红：
    # 华润三九 2024-06 转增后 9.88 亿 → 12.85 亿、2025-06 再增到 16.66 亿，
    # 历史分红被整体放大，三年分红总额 54.76 亿被算成 64.24 亿，OCF/分红
    # 从 2.58 被压低到 2.20。支付率分子同样被放大——这条错会双向污染门槛。
    ss = con.execute("""
        select symbol, change_date, total_shares
        from src.share_structure where total_shares > 0
        order by change_date, symbol
    """).df()
    ss['change_date'] = pd.to_datetime(ss.change_date)
    m = pd.merge_asof(ca.sort_values('ex_date'), ss, left_on='ex_date',
                      right_on='change_date', by='symbol',
                      direction='backward')
    m['shares_src'] = np.where(m.total_shares.notna(), '除权日快照', '')

    # 三级回退（每级都记账，不静默）：
    #  ① 除权日早于该股第一条快照 → 用该股最早一条（湖内快照起点 2022-01-01）
    #  ② 该股完全没有股本记录（东莞控股、现代投资等）→ 归母净利 ÷ 基本每股收益
    #  ③ 连 EPS 都缺 → 保留 NaN，下游 div3 为 0 会自然落选，同时打印告警
    _first = ss.groupby('symbol').first()['total_shares']
    _fix1 = m.total_shares.isna()
    m.loc[_fix1, 'total_shares'] = m.loc[_fix1, 'symbol'].map(_first)
    m.loc[_fix1 & m.total_shares.notna(), 'shares_src'] = '最早快照'
    _lst = ','.join("'" + s + "'" for s in m.symbol.unique())
    imp = con.execute(f"""
        with x as (
          select symbol, item_code, item_value,
                 row_number() over (partition by symbol, item_code
                                    order by report_period desc) rn
          from src.financial_statement_items
          where symbol in ({_lst}) and report_period like '%Q4'
            and item_code in ('net_profit', 'eps')
        )
        select symbol,
               max(case when item_code='net_profit' then item_value end) np,
               max(case when item_code='eps'        then item_value end) eps
        from x where rn = 1 group by 1
    """).df()
    imp['shares_imp'] = imp.np / imp.eps.where(imp.eps > 0)
    _fix2 = m.total_shares.isna()
    m.loc[_fix2, 'total_shares'] = m.loc[_fix2, 'symbol'].map(
        imp.set_index('symbol').shares_imp)
    m.loc[_fix2 & m.total_shares.notna(), 'shares_src'] = '净利÷EPS 反推'

    m['code'] = m.symbol.str[:6]
    m['name'] = ''
    m['per10'] = pd.to_numeric(m.cash_dividend, errors='coerce') * 10
    m['shares'] = m.total_shares
    m['period'] = (m.ex_date - pd.DateOffset(months=8)).dt.year.astype(str)   # 财年，非除权年
    m['progress'] = '实施分配'                        # 湖内只有已实施的记录
    _nb = int(m.shares.isna().sum())
    if _nb:
        print(f'   [告警] {_nb} 笔分红无股本可依（已置 NaN，相关标的三年分红为 0）')
    return m[['period', 'code', 'name', 'per10', 'shares', 'ex_date', 'progress',
              'shares_src']]



def div_agg(div, syms=None, years=None):
    """按除权年份聚合：三年分红总额 + 每年每股分红（判连续分红用）。"""
    years = tuple(years or TH['div_years'])
    d = div.copy()
    if syms is not None:
        d = d[d.code.isin(syms)]
    d = d[d.per10.fillna(0) > 0]
    d['dps'] = d.per10 / 10.0
    d['amt'] = d.dps * d.shares

    rows = []
    for code, g in d.groupby('code'):
        per = {int(str(p)[:4]): 0.0 for p in g.period}
        for p, gg in g.groupby('period'):
            y = int(str(p)[:4])
            per[y] = per.get(y, 0.0) + float(gg.amt.sum())
        dps_by_y = {}
        for p, gg in g.groupby('period'):
            dps_by_y[int(str(p)[:4])] = float(gg.dps.sum())
        ys = years
        rows.append(dict(
            code=code,
            div3=sum(per.get(y, 0.0) for y in ys),
            dps3=sum(dps_by_y.get(y, 0.0) for y in ys),
            dps_fy=float(dps_by_y.get(ys[-1], 0.0)),
            n_years=sum(1 for y in ys if per.get(y, 0.0) > 0),
            dps_series=[round(dps_by_y.get(y, 0.0), 4) for y in ys],
            div_series=[round(per.get(y, 0.0), 0) for y in ys],
        ))
    return pd.DataFrame(rows)


def ttm_dps(div, base):
    """近 12 个月已实施的每股分红合计（除权日口径）。"""
    b = pd.Timestamp(base)
    d = div[(div.progress == '实施分配') & div.ex_date.notna()]
    d = d[(d.ex_date > b - pd.Timedelta(days=365)) & (d.ex_date <= b)]
    d = d.assign(dps=d.per10.fillna(0) / 10)
    return d.groupby('code').dps.sum()


# ==================== 复权收益率 ====================
def rebuilt_returns(con, syms, base, win=252):
    """重建前复权日收益矩阵。返回 DataFrame(index=date, columns=symbol)。

    取数区间必须比窗口宽：252 个交易日 ≈ 1.4 个自然年，只取 312 天会只剩
    约 205 个交易日，于是「252 日波动率」实际算的是 205 日，结果系统性偏高
    约 1 个百分点（双汇 19.79% vs 18.66%）。这里按 1.7 倍自然日取，再截尾。
    """
    lst = ','.join("'" + s + "'" for s in syms)
    days = int(win * 1.7)
    px = con.execute(f"""
        select symbol, trade_date, close from src.daily_bars
        where symbol in ({lst}) and trade_date <= DATE '{base}'
          and trade_date >= DATE '{base}' - INTERVAL {days} DAY
    """).df()
    px['trade_date'] = pd.to_datetime(px.trade_date)
    M = px.pivot(index='trade_date', columns='symbol', values='close').sort_index()
    M = M.ffill()

    ca = con.execute(f"""
        select symbol, ex_date, cash_dividend, bonus_ratio, transfer_ratio
        from src.corporate_actions
        where symbol in ({lst}) and ex_date is not null
          and ex_date >= DATE '{base}' - INTERVAL {days} DAY
    """).df()
    if len(ca):
        ca['ex_date'] = pd.to_datetime(ca.ex_date)
        ca['div'] = pd.to_numeric(ca.cash_dividend, errors='coerce').fillna(0)
        ca['fac'] = 1 + pd.to_numeric(ca.bonus_ratio, errors='coerce').fillna(0) \
            + pd.to_numeric(ca.transfer_ratio, errors='coerce').fillna(0)
        ca = ca.groupby(['symbol', 'ex_date']).agg(div=('div', 'sum'), fac=('fac', 'prod')).reset_index()

        # 除权日必须落到「该股实际有成交的第一天」，不能要求与除权日精确同日。
        # 除权日停牌是常见情形（600717 天津港 2026-06-18 除权，06-08 起停牌、
        # 06-23 复牌）：若按 ex_date 直接 reindex，当天没有行情则整条分红被丢掉，
        # 复牌日就变成「没除权的原始跌幅」——实测 600717 复牌日 +1.39%，
        # 真值 +3.85%，差的 2.46pct 正是 0.102 元分红被当成了下跌。
        # 反向情形同样错：若矩阵里有别的标的当天在交易，该行虽保留，却落在
        # 停牌日上，制造一个假的 −2.37% 和一个假的 +3.85%，波动率被抬起来。
        _sess = {s: M.index[M[s].notna().to_numpy()] for s in M.columns}
        _eff, _keep = [], []
        for _s, _d in zip(ca.symbol, ca.ex_date):
            _arr = _sess.get(_s)
            _p = _arr.searchsorted(_d) if _arr is not None and len(_arr) else 0
            if _arr is not None and 0 < _p < len(_arr):
                _eff.append(_arr[_p]); _keep.append(True)
            else:
                _eff.append(_d); _keep.append(False)
        ca['eff'] = _eff
        ca = ca[_keep]
        ca = ca.groupby(['symbol', 'eff']).agg(div=('div', 'sum'), fac=('fac', 'prod')).reset_index()
        D = ca.pivot(index='eff', columns='symbol', values='div').reindex(M.index).fillna(0)
        F = ca.pivot(index='eff', columns='symbol', values='fac').reindex(M.index).fillna(1)
    else:
        D = pd.DataFrame(0.0, index=M.index, columns=M.columns)
        F = pd.DataFrame(1.0, index=M.index, columns=M.columns)
    D = D.reindex(columns=M.columns).fillna(0)
    F = F.reindex(columns=M.columns).fillna(1)

    denom = (M.shift(1) - D) / F
    R = M / denom - 1
    return R.tail(win)


# ==================== 主流程 ====================
# 基准日判定统一在 lake_base.py，这里只保留别名，避免两处各写一份阈值。
from lake_base import pick_base  # noqa: E402
from data_sufficiency import Sufficiency  # noqa: E402

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true', help='重抓治理核查（分红读湖）')
    ap.add_argument('--no-gov', action='store_true', help='跳过治理核查（只跑财务漏斗）')
    ap.add_argument('--base', default=None, help='基准日，默认取最近一个完整交易日')
    args = ap.parse_args()

    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{LAKE}' AS src (read_only)")
    base = args.base or pick_base(con)
    print(f'基准日 {base}\n')

    # ---- ① 基础池 ----
    # ST 判定改为读湖内 `trading_status.risk_warning`（结构化字段），名称匹配降级
    # 为兜底：名字里带 ST 只是表象，摘帽/戴帽是交易所状态，`risk_warning` 才是
    # 那份状态的落库形式。两者取并集——只要任一信号为真就剔，宁可多剔不可漏剔：
    # 漏掉一只 ST 混进底仓，比少一只候选的代价大得多。
    # 另加 status='delisted'（退市整理）与 delist_date 判空双保险。
    tv = con.execute("select max(trade_date) from src.trading_status").fetchone()[0]
    uni = con.execute(f"""
        select symbol, name, list_date, substr(symbol,1,3) as board
        from src.instruments i
        where exchange='SZ' and asset_type='stock' and delist_date is null
          and symbol not like '200%'
          and not (name like '%ST%' or name like '%退%')
          and not exists (
              select 1 from src.trading_status t
              where t.symbol = i.symbol and t.trade_date = DATE '{tv}'
                and (coalesce(t.risk_warning, false) or t.status = 'delisted')
          )
    """).df()
    _rw = con.execute(f"""
        select count(*) from src.trading_status
        where trade_date = DATE '{tv}' and coalesce(risk_warning,false)
    """).fetchone()[0]
    uni['code'] = uni.symbol.str[:6]
    uni['board'] = uni.board.map(lambda x: '创业板' if x.startswith('30') else '主板')
    n0 = len(uni)
    print(f'① 基础池：深市 A 股（剔除 ST / 退市整理）{n0} 只'
          f'（主板 {(uni.board=="主板").sum()} / 创业板 {(uni.board=="创业板").sum()}）'
          f' · 全市场 risk_warning {_rw} 只（{tv}）')

    # ---- 价格 ----
    lst = ','.join("'" + s + "'" for s in uni.symbol)
    px = con.execute(f"""
        select symbol, arg_max(close, trade_date) px, arg_max(amount, trade_date) amt
        from src.daily_bars where symbol in ({lst}) and trade_date <= DATE '{base}'
        group by 1
    """).df()
    uni = uni.merge(px, on='symbol', how='left')

    # ---- ③ 低波 ----
    # 样本不足直接不给波动率：一个停牌半年的标的，ffill 能凑出 200 个
    # 非空收益，但那不是价格波动，是常数。这种数字参与筛选，等于把停牌
    # 当成低波。
    suf = Sufficiency(con, base, vol_win=TH['vol_win'])
    R = rebuilt_returns(con, uni.symbol.tolist(), base, TH['vol_win'])
    vol = R.std(ddof=1) * np.sqrt(252) * 100
    nobs = R.notna().sum()
    uni['vol'] = uni.symbol.map(vol)
    uni['nobs'] = uni.symbol.map(nobs)
    uni['returns_ok'] = uni.symbol.map(suf.check_returns(uni.symbol.tolist(), R)).fillna(False)
    uni['vol_reason'] = uni.symbol.map(suf.reasons_for(uni.symbol.tolist())).fillna('')
    uni.loc[~uni.returns_ok, 'vol'] = np.nan
    m3 = uni.vol.notna() & (uni.vol < TH['vol_max'])
    print(f'③ 低波 <{TH["vol_max"]}%：{m3.sum()} 只（有效样本不足 {n0 - int(uni.vol.notna().sum())} 只）')
    _ins = uni.loc[uni.vol.isna() & (uni.returns_ok == False), ['symbol', 'vol_reason']]
    if len(_ins):
        print(f'   样本不足未参与判定 {len(_ins)} 只，例：'
              + '、'.join(f'{r.symbol}({r.vol_reason})' for r in _ins.head(3).itertuples()))

    # ---- 分红（湖内 corporate_actions，财年口径）----
    # 除权年在 2026 的可能是 FY2025 年报、也可能是 FY2026 中期，所以窗口按
    # **财年**滚：最近三个完整财年是 base_y-3 .. base_y-1。
    base_y = int(str(base)[:4])
    years = (base_y - 3, base_y - 2, base_y - 1)
    div = load_dividend(con)
    da = div_agg(div, years=years)
    ttm = ttm_dps(div, base)
    uni = uni.merge(da, on='code', how='left').merge(
        ttm.rename('dps_ttm'), left_on='code', right_index=True, how='left')
    print(f'   分红来源：湖内 corporate_actions（财年口径 = 除权日−8个月，{len(div)} 条，窗口 {years}）')

    # ---- ④ 股息 <2.5% 先剔，再判连续分红 ----
    # 主口径回到**财年**（标准 2.3 节：底仓看的是「这家公司一年愿意分多少」，
    # 不是「过去 12 个月到账多少」）。TTM 作对照列——一年派两次且中期已滑出
    # 365 天窗口的标的（双汇 6.05% vs 3.34%），两个数会明显打架，差额本身就是信息。
    uni['yld_fy'] = uni.dps_fy / uni.px * 100
    uni['yld_ttm'] = uni.dps_ttm / uni.px * 100
    uni['yld'] = uni.yld_fy
    m4 = uni.yld_fy.fillna(0) > TH['yld_min']
    print(f'④ 股息率(财年口径) >{TH["yld_min"]}%：{m4.sum()} 只')
    m4b = m3 & m4 & (uni.n_years.fillna(0) >= 3)
    print(f'   且近三年连续分红：{m4b.sum()} 只')

    # ---- ⑤ 现金流 ----
    # 财年口径下分红与年报同属一年，现金流窗口不再移位：分红财年 2023–2025
    # 对应年报 2023–2025。（此前用除权年归因时要错开一年，现在不需要了。）
    ocf_years = years
    _rp = ','.join(f"'{y}Q4'" for y in ocf_years)
    fin = con.execute(f"""
        select symbol, report_period, item_value from src.financial_statement_items
        where item_code='net_cash_operate' and report_period in ({_rp})
          and symbol in ({lst})
    """).df()
    ocf = fin.groupby('symbol').item_value.sum().rename('ocf3')
    uni = uni.merge(ocf, on='symbol', how='left')
    uni['ocfd3'] = uni.ocf3 / uni.div3.replace(0, np.nan)
    m5 = m4b & (uni.ocfd3 > TH['ocfd_min'])
    print(f'⑤ 三年 OCF/分红 >{TH["ocfd_min"]:.0f}：{m5.sum()} 只')

    # ---- ⑥ 支付率（《筛选标准_最终版》4.2 深市三条分红底线之②）----
    # 这条此前**只写在文档里，代码没做**：主流程只查了股息率、连续分红、OCF 覆盖，
    # 于是三年合计支付率 118.77% 的东阿阿胶、100.27% 的华特达因一路留到名单里。
    # 支付率 ≥100% 意味着分红超过当年利润，只能消耗存量现金或加杠杆——这种股息
    # 不可持续，正好是"高股息陷阱"的定义（华特达因股息率 10.24%，全池最高）。
    # 口径与产物列一致：三年分红合计 ÷ 三年归母净利合计，分子分母同属财年。
    npq = con.execute(f"""
        select symbol, report_period, item_value from src.financial_statement_items
        where item_code='net_profit' and report_period in ({_rp})
          and symbol in ({lst})
    """).df()
    np3 = npq.groupby('symbol').item_value.sum().rename('np3')
    uni = uni.merge(np3, on='symbol', how='left')
    uni['pay3'] = np.where(uni.np3 > 0, uni.div3 / uni.np3 * 100, np.nan)

    # 单年最高支付率只作**展示与复核**用，不参与入选判定：书面标准卡的是三年合计，
    # 单年口径会被"一年派两次、财年归属错位"干扰（五粮液 2025 单年 223.58%，
    # 数据本身与年报一致，是中期+年报叠加在同一个财年造成的口径现象，不是算错）。
    _np_y = npq.pivot_table(index='symbol', columns='report_period',
                            values='item_value', aggfunc='sum')
    _py = []
    for _, _r in uni[['symbol', 'div_series']].iterrows():
        _s, _ser = _r['symbol'], _r['div_series']
        if not isinstance(_ser, list) or _s not in _np_y.index:
            _py.append(np.nan)
            continue
        _vals = []
        for _y, _amt in zip(years, _ser):
            _n = _np_y.loc[_s].get(f'{_y}Q4', np.nan)
            if pd.notna(_n) and _n > 0:
                _vals.append(_amt / _n * 100)
        _py.append(max(_vals) if _vals else np.nan)
    uni['pay_max'] = _py

    m6 = m5 & (uni.pay3 < TH['pay3_max'])
    _bad = uni.loc[m5 & ~m6, ['code', 'name', 'pay3', 'pay_max']]
    print(f'⑥ 三年支付率 <{TH["pay3_max"]:.0f}%：{m6.sum()} 只'
          f'（剔除 {len(_bad)} 只）')
    for _, _b in _bad.iterrows():
        print(f'   剔除 {_b.code} {_b["name"]} 三年支付率 {_b.pay3:.2f}%'
              f'（单年最高 {_b.pay_max:.2f}%）')

    # ---- 漏斗 ----
    funnel = pd.DataFrame([
        dict(步骤='① 基础池', 条件='深市 A 股，剔除 ST/退市整理', 剩余=n0),
        dict(步骤='③ + ④', 条件=f'波动 <{TH["vol_max"]}% 且 股息率 >{TH["yld_min"]}%',
             剩余=int((m3 & m4).sum())),
        dict(步骤='④ 连续分红', 条件='近三年每年均有现金分红', 剩余=int(m4b.sum())),
        dict(步骤='⑤ 现金流', 条件=f'三年累计 OCF/分红 >{TH["ocfd_min"]:.0f}', 剩余=int(m5.sum())),
        dict(步骤='⑥ 支付率', 条件=f'三年合计支付率 <{TH["pay3_max"]:.0f}%', 剩余=int(m6.sum())),
    ])

    cand = uni[m6].copy().sort_values('yld', ascending=False)

    # ---- ② 治理 ----
    if not args.no_gov and len(cand):
        print(f'\n② 治理核查（定向抓取 {len(cand)} 只）')
        gd = gov.fetch_many(cand.symbol.tolist(), cache=gov.CACHE, refresh=args.refresh)
        gmap = gov.gate(gd)
        cand['actual_holder'] = cand.symbol.map(lambda s: (gmap.get(s) or (None, '', '', ''))[3])
        cand['owner_type'] = cand.symbol.map(lambda s: (gmap.get(s) or (None, '数据缺失', '', ''))[1])
        cand['gov_ok'] = cand.symbol.map(lambda s: (gmap.get(s) or (None,))[0])
        n_pass = int((cand.gov_ok == True).sum())        # noqa: E712
        n_bad = int((cand.gov_ok == False).sum())        # noqa: E712
        n_na = int(cand.gov_ok.isna().sum())
        print(f'   通过 {n_pass} / 否决 {n_bad} / 缺失 {n_na}')
        funnel = pd.concat([funnel, pd.DataFrame([
            dict(步骤='② 治理底线', 条件='实控人为国资或无实际控制人', 剩余=n_pass)])],
            ignore_index=True)
    else:
        cand['actual_holder'] = ''
        cand['owner_type'] = ''
        cand['gov_ok'] = None

    # 三态必须分开落盘：通过 / 否决 / 待确认。把「待确认」混进入选等于放行，
    # 混进否决等于误杀——两种都会让结论失真，所以单列一张表。
    if args.no_gov:
        final, pending = cand, cand.iloc[0:0]
    else:
        final = cand[cand.gov_ok == True].copy()          # noqa: E712
        pending = cand[cand.gov_ok.isna()].copy()

    # ---- 银行专项（全市场统一，沪深同标准，与沪市同一模块）----
    # 银行豁免了资产负债率/支付率，必须补：核心一级≥9.5%/不良≤1.5%/拨备≥180%。
    from bank_screen import bank_gate
    _isb = {s: bool(('银行' in str(nm)) or ('农商行' in str(nm)))
            for s, nm in zip(final.symbol, final.name)}
    _bg = bank_gate(list(final.symbol), _isb)
    _bank_drop = [s for s, (ok, _r) in _bg.items() if ok is not True]
    if _bank_drop:
        print(f'  [银行专项] 剔除不达标/缺数据银行 {len(_bank_drop)} 只：'
              + '、'.join(f"{final.loc[final.symbol==s,'name'].iloc[0]}({_bg[s][1]})" for s in _bank_drop))
        final = final[~final.symbol.isin(_bank_drop)].copy()

    funnel = pd.concat([funnel, pd.DataFrame([
        dict(步骤='最终入选', 条件='同时满足 7 条（治理须明确通过）', 剩余=len(final)),
        dict(步骤='待确认', 条件='治理为「法人，名称无国资线索」或供销社等集体所有制，未计入入选',
             剩余=len(pending))])], ignore_index=True)

    # ---- 输出 ----
    allr = final if args.no_gov else cand
    _sym_series = allr['symbol'].reset_index(drop=True)   # 保留 symbol 供银行剔除比对
    out = allr[['symbol', 'code', 'name', 'board', 'vol', 'px', 'yld', 'yld_fy',
                 'yld_ttm', 'dps_ttm', 'dps_fy', 'dps3', 'div3', 'ocf3', 'ocfd3',
                 'pay3', 'pay_max', 'n_years', 'owner_type', 'actual_holder', 'gov_ok']].copy()
    out.columns = ['代码', '六位', '名称', '板块', '年化波动%', '最新价', '股息率%',
                   '财年口径%', '股息率TTM%', '近12月每股分红', '财年每股分红',
                   '三年每股分红合计',
                   '三年分红总额(亿)', '三年经营现金流(亿)', 'OCF/分红',
                   '三年支付率%', '单年最高支付率%', '连续分红年数',
                   '所有制性质', '实际控制人', '治理通过']
    for c in ['三年分红总额(亿)', '三年经营现金流(亿)']:
        out[c] = out[c] / 1e8
    out = out.round({'年化波动%': 2, '最新价': 2, '股息率%': 2, '财年口径%': 2,
                     '股息率TTM%': 2,
                     '近12月每股分红': 3, '三年每股分红合计': 3,
                     '三年分红总额(亿)': 2, '三年经营现金流(亿)': 2, 'OCF/分红': 2,
                     '三年支付率%': 2, '单年最高支付率%': 2})
    # 三张表都从同一份 out 切出来，避免「入选表里有、待确认表里没有」这类断链
    _ok = out['治理通过'].astype(str).str.lower().eq('true')
    _na = out['治理通过'].isna()
    # 银行专项不达标的，即便治理通过也不算入选（它们已在银行专项阶段被剔除）
    _bank_drop = locals().get('_bank_drop', [])
    if _bank_drop:
        _ok = _ok & (~_sym_series.isin(_bank_drop).values)
    out[_ok].to_csv(OUT, index=False, encoding='utf-8-sig')
    if _na.any():
        out[_na].to_csv(PEND, index=False, encoding='utf-8-sig')
    if (~_ok & ~_na).any():
        out[~_ok & ~_na].to_csv('深市专用标准_治理否决.csv', index=False, encoding='utf-8-sig')
    funnel.to_csv(FUNNEL, index=False, encoding='utf-8-sig')

    print('\n' + funnel.to_string(index=False))
    print(f'\n入选 {len(final)} 只 → {OUT}')
    if len(final):
        # 必须切 _ok，不能打全量 out：out 里还混着已否决的标的，
        # 打成一张表会出现「标题写入选 26 只、下面列了 46 行」的自相矛盾。
        show = out[_ok][['代码', '名称', '板块', '年化波动%', '股息率%',
                         '三年支付率%', 'OCF/分红', '所有制性质']]
        print(show.to_string(index=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

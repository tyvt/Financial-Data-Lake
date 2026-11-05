# -*- coding: utf-8 -*-
"""
打新底仓 · 周期复核

用途
----
把「什么时候该重拉清单」和「什么情况必须动仓」从人肉记忆变成可执行检查。

两个时间锚点，其余时间不重拉
  年度全量   4/30 年报披露截止后、5 月第一个交易周
             python annual_review.py
  半年报轻量 8/31 半年报截止后
             python annual_review.py --half
低波底仓的季度波动不是卖出理由。频繁拉清单，只会被短期数据噪音带着走。

两套判定，绝不能合并
  入选资格（entry）：「当初为什么选它」。偏离只记录，不构成动作。
  卖出触发（exit）：「底仓资格 / 分红纪律是否被破坏」。触发才动仓。
同一指标两层阈值不同是刻意的：银行核心一级入选要 9.5%，跌到 9.0% 才清仓，
中间 0.5pct 是留给经营波动的缓冲带；公用事业支付率入选要求单年 <80%，但
卖出线是连续两年 >80%——单年超限可能只是项目结算节奏，连续两年才是用存量
分红。用同一个数字兼顾两件事，会让正常波动直接触发清仓。

复核范围
  1) 入选资格：支付率、OCF/分红、年化波动、日均成交额（记录不动作）
  2) 卖出触发：银行资本、拨备+不良双条件、公用分红纪律、深市三只专项、波动红线
  3) 观察池：苏州银行、平安银行回补条件（半年报模式跳过）
  4) 组合层：权重合计、组合波动与最大回撤（按日收益序列合成，不用个股加权平均）
用法
    python annual_review.py                  # 年度全量（分红/行业读湖，不联网）
    python annual_review.py --half           # 半年报轻量：只跑卖出触发线
    python annual_review.py --base 2026-12-31  # 指定基准日（默认数据湖最新交易日）

设计要点
  · 持仓名单的单一真源是 打新底仓_最终8只组合_v2.csv，不在此处硬编码，
    避免「组合改了、复核脚本没改」。
  · 检查项用 qa_checks.audit()（软检查，不中断），一轮跑完看全貌。
  · 只有「卖出触发」「数据完整性」计入需处理；「入选资格」偏离单独统计，
    避免把"当年入选理由松动"直接当成"该卖了"。
  · 抓不到外部数据（东财银行指标）时标 SKIP，不伪造数字、不静默通过；
    数据缺失记入「数据完整性」，因为缺数据不能当作达标。
  · 不在复核窗口时照样能跑，但报告标题会标注「非复核窗口（跟踪）」。

注意：本脚本只能判断「规则是否被触发」。触发任何一条，脚本会标出来，但
「要不要卖」仍是人的判断——单年波动还是趋势恶化，估值和替代机会如何，
脚本都不知道。判定通过也不等于决策正确。
"""
import argparse
import datetime
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
import numpy as np
import pandas as pd
from lake_base import pick_base as _pick_base

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_checks import audit, audit_report
import ownership as gov

# ==================== 路径 ====================
LAKE = 'D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb'
HOLD = '打新底仓_最终8只组合_v2.csv'

# ==================== 分红 / 行业：统一读湖（2026-10-20 改） ====================
# 不再外抓东财年度方案表，也不再读早已删除的 dividend_by_report.parquet 缓存——两者都是
# 「按报告期归因」的年度方案视图，会整片漏掉中期分红（云南白药 2026-09-24 的
# 1.038、五粮液 2026 中期的 2.578 都不在里面），支付率被系统性低估。
# 现在统一读湖内 corporate_actions（除权日口径），与 screen_sz.py 一致。
#
# 代价：湖内没有「报告期」字段，只能从除权日反推财年。本脚本写死的对应是
#   财年 = (除权日 − 8 个月) 的年份
# 这样年报分红（次年 3–8 月除权）、当年中期（9–12 月）、跨年中期（次年 1–2 月）
# 三种情形会落到同一个财年上，详见 load_dividend 里的注释。
# 股息率则用 TTM：近 365 天已实施分红 ÷ 现价，按除权日滚 12 个月，不反推财年。
# 支付率问的是「这一年赚了多少、分了多少」，分子分母必须同属一个财年；
# 股息率问的是「按现价一年能收多少现金」，必须按除权日滚 12 个月。
DIV_SHIFT = 1        # 历史遗留常量，已不参与计算（除权日 − 8 个月定财年）

# 申万一级：industry_code 前两位。行业的唯一真源是湖内 industry_members
# （classification_system='sw'），组合表里的 ind 列只作核对，不再作判据——
# 8 只手写标签曾是一个单点：改错一个字，35% 行业上限就静默失效。
SW1_NAME = {
    '11': '农林牧渔', '22': '基础化工', '23': '钢铁', '24': '有色金属',
    '27': '电子', '28': '汽车', '33': '家电', '34': '食品饮料',
    '35': '纺织服饰', '36': '轻工制造', '37': '医药', '41': '公用事业',
    '42': '交运', '43': '房地产', '45': '商贸零售', '46': '社会服务',
    '48': '银行', '49': '非银金融', '51': '综合', '61': '建材',
    '62': '建筑', '63': '电力设备', '64': '机械', '65': '军工',
    '71': '计算机', '72': '传媒', '73': '通信', '74': '煤炭',
    '75': '石油石化', '76': '环保', '77': '美容护理',
}

# ==================== 阈值（改这里，不要改正文） ====================
TH = dict(
    # ---- 入选资格（entry）：当初为什么选它 ----
    vol_max=25.0,          # ⑫ 年化波动率上限（同时是卖出红线）
    vol_warn=23.0,         # 接近阈值预警
    vol_half_warn=26.0,    # 近半年口径（波动抬升更快）
    amt_min=5000.0,        # ⑬ 日均成交额下限（万元）
    pay_shallow=80.0,      # 沪市 11 条：支付率 30–80%
    pay_deep=100.0,        # 深市子池底线：支付率 <100%
    ocfd_min=1.0,          # 经营现金流 / 分红 下限
    cet1_sh=9.5,           # 沪市银行专项：核心一级（入选线）
    cet1_sz=9.0,           # 深市银行：核心一级门槛
    npl_max=1.5,           # 不良率上限
    cover_min=180.0,       # 拨备覆盖率下限
    amt_win=63,            # 流动性窗口（交易日）
    vol_win=252,           # 波动率窗口
    # ---- 卖出触发线（exit）：什么情况下必须动仓 ----
    # 与 entry 不同值是刻意的。银行核心一级入选要 9.5%、跌到 9.0% 才清仓，
    # 中间 0.5pct 是给经营波动的缓冲带——用同一个数字当入选线和卖出线，
    # 会让一次正常波动直接触发清仓。
    cet1_exit=9.0,         # 银行：资本不足以支撑分红
    cover_exit=180.0,      # 银行：拨备跌破
    npl_rise=0.05,         # 银行：不良率环比上升的判定精度（pct）
    pay_util_exit=80.0,    # 公用/交运：支付率连续两年超此线
    pay_gree_exit=75.0,    # 格力：支付率连续两年超此线 → 深市内权重回调 35%
    gcd_drop_exit=30.0,    # 云南白药：每股分红同比降幅超此线 → 深市内权重 ≤15%
    # ---- 组合层风险上限（与个股基本面无关，只管集中度） ----
    # 没有上限时，公式会把权重推给「暂时最平稳」的标的：某个季度它的波动
    # 和回撤都低，score 就高，权重跟着涨。这是低波陷阱——用「过去很稳」
    # 换取「未来押太重」，一次暴雷就击穿整个底仓。
    w_single_max=25.0,     # 单一标的权重上限（%）
    w_ind_max=35.0,        # 单一行业权重上限（%）
)

# ==================== 黑天鹅登记簿 ====================
# 脚本抓不到立案调查、非标审计意见、重大安全事故——这些不在任何结构化
# 数据源里。所以只能做成人工登记 + 脚本强制触发：
#   · 登记了 → 一票否决，不看财务数据，无条件移出；
#   · 没登记 → 明细写「未登记」，不写「已确认无风险」。
# 差别很重要：后者会让读报告的人误以为系统检查过。
SWAN = '黑天鹅登记簿.csv'
SWAN_CLEARED = ('已解除', '解除', 'closed')
BUYPOOL = '深市替补席.csv'

# ==================== 治理底线（实控人性质） ====================
# 实控人性质不在数据湖里，只能外抓（见 ownership.py）。本脚本是「闸门」：
# 深市准入硬条件 + 持仓一律复查。约束适用于深市，是因为它是深市子池的
# 第④条底线；沪市持仓目前全部为国资/无实控人，暂列警示不列硬否决。
# 若要升级为全池硬门，把这里改成 ('沪', '深') 即可，判定逻辑无需改动。
GOV_MARKETS = ('深',)
GOV_CACHE = gov.CACHE

# ==================== 观察池（不持仓，但保留回补通道） ====================
WATCH = {
    '002966.SZ': dict(name='苏州银行', rule='连续两个报告期核心一级 ≥9.5% 且支付率 ≥30%'),
    '000001.SZ': dict(name='平安银行', rule='支付率连续两年 ≥28% 且核心一级 ≥9.3%'),
}

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
      'Referer': 'https://data.eastmoney.com/'}


def load_blackswan(path=SWAN):
    """读黑天鹅登记簿，返回标准列的空表或数据表。

    status 取「已解除」等值时不计入有效记录；其余（含空）一律视为有效。
    空状态按有效处理是刻意的——漏填状态时，宁可多报一次警，也不要
    因为一个空格让真正的风险被静默忽略。
    """
    cols = ['symbol', 'name', 'date', 'type', 'detail', 'status', 'clear_date']
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    d = pd.read_csv(path, encoding='utf-8-sig', dtype=str).fillna('')
    for c in cols:
        if c not in d.columns:
            d[c] = ''
    d = d[cols].copy()
    d['symbol'] = d['symbol'].str.strip()
    d['status'] = d['status'].str.strip()
    return d


def active_swans(df):
    """未解除的黑天鹅记录。status 留空视为有效——宁可误报一次。"""
    if not len(df):
        return df
    return df[~df.status.isin(SWAN_CLEARED)]


def backup_ok(vol, pay_max, ocfd3, th=TH):
    """替补资格：仍满足深市三条底线（波动、支付率上限、OCF 覆盖）。

    任一项失效即不可递补——替补席不是保险箱，它每年也要过同一道门。
    """
    return (vol < th['vol_max']) and (pay_max < th['pay_deep']) and (ocfd3 > th['ocfd_min'])


def judge_governance(name, info, market='深', gate=GOV_MARKETS):
    """治理底线判定。返回 (ok, detail, action, group)。

    info = (ok, owner_type, reason, actual_holder)，或 None（未核查）。

    三态在这里必须压成两态，但压法不同：
      · 缺失 → group='数据完整性'，FAIL。缺数据不是通过，也不能算「触犯底线」，
        否则分不清「这家是家族企业」和「这家没查到」。
      · 否决 → group='治理底线'，FAIL，给出清仓换仓动作。
    分组不同决定了读报告的人该做什么：补数，还是换仓。
    """
    if info is None:
        return (False, '未核查（实控人字段缺失）',
                '运行 python ownership.py --refresh 后重核', '数据完整性')
    ok, typ, why, holder = info
    if ok is None:
        return (False, f'{typ}：{why}', '补数后重核（缺失不视为通过）', '数据完整性')
    if ok:
        return True, f'{typ}（{holder or "无"}）', '', '治理底线'
    act = ('【移出】触犯治理底线，建议清仓换仓'
           if market in gate else
           '【警示】触犯治理底线，建议清仓换仓（该市场当前不作为硬否决，需人工决定）')
    return False, f'{typ}：{why}', act, '治理底线'


def _sym_list(syms):
    return ','.join("'" + s + "'" for s in syms)


# ==================== 数据加载 ====================
def latest_trade_date(con):
    """取可用基准日：委托 lake_base.pick_base，全系统只保留一份判据。

    历史上这里写过一版「前 5 日均值 70%」的检查，screen_sz 又写了一份
    「前 20 日中位数 80%」的——同一件事两个阈值，早晚会给出两个基准日。
    合并后单一实现，两个脚本调用同一个函数。
    """
    return _pick_base(con)


def infer_year(con):
    """最近一个「完整」会计年度：有 Q4 数据且披露家数 > 1000。"""
    r = con.execute("""
        select substr(report_period,1,4) y
        from src.financial_statement_items
        where report_period like '%Q4'
        group by 1 having count(distinct symbol) > 1000
        order by 1 desc limit 1
    """).fetchone()
    return int(r[0])


def load_dividend(con, syms):
    """读湖内 `corporate_actions` 的现金分红（除权日口径）。

    与 screen_sz.load_dividend 同源同口径，两个脚本的股息率、支付率必须能对上。
    输出列里 `ydiv` 是**除权年**，不是报告期年份——下游 div_by_year 依赖这一点。
    """
    lst = _sym_list(syms)
    ca = con.execute(f"""
        select symbol, cast(ex_date as date) ex_date, cash_dividend
        from src.corporate_actions
        where action_type = 'cash_dividend'
          and ex_date is not null and cash_dividend > 0
          and symbol in ({lst})
    """).df()
    if not len(ca):
        return pd.DataFrame(columns=['symbol', 'ex_date', 'ydiv', 'dps', 'amt_yi'])
    # 股本：按**除权日当日**的股本快照（as-of），缺失时依次回退到
    # 「该股最早快照」→「归母净利 ÷ 基本每股收益」反推。
    # 不能取最新一期：华润三九 2024-06 转增、2025-06 再增发，拿今天的股本乘
    # 两年前的分红，三年分红总额会从 54.76 亿被算成 64.24 亿，支付率与
    # OCF/分红 同时失真。反推那条不是锦上添花——唐山港 601000.SH 在
    # share_structure 里没有记录，直接乘会得到 NaN，三年支付率变成 0%。
    ss = con.execute(f"""
        select symbol, change_date, total_shares
        from src.share_structure where total_shares > 0 and symbol in ({lst})
        order by change_date, symbol
    """).df()
    imp = con.execute(f"""
        with x as (
          select symbol, item_code, item_value,
                 row_number() over (partition by symbol, item_code
                                    order by report_period desc) rn
          from src.financial_statement_items
          where symbol in ({lst}) and report_period like '%Q4'
            and item_code in ('net_profit', 'eps')
        )
        select symbol,
               max(case when item_code='net_profit' then item_value end) np,
               max(case when item_code='eps'        then item_value end) eps
        from x where rn = 1 group by 1
    """).df()
    imp['shares_imp'] = imp.np / imp.eps.where(imp.eps > 0)

    ca['ex_date'] = pd.to_datetime(ca.ex_date)
    if len(ss):
        ss['change_date'] = pd.to_datetime(ss.change_date)
        m = pd.merge_asof(ca.sort_values('ex_date'), ss, left_on='ex_date',
                          right_on='change_date', by='symbol',
                          direction='backward')
    else:
        m = ca.copy()
        m['total_shares'] = np.nan
    _first = ss.groupby('symbol').first()['total_shares'] if len(ss) else pd.Series(dtype=float)
    _miss1 = m.total_shares.isna()
    m.loc[_miss1, 'total_shares'] = m.loc[_miss1, 'symbol'].map(_first)
    m = m.merge(imp[['symbol', 'shares_imp']], on='symbol', how='left')
    _miss2 = m.total_shares.isna()
    m.loc[_miss2, 'total_shares'] = m.loc[_miss2, 'shares_imp']
    m['shares'] = m.total_shares
    m['ydiv'] = m.ex_date.dt.year
    # 财年 = (除权日 − 8 个月) 的年份。为什么不是「除权年」也不是「除权年−1」：
    # A 股一年可能派两次，除权日落在哪个日历年和财年没有固定偏移——
    #   年报分红   次年 3–8 月除权        (−8 月 → 上年 7–12 月，年份 = 财年)
    #   中期分红   当年 9–12 月除权       (−8 月 → 当年 1–4 月，年份 = 财年)
    #   中期分红   次年 1–2 月除权        (−8 月 → 上年 5–6 月，年份 = 财年)
    # 云南白药的 1.019（2025-09-24，FY2025 中期）和格力的 1.00（2026-01-23，
    # FY2025 三季报）同为 FY2025，除权年却差一年。用除权年归因会把中期分红
    # 串进上一个财年，支付率直接被推到 143%。减 8 个月把三种情形统一对齐。
    m['fy'] = (m.ex_date - pd.DateOffset(months=8)).dt.year
    m['dps'] = pd.to_numeric(m.cash_dividend, errors='coerce')
    m['amt_yi'] = m.dps * m.shares / 1e8
    return m[['symbol', 'ex_date', 'ydiv', 'fy', 'dps', 'amt_yi']]


def ttm_dps(div, base, syms=None):
    """近 365 天已实施每股分红合计（除权日口径），用于股息率。"""
    b = pd.Timestamp(base)
    d = div[(div.ex_date > b - pd.Timedelta(days=365)) & (div.ex_date <= b)]
    if syms is not None:
        d = d[d.symbol.isin(syms)]
    return d.groupby('symbol').dps.sum()


def ind_from_lake(con, syms):
    """湖内申万一级行业映射 symbol -> 一级行业名。

    读不到就返回空 dict，由调用方保留组合表标签并打印提示——行业缺失会
    让 35% 上限失效，所以宁可显式告警，也不静默当「综合」处理。
    """
    lst = _sym_list(syms)
    try:
        rows = con.execute(f"""
            select symbol, industry_code from src.industry_members
            where classification_system = 'sw'
              and as_of_date = (select max(as_of_date) from src.industry_members
                                where classification_system = 'sw')
              and symbol in ({lst})
        """).fetchall()
    except Exception as exc:                                   # noqa: BLE001
        print(f'  [WARN] 申万行业读湖失败（{type(exc).__name__}: {exc}），沿用组合表标签')
        return {}
    return {s: SW1_NAME.get(str(c)[:2], f'码{str(c)[:2]}') for s, c in rows}


def ttm_net_profit(con, syms, rp):
    """TTM 净利润（亿元）：最近完整财年 + 本期累计 − 去年同期累计。

    湖内 report_period 的 item_value 是**年初至今累计**（Q2 约等于 Q4 的一半），
    所以 TTM 不能简单把四个季度相加。
    """
    if not rp:
        return pd.Series(dtype=float)
    y, q = int(rp[:4]), rp[4:]
    prev = f'{y - 1}{q}'
    pers = sorted({f'{y - 1}Q4', rp, prev})
    lst = ', '.join("'" + p + "'" for p in pers)
    f = con.execute(f"""
        select symbol, report_period, max(item_value)/1e8 as np_yi
        from src.financial_statement_items
        where symbol in ({_sym_list(syms)}) and item_code='net_profit'
          and report_period in ({lst})
        group by 1,2
    """).df()
    if not len(f):
        return pd.Series(dtype=float)
    p = f.pivot_table(index='symbol', columns='report_period', values='np_yi', aggfunc='max')
    fy = p[f'{y - 1}Q4'] if f'{y - 1}Q4' in p else 0.0
    cur = p[rp] if rp in p else 0.0
    old = p[prev] if prev in p else 0.0
    return (fy + cur - old).dropna()


def load_fin(con, syms, y0, y3):
    return con.execute(f"""
        select symbol, cast(substr(report_period,1,4) as int) y,
               max(case when item_code='net_profit'       then item_value end)/1e8 as np_yi,
               max(case when item_code='net_cash_operate' then item_value end)/1e8 as ocf_yi
        from src.financial_statement_items
        where symbol in ({_sym_list(syms)}) and report_period like '%Q4'
          and cast(substr(report_period,1,4) as int) between {y0} and {y3}
        group by 1,2 order by 1,2
    """).df()


def div_by_year(div, y0, y3):
    """按**财年**聚合每股分红与分红总额（亿元）。

    财年由 `load_dividend` 用「除权日 − 8 个月」算好（`fy` 列），与年报年份
    直接对齐，调用方不需要再移位。
    """
    d = div[div.fy.between(y0, y3)].copy()
    if not len(d):
        return pd.DataFrame(columns=['symbol', 'y', 'dps', 'amt_yi'])
    g = d.groupby(['symbol', 'fy']).agg(dps=('dps', 'sum'), amt_yi=('amt_yi', 'sum')).reset_index()
    return g.rename(columns={'fy': 'y'})


def load_returns(con, syms, base):
    """重建前复权日收益率 + 复权净值最大回撤。

    2026-10-19 起 daily_bars_adj.qfq_close 已修好（与自建结果逐日一致，
    偏差 <0.01pct），但仍保留自建：它是独立于该字段的第二条路径，
    两边对不上就说明湖里又出问题了——这是校验价值，不是重复劳动。
    """
    px = con.execute(f"""
        select symbol, trade_date, close from src.daily_bars
        where symbol in ({_sym_list(syms)}) and trade_date <= DATE '{base}' and close > 0
        order by 1,2
    """).df()
    ca = con.execute(f"""
        select symbol, cast(ex_date as date) ex_date,
               coalesce(cash_dividend,0) cash,
               coalesce(bonus_ratio,0) bonus,
               coalesce(transfer_ratio,0) transfer
        from src.corporate_actions
        where symbol in ({_sym_list(syms)}) and cast(ex_date as date) <= DATE '{base}'
    """).df()

    ca['ex_date'] = pd.to_datetime(ca.ex_date)

    out = []
    for s, g in px.groupby('symbol'):
        g = g.sort_values('trade_date').reset_index(drop=True)
        g['c_prev'] = g['close'].shift(1)
        c = ca[ca.symbol == s].sort_values('ex_date')
        # 除权日必须映射到「该股实际有成交的第一天」：除权日停牌是常见情形
        # （600717 天津港 2026-06-18 除权、06-08 起停牌、06-23 复牌），
        # 按 ex_date 精确 merge 会把这条分红丢掉，复牌日就变成未除权的原始
        # 跌幅——实测 +1.39% 而真值 +3.85%，差的 2.46pct 就是被当成下跌的分红。
        g['eff'] = pd.NaT
        g['cash'] = 0.0
        g['bonus'] = 0.0
        g['transfer'] = 0.0
        if len(c):
            dates = g.trade_date.to_numpy()
            for row in c.itertuples():
                pos = int(np.searchsorted(dates, np.datetime64(row.ex_date)))
                if pos < len(dates):
                    g.loc[pos, 'eff'] = g.trade_date.iloc[pos]
                    g.loc[pos, 'cash'] = float(row.cash)
                    g.loc[pos, 'bonus'] = float(row.bonus)
                    g.loc[pos, 'transfer'] = float(row.transfer)
        prev_adj = (g['c_prev'] - g['cash']) / (1 + g['bonus'] + g['transfer'])
        g['r'] = np.where(g['eff'].notna(), g['close'] / prev_adj - 1,
                          g['close'] / g['c_prev'] - 1)
        out.append(g[['symbol', 'trade_date', 'close', 'r']])
    return pd.concat(out, ignore_index=True)


def load_amount(con, syms, base, win):
    return con.execute(f"""
        with d as (
          select symbol, amount,
                 row_number() over (partition by symbol order by trade_date desc) rk
          from src.daily_bars
          where symbol in ({_sym_list(syms)}) and trade_date <= DATE '{base}'
            and amount is not null)
        select symbol, avg(amount)/1e4 as amt_wan from d where rk <= {win} group by 1
    """).df()


def load_calendar(con, base, win):
    """最近 `win` 个**交易日**（交易所日历，不是各股自己的成交日）。

    为什么必须用日历：原实现是 `r.dropna().tail(252)`——对停牌过的标的说，
    252 行成交记录可能横跨 300+ 个自然交易日，于是「252 日波动率」实际量的是
    更长窗口，和写进文档、写进阈值的那条定义不是同一个东西。停牌日按 0 收益
    处理（无成交即无损益），窗口长度对所有标的**严格一致**。
    """
    d = con.execute(f"""
        select trade_date from src.trading_calendar
        where is_trading and trade_date <= DATE '{base}'
        order by trade_date desc limit {win}
    """).df()
    return pd.DatetimeIndex(sorted(pd.to_datetime(d.trade_date)))


# ==================== 银行专项（东财 F10，数据湖无此字段） ====================
def fetch_bank(sym):
    """湖内银行快照优先；首次缺失时抓取并入湖。定时更新负责刷新。"""
    from external_lake import bank_from_lake, store_bank, configure_proxy
    local = bank_from_lake(sym)
    if local is not None:
        return local
    configure_proxy()
    """抓银行专项指标（东财 F10）。

    注意：columns 中任一字段名不存在会导致整个请求失败并返回
    success=false（如 CAPITALADER 是错误的，应为 NEWCAPITALADER）。
    改动字段时务必先单测，否则会静默拿不到数据。
    """
    url = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
    p = {'reportName': 'RPT_F10_FINANCE_MAINFINADATA',
         'columns': ('SECUCODE,REPORT_DATE,REPORT_DATE_NAME,HXYJBCZL,'
                     'NEWCAPITALADER,NONPERLOAN,BLDKBBL,NET_INTEREST_MARGIN,ROEJQ'),
         'filter': f'(SECUCODE="{sym}")', 'pageSize': '12', 'pageNumber': 1,
         'sortColumns': 'REPORT_DATE', 'sortTypes': -1, 'source': 'HSF10', 'client': 'PC'}
    try:
        r = urllib.request.Request(url + '?' + urllib.parse.urlencode(p), headers=UA)
        j = json.load(urllib.request.urlopen(r, timeout=25))
        if not j.get('success'):
            print(f'  [SKIP] 银行指标接口报错 {sym}: {j.get("message")}')
            return None
        rows = (j.get('result') or {}).get('data') or []
        if rows:
            store_bank(sym, rows)
        return pd.DataFrame(rows) if rows else None
    except Exception as e:
        print(f'  [SKIP] 银行指标抓取失败 {sym}: {e}')
        return None


# ==================== 判定规则（纯函数，便于单测） ====================
def sw_triggers(pay_series, ocfd_series, pay_deep=TH['pay_deep'], ocfd_min=TH['ocfd_min']):
    """深市标的通用的两条分红触发线（支付率上限 / OCF·分红连续性）。

    返回 (支付率是否超上限, OCF/分红是否连续两年低于下限)。
    设计要点：单年 OCF/分红 <1 可容忍（2023 年双汇 0.67，次年修复到 1.72），
    连续两年才说明分红在持续消耗存量现金。该判例随双汇清仓仍保留——
    规则由个案长出来，但不随个案退场。

    缺失值（None/NaN）计为「不满足」，不跳过——缺一年数据不能用来拼出
    「连续两年」，否则数据缺口会被当成恶化证据。
    """
    ok = lambda v: v is not None and v == v
    over = any(ok(v) and v > pay_deep for v in pay_series)
    flags = [bool(ok(v) and v < ocfd_min) for v in ocfd_series]
    consec = any(flags[i] and flags[i + 1] for i in range(len(flags) - 1))
    return over, consec


def judge_holding(r, th=TH):
    """单只持仓的达标判定，返回 [(检查项, 是否通过, 明细, 动作)]。

    供 annual_review 主流程与负向测试共用，避免判定逻辑只在 main 里存在。
    """
    deep = r['market'] == '深'
    pay_cap = th['pay_deep'] if deep else th['pay_shallow']
    nm = r['name']
    return [
        (f'{nm} 连续三年分红', r['years_ok'], f"实际 {len(r['pay_series'])} 年", '移出，分红意愿不稳定'),
        (f'{nm} 支付率 <{pay_cap:.0f}%', r['pay_max'] < pay_cap, f"单年最高 {r['pay_max']:.1f}%", '透支式分配，降级观察仓'),
        (f'{nm} OCF/分红 >{th["ocfd_min"]:.0f}', r['ocfd3'] > th['ocfd_min'], f"三年 {r['ocfd3']:.2f}", '分红现金流覆盖不足，移出'),
        (f'{nm} 年化波动 <{th["vol_max"]:.0f}%', r['vol'] < th['vol_max'], f"实际 {r['vol']:.2f}%", '移出，与底仓市值稳定目标不兼容'),
        (f'{nm} 近半年波动 <{th["vol_half_warn"]:.0f}%', r['vol_h'] < th['vol_half_warn'], f"实际 {r['vol_h']:.2f}%", '波动抬升，降权并跟踪'),
        (f'{nm} 日均成交 ≥{th["amt_min"]:.0f}万', r['amt_wan'] >= th['amt_min'], f"实际 {r['amt_wan']:,.0f}万", '流动性不足，移出'),
    ]


def load_fin_period(con, syms, periods):
    """按报告期取净利润与经营现金流（半年报轻量模式用）。"""
    if not periods:
        return pd.DataFrame(columns=['symbol', 'report_period', 'np_yi', 'ocf_yi'])
    lst = ', '.join("'" + p + "'" for p in periods)
    return con.execute(f"""
        select symbol, report_period,
               max(case when item_code='net_profit'       then item_value end)/1e8 as np_yi,
               max(case when item_code='net_cash_operate' then item_value end)/1e8 as ocf_yi
        from src.financial_statement_items
        where symbol in ({_sym_list(syms)}) and report_period in ({lst})
        group by 1,2
    """).df()


def latest_report_period(con, min_syms=3000):
    """最近一个披露家数足够多的报告期（含半年报，用于轻量模式）。"""
    r = con.execute("""
        select report_period, count(distinct symbol) n
        from src.financial_statement_items
        group by 1 having n > ? order by 1 desc limit 1
    """, [min_syms]).fetchone()
    return r[0] if r else None


_Q_END = {'Q1': '03-31', 'Q2': '06-30', 'Q3': '09-30', 'Q4': '12-31'}


# 除权日口径下不再需要 div_last_avail / div_one_period（原来按报告期取数），
# 半年报的当期支付率改由 ttm_net_profit + ttm_dps 合成，见主流程。


# ==================== 时间锚点 ====================
CAL = dict(full=((4, 30), (5, 15)), half=((8, 31), (9, 15)))


def window_note(today, half=False):
    """返回 (是否在复核窗口, 提示文案)。

    A 股年报法定截止 4/30、半年报 8/31。全量复核放在年报后（分红方案、
    支付率、经营现金流都已落地），半年报后只做轻量——数据完整度不足以
    支撑全量判定。其余时间跑出来的结果会被未更新的报告期污染。
    """
    md = (today.month, today.day)
    for (a, b), label, mode in ((CAL['full'], '年度全量', 'full'),
                                (CAL['half'], '半年报轻量', 'half')):
        if a <= md <= b:
            want = (mode == 'half')
            same = (want == half)
            return True, (f'[窗口] {today} 处于{label}复核窗口（{a[0]}/{a[1]}–{b[0]}/{b[1]}）'
                          + ('' if same else '；当前模式与窗口不匹配，请确认 --half 是否选对'))
    return False, (f'[窗口] {today} 不在复核窗口。锚点：年报披露截止后（5 月首个交易周，全量）、'
                   f'半年报截止后（9 月首个交易周，轻量）。低波底仓的季度波动不是卖出理由，'
                   f'此时跑仅供跟踪，结论不宜直接执行。')


# ==================== 卖出触发线（纯函数，便于单测） ====================
LV_OK, LV_TRIM, LV_EXIT, LV_SKIP = 'OK', 'TRIM', 'EXIT', 'SKIP'


def consec(vals, pred, n=2):
    """vals 中是否出现连续 n 个满足 pred 的值。单年噪声不算触发。

    缺失值视为「不满足」而不是「跳过」：若某年分红数据缺失，前后两年不能
    拼成「连续两年」——否则缺数据会被当成恶化证据。
    """
    f = [bool(v is not None and v == v and pred(v)) for v in vals]
    return any(all(f[i:i + n]) for i in range(len(f) - n + 1))


def judge_exit(r, bank=None, th=TH):
    """卖出触发线判定。返回 [(项名, 级别, 明细, 动作)]，级别 OK 表示未触发。

    与 judge_holding() 的分工：那个回答「是否仍符合入选标准」，这个回答
    「是否需要动仓」。两者不能合并——单年支付率 82% 会让前者 FAIL，但按
    卖出线（连续两年 >80%）不该卖；核心一级从 9.6% 掉到 9.3% 会让前者
    FAIL，但仍在 9.0% 的卖出线上方，只降权不清仓。

    级别：EXIT 移出 / TRIM 降权 / OK 未触发 / SKIP 数据缺失未判定。
    """
    nm, ind = r['name'], r.get('ind', '')
    ps_raw = list(r.get('pay_series') or [])
    os_raw = list(r.get('ocfd_series') or [])
    ds_raw = list(r.get('dps_series') or [])
    ok_v = lambda v: v is not None and v == v
    ps = [v for v in ps_raw if ok_v(v)]
    os_ = [v for v in os_raw if ok_v(v)]
    ds = [v for v in ds_raw if ok_v(v)]
    out = []

    # ① 全类共用：波动率红线。这是唯一与分红无关的卖出逻辑——
    #    底仓的首要任务是保住市值，不是收股息。
    v = r.get('vol')
    if v is None or v != v:
        out.append((f'{nm} 年化波动 ≤{th["vol_max"]:.0f}%', LV_SKIP, '无数据', ''))
    else:
        out.append((f'{nm} 年化波动 ≤{th["vol_max"]:.0f}%',
                    LV_OK if v < th['vol_max'] else LV_EXIT, f'{v:.2f}%',
                    '底仓市值稳定功能失效，降权至 15% 以下或移出（分红再好不豁免）'))

    # ② 银行：资本是否还支撑得起分红
    if ind == '银行':
        if not bank or not bank.get('cet1'):
            out.append((f'{nm} 核心一级 ≥{th["cet1_exit"]:.1f}%（卖出线）', LV_SKIP, '指标缺失', ''))
            return out
        c = bank['cet1'][0]
        if c < th['cet1_exit']:
            lv, act = LV_EXIT, '资本不足以支撑分红，清仓'
        elif c < th['cet1_sh']:
            lv, act = LV_TRIM, f'跌破入选线 {th["cet1_sh"]}%、仍在卖出线上方，该市场内权重减半并跟踪'
        else:
            lv, act = LV_OK, ''
        out.append((f'{nm} 核心一级 ≥{th["cet1_exit"]:.1f}%（卖出线）', lv, f'{c:.2f}%', act))

        npl, cov = bank.get('npl') or [], bank.get('cover') or []
        if cov and len(npl) >= 2:
            low, rise = cov[0] < th['cover_exit'], (npl[0] - npl[1]) > th['npl_rise']
            if low and rise:
                lv, act = LV_EXIT, '拨备不足且不良上行，抵补与质量同时恶化，清仓'
            elif low:
                lv, act = LV_TRIM, '拨备单点跌破、不良未上行，降权观察'
            elif rise and npl[0] > th['npl_max']:
                lv, act = LV_TRIM, '不良上行且已超上限，降权观察'
            else:
                lv, act = LV_OK, ''
            out.append((f'{nm} 拨备+不良 双条件', lv,
                        f'拨备 {cov[0]:.1f}%，不良 {npl[1]:.2f}%→{npl[0]:.2f}%', act))
        else:
            out.append((f'{nm} 拨备+不良 双条件', LV_SKIP, '指标缺失或不足两期', ''))
        return out

    # ③ 深市三只：各自规则已写死在报表里
    if nm == '粤高速A':
        # 收费公路的分红逻辑与消费股不同：它不是「赚多少分多少」的自由裁量，
        # 而是承诺按 70% 分红率派（三年实际 70.0 / 70.0 / 70.1）。这意味着
        # 支付率天然贴着 70%，用「支付率是否过高」去判它等于永远不触发；
        # 真正的风险在分子：改扩建分流、费率政策下调 → 净利下滑 → 分红跟着降。
        # 所以判据盯「分红金额是否被砍」和「现金流还能不能覆盖」。
        over, _ = sw_triggers(ps_raw, os_raw, th['pay_deep'], th['ocfd_min'])
        mx = max(ps) if ps else float('nan')
        out.append((f'{nm} 支付率 ≤{th["pay_deep"]:.0f}%', LV_TRIM if over else LV_OK,
                    f'单年最高 {mx:.1f}%（承诺 70% 分红率）' if ps else '无数据',
                    '越线说明在用存量现金维持承诺，权重减半'))
        e = consec(os_raw, lambda x: x < th['ocfd_min'])
        out.append((f'{nm} OCF/分红未连续两年 <1', LV_EXIT if e else LV_OK, f'序列 {os_}',
                    '通行费收入下滑但硬撑分红，移出深市子池'))
        if len(ds) >= 2 and ds[-2]:
            drop = (ds[-2] - ds[-1]) / ds[-2] * 100
            out.append((f'{nm} 每股分红同比降幅 ≤{th["gcd_drop_exit"]:.0f}%',
                        LV_TRIM if drop > th['gcd_drop_exit'] else LV_OK,
                        f'{ds[-2]:.3f}→{ds[-1]:.3f}（同比 {-drop:+.1f}%）',
                        '分红被砍，复核 70% 承诺是否仍在执行'))
        else:
            out.append((f'{nm} 每股分红同比降幅 ≤{th["gcd_drop_exit"]:.0f}%', LV_SKIP,
                        '分红序列不足', ''))
        return out

    if nm == '格力电器':
        t = consec(ps_raw, lambda x: x > th['pay_gree_exit'])
        out.append((f'{nm} 支付率未连续两年 >{th["pay_gree_exit"]:.0f}%', LV_TRIM if t else LV_OK,
                    f'序列 {ps}', f'深市内权重回调至 35%，差额给兴蓉环境/粤高速A'))
        return out

    if nm == '云南白药':
        # 已清仓（2026-10-20 口径修正后触发卖出线）。保留这段是因为口径修正
        # 后仍可能被重新纳入观察，规则本身不因持仓变化而删除；但不再出现在
        # 当前 8 只的行动建议里。
        if len(ds) >= 2 and ds[-2]:
            drop = (ds[-2] - ds[-1]) / ds[-2] * 100
            out.append((f'{nm} 每股分红同比降幅 ≤{th["gcd_drop_exit"]:.0f}%',
                        LV_TRIM if drop > th['gcd_drop_exit'] else LV_OK,
                        f'{ds[-2]:.2f}→{ds[-1]:.2f}（同比 {-drop:+.1f}%）', '深市内权重压到 15% 以下'))
        else:
            out.append((f'{nm} 每股分红同比降幅 ≤{th["gcd_drop_exit"]:.0f}%', LV_SKIP, '分红序列不足', ''))
        # 口径：按「最新年度」判定当期状态（决策口径只有一个，就是当前）。
        # 但历史触发过的年份必须在明细里写明年份和数值——2023 年 OCF/分红
        # 0.95、支付率 90.5% 就同时满足过。
        # 不改规则的理由：2024 年公司自己把分红砍了 43%，属于已自我修正的
        # 旧错误；用历史触发去惩罚当前决策，等于假设公司不会纠错。但也不
        # 能不提示——读报表的人有权知道这条线被触碰过。
        both = bool(os_) and bool(ps) and os_[-1] < th['ocfd_min'] and ps[-1] > th['pay_shallow']
        yrs = list(r.get('years') or [])
        hits = [(i, a, b) for i, (a, b) in enumerate(zip(os_raw, ps_raw))
                if ok_v(a) and ok_v(b) and a < th['ocfd_min'] and b > th['pay_shallow']]
        note = ''
        if hits:
            i, a, b = hits[-1]
            note = f'；{yrs[i] if i < len(yrs) else "历史"} 年曾同时满足（{a:.2f} / {b:.1f}%）'
            if (i + 1 < len(ds_raw) and i + 1 < len(yrs)
                    and ok_v(ds_raw[i]) and ok_v(ds_raw[i + 1]) and ds_raw[i] > 0):
                cut = (ds_raw[i] - ds_raw[i + 1]) / ds_raw[i] * 100
                if cut > 0:
                    note += f'，{yrs[i + 1]} 年分红 −{cut:.0f}% 已自我修正'
            note += '，按最新年度口径当前不触发'
        out.append((f'{nm} OCF/分红与支付率未同时恶化（最新年度）', LV_EXIT if both else LV_OK,
                    f'OCF/分红 {os_[-1] if os_ else "无"}，支付率 {ps[-1] if ps else "无"}%' + note,
                    '降级为观察仓，撤出底仓'))
        return out

    # ④ 公用事业 / 交运：分红纪律。单年超限可容忍，连续两年说明在用存量分红。
    t_pay = consec(ps_raw, lambda x: x > th['pay_util_exit'])
    out.append((f'{nm} 支付率未连续两年 >{th["pay_util_exit"]:.0f}%', LV_EXIT if t_pay else LV_OK,
                f'序列 {ps}', '用留存利润甚至负债分红，不可持续，移出'))
    t_ocf = consec(os_raw, lambda x: x < th['ocfd_min'])
    out.append((f'{nm} OCF/分红未连续两年 <1', LV_EXIT if t_ocf else LV_OK, f'序列 {os_}',
                '分红无现金流覆盖，移出'))
    return out


def pack_bank(b):
    """把东财银行指标转成 {cet1, npl, cover} 三个降序列表（最近在前）。"""
    if b is None or not len(b):
        return None
    b = b.sort_values('REPORT_DATE', ascending=False)
    num = lambda c: [float(x) for x in pd.to_numeric(b[c], errors='coerce').dropna().tolist()]
    return dict(cet1=num('HXYJBCZL'), npl=num('NONPERLOAN'), cover=num('BLDKBBL'))

# ==================== 主流程 ====================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true',
                    help='保留参数：分红已改读数据湖，无需联网刷新')
    ap.add_argument('--base', default=None, help='基准日 YYYY-MM-DD，默认数据湖最新交易日')
    ap.add_argument('--no-bank', action='store_true', help='跳过银行专项（离线）')
    ap.add_argument('--half', action='store_true', help='半年报轻量复核：只跑卖出触发线')
    ap.add_argument('--out', default=None, help='报告文件名（默认按模式区分）')
    args = ap.parse_args()

    in_win, note = window_note(datetime.date.today(), half=args.half)
    print(note)

    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{LAKE}' AS src (read_only)")

    base = args.base or str(latest_trade_date(con))
    year = infer_year(con)
    y0, y3 = year - 2, year
    mode = '半年报轻量' if args.half else '年度全量'
    print(f'模式 {mode} · 基准日 {base} · 复核年度 {y0}–{y3}（最近完整年度 {year}）\n')

    hold = pd.read_csv(HOLD, encoding='utf-8-sig')
    if 'sym' not in hold:
        raise SystemExit(f'{HOLD} 缺少 sym 列，无法复核')
    syms = hold.sym.tolist()
    all_syms = syms + list(WATCH)
    name_of = dict(zip(hold.sym, hold.name))
    name_of.update({k: v['name'] for k, v in WATCH.items()})
    # 行业以湖内申万一级为准，组合表的 ind 列降级为「核对用」：
    # 手写标签一旦写错，35% 上限会静默失效，而它偏偏是组合层唯一的行业约束。
    hand_ind = dict(zip(hold.sym, hold.ind)) if 'ind' in hold else {}
    lake_ind = ind_from_lake(con, all_syms)
    ind_of = {s: lake_ind.get(s) or hand_ind.get(s, '') for s in all_syms}
    ind_of.update({k: lake_ind.get(k, '银行') for k in WATCH})
    _chg = [(name_of.get(s, s), hand_ind[s], ind_of[s])
            for s in syms if s in hand_ind and lake_ind.get(s) and lake_ind[s] != hand_ind[s]]
    if _chg:
        print('  [核对] 组合表手写行业与申万一级不一致（以申万为准）：')
        for nm, a, b in _chg:
            print(f'    {nm}：{a} → {b}')
    _miss = [s for s in all_syms if s not in lake_ind]
    if _miss:
        print(f'  [WARN] {len(_miss)} 只缺申万分类，行业上限判定可能失真：{_miss}')

    # ---------- 治理核查（实控人性质）----------
    # 缓存优先；本次涉及的标的若不在缓存里，现场补抓。抓不到的会被记成
    # 「数据缺失」并进异常表——不会静默通过。
    gdf = gov.load_cached(GOV_CACHE)
    if 'code' not in gdf.columns:
        gdf = pd.DataFrame(columns=gov.COLS)
    need = [s for s in all_syms if s not in set(gdf.code)]
    if need:
        print(f'治理核查：缓存缺 {len(need)} 只，现场补抓…')
        gov.fetch_many(need, cache=GOV_CACHE)
        gdf = gov.load_cached(GOV_CACHE)
    gmap = gov.gate(gdf)

    # ---------- 取数 ----------
    div = load_dividend(con, all_syms)
    dv = div_by_year(div, y0, y3)
    dps_ttm = ttm_dps(div, base, all_syms)          # 股息率分子：近 365 天除权
    fin = load_fin(con, all_syms, y0, y3)
    rets = load_returns(con, all_syms, base)
    _cal = load_calendar(con, base, TH['vol_win'])
    amt = load_amount(con, all_syms, base, TH['amt_win'])

    # 半年报轻量模式：当期支付率改为 TTM 口径（近 12 个月已实施分红 ÷ TTM 净利）。
    # 原来按「报告期归因」抓东财年度方案表，中期方案不在表里，一年分两次的
    # 标的会被算成只分了一半——那个数字没法看。TTM 口径分子分母都滚 12 个月，
    # 至少自洽，也不需要报告期字段。
    rp = latest_report_period(con) if args.half else None
    np_ttm = ttm_net_profit(con, all_syms, rp) if rp else pd.Series(dtype=float)
    div12 = div[div.ex_date > pd.Timestamp(base) - pd.Timedelta(days=365)] \
        .groupby('symbol').agg(dps=('dps', 'sum'), amt_yi=('amt_yi', 'sum'))
    px_now = con.execute(f"""
        select symbol, arg_max(close, trade_date) as px from src.daily_bars
        where symbol in ({_sym_list(all_syms)}) and trade_date <= DATE '{base}' group by 1
    """).df()

    # ---------- 汇总指标 ----------
    rows = []
    for s in all_syms:
        f = fin[fin.symbol == s].drop(columns='symbol').set_index('y')
        d = dv[dv.symbol == s].drop(columns='symbol').set_index('y')
        g = f.join(d, how='left')
        yrs = [y for y in (y0, y0 + 1, y3) if y in g.index]
        np_sum = float(g.loc[yrs, 'np_yi'].sum()) if yrs else np.nan
        amt_sum = float(g.loc[yrs, 'amt_yi'].sum()) if yrs else np.nan
        ocf_sum = float(g.loc[yrs, 'ocf_yi'].sum()) if yrs else np.nan
        pay_y = (g.loc[yrs, 'amt_yi'] / g.loc[yrs, 'np_yi'] * 100) if yrs else pd.Series(dtype=float)
        ocfd_y = (g.loc[yrs, 'ocf_yi'] / g.loc[yrs, 'amt_yi']) if yrs else pd.Series(dtype=float)

        # 波动 / 回撤：窗口按**交易日历**对齐，停牌日记 0 收益（无成交即无损益）。
        # 与 screen_sz 同一套复权口径，差异只在窗口取法——那边是矩阵 tail(252)，
        # 对全市场同时交易的矩阵来说两者等价；这里因逐股循环必须先对齐日历，
        # 否则停牌股会被"跳过"成更长的日历窗口。
        rs_all = rets[rets.symbol == s].set_index('trade_date')['r']
        n_obs = int(rs_all.reindex(_cal).notna().sum())
        rs = rs_all.reindex(_cal).fillna(0.0)
        vol = float(np.std(rs, ddof=1) * np.sqrt(252) * 100) if n_obs >= 200 else np.nan
        rs_h = rs.iloc[-126:]
        vol_h = float(np.std(rs_h, ddof=1) * np.sqrt(252) * 100) if n_obs >= 100 else np.nan
        nav = (1 + rs).cumprod()
        mdd = float((nav / nav.cummax() - 1).min() * 100) if len(nav) else np.nan

        px = float(px_now.loc[px_now.symbol == s, 'px'].iloc[0]) if s in set(px_now.symbol) else np.nan
        # dps_last = 最近一个已除权年度的每股分红（财年口径，只作对照）
        dps_last = float(g.loc[y3, 'dps']) if y3 in g.index and pd.notna(g.loc[y3, 'dps']) else np.nan
        dps_ser = [float(g.loc[y, 'dps']) if y in g.index and pd.notna(g.loc[y, 'dps']) else None
                   for y in yrs]
        # 股息率分子用 TTM：按除权日滚 12 个月，不跨财年
        dttm = float(dps_ttm.get(s)) if s in dps_ttm.index else np.nan

        # 当期支付率：TTM 口径（近 12 个月分红总额 ÷ TTM 净利），替代原报告期口径
        pay_half, pay_half_p = np.nan, None
        if rp:
            a12 = float(div12.amt_yi.get(s, 0.0)) if s in div12.index else 0.0
            n12 = float(np_ttm.get(s)) if s in np_ttm.index else np.nan
            if a12 > 0 and np_ttm is not np.nan and n12 == n12 and n12 > 0:
                pay_half, pay_half_p = a12 / n12 * 100, f'TTM@{rp}'

        rows.append(dict(
            symbol=s, name=name_of.get(s, s), ind=ind_of.get(s, ''),
            market='沪' if s.startswith('6') else '深',
            np3=np_sum, div3=amt_sum, ocf3=ocf_sum,
            pay3=amt_sum / np_sum * 100 if np_sum and np_sum > 0 else np.nan,
            pay_max=float(pay_y.max()) if len(pay_y) else np.nan,
            pay_half=pay_half, pay_half_p=pay_half_p,
            ocfd3=ocf_sum / amt_sum if amt_sum and amt_sum > 0 else np.nan,
            years_ok=len(yrs) == 3,
            vol=vol, vol_h=vol_h, mdd=mdd,
            amt_wan=float(amt.loc[amt.symbol == s, 'amt_wan'].iloc[0]) if s in set(amt.symbol) else np.nan,
            dps_last=dps_last, dps_ttm=dttm, px=px,
            yld_now=dttm / px * 100 if dttm == dttm and px and px > 0 else np.nan,
            pay_series=[round(v, 1) for v in pay_y.tolist()],
            ocfd_series=[round(v, 2) for v in ocfd_y.tolist()],
            dps_series=dps_ser,
            years=list(yrs),          # 与上面三条序列逐项对齐，供明细标注年份
        ))
    df = pd.DataFrame(rows)

    # ---------- 明细表 ----------
    pd.set_option('display.width', 300); pd.set_option('display.max_columns', 40)
    show = df[['market', 'name', 'pay3', 'pay_max', 'ocfd3', 'vol', 'vol_h', 'mdd',
               'amt_wan', 'yld_now']].copy()
    show.columns = ['市', '名称', '三年支付率%', '单年最高支付率%', 'OCF/分红',
                    '年化波动%', '近半年波动%', '近1年回撤%', '日均成交(万)', '当前股息率%']
    print('=== 指标现值 ===')
    print(show.round(2).to_string(index=False))
    print()

    # ---------- -1) 黑天鹅一票否决（人工登记，登记即触发） ----------
    print('=== 黑天鹅一票否决 ===')
    swan = load_blackswan()
    act_sw = active_swans(swan)
    hit_sw = [s for s in syms if len(act_sw) and s in set(act_sw.symbol)]
    if hit_sw:
        for s in hit_sw:
            a0 = act_sw[act_sw.symbol == s].iloc[-1]
            print(f"  [否决] {name_of.get(s, s)}  {a0.date} {a0.type}：{a0.detail}")
            audit(f"{name_of.get(s, s)} 黑天鹅一票否决", False,
                  f"{a0.date} {a0.type}：{a0.detail}",
                  '【移出】无条件清仓，不看财务数据；解除需在登记簿写明依据', group='黑天鹅')
    else:
        print(f'  {len(syms)} 只均无有效记录（登记簿共 {len(swan)} 条；'
              f'未登记 ≠ 已确认无风险）')
        audit('持仓无黑天鹅记录', True, f'登记簿 {len(swan)} 条', '', group='黑天鹅')

    # ---------- -0.5) 治理底线（实控人性质，一票否决） ----------
    print('\n=== 治理底线（实控人性质）===')
    gov_fail = []
    for s in syms:
        mk = '沪' if s.startswith('6') else '深'
        info = gmap.get(s)
        ok, detail, act, grp = judge_governance(name_of.get(s, s), info, market=mk)
        flag = {True: '  ok  ', False: ' 否决 '}.get(ok, ' 缺失 ')
        print(f"  [{flag.strip()}] {name_of.get(s, s):<6} {mk}  {detail}")
        audit(f"{name_of.get(s, s)} 治理底线（{mk}市）", ok, detail, act, group=grp)
        if ok is False and info is not None and info[0] is False:
            gov_fail.append(name_of.get(s, s))
    if gov_fail:
        print(f"  [RED] {len(gov_fail)} 只触犯治理底线：{'、'.join(gov_fail)} → 建议清仓换仓")
    else:
        print('  持仓未发现个人/家族、非国资法人或外资控股')

    # ---------- 0) 银行指标（卖出线判定与观察池共用） ----------
    bank_data = {}
    if not args.no_bank:
        for s in [x for x in all_syms if ind_of.get(x) == '银行']:
            bank_data[s] = pack_bank(fetch_bank(s))

    # ---------- 1) 入选资格（只记录，不构成动作） ----------
    if not args.half:
        print('=== 入选资格复核（偏离只记录，不构成动作）===')
        for _, r in df[df.symbol.isin(syms)].iterrows():
            for nm, ok, detail, _ in judge_holding(r):
                audit(f'{nm}（{y0}–{y3}）' if '连续三年' in nm else nm, ok, detail,
                      '入选资格偏离，记录备查（是否动仓见卖出触发线）', group='入选资格')

    # ---------- 2) 半年报当期支付率（轻量模式的核心信号） ----------
    if args.half:
        print(f'\n=== TTM 支付率（近 12 个月已实施分红 ÷ TTM 净利，基准期 {rp or "无"}）===')
        if rp is None:
            print('  [SKIP] 未取到可用报告期')
        else:
            h = df[df.symbol.isin(syms)].copy()
            h['fy_last'] = [v[-1] if v else np.nan for v in h.pay_series]
            t = h[['name', 'pay_half_p', 'pay_half', 'fy_last', 'pay_series']].copy()
            t['pay_half'] = t['pay_half'].round(1)
            t['fy_last'] = t['fy_last'].round(1)
            t.columns = ['名称', '口径', 'TTM支付率%', '最近财年%', '近三年序列%']
            print(t.to_string(index=False))
            print('  口径：「TTM支付率%」= 近 365 天已实施分红总额 ÷ TTM 净利润（年报 + 本期累计 − 去年同期）。')
            print('        分子按除权日滚 12 个月，分母滚 12 个月，两边都是「最近一年」；')
            print('        与年度阈值（80%/100%）口径不同，只作趋势观察，判阈值看「最近财年%」。')

    # ---------- 3) 卖出触发线（触发才动仓） ----------
    print('\n=== 卖出触发线（触发才动仓）===')
    for _, r in df[df.symbol.isin(syms)].iterrows():
        for nm, lv, detail, act in judge_exit(r, bank_data.get(r['symbol'])):
            if lv == LV_SKIP:
                audit(f'{nm} 可验证', False, detail,
                      '手动补数后再核（缺数据≠达标）', group='数据完整性')
                continue
            audit(nm, lv == LV_OK, detail,
                  f'【{"移出" if lv == LV_EXIT else "降权"}】{act}' if lv != LV_OK else '',
                  group='卖出触发')

    # ---------- 4) 观察池（半年报模式跳过） ----------
    if args.half:
        print('\n=== 观察池：半年报模式跳过（回补条件需年报数据）===')
    else:
        print('\n=== 观察池回补条件 ===')
        for s, cfg in WATCH.items():
            nm = cfg['name']
            r = df[df.symbol == s]
            b = bank_data.get(s)
            if not b or not b.get('cet1'):
                print(f'  [SKIP] {nm} 银行指标未取到，条件无法验证（不视为通过）')
                audit(f'{nm} 回补条件可验证', False, '东财指标缺失',
                      '手动补数后再核（数据缺失≠条件不满足）', group='数据完整性')
                continue
            cet1 = b['cet1']
            pay_s = r.iloc[0]['pay_series'] if len(r) else []
            if s == '002966.SZ':
                ok = (len(cet1) >= 2 and cet1[0] >= TH['cet1_sh'] and cet1[1] >= TH['cet1_sh']
                      and (not pay_s or min(pay_s[-2:]) >= 30))
                audit(f'{nm} 回补（连续两期核心一级 ≥{TH["cet1_sh"]}% 且支付率 ≥30%）', ok,
                      f"最近两期 {cet1[:2]}，支付率 {pay_s}", '', group='观察池')
            else:
                last2 = pay_s[-2:] if len(pay_s) >= 2 else pay_s
                ok = len(last2) >= 2 and min(last2) >= 28 and bool(cet1) and cet1[0] >= 9.3
                audit(f'{nm} 回补（支付率连续两年 ≥28% 且核心一级 ≥9.3%）', ok,
                      f"支付率 {pay_s}，核心一级 {cet1[:1]}", '', group='观察池')

        hit = [a for a in __import__('qa_checks').AUDIT if a['group'] == '观察池' and a['ok']]
        if hit:
            print(f"  [建议] {len(hit)} 只观察池标的已满足回补条件，可评估是否纳入深市子池")

    # ---------- 4b) 深市替补席（按既定顺序递补，不临时重筛） ----------
    if not args.half:
        print('\n=== 深市替补席（触发卖出时按顺序递补）===')
        if os.path.exists(BUYPOOL):
            bp = pd.read_csv(BUYPOOL, encoding='utf-8-sig').sort_values('order')
            for _, b in bp.iterrows():
                v, pm, od = float(b.vol), float(b.pay_max), float(b.ocfd3)
                # eligible=False 的行是「留痕行」：流动性不达标、人工排除、
                # 治理否决。它们本来就不该递补，用财务底线去判会得出
                # 「✓ 可递补」这种自相矛盾的结论——所以先看标记，再看指标。
                if 'eligible' in b and str(b.eligible).lower() == 'false':
                    print(f"  {int(b['order'])}. {b['name']}  波动 {v:.2f}%  股息 {float(b.yld):.2f}%"
                          f"  ✗ 不可递补（{str(b.get('note') or '')[:40]}…）")
                    audit(f"替补 {b['name']} 已标记不可递补", True,
                          str(b.get('note') or '记录在案'), '', group='替补席')
                    continue
                fin_ok = backup_ok(v, pm, od)
                g_ok, g_detail, g_act, g_grp = judge_governance(
                    b['name'], gmap.get(b['symbol']), market='深')
                ok = fin_ok and g_ok
                why = '✓ 可递补' if ok else (
                    '✗ 失格：治理底线' if not g_ok else '✗ 已失效：财务底线')
                print(f"  {int(b['order'])}. {b['name']}  波动 {v:.2f}%  股息 {float(b.yld):.2f}%"
                      f"  支付率最高 {pm:.1f}%  实控人 {g_detail}  {why}")
                audit(f"替补 {b['name']} 资格仍有效", ok,
                      f"波动 {v:.2f}%，支付率最高 {pm:.1f}%，OCF/分红 {od:.2f}；{g_detail}",
                      g_act or '替补失效，需重新筛选深市候选，不可直接递补',
                      group='替补席')
                if not g_ok:
                    # 替补被治理底线拦下是「记录」而非组合风险：它不在持仓里，
                    # 计入需处理会让报告每年固定多一条噪声。分组保持 '替补席'。
                    audit(f"替补 {b['name']} 治理底线", False, g_detail,
                          '触犯治理底线，不得递补；由下一顺位替补', group='替补席')
        else:
            print(f'  [SKIP] 未找到 {BUYPOOL}，替补席不可用（触发卖出时将无预案）')

    # ---------- 5) 组合层面 ----------
    print('\n=== 组合层面 ===')
    hp = df[df.symbol.isin(syms)].merge(hold[['sym', 'w60']], left_on='symbol', right_on='sym')
    if len(hp) == len(syms):
        w = hp.w60.values / 100
        audit('组合权重合计 100%', abs(w.sum() - 1) < 1e-6, f'{w.sum()*100:.2f}%',
              '权重表有残差，重算', group='数据完整性')
        # 组合波动：必须按**日收益序列合成**后重算，不能用个股波动的加权平均。
        # 后者是个股风险的算术平均，不含相关性；低相关标的会互相抵消，真实值
        # 通常显著更低（本组合 ~12.7% vs ~20.2%）。用它做「<25%」判定，等于拿
        # 一个偏高一倍的数去卡红线——判定本身自洽，但阈值的经济含义完全错了。
        _R = rets.pivot_table(index='trade_date', columns='symbol', values='r')
        _R = _R.reindex(_cal)
        _wp = pd.Series(w, index=hp.symbol.values)
        _rp = _R[list(_wp.index)].fillna(0.0).to_numpy() @ _wp.to_numpy()
        _vp = float(np.std(_rp, ddof=1) * np.sqrt(252) * 100)
        _navp = np.cumprod(1 + _rp)
        _mddp = float((_navp / np.maximum.accumulate(_navp) - 1).min() * 100)
        va = float(np.average(hp.vol, weights=w))      # 个股加权，仅作对照
        audit(f'组合年化波动 <{TH["vol_max"]:.0f}%（日序列合成）', _vp < TH['vol_max'],
              f'{_vp:.2f}%（个股加权 {va:.2f}%，最大回撤 {_mddp:.2f}%）',
              '底仓整体波动超标，降权高波标的', group='卖出触发')
        print(f"  组合最大回撤（日序列）{_mddp:.2f}% · 个股加权回撤 "
              f"{float(np.average(hp.mdd.abs(), weights=w)):.2f}%")
        print(f"  加权股息率 {np.average(hp.yld_now.dropna(), weights=w[hp.yld_now.notna().values]):.2f}%（静态，非收益预期）")
        print(f"  沪 {hp[hp.market=='沪'].w60.sum():.1f}% / 深 {hp[hp.market=='深'].w60.sum():.1f}%")

        # 集中度上限：防止「低波陷阱」——公式把权重推给暂时最平稳的标的，
        # 一旦暴雷，仓位最重的那只恰好就是风险最大的那只。
        _mx = hp.loc[hp.w60.idxmax()]
        audit(f'单一标的权重 ≤{TH["w_single_max"]:.0f}%', _mx.w60 <= TH['w_single_max'],
              f'最大 {_mx.w60:.2f}%（{_mx["name"]}）',
              '超出部分按比例分配给未超标标的，不得由单一标的承担', group='数据完整性')
        _iw = hp.groupby('ind').w60.sum().sort_values(ascending=False)
        audit(f'单一行业权重 ≤{TH["w_ind_max"]:.0f}%', _iw.iloc[0] <= TH['w_ind_max'],
              f'最高 {_iw.iloc[0]:.2f}%（{_iw.index[0]}，{int((hp.ind == _iw.index[0]).sum())} 只）',
              '超出部分按比例分配给其他行业', group='数据完整性')

    # ---------- 输出 ----------
    out = args.out or ('年度复核结果_半年报.txt' if args.half else '年度复核结果.txt')
    det = '半年报复核_指标明细.csv' if args.half else '年度复核_指标明细.csv'
    title = f'打新底仓{mode}复核 · {base}' + ('' if in_win else ' · 非复核窗口（跟踪）')
    fails, _ = audit_report(out, title)
    df.drop(columns=['pay_series', 'ocfd_series', 'dps_series', 'years']).to_csv(
        det, index=False, encoding='utf-8-sig')
    print(f'  明细已写 {det}')

    print(f'\n结论：{"无卖出触发，维持当前组合" if fails == 0 else f"{fails} 项触发，见 {out}"}')
    return 0 if fails == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

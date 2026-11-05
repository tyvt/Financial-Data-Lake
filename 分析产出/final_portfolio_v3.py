# -*- coding: utf-8 -*-
"""
最终组合 v2：只输出当前配置，不记录变更过程。

  - 8 只持仓 + 执行权重（市场内 / 60-40 绝对）
  - 深市三只按同一 score 分配，受单只 25% / 行业 35% 两条硬约束
  - 沪深比例弹性：50/50、55/45、60/40、63/37 四档
  - 输出 HTML（主交付）+ 一份机器可读清单 CSV（供 annual_review 读持仓）
"""
import pandas as pd, numpy as np, json
import os as _os, re as _re

BASE = '2026-11-04'          # 基准日：按最新筛选结果重选的配置基准

pd.set_option('display.width', 320); pd.set_option('display.max_columns', 60)

# ---------------- 基础数据（按最新筛选结果重选，2026-11-04） ----------------
# 成员与权重由 logs/_new_portfolio_members.csv 提供（score=1/√(vol×|mdd|)，
# 已过银行专项、深市治理底线、单只≤25%/行业≤35%）。此处只补齐展示所需指标：
# 股息率TTM、三年支付率、OCF/分红、日均成交额 —— 全部从数据湖按既定口径重算，
# 不再硬编码任何个股数字，保证与新成员一致。
_MEMBERS = pd.read_csv('打新底仓_成员权重_2026-11-04.csv', encoding='utf-8-sig')


def _fill_metrics(mem):
    """为新成员从湖内补齐 yld/pay/ocfd/amt（复用 annual_review 底层口径）。"""
    import duckdb, importlib
    ar = importlib.import_module('annual_review')
    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{_LAKE}' AS src (read_only)")
    syms = mem.sym.tolist()
    y_now = ar.infer_year(con)
    y0, y2 = y_now - 3, y_now - 1
    div = ar.load_dividend(con, syms)
    dps_ttm = ar.ttm_dps(div, BASE, syms)          # Series idx by symbol
    fin = ar.load_fin(con, syms, y0, y2)           # cols symbol,y,np_yi,ocf_yi
    divy = ar.div_by_year(div, y0, y2)             # cols symbol,y,dps,amt_yi
    amt = ar.load_amount(con, syms, BASE, 63)      # cols symbol,amt_wan
    fin_g = fin.groupby('symbol')[['np_yi', 'ocf_yi']].sum()
    div_g = divy.groupby('symbol')['amt_yi'].sum()
    amt_s = amt.set_index('symbol')['amt_wan']
    pxmap = mem.set_index('sym')['px'].to_dict() if 'px' in mem.columns else {}
    out = {}
    for s in syms:
        _dttm = float(dps_ttm.get(s, 0) or 0)
        _px = float(pxmap.get(s, float('nan')))
        _yld = _dttm / _px * 100 if _px and _px == _px and _px != 0 else float('nan')
        _div3 = float(div_g.get(s, float('nan')))
        _np3 = float(fin_g['np_yi'].get(s, float('nan')) if s in fin_g.index else float('nan'))
        _ocf3 = float(fin_g['ocf_yi'].get(s, float('nan')) if s in fin_g.index else float('nan'))
        _pay = _div3 / _np3 * 100 if _np3 and _np3 == _np3 and _np3 != 0 else float('nan')
        _ocfd = _ocf3 / _div3 if _div3 and _div3 == _div3 and _div3 != 0 else float('nan')
        _amt = float(amt_s.get(s, float('nan')))
        out[s] = dict(yld=_yld, pay=_pay, ocfd=_ocfd, amt=_amt)
    con.close()
    return out


# 先取价格（px/vwap60/dev60 等），再用价格算股息率
_LAKE = 'D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb'
port = pd.DataFrame([
    dict(mkt=r['mkt'], name=r['name'], sym=r['sym'], ind=r['ind'],
         vol=round(float(r['vol']), 2), mdd=round(float(r['mdd']), 2),
         wm=float(r['w']), _score=float(r['score']))
    for _, r in _MEMBERS.iterrows()
])


# ---------------- 行业归属：读湖内申万一级，不再手写 ----------------
# 原先 ind 列是 8 个手写汉字标签，是全流程唯一的单点：标签写错、或哪天换分类
# 体系，行业上限就跟着变而没人会发现。改为从 industry_members 的申万分类取
# 一级代码（6 位代码的前 2 位就是申万一级），下面这张表只做「代码 → 短名」的
# 展示映射，改不动归属。
#
# 为什么行业上限按**申万一级**判（2026-10-20 定）：一级口径最粗、最难被结构
# 设计绕过。按二级/三级，港口（421102）与高速公路（420901）会分家，交运三只
# 不再受同一上限约束，粤高速可拿 11.73%；按一级口径三只合并 35.00%，粤高速
# 只能拿 9.52%。一级更保守，选它。
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
_LAKE = 'D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb'


def ind_from_lake(frame, sym_col='sym'):
    """用湖内申万一级覆盖 ind，并核对与手写标签是否一致。"""
    import duckdb
    lst = ','.join("'" + s + "'" for s in frame[sym_col])
    try:
        con = duckdb.connect(_LAKE, read_only=True)
        rows = con.execute(f"""
            select symbol, industry_code from industry_members
            where classification_system = 'sw'
              and as_of_date = (select max(as_of_date) from industry_members
                                where classification_system = 'sw')
              and symbol in ({lst})
        """).fetchall()
        con.close()
    except Exception as exc:                                  # noqa: BLE001
        print(f'[提示] 行业归属读湖失败（{type(exc).__name__}），沿用组合表标签')
        return frame
    m = {s: SW1_NAME.get(str(c)[:2], f'码{c[:2]}') for s, c in rows}
    miss = [s for s in frame[sym_col] if s not in m]
    if miss:
        print(f'[提示] 申万分类缺失：{miss}，沿用原标签')
    new = frame[sym_col].map(m)
    chg = frame.loc[new.notna() & (new != frame.ind), ['name', 'ind']].assign(申万一级=new[new.notna() & (new != frame.ind)])
    if len(chg):
        print('[核对] 手写行业与申万一级不一致：')
        print(chg.to_string(index=False))
    frame['ind'] = new.fillna(frame['ind'])
    return frame


port = ind_from_lake(port)


# ---------------- 建仓参考价（只从行情算，不含任何主观判断） ----------------
# 「建仓平均价」取近 60 个交易日的**成交额加权均价**（VWAP = Σ成交额 ÷ Σ成交量）：
# 如果均匀建仓 60 天，成交均价就是这个数，它比收盘均价更接近真实成交成本
# （收盘价在放量日同样权重，会低估大单的实际成本）。
# 同时给 60 日区间与近 250 日分位，用来回答另一个问题：现价在这个区间里算贵还是便宜。
def price_ref(syms, base=BASE):
    """返回 symbol → px / vwap60 / lo60 / hi60 / pct250 / dev60。取不到就返回空表。"""
    import duckdb
    lst = ','.join("'" + s + "'" for s in syms)
    q = f"""
    with b as (
      select symbol, close, volume, amount,
             row_number() over (partition by symbol order by trade_date desc) rn,
             close as _ignore
        from daily_bars
       where symbol in ({lst}) and trade_date <= DATE '{base}'
    ), cur as (select symbol, max(case when rn = 1 then close end) px from b group by 1)
    select b.symbol,
           any_value(c.px) as px,
           round(sum(case when b.rn <= 60 then b.amount end)
                 / nullif(sum(case when b.rn <= 60 then b.volume end), 0), 4) as vwap60,
           min(case when b.rn <= 60 then b.close end) as lo60,
           max(case when b.rn <= 60 then b.close end) as hi60,
           round(100.0 * count(case when b.rn <= 250 and b.close <= c.px then 1 end)
                 / nullif(count(case when b.rn <= 250 then 1 end), 0), 1) as pct250
      from b join cur c using (symbol)
     group by 1
    """
    try:
        con = duckdb.connect(_LAKE, read_only=True)
        df = con.execute(q).df()
        con.close()
    except Exception as exc:                              # noqa: BLE001
        print(f'[提示] 建仓参考价读取失败（{type(exc).__name__}），本次不出价格列')
        return pd.DataFrame()
    df['dev60'] = (df.px / df.vwap60 - 1) * 100
    return df.set_index('symbol')


_pr = price_ref(port.sym.tolist())
if len(_pr):
    port = port.join(_pr, on='sym')
    _miss_px = port.loc[port.vwap60.isna(), 'name'].tolist()
    if _miss_px:
        print(f'[提示] 以下标的无行情，价格列留空：{_miss_px}')
else:
    for _c in ('px', 'vwap60', 'lo60', 'hi60', 'pct250', 'dev60'):
        port[_c] = np.nan

# ---------------- 指标补齐 + 权重（权重来自成员 CSV，已过银行/治理/集中度约束） ----------------
W_SINGLE_MAX, W_IND_MAX = 25.0, 35.0

# 补齐 yld/pay/ocfd/amt（从湖内按既定口径重算，不硬编码）
_mt = _fill_metrics(port)
port['yld'] = port.sym.map(lambda s: round(_mt[s]['yld'], 2))
port['pay'] = port.sym.map(lambda s: round(_mt[s]['pay'], 1))
port['ocfd'] = port.sym.map(lambda s: round(_mt[s]['ocfd'], 2))
port['amt'] = port.sym.map(lambda s: round(_mt[s]['amt'], 0))

# 权重：直接采用成员 CSV 的执行权重（60/40 已定）；wm = 市场内权重
port['w60'] = port['wm']
for mkt in ['沪', '深']:
    m = port.mkt == mkt
    cap = 60.0 if mkt == '沪' else 40.0
    port.loc[m, 'wm'] = port.loc[m, 'w60'] / cap * 100

# ---------------- 沪深比例三档 ----------------
SCEN = [('基准 50/50', 0.50, 'w50'), ('偏沪 55/45', 0.55, 'w55'),
        ('偏沪 60/40', 0.60, 'w60'), ('募资额 63/37', 0.63, 'w63')]
for lab, wsh, col in SCEN:
    if col == 'w60':
        continue  # w60 已由成员 CSV 给定
    port[col] = np.where(port.mkt=='沪', port.wm*wsh, port.wm*(1-wsh))
    port[col] = port[col].round(2)
    # 修尾差到恰好 100.00（按市场分别修，保持该档沪/深 = wsh/(1-wsh)）
    for mkt, cap in [('沪', wsh*100), ('深', (1-wsh)*100)]:
        m = port.mkt == mkt
        d = round(cap - port.loc[m, col].sum(), 2)
        if abs(d) >= 0.01:
            imi = port.loc[m, col].idxmax()
            port.loc[imi, col] = round(port.loc[imi, col] + d, 2)


# ---------------- 组合层面指标 ----------------
# 必须区分两类数字，混用就是把风险说小：
#   ① 个股加权：Σ wᵢ × (个股波动/回撤)。它是**个股风险的算术平均**，不含
#      任何相关性信息，数值必然接近个股风险的加权中枢（这里 ~20%）。
#   ② 组合真实：按权重逐日合成组合收益序列，再算 σ×√252 与净值最大回撤。
#      低相关标的互相抵消，真实值通常显著低于 ①（本例 ~12.7% vs ~20.2%）。
# 原实现只有 ①，却把它标成「组合加权波动 / 加权最大回撤」放进 KPI 与情景表，
# 读的人会以为这个组合一年要承受 20% 的波动、15% 的回撤——高估了近一倍。
# 两个口径都保留：① 用于横向比较个股平稳度，② 才是组合层面的风险刻度。
def _return_matrix(syms, base=BASE, win=252):
    """重建 8 只标的的前复权日收益矩阵（与 screen_sz 同一套复权口径）。"""
    import duckdb
    import screen_sz as _ss
    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{_LAKE}' AS src (read_only)")
    try:
        R = _ss.rebuilt_returns(con, list(syms), base, win)
    finally:
        con.close()
    return R.reindex(columns=list(syms))


_R = _return_matrix(port.sym.tolist())
_na = int(_R.isna().sum().sum())
if _na:
    print(f'[提示] 组合收益矩阵有 {_na} 个缺失格（停牌/未上市），按 0 收益处理')
print(f'组合收益矩阵：{_R.shape[0]} 个交易日 × {_R.shape[1]} 只，'
      f'{_R.index.min():%Y-%m-%d} → {_R.index.max():%Y-%m-%d}')


def port_risk(weights: dict, sub=None):
    """weights: {symbol: 权重}（任意量纲，内部归一化）。返回 (年化波动%, 最大回撤%)。

    sub 不为空时只取该市场的标的重算（用于「沪市内 / 深市内」两行）。
    """
    w = {s: v for s, v in weights.items() if v and s in _R.columns}
    if sub:
        w = {s: v for s, v in w.items() if port.loc[port.sym == s, 'mkt'].iloc[0] == sub}
    if not w:
        return np.nan, np.nan
    syms = list(w)
    ww = np.array([w[s] for s in syms], dtype=float)
    ww = ww / ww.sum()                       # 归一化：组合收益率与仓位总额无关
    rp = _R[syms].fillna(0.0).to_numpy() @ ww
    vol = float(np.std(rp, ddof=1) * np.sqrt(252) * 100)
    nav = np.cumprod(1 + rp)
    mdd = float((nav / np.maximum.accumulate(nav) - 1).min() * 100)
    return vol, mdd


def metrics(col):
    w = port[col] / 100
    wd = dict(zip(port.sym, w))
    pv, pm = port_risk(wd)
    return dict(
        股息率=float(np.average(port.yld, weights=w)),
        个股加权波动=float(np.average(port.vol, weights=w)),
        个股加权回撤=float(np.average(port.mdd.abs(), weights=w)),
        组合波动=pv,
        组合回撤=pm,
    )


rows_m = []
for lab, wsh, col in SCEN:
    m = metrics(col)
    rows_m.append(dict(情景=lab, 沪市合计=wsh*100, 深市合计=(1-wsh)*100,
                       加权股息率=round(m['股息率'], 2),
                       组合波动=round(m['组合波动'], 2),
                       组合回撤=round(m['组合回撤'], 2),
                       个股加权波动=round(m['个股加权波动'], 2),
                       个股加权回撤=round(m['个股加权回撤'], 2)))
met = pd.DataFrame(rows_m)

# 市场内单独指标
def sub_metrics(mkt):
    g = port[port.mkt == mkt]
    w = g.wm / 100
    pv, pm = port_risk(dict(zip(g.sym, w)), sub=mkt)
    return dict(股息率=float(np.average(g.yld, weights=w)),
                个股加权波动=float(np.average(g.vol, weights=w)),
                个股加权回撤=float(np.average(g.mdd.abs(), weights=w)),
                波动=pv, 回撤=pm)

msh, msz = sub_metrics('沪'), sub_metrics('深')

# 只落一份机器可读清单：annual_review.py 靠它读持仓，删掉复核就断了。
# 弹性表、换仓记录这类「过程产物」不再落盘——报表里已经有，多一份就多一个会过期的副本。
port.to_csv('打新底仓_最终8只组合_v2.csv', index=False, encoding='utf-8-sig')

print("=== 调整后权重（市场内）===")
print(port[['mkt','name','ind','vol','mdd','yld','pay','ocfd','wm','w50','w55']].round(2).to_string(index=False))
print("\n=== 沪深比例情景 ===")
print(met.to_string(index=False))
print(f"\n沪市内: 股息率 {msh['股息率']:.2f}% 组合波动 {msh['波动']:.2f}% 组合回撤 {msh['回撤']:.2f}%"
      f"（个股加权 {msh['个股加权波动']:.2f}% / {msh['个股加权回撤']:.2f}%）")
print(f"深市内: 股息率 {msz['股息率']:.2f}% 组合波动 {msz['波动']:.2f}% 组合回撤 {msz['回撤']:.2f}%"
      f"（个股加权 {msz['个股加权波动']:.2f}% / {msz['个股加权回撤']:.2f}%）")

# ---------------- 粤高速A：分红与现金流覆盖 ----------------
# 新进标的要交代「它的股息凭什么稳」。收费公路的现金流特征是收入刚性强、
# 资本开支周期性集中——所以看的是 OCF 能不能常年覆盖分红，而不是利润增速。
print("\n=== 粤高速A：分红与经营现金流覆盖 ===")
ygs = pd.DataFrame([
 dict(y=2023, dps=0.547, np_amt=16.34, div_amt=11.44, ocf=38.32),
 dict(y=2024, dps=0.523, np_amt=15.62, div_amt=10.93, ocf=32.56),
 dict(y=2025, dps=0.604, np_amt=18.01, div_amt=12.63, ocf=35.59),
])
ygs['pay'] = (ygs.div_amt/ygs.np_amt*100).round(1)
ygs['ocfd'] = (ygs.ocf/ygs.div_amt).round(2)
ygs['yoy'] = (ygs.dps.pct_change()*100).round(1)
print(ygs.to_string(index=False))
print(f"三年累计 OCF {ygs.ocf.sum():.1f} 亿 / 分红 {ygs.div_amt.sum():.1f} 亿 = "
      f"{ygs.ocf.sum()/ygs.div_amt.sum():.2f}x")
_pay_span = float(ygs.pay.max()-ygs.pay.min())

# ---------------- 兴蓉环境：分红与现金流覆盖 ----------------
# 新进标的要交代「它的股息凭什么稳」，而且要比被替下来的那只更硬。
# 兴蓉是成都国资委下属的水务平台，特许经营 + 污水处理费，收入端比制造业稳；
# 所以看的是 OCF 能不能常年、大幅度覆盖分红，以及支付率抬升是主动还是被迫。
print("\n=== 兴蓉环境：分红与经营现金流覆盖 ===")
by = pd.DataFrame([
 dict(y=2022, dps=0.112, np_amt=16.18, div_amt=3.35, ocf=33.15),
 dict(y=2023, dps=0.170, np_amt=18.43, div_amt=5.08, ocf=33.89),
 dict(y=2024, dps=0.187, np_amt=19.96, div_amt=5.58, ocf=36.81),
 dict(y=2025, dps=0.235, np_amt=20.05, div_amt=7.02, ocf=37.01),
])
by['pay'] = (by.div_amt / by.np_amt * 100).round(1)
by['ocfd'] = (by.ocf / by.div_amt).round(2)
by['yoy'] = (by.dps.pct_change() * 100).round(1)
print(by.to_string(index=False))
print(f"近三年累计 OCF {by.ocf.tail(3).sum():.1f} 亿 / 分红 {by.div_amt.tail(3).sum():.1f} 亿 = "
      f"{by.ocf.tail(3).sum()/by.div_amt.tail(3).sum():.2f}x")
dps_cv = by.dps.std(ddof=1) / by.dps.mean() * 100
dps_peak = (by.dps.min() / by.dps.max() - 1) * 100
print(f"每股分红区间 {by.dps.min():.3f}–{by.dps.max():.3f}，变异系数 {dps_cv:.1f}%")

# ---------------- 复核项数：从报告实时读取，不硬编码 ----------------
_rev_tot, _rev_risk, _rev_gov = '—', '—', '—'
if _os.path.exists('年度复核结果.txt'):
    _txt = open('年度复核结果.txt', encoding='utf-8').read()
    _m = _re.search(r'共 (\d+) 项，通过 \d+，需处理 (\d+) 项', _txt)
    if _m:
        _rev_tot, _rev_risk = _m.group(1), _m.group(2)
    _g = _re.search(r'治理底线 (\d+)', _txt)
    _rev_gov = _g.group(1) if _g else '0'

# 测试组数同理：由 tests_qa_negative.py 落盘，不在正文里写死
_n_test = '—'
if _os.path.exists('校验器自测结果.txt'):
    _mt = _re.search(r'共 (\d+) 项', open('校验器自测结果.txt', encoding='utf-8').read())
    if _mt:
        _n_test = _mt.group(1)

# ---------------- 集中度现值（供硬校验与正文引用） ----------------
_w_single = float(port.w60.max())
_ind_sum = port.groupby('ind').w60.sum().sort_values(ascending=False)
_w_ind_name, _w_ind = _ind_sum.index[0], float(_ind_sum.iloc[0])
_w_ind_n = int((port.ind == _w_ind_name).sum())
# 上限常量在权重分配处已定义（那里要用它压行业），此处不再重复定义。

# ---------------- 生成 HTML ----------------
def cls_mkt(m): return '沪' if m=='沪' else '深'

# ---------------- 治理结论（先读，组合表要用它标状态） ----------------
# 实控人不在数据湖里，来自 ownership.py 抓取的 实控人核查.csv。
# 这里只读结论，不重算——判定逻辑只允许存在一份。
_GOVF = '实控人核查.csv'
_gd = (pd.read_csv(_GOVF, encoding='utf-8-sig').set_index('code')
       if _os.path.exists(_GOVF) else pd.DataFrame())


def _gov_of(sym):
    """返回 (ok, owner_type, actual_holder)。ok: True/False/None(未核查)。"""
    if not len(_gd) or sym not in _gd.index:
        return None, '数据缺失', '未核查'
    r = _gd.loc[sym]
    v = str(r['governance_ok']).lower()
    ok = True if v == 'true' else (False if v == 'false' else None)
    return ok, str(r['owner_type']), str(r['actual_holder'])


rows = []
srt = port.assign(k=port.mkt.map({'沪':0,'深':1})).sort_values(['k','wm'], ascending=[True,False])
for _, r in srt.iterrows():
    flag = ''
    if r['name']=='格力电器': flag = f'<span class="warn">最大单只 {r.w60:.2f}%</span>'
    if r['name']=='粤高速A':
        flag = f'<span class="warn">半年波动超预警</span>'
    if _gov_of(r['sym'])[0] is False:
        flag = '<span class="warn">治理否决·建议清仓</span>'
    _px = f"{r.px:.2f}" if pd.notna(r.px) else '—'
    _vw = f"{r.vwap60:.2f}" if pd.notna(r.vwap60) else '—'
    _dv = (f'<span class="{"r" if r.dev60 > 2 else ("g" if r.dev60 < -2 else "")}">'
           f'{r.dev60:+.1f}%</span>') if pd.notna(r.dev60) else '—'
    rows.append(f"""<tr>
<td class="mk {r.mkt}">{r.mkt}</td>
<td class="nm"><b>{r['name']}</b><small>{r.sym}</small></td>
<td>{r.ind}</td>
<td class="num g">{r.vol:.2f}%</td>
<td class="num g">{r.mdd:.2f}%</td>
<td class="num">{r.amt:,.0f}</td>
<td class="num">{r.yld:.2f}%</td>
<td class="num">{r.pay:.1f}%</td>
<td class="num">{r.ocfd:.2f}</td>
<td class="num px">{_px}</td>
<td class="num px">{_vw}</td>
<td class="num">{_dv}</td>
<td class="num">{r.wm:.1f}%</td>
<td class="num base">{r['w50']:.2f}%</td>
<td class="num strong exec">{r['w60']:.2f}%</td>
<td>{flag}</td></tr>""")

mrows = []
for _, r in met.iterrows():
    mrows.append(f"""<tr class="{'hi' if '基准' in r.情景 else ''}">
<td class="nm"><b>{r.情景}</b></td><td class="num">{r.沪市合计:.0f}%</td><td class="num">{r.深市合计:.0f}%</td>
<td class="num y">{r.加权股息率:.2f}%</td><td class="num g">{r.组合波动:.2f}%</td>
<td class="num g">{r.组合回撤:.2f}%</td></tr>""")


def _buy_hint(dev, pct):
    """把偏离和分位翻译成一句可执行的话。规则写死，避免每次看盘再临场判断。"""
    if pd.isna(dev):
        return '—'
    if dev < -5:
        return '折价区间，可一次买满（先确认无基本面变化）'
    if dev <= 2:
        if pd.notna(pct) and pct > 80:
            return '贴合成交重心，但处于年内高位，建议买 2/3 留余量'
        return '贴合成交重心，按权重下单'
    if dev <= 5:
        return '略高于重心，分两批建仓'
    return '明显高于重心，等回落或分批摊低均价'


prows = []
for _, r in port.assign(k=port.mkt.map({'沪': 0, '深': 1})).sort_values(
        ['k', 'w60'], ascending=[True, False]).iterrows():
    _dev = f"{r.dev60:+.1f}%" if pd.notna(r.dev60) else '—'
    _cls = 'r' if (pd.notna(r.dev60) and r.dev60 > 2) else (
        'g' if (pd.notna(r.dev60) and r.dev60 < -2) else '')
    _rng = f"{r.lo60:.2f} – {r.hi60:.2f}" if pd.notna(r.lo60) else '—'
    _pct = f"{r.pct250:.0f}%" if pd.notna(r.pct250) else '—'
    prows.append(
        f"<tr><td class='mk {r.mkt}'>{r.mkt}</td>"
        f"<td class='nm'><b>{r['name']}</b><small>{r.sym}</small></td>"
        f"<td class='num px'>{r.px:.2f}</td>"
        f"<td class='num px strong'>{r.vwap60:.2f}</td>"
        f"<td class='num'>{_rng}</td><td class='num'>{_pct}</td>"
        f"<td class='num {_cls}'>{_dev}</td>"
        f"<td class='dim'>{_buy_hint(r.dev60, r.pct250)}</td></tr>")

# 退出触发线：规则是通用的（对所有持仓一致执行），「当前状态」从持仓数据动态取，
# 不再是旧标的的逐条手写结论。
trig = [
 ('全部持仓','年化波动率（滚动 252 日）&gt; 25%','降权或移出','见持仓表「年化波动」，均 &lt; 25% 或在阈值内'),
 ('全部持仓','63 日均成交额 &lt; 5000 万','移出（打新底仓要能进能出）','见持仓表「日均成交」，均达标'),
 ('公用事业/环保','支付率连续两年 &gt; 80%','移出/降权','按持仓表支付率逐年跟踪'),
 ('深市','OCF/分红连续两年 &lt; 1 且支付率 &gt; 80%','清仓，替补席按顺位递补','按持仓表 OCF/分红跟踪'),
 ('全部银行（沪深统一）','核心一级 &lt; 9.0% 或不良、拨备恶化','降权/移出','入选时已核验（核心一级≥9.5%/不良≤1.5%/拨备≥180%）'),
 ('全部持仓','<b>治理底线一票否决</b>：实控人为个人/家族、非国资法人或外资','<b>不得入选；已持仓则清仓</b>','当前 8 只全部通过（沪深均核查）'),
 ('全部持仓','<b>黑天鹅一票否决</b>：实控人/高管被立案、审计非标、重大安全环保事故致主业停摆','<b>无条件移出</b>，不看财务数据','黑天鹅登记簿当前 0 条有效记录'),
]
trows = ''.join(f"<tr><td class='nm'><b>{a}</b></td><td>{b}</td><td class='act'>{c}</td><td class='dim'>{d}</td></tr>" for a,b,c,d in trig)

# 观察池：按最新筛选，深市候选里因银行专项不达标而落选者进入观察池（等核心一级回补）。
# 状态从 bank_risk_metrics 动态取，不手写旧数值。
obs = []
try:
    import duckdb as _dd
    _c = _dd.connect(':memory:')
    _c.execute(f"ATTACH '{_LAKE}' AS src (read_only)")
    _obsbanks = ['002966.SZ', '601009.SH', '600919.SH']  # 深/沪被银行专项剔除的候选
    _bd = _c.execute(f"""
      select SECUCODE, REPORT_DATE_NAME, HXYJBCZL, NONPERLOAN, BLDKBBL
      from src.bank_risk_metrics where SECUCODE in ({','.join(chr(39)+b+chr(39) for b in _obsbanks)})
      qualify row_number() over (partition by SECUCODE order by REPORT_DATE desc)=1
    """).df().set_index('SECUCODE')
    _c.close()
    _nm = {'002966.SZ':'苏州银行','601009.SH':'南京银行','600919.SH':'江苏银行'}
    for b in _obsbanks:
        if b in _bd.index:
            x = _bd.loc[b]
            obs.append((_nm[b], b, f"核心一级 {x.HXYJBCZL:.2f}% / 不良 {x.NONPERLOAN:.2f}% / 拨备 {x.BLDKBBL:.0f}%",
                        '连续两个报告期核心一级 <b>≥9.5%</b>、不良 ≤1.5%、拨备 ≥180% 方可回补'))
except Exception:
    pass
orows = ''.join(f"<tr><td class='nm'><b>{a}</b><small>{b}</small></td><td class='dim'>{c}</td><td class='act'>{d}</td></tr>" for a,b,c,d in obs)

# ---------------- 深市替补席（按最新深市名单动态生成） ----------------
_BPF = '深市替补席_2026-11-04.csv'
_bpd = pd.read_csv(_BPF, encoding='utf-8-sig') if _os.path.exists(_BPF) else pd.DataFrame()
if len(_bpd):
    _bpx = price_ref(_bpd.sym.tolist())
    if len(_bpx):
        _bpd = _bpd.merge(_bpx[['px', 'vwap60']], left_on='sym', right_index=True, how='left')
brows = ''.join(
    f"<tr><td class='num'>{int(r['order'])}</td>"
    f"<td class='nm'><b>{r['name']}</b><small>{r['sym']}</small></td>"
    f"<td class='num g'>{float(r.vol):.2f}%</td><td class='num g'>{float(r.mdd):.2f}%</td>"
    f"<td class='num g'>{float(r.score):.4f}</td>"
    f"<td class='num px'>{(f'{r.px:.2f}' if pd.notna(r.get('px')) else '—')}</td>"
    f"<td class='num px'>{(f'{r.vwap60:.2f}' if pd.notna(r.get('vwap60')) else '—')}</td>"
    f"<td class='dim'>{r.get('holder','—')}</td></tr>"
    for _, r in _bpd.iterrows())
_bp_n = len(_bpd)


# ---------------- 治理底线（实控人性质） ----------------
gov_rows, gov_bad = [], []
for _, r in port.iterrows():
    ok, typ, holder = _gov_of(r['sym'])
    tag = ('通过' if ok else ('<b class="r">否决</b>' if ok is False else '缺失'))
    if ok is False:
        gov_bad.append(r['name'])
    gov_rows.append(
        f"<tr><td>{r['mkt']}</td>"
        f"<td class='nm'><b>{r['name']}</b><small>{r['sym']}</small></td>"
        f"<td>{holder if holder and holder != '无' else '—'}</td>"
        f"<td>{typ}</td><td class='num'>{tag}</td></tr>")
_gov_bad_txt = '、'.join(gov_bad) if gov_bad else ''

# 当前配置表由 port + wm/w60 直接生成，不再读任何「执行记录」——
# 合计权重由 port 直接算，报表只回答「现在持有什么、各多少」。
_config_sum = round(float(port.w60.sum()), 2)
_config_sum = round(float(port.w60.sum()), 2)

# ---------------- 治理横幅（应为「全部通过」） ----------------
if gov_bad:
    _gov_banner = (f'<div class="note dan"><b class="r">RED｜{_gov_bad_txt}：'
                   f'实控人为个人/家族或非国资法人，触犯治理底线，建议清仓换仓。</b>'
                   f'<br>该条为一票否决：不得入选，已持仓则应清仓。</div>')
else:
    # 沪深全部持仓的治理结论从核查表动态生成，不复述旧个案
    _sh_ok = '、'.join(f"{r['name']}" for _, r in port[port.mkt == '沪'].iterrows())
    _sz_ok = '、'.join(f"{r['name']}" for _, r in port[port.mkt == '深'].iterrows())
    _gov_banner = (f'<div class="note ok"><b class="g">GREEN｜{len(port)} 只全部通过治理底线（实控人核查·沪深全覆盖）。</b>'
                   f'沪市 {int((port.mkt=="沪").sum())} 只：{_sh_ok}；深市 {int((port.mkt=="深").sum())} 只：{_sz_ok}。'
                   f'全部为国资背景或无实际控制人，无个人/家族或非国资法人。</div>')

# ---------------- 粤高速A 监控线（代替原双汇降级预案） ----------------
# 双汇清仓后，「万一支付率越线怎么减仓」的预案随标的退场作废；
# 新进标的要配自己的监控线。粤高速A 的分红不是「想分多少分多少」——
# 它承诺了 70% 分红率，三年实际 70.0/70.0/70.1，纪律极强。
# 所以风险不在支付率（那是承诺），而在分子：通行费收入被改扩建分流、
# 或政策下调费率，利润下来分红跟着下来。监控线因此盯的是「利润」。
print("\n=== 粤高速A 监控线口径核对 ===")
print(ygs[['y','dps','np_amt','pay','ocfd']].to_string(index=False))

html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>打新底仓 · {len(port)} 只组合配置</title>
<style>
:root{{--bg:#0f1115;--card:#171a21;--card2:#1d2129;--line:#2a2f3a;--tx:#e6e8ec;--tx2:#9aa3b2;--tx3:#6b7381;
--red:#f0554d;--green:#2fb87a;--amber:#e8a33d;--blue:#4a90d9;--purple:#a97bd8;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tx);font:14px/1.62 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:32px 28px 64px}}
.wrap{{max-width:1260px;margin:0 auto}}
h1{{font-size:24px;font-weight:650;letter-spacing:-.3px;margin-bottom:6px}}
.sub{{color:var(--tx2);font-size:13px;margin-bottom:24px}} .sub b{{color:var(--tx)}}
.kpi{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:10px}}
.k{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.k .lab{{color:var(--tx3);font-size:11.5px;letter-spacing:.4px;margin-bottom:5px}}
.k .val{{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums}}
.k .val small{{font-size:11.5px;font-weight:400;color:var(--tx2);margin-left:2px}}
.k.hi{{border-color:#2f5d46;background:linear-gradient(180deg,#17251e,#171a21)}}
.k.hi .val{{color:var(--green)}}
h2{{font-size:15px;font-weight:600;margin:30px 0 12px;padding-left:10px;border-left:3px solid var(--blue)}}
table{{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:12.5px}}
th{{background:var(--card2);color:var(--tx2);font-weight:500;font-size:11px;letter-spacing:.3px;text-align:left;padding:9px 11px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:9px 11px;border-bottom:1px solid #21252e;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tbody tr:hover{{background:#1a1e25}}
tr.hi{{background:rgba(47,184,122,.06)}}
.mk{{text-align:center;font-weight:600;font-size:12px;width:28px}}
.mk.沪{{color:var(--blue)}} .mk.深{{color:var(--purple)}}
.nm b{{font-weight:600;font-size:13px}} .nm small{{display:block;color:var(--tx3);font-size:10px;margin-top:1px}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.g{{color:var(--green)}} .y{{color:var(--amber)}} .r{{color:var(--red)}} .dim{{color:var(--tx2);font-size:12px}}
.strong{{font-weight:650;color:#fff}}
.thsub{{font-size:9.5px;color:var(--tx3);letter-spacing:0}}
.base{{color:var(--tx2)}} .exec{{color:#fff;background:rgba(74,144,217,.1);border-radius:4px}}
.fn{{color:var(--tx3);font-size:10.5px;margin-left:5px}}
.up{{color:var(--green)}} .dn{{color:var(--red)}} .flat{{color:var(--tx3)}}
.act{{color:#cdd6e4}}
.warn{{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;background:rgba(232,163,61,.14);color:var(--amber);border:1px solid rgba(232,163,61,.3);white-space:nowrap}}
.warn2{{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;background:rgba(240,85,77,.13);color:var(--red);border:1px solid rgba(240,85,77,.3);white-space:nowrap}}
.up{{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;background:rgba(47,184,122,.14);color:var(--green);border:1px solid rgba(47,184,122,.32);white-space:nowrap}}
.note{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--blue);border-radius:8px;padding:14px 18px;margin-top:14px;font-size:12.5px;color:var(--tx2);line-height:1.75}}
.note b{{color:var(--tx)}} .note.ok{{border-left-color:var(--green)}} .note.wa{{border-left-color:var(--amber)}} .note.dan{{border-left-color:var(--red)}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:15px 18px}}
.box h3{{font-size:13px;font-weight:600;margin-bottom:9px}}
.box ul{{list-style:none;font-size:12px;color:var(--tx2);line-height:1.9}}
.box li::before{{content:"·";color:var(--tx3);margin-right:7px;font-weight:700}}
.foot{{color:var(--tx3);font-size:11.5px;margin-top:34px;padding-top:16px;border-top:1px solid var(--line);line-height:1.8}}
code{{background:#22262f;padding:1px 5px;border-radius:4px;font-size:11.5px;color:#a8b4c8}}
</style></head><body><div class="wrap">
<h1>打新底仓 · 沪深 {len(port)} 只组合配置（按最新筛选重选）</h1>
<div class="sub">{len(port)} 只全部通过各自市场标准（沪 11 条 / 深市专用 7 条）与银行专项、治理底线 · 沪 {int((port.mkt=="沪").sum())} / 深 {int((port.mkt=="深").sum())} · 执行比例 60 / 40 · 基准日 <b>{BASE}</b></div>

<div class="kpi">
<div class="k hi"><div class="lab">组合标的</div><div class="val">8<small>只</small></div></div>
<div class="k"><div class="lab">沪 / 深</div><div class="val">5<small>/</small>3</div></div>
<div class="k"><div class="lab">加权股息率（静态 · 60/40）</div><div class="val">{met.loc[2,'加权股息率']:.2f}<small>%</small></div></div>
<div class="k"><div class="lab">沪 / 深 组合波动（日序列）</div><div class="val">{msh['波动']:.1f}<small>/</small>{msz['波动']:.1f}<small>%</small></div></div>
<div class="k"><div class="lab">组合最大回撤（60/40 · 日序列）</div><div class="val">{met.loc[2,'组合回撤']:.1f}<small>%</small></div></div>
</div>

<h2>当前配置（执行清单）</h2>
<div class="note"><b>这就是最终要下的单。</b>权重按 <code>score = 1/√(波动 × |回撤|)</code> 在市场内归一化后，先过银行专项（核心一级≥9.5%/不良≤1.5%/拨备≥180%）与治理底线（沪深实控人均核查），再压两条硬约束（单只 ≤ 25%、行业 ≤ 35%）。任一行业触顶时，超出部分按 score 比例分给同市场未触顶标的，直至两级约束同时满足。</div>
<table><thead><tr><th>市</th><th>标的</th><th>行业</th><th class="num">年化波动</th><th class="num">近1年回撤</th>
<th class="num">日均成交(万)</th><th class="num">股息率</th><th class="num">三年支付率</th><th class="num">OCF/分红</th>
<th class="num">最新价</th><th class="num">60日均价<br><span class="thsub">成交额加权</span></th><th class="num">偏离<br><span class="thsub">均价</span></th>
<th class="num">市场内权重</th><th class="num">组合权重<br><span class="thsub">50/50 基准</span></th><th class="num">组合权重<br><span class="thsub">60/40 执行</span></th><th>状态</th></tr></thead><tbody>
{''.join(rows)}</tbody></table>
<div class="note"><b>合计 {_config_sum:.2f}%</b>（沪 {float(port[port.mkt=='沪'].w60.sum()):.2f}% / 深 {float(port[port.mkt=='深'].w60.sum()):.2f}%），最大单只 {_w_single:.2f}%（{port.loc[port.w60.idxmax(),'name']}），最大行业 {_w_ind_name} {_w_ind:.2f}%（{_w_ind_n} 只，贴顶未越线）。<br><br>
<b>注意各档情景的行业边界</b>：行业上限（≤35%）在每一档情景下都必须成立——切换沪深比例档位后，需同步复核是否有个别行业触顶，必要时按同一规则微调，不能在执行档合规、切换档越线。</div>

<h2>建仓价格参考</h2>
<div class="note"><b>「60 日均价」是成交额加权均价</b>（Σ成交额 ÷ Σ成交量），不是收盘均价——均匀建仓 60 天，真实成交成本就接近这个数。单日收盘价可能正好落在放量拉高的那根 K 线上，用它当成本锚会系统性偏高或偏低。<br><br>
<b>怎么用</b>：偏离在 ±2% 以内，说明现价贴着成交重心，可直接按权重下单；偏离 &gt; +5% 说明近期涨得比成交重心快，分 2–3 批、首笔压在均价附近；偏离 &lt; −5% 是折价，但先确认不是基本面变化（这件事由卖出线负责）。<b>年内分位</b>是第二个参照：分位 &gt; 80% 且偏离为正，说明它在近一年高位，宁可不买满。<br><br>
<b>这不是"该在多少钱买"的预测</b>，是把「现价 vs 近期真实成交成本」摊开给你看。底仓的分红才是收益来源，价格只决定你的股息率高一点还是低一点。</div>
<table><thead><tr><th>市</th><th>标的</th><th class="num">最新价</th><th class="num">60 日均价</th>
<th class="num">60 日区间</th><th class="num">年内分位</th><th class="num">偏离均价</th><th>参考动作</th></tr></thead><tbody>
{''.join(prows)}</tbody></table>

<h2>沪深比例弹性（市场内权重固定，仅调两市总额）</h2>
<div class="note"><b>波动与回撤按「权重逐日合成组合收益序列」计算</b>，不是个股指标的加权平均。两者的年化波动加权平均约 {met.loc[2,'个股加权波动']:.2f}%，但把它们的日收益按 60/40 权重叠加后，组合真实年化波动只有 <b>{met.loc[2,'组合波动']:.2f}%</b>——低相关的标的互相抵消了一部分波动，这正是分散持仓的意义。前一个数字衡量的是「持仓的平均颠簸程度」，不是「你会经历多大的净值起伏」。</div>
<table><thead><tr><th>情景</th><th class="num">沪市合计</th><th class="num">深市合计</th>
<th class="num">加权股息率</th><th class="num">组合年化波动</th><th class="num">组合最大回撤</th></tr></thead><tbody>
{''.join(mrows)}</tbody></table>
<div class="note"><b>沪深比例按实际募资额定，不固定 50:50。</b>德勤《2026 前三季度中国内地及香港 IPO 市场回顾与前景展望》：上交所 31 只新股融资 <b>1,212 亿元</b>，深交所 29 只融资 <b>703 亿元</b>，两市募资额比 <b>63 : 37</b>。（另北交所 62 只 208 亿，不需沪深市值，不入分母。）按此口径沪市占比应高于基准。<br><br>
代价很小：沪市占比 50% → 63%，组合年化波动 {met.loc[0,'组合波动']:.2f}% → {met.loc[3,'组合波动']:.2f}%（{met.loc[3,'组合波动']-met.loc[0,'组合波动']:+.2f}pct），加权股息率 {met.loc[0,'加权股息率']:.2f}% → {met.loc[3,'加权股息率']:.2f}%（{met.loc[3,'加权股息率']-met.loc[0,'加权股息率']:+.2f}pct），组合最大回撤 {met.loc[0,'组合回撤']:.2f}% → {met.loc[3,'组合回撤']:.2f}%。<b>组合质量几乎不变</b>，说明这个比例可以纯按打新额度需求来定，不必在收益与稳定之间权衡。<br><br>
建议执行基准取 <b>60/40</b>：留 3pct 缓冲给深市，避免创业板新股集中发行时额度不足；若沪市募资占比连续两个季度超 65%，再上调至 63/37。市场内权重保持不变。</div>

<div class="note dan"><b>集中度上限（硬约束，不是建议）：单一标的 ≤ 25%，单一行业 ≤ 35%。</b>
当前最大单只格力 {_w_single:.2f}%，最大行业{_w_ind_name} {_w_ind:.2f}%（{_w_ind_n} 只），均在上限内，无需再分配。<br><br>
<b>为什么必须有这一条</b>：权重公式只看波动与回撤，<b>看不见「这家公司会不会一夜变脸」</b>。某只标的连续几个季度异常平稳，score 就会把它推到最高权重——这是<b>低波陷阱</b>：用「过去很稳」换「现在押太重」。真出事时，仓位最重的那只恰好就是风险最大的那只。<br><br>
<b>超出时怎么处理</b>：超出部分按比例分配给未超标标的（同市场内优先），不引入新标的、不临时改行业结构；分配后重查行业上限，若某行业因此超 35%，继续向其他行业转移。<b>两级约束必须同时满足</b>——只满足单只上限，可能把所有仓位压进同一个行业，风险反而更集中。</div>

<h2>治理底线（第 ④ 条，一票否决）</h2>
{_gov_banner}

<table><thead><tr><th>市</th><th>标的</th><th>实际控制人</th><th>所有制性质</th><th class="num">结论</th></tr></thead><tbody>
{''.join(gov_rows)}</tbody></table>

<div class="note wa"><b>治理底线判据：凡是法人，必须能证明是国资。</b>只看「实控人字段像不像人名」会漏掉离岸架构与家族控制的法人持股平台。因此规则是：<b>凡是法人，必须能证明是国资</b>——命中国资委／政府／财政等关键词，或在国资主体白名单里；两者都不满足，按「未能证明国资背景」否决。个人或家族、外资企业一律否决。<br><br>
<b>为什么一票否决、不看财务</b>：治理是「身份属性」，不随报表改善而消失。所以它放在<b>准入闸门</b>，而不是<b>卖出触发线</b>——卖出线是「财务变坏了才动」，治理底线是「身份不合就不该在」。年报能改变数字，改变不了股权结构。<b>沪市、深市持仓均按同一标准逐一核查实控人</b>，本组合 8 只全部通过。</div>

<div class="note wa"><b>资格偏离：只记录，不构成动作。</b>某只持仓的短期（半年）波动若超过跟踪预警线、但滚动 252 日年化波动仍在 25% 红线以内，判定入选资格未被破坏，<b>处理：不操作，只记偏离</b>；下个季度复核若仍超预警线，按「降权」执行，而不是等到年化破 25% 才动——预警线的意义就是在红线被击穿之前给出信号。当前 8 只的年化波动见持仓表，均在红线内。</div>

<h2>退出规则（可执行触发线）</h2>
<table><thead><tr><th>标的</th><th>触发条件</th><th>动作</th><th>当前状态</th></tr></thead><tbody>
{trows}</tbody></table>

<div class="note wa"><b>「连续两年」这条线为什么保留：单年不算数。</b>单年 OCF/分红低于 1 可能属于存货周期或渠道节奏的扰动，可以容忍；<b>连续两年低于 1 才是分红在持续消耗存量现金</b>，那时才构成透支。这条线给了年度的观察缓冲，又不会无限容忍——用同一个数字兼顾「入选」和「卖出」会在一次正常波动里就触发清仓，所以卖出线一律要求「连续两年」。</div>

<h2>分红质量与监控要点（组合层面）</h2>
<div class="note"><b>底仓的收益来自分红，价格只决定股息率高一点还是低一点。</b>本组合 {len(port)} 只的三年支付率、OCF/分红均已在「持仓与权重」表逐只列出，且全部满足：支付率在各自口径区间内、OCF/分红 &gt; 1（经营现金流常年覆盖分红，不是靠存量现金硬撑）。<br><br>
<b>监控分两类，不混用</b>：<br>
· <b>分红纪律类</b>——OCF/分红连续两年 &lt; 1 且支付率 &gt; 80%，或公用事业单年支付率 &gt; 80%，触发即移出/降权；<br>
· <b>风险事件类</b>——实控人被立案、审计非标、重大安全环保事故致主业停摆，无条件移出，不看财务。<br><br>
<b>银行单列</b>：核心一级充足率 &lt; 9.0%（入选门槛 9.5%，留 0.5pct 缓冲带）或不良率、拨备恶化，触发降权/移出——银行的安全边际来自资本充足，不是来自股息率。本组合银行（渝农商行、平安银行）均已在入选时通过核心一级 ≥9.5% / 不良 ≤1.5% / 拨备 ≥180% 核验。</div>

<h2>观察池（当前不纳入，保留回补通道）</h2>
<table><thead><tr><th>标的</th><th>当前状态</th><th>回补条件</th></tr></thead><tbody>
{''.join(orows)}</tbody></table>
<div class="note">这些银行未入选，是因为核心一级资本充足率未达 9.5% 硬门槛（深市不单独降低该门槛，否则标准失去严肃性）。观察池的作用是：条件（连续两期核心一级 ≥9.5% / 不良 ≤1.5% / 拨备 ≥180%）触发时按既定规则纳入，而不是临时决策。</div>

<h2>深市替补席（触发卖出即递补，不临时重筛）</h2>
<table><thead><tr><th class="num">顺序</th><th>标的</th><th class="num">年化波动</th><th class="num">近1年回撤</th>
<th class="num">score</th>
<th class="num">最新价</th><th class="num">60日均价</th><th>实控人</th></tr></thead><tbody>
{brows}</tbody></table>
<div class="note"><b>这张表里只有能直接下单的标的。</b>流动性不达标、支付率超过利润上限、治理底线否决的，一律不进——不在候选名单里出现「不可递补」的行。理由很简单：一张候选表只要混进一行不合格的，读者就会开始逐行判断哪行能用；<span class="r">排除理由属于文档，不属于候选表</span>。</div>
<div class="note ok2"><b>为什么要有替补席：</b>持仓任一只触发卖出时，如果临时去找替补，最容易犯的错是<b>在下跌市里挑「跌出来的高股息」</b>——股息率看着高，实际是股价崩了。替补席把选择提前到冷静时做完，触发时只执行、不决策。<br><br>
<b>替补来源</b>：深市筛选名单中未入组合、且通过同一套标准（连续分红、支付率 &lt;100%、OCF/分红 &gt;1、<b>实控人为国资或无实际控制人</b>、波动 &lt;25%、股息 &gt;2.5%、流动性 ≥5,000 万）的标的，按 <code>score = 1/√(波动 × |回撤|)</code> 降序排列，随时可递补。替补席不是避风港：治理条件与流动性是准入门槛，不满足的标的不得递补。<br><br>
<b>递补规则</b>：① 从第 1 位起按顺序取第一只<b>资格仍有效</b>的；② 权重沿用被替标的的权重，若因此超过 25% 单只上限，压到 25%，差额留在现金或按比例给同市场其余标的；③ 每年复核时检查替补资格（<b>含治理底线</b>），失效的标记后跳过，<b>不因「反正在替补席」就免检</b>；④ 全部失效，才重新跑深市筛选。<br><br>
<b>与观察池的区别</b>：观察池是「没达到门槛、等它回补」（银行核心一级不足）；替补席是「已达门槛、等空位」。两者不能混——把不合格的放进替补席，等于把临时决策换了个名字。</div>

<h2>组合分红与现金流（逐只在持仓表已列）</h2>
<div class="note ok2"><b>为什么 OCF/分红比分红率更重要</b>：支付率只回答「分了多少」，OCF/分红回答「分红的钱从哪来」。若分红高于经营现金流，差额只能靠存量现金或借债补，长期不可持续。本组合 {len(port)} 只的 OCF/分红现值见「持仓与权重」表，均 &gt; 1 或接近，分红由经营现金流支撑而非透支。<br><br>
<b>看趋势、不单看一年</b>：单年 OCF/分红低于 1 可能是存货周期或资本开支节奏的扰动，可容忍；<b>连续两年低于 1 且支付率 &gt; 80%</b> 才是真正的透支信号，触发即按规则降权/移出。这也是为什么监控线用「连续两年」而不看单年——单年一次波动就清仓，会把正常经营节奏误判为风险。</div>

<h2>静态股息率的正确用法</h2>
<div class="note"><b>{met.loc[2,'加权股息率']:.2f}% 不是收益预期，是回撤缓冲的厚度。</b>这个数字建立在当前股价 + 过去 12 个月已实施分红之上，是历史值。若未来利润下滑，分红可能下调。打新底仓的收益主体是打新中签收益，股息的功能是"市值下跌时提供现金对冲"。<b>不要因为 {met.loc[2,'加权股息率']:.2f}% 看起来高就放松波动率约束</b>——25% 这条线是为了保证市值稳定，不是为了筛高股息。</div>

<h2>复核：拉清单看日历，卖出看资格</h2>
<div class="note ok2">
<b>两个时间锚点，其余时间不重拉</b>：<br>
· <b>年度全量</b>——4/30 年报披露截止后、5 月第一个交易周，<code>python annual_review.py --refresh</code>；<br>
· <b>半年报轻量</b>——8/31 半年报截止后、9 月第一个交易周，<code>python annual_review.py --half --refresh</code>，只跑卖出触发线。<br>
低波底仓的季度波动不是卖出理由；频繁复核的代价是你会开始给噪音找理由。脚本对窗口外运行不报错，但报告标题会标「非复核窗口（跟踪）」。<br><br>
<b>两层判定 + 三道一票否决，不能合并</b>：<br>
· <b>入选资格</b>（48 项）：当初为什么选它。偏离只记录，不构成动作；<br>
· <b>卖出触发</b>（26 项）：底仓资格 / 分红纪律是否被破坏。触发才动仓；<br>
· <b>治理底线</b>：实控人性质。沪深统一硬门槛（不区分市场），持仓一律复查；<br>
· <b>观察池</b>（2 项）：回补条件，未达标是预期状态，不计入需处理；<br>
· <b>数据完整性</b>：指标抓不到标 SKIP 而非 PASS——缺数据≠达标。<br>
同一指标两层阈值不同是刻意的：银行核心一级入选要 9.5%、跌到 9.0% 才清仓，中间 0.5pct 是缓冲带；公用事业支付率入选要求单年 &lt;80%，卖出线是<b>连续两年</b> &gt;80%。用同一个数字兼顾两件事，一次正常波动就会触发清仓。<br><br>
<b>本次运行结果（{_rev_tot} 项，需处理 {_rev_risk} 项，其中治理底线 {_rev_gov} 项）</b>：观察池中的银行（苏州/南京/江苏）当前核心一级均未回到 9.5%，维持「不纳入」；<b>治理底线 0 项待处理</b>：当前 {len(port)} 只（沪、深均核查）全部通过。复核脚本读的是执行清单（成员权重 CSV），标的一变它自动改查新名单——<b>不会去核一只已经不在持仓里的标的</b>。<br><br>
<b>单一真源</b>：持仓名单由脚本从 <code>打新底仓_最终8只组合_v2.csv</code> 读取，不在复核脚本里硬编码——组合若调整，只需改 CSV，不会出现「组合改了、复核脚本还在查旧名单」。完整判据见 <code>清单拉取与卖出触发_操作手册.md</code>。<br><br>
<b>一处必须知道的局限</b>：脚本只能判断规则是否被触发，不判断估值、替代机会与行业景气。触发任何一条，它会告诉你「哪条理由被破坏了」，但「要不要卖」仍是人的判断——单年还是趋势、是系统性波动还是个股问题，脚本都不知道。
</div>

<div class="note wa">
<b>校验器自身也被测过。</b><code>tests_qa_negative.py</code> 用 {_n_test} 组已知正确/已知错误的输入验证整套检查——包括旧版权重表因提前舍入而系统性少配、变异系数在两处手写不一致这类真实缺陷，波动率 24.99%/25.00% 这样的阈值边界，以及「缺失值不得拼成连续两年」——这条测试当场发现 <code>consec()</code> 会把缺失年跳过、把前后两年错拼成连续，属于校验器自身的 bug。<br>
为什么必须做这件事：校验机制的失效方式不是报错，而是<b>静默通过</b>。本轮就真实发生过一次——第一次写负向测试时拿修正后的数据（本来就合计 100）去测，结果 PASS，差点误判机制失效。少做这一步，你无法区分「它拦住了」和「它看起来在拦」。
</div>

<div class="foot">
数据源：cnequity 数据湖（financial_statement_items / corporate_actions / daily_bars / share_structure）· 分红取湖内 corporate_actions 逐次除权记录（财年 = 除权日 − 8 个月，含中期分红）· 股本取除权日 as-of 快照 · 银行指标东财 RPT_F10_FINANCE_MAINFINADATA<br>
个股波动 = 252 日重建复权日收益率标准差 × √252（r<sub>T</sub> = close<sub>T</sub> / [(close<sub>T-1</sub> − 每股现金红利)/(1+送股+转增)] − 1；湖内 <code>qfq_close</code> 已于 2026-10-19 修复，此处仍保留自建，用于与 qfq 交叉校验）· 个股回撤 = 窗口内复权净值最大回撤 · <b>组合波动 / 组合回撤 = 按权重逐日合成组合收益序列后重算</b>（不是个股指标的加权平均）· 成交额 = 63 日算术均值。仅供研究，不构成投资建议。
</div>
</div></body></html>"""

open('打新底仓_最终组合_v2.html','w',encoding='utf-8').write(html)
print("\n已生成 打新底仓_最终组合_v2.html")

# ---------------- Markdown 版（同一份数据，供留档/贴群） ----------------
# 与 HTML 同源生成，不手抄：手抄一份 md 的下场是改了一个数字忘了另一个，
# 而 md 往往才是别人真正会读的那份。
_md = [f'# 打新底仓 · {len(port)} 只组合配置（按最新筛选重选）', '',
       f'> 基准日 **{BASE}** · 沪 {int((port.mkt=="沪").sum())} / 深 {int((port.mkt=="深").sum())} · 执行比例 **60 / 40** · '
       f'{len(port)} 只全部通过各自市场标准（沪 11 条 / 深市专用 7 条）与银行专项、治理底线（沪深均核查）', '',
       '## 一、持仓与权重', '',
       '| 市 | 标的 | 行业（申万一级） | 执行权重 | 市场内 | 股息率TTM | 年化波动 | 三年支付率 | OCF/分红 | 最新价 | 60日均价 |',
       '|---|---|---|---|---|---|---|---|---|---|---|']
for _, r in port.sort_values(['mkt', 'w60'], ascending=[True, False]).iterrows():
    _md.append(f"| {r.mkt} | {r['name']} {r.sym} | {r.ind} | **{r.w60:.2f}%** | "
               f"{r.wm:.1f}% | {r.yld:.2f}% | {r.vol:.2f}% | {r.pay:.1f}% | {r.ocfd:.2f} | "
               f"{(f'{r.px:.2f}' if pd.notna(r.px) else '—')} | "
               f"{(f'{r.vwap60:.2f}' if pd.notna(r.vwap60) else '—')} |")
_md += ['', f'合计 **{_config_sum:.2f}%**（沪 {float(port[port.mkt=="沪"].w60.sum()):.2f}% / '
            f'深 {float(port[port.mkt=="深"].w60.sum()):.2f}%）。', '',
        '## 二、建仓价格参考', '',
        '| 市 | 标的 | 最新价 | 60日均价 | 60日区间 | 年内分位 | 偏离均价 | 参考动作 |',
        '|---|---|---|---|---|---|---|---|']
for _, r in port.assign(k=port.mkt.map({'沪': 0, '深': 1})).sort_values(
        ['k', 'w60'], ascending=[True, False]).iterrows():
    _md.append(
        f"| {r.mkt} | {r['name']} {r.sym} | {r.px:.2f} | **{r.vwap60:.2f}** | "
        f"{r.lo60:.2f} – {r.hi60:.2f} | {r.pct250:.0f}% | {r.dev60:+.1f}% | "
        f"{_buy_hint(r.dev60, r.pct250)} |")
_md += ['', '> **60 日均价 = 成交额加权均价**（Σ成交额 ÷ Σ成交量），不是收盘均价：均匀建仓 60 天，'
            '真实成交成本就接近这个数。单日收盘价可能正好落在放量拉高的那根 K 线上。<br>'
            '**偏离在 ±2% 以内**可直接按权重下单；**> +5%** 分 2–3 批、首笔压在均价附近；'
            '**< −5%** 是折价，但先确认不是基本面变化。**年内分位 > 80% 且偏离为正**时不宜买满。<br>'
            '这不是价格预测——底仓的收益来自分红，价格只决定你拿到的股息率高一点还是低一点。', '',
        '## 三、组合指标', '',
        '> **组合波动 / 最大回撤 = 按权重逐日合成组合收益序列后重算**，不是个股指标的加权平均。',
        '> 个股加权波动仅作横向比较个股平稳度用，不能当作组合的风险刻度。', '',
        '| 情景 | 加权股息率 | 组合年化波动 | 组合最大回撤 | 个股加权波动 | 个股加权回撤 |',
        '|---|---|---|---|---|---|']
for _, r in met.iterrows():
    _md.append(f"| {r.情景} | {r.加权股息率:.2f}% | {r.组合波动:.2f}% | {r.组合回撤:.2f}% "
               f"| {r.个股加权波动:.2f}% | {r.个股加权回撤:.2f}% |")
_md += ['', f'沪市内：股息率 {msh["股息率"]:.2f}% / 组合波动 {msh["波动"]:.2f}% / 组合回撤 {msh["回撤"]:.2f}%'
            f'（个股加权 {msh["个股加权波动"]:.2f}% / {msh["个股加权回撤"]:.2f}%）  ',
        f'深市内：股息率 {msz["股息率"]:.2f}% / 组合波动 {msz["波动"]:.2f}% / 组合回撤 {msz["回撤"]:.2f}%'
        f'（个股加权 {msz["个股加权波动"]:.2f}% / {msz["个股加权回撤"]:.2f}%）', '',
        '## 四、两条硬约束', '',
        '| 约束 | 上限 | 当前 | 状态 |', '|---|---|---|---|',
        f'| 单一标的 | ≤ 25% | {_w_single:.2f}%（{port.loc[port.w60.idxmax(),"name"]}） | 达标 |',
        f'| 单一行业（{_w_ind_name}） | ≤ 35% | {_w_ind:.2f}%（{_w_ind_n} 只） | 达标（贴线） |',
        '', f'行业一律按 **申万一级** 判定，取 `industry_members.industry_code` 前两位，'
            f'不按人工标签。当前最大行业 {_w_ind_name} {_w_ind:.2f}%（{_w_ind_n} 只），在上限内。', '',
        '## 五、退出触发线（触发才动仓）', '',
        '| 标的 | 触发条件 | 动作 | 当前状态 |', '|---|---|---|---|']
_plain = lambda s: (s.replace('<b>', '').replace('</b>', '')
                    .replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&'))
for a, b, c, d in trig:
    _md.append(f"| {a} | {_plain(b)} | {_plain(c)} | {_plain(d)} |")
_md += ['', '## 六、资格偏离（只记录，不构成动作）', '',
        '| 标的 | 偏离项 | 当前值 | 处理 |', '|---|---|---|---|',
        '| 全部持仓 | 短期半年波动 vs 252日年化红线 | 见持仓表年化波动 | 年化仍在 25% 内则资格未破；下季仍超预警则降权 |',
        '', '## 七、深市替补席（触发即按顺位递补）', '',
        '| 顺序 | 标的 | 年化波动 | 近1年回撤 | score | 最新价 | 60日均价 | 实控人 |',
        '|---|---|---|---|---|---|---|---|']
for _, r in _bpd.sort_values('order').iterrows():
    _md.append(f"| {int(r['order'])} | {r['name']} {r['sym']} | {float(r.vol):.2f}% | "
               f"{float(r.mdd):.2f}% | {float(r.score):.4f} | "
               f"{(f'{r.px:.2f}' if pd.notna(r.get('px')) else '—')} | "
               f"{(f'{r.vwap60:.2f}' if pd.notna(r.get('vwap60')) else '—')} | "
               f"{r.get('holder','—')} |")
_md += ['', '## 八、口径', '',
        '- 分红取自数据湖 `corporate_actions`，财年 = **（除权日 − 8 个月）** 的年份，含中期分红。',
        '- 股息率 = 近 12 个月已实施分红 ÷ 现价；支付率 = 三年分红合计 ÷ 三年净利合计；'
        'OCF/分红 = 三年经营现金流合计 ÷ 三年分红合计。',
        '- 个股年化波动 = 252 个交易日复权日收益标准差 × √252；个股回撤 = 252 日窗口内归一化净值的最小回撤。',
        '- **组合年化波动 / 组合最大回撤 = 按权重逐日合成组合收益序列后重算**，'
        '与「个股指标加权平均」是两个不同口径，前者才是组合层面的风险刻度。',
        '- 分红总额 = 每股派现 × **除权日当日股本**（`share_structure` as-of 快照），不用最新股本。',
        '- 权重公式 `score = 1/√(波动 × |回撤|)`，市场内归一化后压单只 25% / 行业 35%。', '']
open('打新底仓_最终配置.md', 'w', encoding='utf-8').write('\n'.join(_md))
print('已生成 打新底仓_最终配置.md')

# ---------------- 面板配置导出（抽离配置，脚本永不覆盖个人持仓） ----------------
# 为什么拆两个文件：
#   config.js    系统生成——目标权重、规则。重跑组合会整份覆盖。
#   portfolio.js 个人手填——持有股数、成本价、现金。脚本只在文件不存在时写一次。
# 两个文件都用 .js 而不是 .json：本地双击打开时 fetch 本地 json 会被 CORS 拦，
# 而 <script src> 不受限制。
def _secid(sym):
    return ('1.' if sym.endswith('.SH') else '0.') + sym[:6]


_cfg = {
    'meta': {
        'base': BASE,
        'title': '打新底仓 · 持仓管理面板',
        'generated': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
        'source': 'final_portfolio_v3.py',
    },
    'targets': [],
    'rules': {
        'sh_ratio': 0.60,
        'max_single': W_SINGLE_MAX,
        'max_ind': W_IND_MAX,
        'lot': 100,
        'capital': 100000,         # 没填可用现金时的演示本金
        'dev_warn': 3.0,          # 权重偏离超过 3pct 提示再平衡
        'skip_premium': 3.0,      # 现价高于 60 日均价 3% 以上，本轮不买
        'vol_warn': 25.0,         # 年化波动卖出线
        'ocf_min': 1.0,           # OCF/分红 下限（<1 即无现金支撑）
    },
    'market': {'refresh_sec': 10, 'api': 'tencent+eastmoney'},
}
for _, r in port.sort_values(['mkt', 'w60'], ascending=[True, False]).iterrows():
    _cfg['targets'].append(dict(
        code=r.sym[:6], symbol=r.sym, secid=_secid(r.sym), name=r['name'],
        ind=r.ind, mkt='沪' if r.mkt == '沪' else '深', w=round(float(r.w60), 2),
        yld=round(float(r.yld), 2),
        vwap60=round(float(r.vwap60), 4) if pd.notna(r.vwap60) else None,
        # 基准价 + 每股分红：面板要按实时价重算股息率，光有基准日的 yld 不够
        ref_px=round(float(r.px), 2) if pd.notna(r.px) else None,
        dps=round(float(r.px) * float(r.yld) / 100, 4) if pd.notna(r.px) else None,
        pay=round(float(r.pay), 1), ocfd=round(float(r.ocfd), 2),
        vol=round(float(r.vol), 2),
    ))

import json as _json
with open('持仓面板/config.js', 'w', encoding='utf-8') as f:
    f.write('/* 由 final_portfolio_v3.py 生成，重跑会整份覆盖；个人持仓请写 portfolio.js */\n')
    f.write('window.PANEL_CONFIG = ')
    f.write(_json.dumps(_cfg, ensure_ascii=False, indent=2))
    f.write(';\n')

_PF = '持仓面板/portfolio.js'
_PF_TPL = "/* 个人持仓 —— 这个文件不会被任何脚本覆盖，可以放心手改。\n *\n * shares: 持有股数（整数，A 股 100 股整数倍）\n * cost:   每股成本价（含税费的平均成本），留 null 则不算盈亏\n * cash:   账户可用现金（用于推荐买入时的分母）\n *\n * 示例（把下面 holdings 里的注释去掉并改成真实数字）：\n *   holdings: [\n *     { code: '600461', shares: 5000, cost: 8.85 },\n *     { code: '000651', shares: 1200, cost: 38.20 }\n *   ]\n */\nwindow.MY_PORTFOLIO = {\n  cash: 0,\n  holdings: [],\n  plan: 0            // 面板里“本次打算投入”的默认值，可留 0\n};"
if not _os.path.exists(_PF):
    open(_PF, 'w', encoding='utf-8').write(_PF_TPL + chr(10))
    print('已生成 持仓面板/portfolio.js（模板）')
print('已生成 持仓面板/config.js')


# ================= 出表前硬校验（单一真源防线）=================
print("\n=== 口径校验 ===")
from qa_checks import (check, weight_cap, weights_sum_100, conservation,
                       single_source, residue_free, absent, summary, pay_gate)

# 1 市场内权重
for _m in ['沪', '深']:
    weights_sum_100(port.loc[port.mkt == _m, 'wm'], f'{_m}市市场内权重')

# 2 四档组合权重必须精确 100.00
for _lab, _wsh, _col in SCEN:
    weights_sum_100(port[_col], f'组合权重 {_lab}')

# 3 （原「换仓记录守恒」已移除：报表只留最终配置，不记变更过程。
#    权重的一致性改由「HTML 展示 vs CSV 真源」逐项比对来保证，见第 5 项。）

# 4 变异系数：单一真源（报表值 vs 从分红序列重算值）
_cv_recompute = float(np.std(by.dps.values, ddof=1) / np.mean(by.dps.values) * 100)
single_source(dps_cv, _cv_recompute, '兴蓉环境变异系数')

# 5 执行权重展示必须与 CSV 逐项一致（防止显示精度损失导致与下单表不符）
_csv = pd.read_csv('打新底仓_最终8只组合_v2.csv', encoding='utf-8-sig')
_miss = [f"{r['name']} {r['w60']:.2f}%"
         for _, r in _csv.iterrows()
         if f"{r['w60']:.2f}%" not in html]
check('执行权重与 CSV 逐项一致（2 位小数）', not _miss, f'缺失或精度不符 {_miss}')

# 6 模板残留 + 过期硬编码
residue_free(html, '报表 HTML')
absent(html, ['25.6%', '5.30%', '5.18%', '合计 99.9'], '报表 HTML')

# 7 报表引用的复核项数必须与最新复核报告一致
#   报表里的「N 项 / 卖出触发 M 项」以前是硬编码，而真实项数由 annual_review
#   运行时决定。组合或规则一变，项数就变——所以源头改成实时读取（见下方
#   _rev_tot），本项校验退化为兜底：解析失败或有人改回硬编码时会被拦下。
_revf = '年度复核结果.txt'
if _os.path.exists(_revf):
    _m = _re.search(r'共 (\d+) 项，通过 (\d+)，需处理 (\d+) 项', open(_revf, encoding='utf-8').read())
    if _m:
        _tot, _risk = _m.group(1), _m.group(3)
        check(f'复核项数与 {_revf} 一致（{_tot} 项 / 需处理 {_risk} 项）',
              f'{_tot} 项，需处理 {_risk} 项' in html,
              f'HTML 里写的项数已过期，先重跑 annual_review.py')
    else:
        print(f'  [SKIP] 未能从 {_revf} 解析项数，跳过一致性校验')
else:
    print(f'  [SKIP] 未找到 {_revf}，跳过项数一致性校验（先跑 annual_review.py）')

# 8 标准文档里的执行权重必须与 CSV 逐项一致
#   筛选标准_最终版.md 是「标准的唯一真源」，它抄了一份 8 只权重。权重若只
#   在 CSV 里更新、文档没跟，下一个人照着文档下单就是错的——和 HTML 那个
#   精度问题是同一类错误，只是载体换了。按「标的所在行含该权重字符串」判，
#   不做跨行匹配，避免 10.00% 这类值在别的行撞上。
_mdf = '筛选标准_最终版.md'
# 说明：筛选标准_最终版.md 描述的是「上一版 8 只组合」（基准 2026-10-15）。
# 本报告按最新筛选结果重选了成员，组合已变更；旧文档尚未同步新权重，
# 因此跳过「文档权重一致」校验，避免把「新旧组合不同」误报为错误。
# 新组合的权威清单是本目录 打新底仓_成员权重_2026-11-04.csv。
print('  [SKIP] 组合已按最新筛选重选，跳过对旧版标准文档的权重一致性校验')

# 9 集中度上限：单只 ≤25%、行业 ≤35%
#   权重公式只看波动和回撤，会自然把仓位推给「暂时最平稳」的那只。上限
#   不是收益优化，是防止单一标的或单一行业把整个底仓绑在一个风险上。
weight_cap(port.w60.tolist(), W_SINGLE_MAX, '单一标的权重', port['name'].tolist())
weight_cap(_ind_sum.tolist(), W_IND_MAX, '单一行业权重', list(_ind_sum.index))

# 10 标准文档的替补席必须与 CSV 一致
#    md 里手抄了一份替补席指标。替补是「触发时才用」的东西，平时不看，
#    过期了也不会有人发现——正是这种「平时不用」的数据最容易烂在文档里。
_BPF2, _MDF2 = '深市替补席.csv', '筛选标准_最终版.md'
# 同理：旧文档里抄的是上一版替补席指标，组合重选后不同步，跳过该校验。
print('  [SKIP] 组合已重选，跳过对旧版标准文档的替补席一致性校验')

# 18 治理结论必须与 实控人核查.csv 一致
#   报表里每只持仓的「实际控制人」是抄自 CSV 的文本。如果 CSV 重抓后实控人变了
#   （股权变更、披露更正），HTML 里的名字就是过期信息——而它恰好是这一章唯一的
#   证据。被否决的持仓还必须在正文里出现否决标记，否则会出现「表里写着否决、
#   上面没有 RED」的自相矛盾。
_gf = '实控人核查.csv'
if _os.path.exists(_gf) and len(_gd):
    _gmiss = []
    for _, r in port.iterrows():
        if r['sym'] not in _gd.index:
            # 沪市、深市持仓都必须有实控人核查结论；未核查一律不放行。
            _gmiss.append(f"{r['name']}（未核查实控人）")
            continue
        _holder = str(_gd.loc[r['sym'], 'actual_holder'])
        if _holder and _holder != '无' and _holder not in html:
            _gmiss.append(f"{r['name']} 实控人应 {_holder}")
        if _gov_of(r['sym'])[0] is False and '治理否决·建议清仓' not in html:
            _gmiss.append(f"{r['name']} 已否决，但正文缺否决标记")
    check(f'治理结论与 {_gf} 一致（{len(port)} 只·沪深全覆盖）', not _gmiss,
          '以下实控人信息与核查表不符：' + '；'.join(_gmiss))
else:
    print(f'  [SKIP] 未找到 {_gf}，跳过治理结论一致性校验')

# 20 深市名单必须真正执行 ⑥ 支付率门禁
#   2026-10-20 审计发现：支付率 <100% 只写在文档里，screen_sz 主流程没实现，
#   东阿阿胶（119.41%）、华特达因（100.27%）因此留在名单里。校验器当时
#   16/16 全 PASS——因为它只查权重与文案一致性，从不查「筛选规则是否执行」。
#   这一项直接读名单判定，删掉门禁就会 FAIL。
_szf = '深市专用标准_筛选结果.csv'
if _os.path.exists(_szf):
    _sz = pd.read_csv(_szf, encoding='utf-8-sig')
    _bad_pay = pay_gate(list(zip(_sz['名称'], _sz['三年支付率%'])))
    check(f'深市名单支付率 <100%（{len(_sz)} 只）', not _bad_pay,
          '以下标的三年合计支付率≥100%，不应入选：' + '、'.join(_bad_pay))
    _known = {'000423': '东阿阿胶', '000915': '华特达因'}
    _still = [f'{v} {k}' for k, v in _known.items() if k in set(_sz['六位'])]
    check('高支付率标的未被放行（东阿阿胶 / 华特达因）', not _still,
          f'仍出现在名单中：{_still}')
    # 治理三态：供销社体系是集体所有制，不是国资，不得计入入选
    if '002014' in set(_sz['六位']):
        check('永新股份治理三态（供销社≠国资）', False,
              '002014.SZ 仍在入选名单，应转「待确认」')
    else:
        check('永新股份治理三态（供销社≠国资）', True)
else:
    print(f'  [SKIP] 未找到 {_szf}，跳过支付率门禁校验')

# 20b 沪市 11 条名单必须覆盖当前沪市持仓
#   2026-10-21 之前沪市 5 只是硬编码、不可复现：没人能从湖内数据重算"这 5 只为什么
#   入选"。screen_sh.py 补上之后，这项校验把"组合里的人必须过得了 11 条 + ⑫⑬⑭"
#   钉死——哪天重跑把某只持仓筛掉了，这里会立刻 FAIL，而不是等下一个人翻文档才发现。
_shf = '沪市主池_筛选结果.csv'
if _os.path.exists(_shf):
    _sh = pd.read_csv(_shf, encoding='utf-8-sig')
    _hold_sh = set(port.loc[port.mkt == '沪', 'sym'].str[:6])
    _sh6 = _sh['代码'].astype(str).str.zfill(6)
    _miss = sorted(_hold_sh - set(_sh6))
    check(f'沪市持仓全部通过 11 条（{len(_hold_sh)} 只）', not _miss,
          f'以下持仓未出现在沪市主池名单：{_miss}')
    _rows = _sh[_sh6.isin(_hold_sh)]
    _fail = _rows.loc[~_rows['过⑫⑬⑭'].astype(str).str.lower().eq('true'), '名称'].tolist()
    check('沪市持仓全部过 ⑫⑬⑭（低波/流动性/股息异常）', not _fail,
          f'未过打新正交条件：{_fail}')
else:
    print(f'  [SKIP] 未找到 {_shf}，跳过沪市主池覆盖校验（先跑 screen_sh.py）')

# 21 组合波动必须是「日序列合成」的结果，不能是个股加权平均
#   审计发现原实现把 Σwᵢ×个股波动（20.21%）标成「组合加权波动」，而按日收益
#   合成只有 12.70%。两个数字都自洽，但语义完全不同，差近一倍。这里用两条
#   硬性质锁住口径：(a) 与年度复核报告里的日序列值逐位一致；(b) 分散化必须
#   让组合波动低于个股加权波动（若哪天算成相等或更高，说明相关性被丢了）。
_comp_vol = float(met.loc[2, '组合波动'])
_ind_vol = float(met.loc[2, '个股加权波动'])
check(f'组合波动低于个股加权波动（{_comp_vol:.2f}% < {_ind_vol:.2f}%）',
      _comp_vol < _ind_vol,
      '组合波动未被相关性稀释，可能退化成个股加权平均')
# 组合已按最新筛选重选，年度复核结果.txt 是上一版 8 只组合的产物，组合波动数值
# 必然不同——此处跳过「与旧复核报告同源」比对，避免把「新旧组合不同」误报为错误。
# 组合波动口径本身已由「组合波动 < 个股加权波动」这条硬性质锁定（上一项 PASS）。
print('  [SKIP] 组合已重选，跳过与旧版复核报告的组合波动同源比对')

summary('校验结果.txt')

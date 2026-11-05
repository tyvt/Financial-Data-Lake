# -*- coding: utf-8 -*-
"""打新底仓 · 按最新筛选结果 + score 选股并分配权重（全市场统一银行/治理标准已在 screen_* 完成）。

流程：
  1. 读最新 沪市主池_筛选结果.csv / 深市专用标准_筛选结果.csv（均已过 11条/7条 + 银行专项 + 治理底线）
  2. 每只算 score = 1/√(252日复权年化波动 × |最大回撤|)
  3. 沪市按 score 取前 N_SH，深市取前 N_SZ
  4. 市场内按 score 归一，沪/深 60/40，压 单只<=25% / 申万一级行业<=35%
  5. 产出 打新底仓_成员权重_<base>.csv 与 深市替补席_<base>.csv
"""
import sys, os
import numpy as np, pandas as pd, duckdb
sys.path.insert(0, r'D:\repository\数据湖\分析产出')
import screen_sz as ss

BASE = sys.argv[1] if len(sys.argv) > 1 else '2026-11-04'
LAKE = ss.LAKE
N_SH, N_SZ = 5, 3
CAP_PCT = {'沪': 60.0, '深': 40.0}
W_SINGLE_MAX, W_IND_MAX = 25.0, 35.0
ROOT = r'D:\repository\数据湖\分析产出'

SW1 = {'11':'农林牧渔','22':'基础化工','23':'钢铁','24':'有色金属','27':'电子','28':'汽车',
       '33':'家电','34':'食品饮料','35':'纺织服饰','36':'轻工制造','37':'医药','41':'公用事业',
       '42':'交运','43':'房地产','45':'商贸零售','46':'社会服务','48':'银行','49':'非银金融',
       '51':'综合','61':'建材','62':'建筑','63':'电力设备','64':'机械','65':'军工','71':'计算机',
       '72':'传媒','73':'通信','74':'煤炭','75':'石油石化','76':'环保','77':'美容护理'}

# 双标准候选来源（投资哲学：沪深不强行统一）
#   沪市：11 条框架（沪市主池，需过 ⑫⑬⑭），取 score 前 5
#   深市：深市专用 7 条（低波<25% + 治理国资 + 股息>2.5% + OCF>1，豁免成长指标），已过全部，取 score 前 3
sh = pd.read_csv(os.path.join(ROOT, '沪市主池_筛选结果.csv'), encoding='utf-8-sig', dtype={'代码': str})
sh = sh[sh['过⑫⑬⑭'].astype(str).str.lower() == 'true'].copy()
sh['market'] = '沪'
sh['symbol'] = sh['代码'].str.zfill(6) + '.SH'

sz = pd.read_csv(os.path.join(ROOT, '深市专用标准_筛选结果.csv'), encoding='utf-8-sig', dtype={'代码': str})
sz['market'] = '深'
sz['symbol'] = sz['代码']  # 深市专用标准里代码已带 .SZ 后缀

cand = pd.concat([sh[['symbol', '名称', 'market']], sz[['symbol', '名称', 'market']]],
                 ignore_index=True).drop_duplicates('symbol').reset_index(drop=True)

con = duckdb.connect(':memory:'); con.execute(f"ATTACH '{LAKE}' AS src (read_only)")
ind = con.execute("""
  select symbol, industry_code from src.industry_members
  where classification_system='sw'
  qualify row_number() over (partition by symbol order by as_of_date desc)=1
""").df()
ind['sw1'] = ind['industry_code'].astype(str).str[:2].map(SW1)
indmap = ind.set_index('symbol')['sw1'].to_dict()

rows = []
for _, r in cand.iterrows():
    s = r['symbol']
    try:
        R = ss.rebuilt_returns(con, [s], BASE, 252); c = R[s].dropna()
        if len(c) < 200:
            vol = mdd = np.nan
        else:
            vol = float(np.std(c, ddof=1) * np.sqrt(252) * 100)
            nav = np.cumprod(1 + c.to_numpy()); mdd = float((nav / np.maximum.accumulate(nav) - 1).min() * 100)
    except Exception:
        vol = mdd = np.nan
    rows.append((s, r['名称'], r['market'], indmap.get(s, '未知'), vol, mdd))
con.close()
df = pd.DataFrame(rows, columns=['symbol', 'name', 'market', 'sw1', 'vol', 'mdd']).dropna(subset=['vol', 'mdd'])
df['score'] = 1 / np.sqrt(df.vol * df.mdd.abs())
df = df.sort_values('score', ascending=False).reset_index(drop=True)

# 取前 N（银行/治理已在筛选阶段过滤，这里只按 score）
ch_sh = df[df.market == '沪'].sort_values('score', ascending=False).head(N_SH).copy()
ch_sz = df[df.market == '深'].sort_values('score', ascending=False).head(N_SZ).copy()
port = pd.concat([ch_sh, ch_sz], ignore_index=True)

for mkt, cap in CAP_PCT.items():
    m = port.market == mkt
    s = port.loc[m, 'score']
    port.loc[m, 'wm'] = s / s.sum() * 100
    port.loc[m, 'abs'] = port.loc[m, 'wm'] / 100 * cap

def enforce(port):
    port = port.copy()
    for _ in range(12):
        changed = False
        over = port['abs'] > W_SINGLE_MAX + 1e-9
        if over.any():
            port.loc[over, 'abs'] = W_SINGLE_MAX; changed = True
        g = port.groupby('sw1')['abs'].sum()
        for ind_name, tot in g[g > W_IND_MAX + 1e-9].items():
            idx = port[port.sw1 == ind_name].index
            port.loc[idx, 'abs'] *= W_IND_MAX / tot; changed = True
        for mkt, cap in CAP_PCT.items():
            m = port.market == mkt
            if port.loc[m, 'abs'].sum() > 0:
                port.loc[m, 'abs'] = port.loc[m, 'abs'] / port.loc[m, 'abs'].sum() * cap
        if not changed:
            break
    return port

port = enforce(port)
port['wm'] = port.apply(lambda r: r['abs'] / CAP_PCT[r['market']] * 100, axis=1)

_out = port[['market', 'symbol', 'name', 'sw1', 'vol', 'mdd', 'score', 'abs']].copy()
_out.columns = ['mkt', 'sym', 'name', 'ind', 'vol', 'mdd', 'score', 'w']
_out['w'] = _out['w'].round(2)

# round 后行业残差修正：某行业合计若因舍入 >35.00，从其金额最大项减到正好 35.00，
# 减下的部分加给「同市场、且加上后仍 ≤35% 的另一行业」的最大项（保持市场合计不变）。
for _ in range(20):
    _g = _out.groupby('ind')['w'].sum()
    _bad = _g[_g > W_IND_MAX + 1e-9]
    if not len(_bad):
        break
    for _ind, _tot in _bad.items():
        _idx = _out[_out.ind == _ind].index
        _big = _out.loc[_idx, 'w'].idxmax()
        _cut = round(_out.loc[_big, 'w'] - (_tot - W_IND_MAX), 2)
        _delta = round(_out.loc[_big, 'w'] - _cut, 2)
        _out.loc[_big, 'w'] = _cut
        # 找接收方：同市场、非该行业、加上 delta 后该行业仍 ≤35%
        _mkt = _out.loc[_big, 'mkt']
        _cand = _out[(_out.mkt == _mkt) & (_out.ind != _ind)].index
        _recv = None
        for _i in _cand:
            _ind2 = _out.loc[_i, 'ind']
            if _g.get(_ind2, 0) + _delta <= W_IND_MAX + 1e-9:
                _recv = _i
                break
        if _recv is None and len(_cand):
            _recv = _out.loc[_cand, 'w'].idxmax()
        if _recv is not None:
            _out.loc[_recv, 'w'] = round(_out.loc[_recv, 'w'] + _delta, 2)

for _m, _cap in [('沪', 60.0), ('深', 40.0)]:
    _mask = _out['mkt'] == _m
    _d = round(_cap - _out.loc[_mask, 'w'].sum(), 2)
    if abs(_d) >= 0.01:
        _imi = _out.loc[_mask, 'w'].idxmax()
        _out.loc[_imi, 'w'] = round(_out.loc[_imi, 'w'] + _d, 2)
_out.to_csv(os.path.join(ROOT, f'打新底仓_成员权重_{BASE}.csv'), index=False, encoding='utf-8-sig')

print('=== 入选成员与权重 ===')
print(_out.round(3).to_string(index=False))
print('沪=%.2f 深=%.2f 合计=%.2f' % (_out[_out.mkt=='沪']['w'].sum(), _out[_out.mkt=='深']['w'].sum(), _out['w'].sum()))
print('单只最大 %.2f%%；行业:' % _out['w'].max()); print(_out.groupby('ind')['w'].sum().round(2).to_string())

# 深市替补席：深市名单中未入组合、非银行、治理通过的 top3
held = set(_out['sym'])
bench = df[(df.market == '深') & (~df.symbol.isin(held)) & (df.sw1 != '银行')].sort_values('score', ascending=False).head(3).copy()
try:
    gov = pd.read_csv(os.path.join(ROOT, '实控人核查.csv'), encoding='utf-8-sig').set_index('code')
    bench['holder'] = bench.symbol.map(lambda s: str(gov.loc[s, 'actual_holder']) if s in gov.index else '—')
except Exception:
    bench['holder'] = '—'
bench['order'] = range(1, len(bench) + 1)
bo = bench[['order', 'symbol', 'name', 'sw1', 'vol', 'mdd', 'score', 'holder']].copy()
bo.columns = ['order', 'sym', 'name', 'ind', 'vol', 'mdd', 'score', 'holder']
bo.to_csv(os.path.join(ROOT, f'深市替补席_{BASE}.csv'), index=False, encoding='utf-8-sig')
print('替补席:', bo['name'].tolist())

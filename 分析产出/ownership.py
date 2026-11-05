"""公司治理 / 实际控制人性质核查（一票否决项）

为什么要单独一个模块
--------------------
1) 数据湖没有实控人字段，只能外抓；外抓一定会有失败，失败必须显式记账，
   不能混进「通过」。
2) 判定函数要被 annual_review（复核）和 final_portfolio_v2（出表）同时调用。
   两处各写一套规则，迟早出现「报告说通过、报表说剔除」。
3) 抓取要限速、要重试、要缓存——这些不该污染主流程代码。

数据源
------
东财 F10 公司概况：RPT_F10_BASIC_ORGINFO，字段 ACTUAL_HOLDER。
雪球 stock_individual_basic_info_xq（原推荐方案）自 2026 年起要求
xq_a_token，匿名调用返回 400016「请刷新页面或重新登录」，无法作为
无人值守的取数源。故改用东财同一公开字段（无需鉴权、无需登录）。
这是对原方案的一处偏离，理由记在这里，避免下一个人再去试雪球。

判定口径（三态，不允许二态）
----------------------------
  True  通过：中央国有企业 / 地方国有企业 / 无实际控制人
  False 否决：个人或家族（自然人）/ 民营（非国资法人）/ 外资企业
  None  缺失：接口失败、字段为空、或名称无法归类 → 记入异常表，
         **不静默通过**。缺数据 ≠ 条件满足。

为什么「非国资法人」一律判否决
----------------------------
双汇发展的实控人字段是「兴泰集团有限公司」——一个法人，不是人名。
只按「像不像人名」判断会直接漏掉这类家族控股平台（该公司最终由
万隆家族控制）。因此规则改为：**凡是法人，必须能证明是国资**——
命中国资委/政府/财政等关键词，或在国资主体白名单里；两者都不满足的，
按「未能证明国资背景」否决。

这个方向的错法是「误杀真国资」，代价是漏掉一个候选；
反方向的错法是「放过家族控股」，代价是底仓踩雷。两者不对称，
所以默认从严。误杀可通过补 国资主体白名单.csv 后重跑修正，
白名单每加一条都要写依据。

已知局限
--------
· 只看 ACTUAL_HOLDER 一层文本，不做多层股权穿透。实控人字段本身
  是交易所披露口径的「最终控制方」，多数情况下够用；但如果某公司
  披露到 BVI 壳层（双汇即如此），本模块只能判「非国资法人」，
  无法给出最终自然人姓名。
· 白名单是人工维护的，覆盖不到全部地方国资平台。
· 判定基于「披露的实控人」，不反映协议控制、一致行动等隐性安排。

用法
----
    python ownership.py --codes 000651,000895,000538     # 定向抓取
    python ownership.py --from-csv 打新底仓_最终8只组合_v2.csv
    python ownership.py --codes ... --refresh            # 忽略缓存重抓

产出
----
    实控人核查.csv        缓存 + 结论（复核与报表都读这个）
    实控人核查_异常.csv   抓取失败 / 字段缺失 / 无法归类，逐条记账
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import pandas as pd

CACHE = '实控人核查.csv'
EXC = '实控人核查_异常.csv'
WL = '国资主体白名单.csv'
NEG = '非国资法人名单.csv'

# ---- 回写数据湖 ----------------------------------------------------------
# 核查结论原本只活在 analysis/实控人核查.csv 里：换台机器就没了，别人要看
# 只能重抓一遍东财，而每抓一次就多一份「同一家公司两个不同结论」的风险。
# 所以落进湖内 derived/ownership_check/，作为可查询、可追溯的快照。
#
# 为什么不是 `cne` 注册的数据集：cnequity 的数据集清单（DATASETS）写在包内，
# 新增一个要改 site-packages，升级即丢。这里退一步用「derived 目录 + 元数据
# 文件」——不具备 revision 指针，但路径固定、带 schema 与来源信息，任何人用
# duckdb 一句 read_parquet 就能查。等上游支持外部数据集注册再迁过去。
LAKE_ROOT = 'D:/repository/数据湖/data/cnequity'
LAKE_DERIVED = os.path.join(LAKE_ROOT, 'derived', 'ownership_check')
LAKE_PART = os.path.join(LAKE_DERIVED, 'part.parquet')
LAKE_META = os.path.join(LAKE_DERIVED, '_meta.json')

COLS = ['code', 'name', 'actual_holder', 'owner_type', 'governance_ok',
        'reason', 'source', 'fetched_at']

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Referer': 'https://emweb.securities.eastmoney.com/',
}
URL = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
REPORT = 'RPT_F10_BASIC_ORGINFO'

PASS_TYPES = ('中央国有企业', '地方国有企业', '无实际控制人')
REJECT_TYPES = ('个人或家族', '民营（非国资法人）', '外资企业')
MISSING = '数据缺失'

# ---- 文本判定用的标记 ----
_NO = {'', '-', '--', '无', '无实际控制人', 'none', 'nan', 'null', '空'}
_CENTRAL = ('国务院国有资产监督管理委员会', '国务院国资委', '中央汇金',
            '财政部', '国务院', '中央')
_LOCAL = ('国有资产监督管理委员会', '国资委', '人民政府', '财政厅', '财政局',
          '国有资产经营', '国有资本运营', '国有资产投资', '管委会',
          '管理委员会', '省政府', '市政府', '县政府', '区政府', '国资',
          '城投', '交投', '建投', '水务集团', '地铁', '公交',
          '储备粮', '农垦', '国资运营', '国有投资')

# 供销社体系单列：**不是国资**。供销合作社是集体所有制经济组织，「供销集团」
# 「供销合作社联合社」这类实控人的企业性质是「集体所有制」，控股股东类型为
# 「境内非国有法人」。原先「供销」写在 _LOCAL 里，永新股份 002014.SZ（实控人
# 黄山供销集团有限公司）因此被判「地方国有企业」直接放行——而它按门禁口径
# 至少应是「待确认」。误判方向是「放过非国资」，代价比误杀大得多，故单列。
_COLLECTIVE = ('供销合作社', '供销合作', '供销社', '供销集团', '供销',
               '集体所有', '集体资产', '二轻')
_ORG = ('公司', '集团', '有限', '企业', '中心', '银行', '基金', '大学',
        '研究院', '医院', '合作社', '合伙', '厂', '局', '署', '会', '社',
        '机构', '协会', '办公室', '委员会')
_FOREIGN = ('limited', 'ltd', 'inc', 'corp', 'co.', 's.a.', 's.a', 'gmbh',
            'holdings', 'group', 'b.v.', 'nv', 'plc', 'llc', 'lp',
            '香港', '开曼', '開曼', '百慕大', '维尔京', 'bvi')

_CJK = re.compile(r'^[\u4e00-\u9fa5·]{2,6}$')


def _norm(v) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return ''
    return str(v).replace('\u3000', ' ').strip()


def load_negative(path: str = NEG) -> dict:
    """读「非国资法人名单」：名称->依据。

    与白名单对称：名称本身看不出国资/非国资的法人，两个方向都只能靠登记。
    差别在于默认值——未登记的一律进「待确认」，既不当国资放行，也不当
    民营否决。这个默认值是本模块最重要的一条设计：把无知的成本显性化，
    而不是让它伪装成一个确定结论。
    """
    if not os.path.exists(path):
        return {}
    d = pd.read_csv(path, encoding='utf-8-sig')
    if '主体名称' not in d.columns:
        return {}
    return {str(r['主体名称']).strip(): str(r.get('依据', '')).strip()
            for _, r in d.iterrows()}


def load_whitelist(path: str = WL) -> dict:
    """读国资主体白名单：{主体名称: 级别}。文件不存在时返回空表（不影响判定）。"""
    if not os.path.exists(path):
        return {}
    d = pd.read_csv(path, encoding='utf-8-sig')
    if '主体名称' not in d.columns:
        return {}
    return {str(r['主体名称']).strip(): str(r.get('级别', '地方')).strip()
            for _, r in d.iterrows()}


def classify_owner(actual_holder, wl: dict | None = None, neg: dict | None = None):
    """把实控人文本归类。返回 (owner_type, governance_ok, reason)。

    governance_ok: True 通过 / False 否决 / None 缺失或待确认。
    这是唯一一处判定逻辑，其它模块只调用不复制。
    """
    wl = load_whitelist() if wl is None else wl
    neg = load_negative() if neg is None else neg
    name = _norm(actual_holder)
    low = name.lower()

    if low in _NO:
        return '无实际控制人', True, '披露为无实际控制人'

    # 集体所有制先判：供销社体系不属于国资，也不属于民营，三态里只能是「待确认」。
    # 放在 _CENTRAL/_LOCAL 之前，否则「供销」以外的关键词仍可能把它拉回国资。
    if any(k in name for k in _COLLECTIVE):
        return '集体所有制（供销社体系）', None, \
            f'实控人含集体所有制标记「{_hit(name, _COLLECTIVE)}」（{name}）：' \
            f'供销合作社为集体所有制，非国资，需人工确认'

    if any(k in name for k in _CENTRAL):
        typ = '中央国有企业'
        return typ, True, f'实控人含国资标记「{_hit(name, _CENTRAL)}」'
    if any(k in name for k in _LOCAL):
        return '地方国有企业', True, f'实控人含地方国资标记「{_hit(name, _LOCAL)}」'

    if name in wl:
        lvl = wl[name]
        return ('中央国有企业' if lvl == '中央' else '地方国有企业'), True, \
            '命中国资主体白名单'

    # 外资 / 境外主体：拉丁字母或境外注册地标记
    if re.search(r'[A-Za-z]', name) or any(k in low for k in _FOREIGN):
        return '外资企业', False, f'实控人为境外主体（{name}）'

    if name in neg:
        return '民营（非国资法人）', False, f'命中非国资法人名单：{neg[name]}'

    # 自然人：短、纯汉字、不含任何机构后缀。支持「张三,李四」这种家族持股写法，
    # 逐段判断——「刘德铭,刘子斌」是两个人，不是一家机构。
    parts = [p.strip() for p in re.split(r'[,,、;;/|]', name) if p.strip()]
    if parts and all(_CJK.match(p) and not any(k in p for k in _ORG) for p in parts):
        return '个人或家族', False, \
            f'实控人为自然人（{name}），共 {len(parts)} 人'

    # 法人，但名称看不出国资、也未列入名录
    if any(k in name for k in _ORG):
        return '待确认（法人，未列名录）', None, \
            f'实控人为法人「{name}」，名称无国资线索、未进白名单/黑名单'

    return MISSING, None, f'实控人「{name}」无法归类，需人工确认'


def _hit(name: str, keys) -> str:
    return next((k for k in keys if k in name), keys[0])


def reclassify(df: pd.DataFrame) -> pd.DataFrame:
    """用**当前**判定规则重算缓存结论，只改结论、不动抓取到的 actual_holder。

    缓存里存的是「某次抓取 + 当时那版规则」的结果。规则一改（例如把「供销」
    从国资关键词摘出来、单列集体所有制），缓存照样返回旧结论，名单错了而
    报告看上去完全自洽——只有重跑一次全量抓取才会暴露，而抓取依赖外网。
    所以把「抓取」与「判定」拆开：只要 actual_holder 还在，就按现在的规则重判。
    """
    if not len(df) or 'actual_holder' not in df.columns:
        return df
    wl, neg = load_whitelist(), load_negative()
    out = df.copy()
    for i, r in out.iterrows():
        holder = _norm(r.get('actual_holder'))
        # 抓取失败的行没有实控人可判，保持原样（它本来就是「缺失」）
        if not holder or _norm(r.get('owner_type')) == MISSING:
            continue
        typ, ok, why = classify_owner(holder, wl, neg)
        out.at[i, 'owner_type'] = typ
        out.at[i, 'governance_ok'] = ok
        out.at[i, 'reason'] = why
    return out


def check_ownership(owner_type, owner_name, wl: dict | None = None):
    """三态判定：True 通过 / False 一票否决 / None 数据缺失（必须记异常）。

    优先用接口给的所有制性质；性质字段缺失时回落到按实控人名称判定。
    """
    t = _norm(owner_type)
    n = _norm(owner_name)
    if t in PASS_TYPES:
        return True
    if t in REJECT_TYPES:
        return False
    if not n and not t:
        return None
    return classify_owner(n, wl)[1]


# ---------------- 抓取 ----------------

def _sec(code: str) -> str:
    c = code.split('.')[0]
    return f'{c}.{"SH" if c[0] == "6" else "SZ"}'


def fetch_one(code: str, timeout: int = 15):
    """抓单只。返回 dict；失败抛异常由调用方计数。"""
    import requests
    from external_lake import configure_proxy
    configure_proxy()
    sec = _sec(code)
    r = requests.get(URL, params={
        'reportName': REPORT,
        'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR,ACTUAL_HOLDER',
        'filter': f'(SECUCODE="{sec}")',
        'client': 'PC', 'source': 'HSF10', 'pageNumber': 1, 'pageSize': 5,
    }, headers=UA, timeout=timeout)
    if r.status_code == 403:
        raise PermissionError('403 限流')
    r.raise_for_status()
    data = (r.json().get('result') or {}).get('data') or []
    if not data:
        raise LookupError(f'{sec} 返回 0 行')
    x = data[0]
    from external_lake import append_snapshot
    holder = _norm(x.get('ACTUAL_HOLDER'))
    if not holder:
        raise LookupError(f'{sec} 实控人字段缺失；保留旧记录')
    append_snapshot('actual_controller_disclosures', [{
        'SECUCODE': sec, 'SECURITY_NAME_ABBR': _norm(x.get('SECURITY_NAME_ABBR')),
        'ACTUAL_HOLDER': holder, 'source': f'eastmoney:{REPORT}',
        'source_published_at': None,
    }])
    return dict(code=sec, name=_norm(x.get('SECURITY_NAME_ABBR')),
                actual_holder=holder)


def fetch_many(codes, refresh: bool = False, cache: str = CACHE,
               exc_path: str = EXC, sleep=(0.6, 1.8), retries: int = 3,
               verbose: bool = True) -> pd.DataFrame:
    """定向批量抓取 + 缓存。命中缓存的不重复请求（refresh=True 时全量重抓）。"""
    old = pd.read_csv(cache, encoding='utf-8-sig') if os.path.exists(cache) else \
        pd.DataFrame(columns=COLS)
    today = datetime.now().strftime('%Y-%m-%d')
    cached = {} if refresh else {
        r['code']: r for _, r in old.iterrows()
        if str(r.get('fetched_at', ''))[:10] == today
    }

    wl = load_whitelist()
    rows, bad = [], []
    for i, c in enumerate(codes):
        code = _sec(c)
        if code in cached:
            rows.append(dict(cached[code]))
            continue
        got = None
        for k in range(retries):
            try:
                got = fetch_one(code)
                break
            except Exception as e:                       # noqa: BLE001
                wait = (2 ** k) * 1.5 + random.uniform(0, 0.8)
                if verbose:
                    print(f'  [retry {k + 1}/{retries}] {code} {type(e).__name__}: '
                          f'{str(e)[:50]} → 等 {wait:.1f}s')
                time.sleep(wait)
        if got is None:
            bad.append(dict(code=code, name='', issue='抓取失败',
                            detail=f'{retries} 次重试均失败', fetched_at=today))
            existing = old[old.code.astype(str) == code] if len(old) else old
            if len(existing) and str(existing.iloc[-1].get('actual_holder', '')).strip():
                rows.append(existing.iloc[-1].to_dict())
            else:
                rows.append(dict(code=code, name='', actual_holder='',
                                 owner_type=MISSING, governance_ok=None,
                                 reason='抓取失败，数据缺失', source='eastmoney',
                                 fetched_at=today))
            continue

        typ, ok, why = classify_owner(got['actual_holder'], wl)
        if ok is None:
            bad.append(dict(code=code, name=got['name'],
                            issue='无法判定' if got['actual_holder'] else '字段缺失',
                            detail=why, fetched_at=today))
        rows.append(dict(code=code, name=got['name'],
                         actual_holder=got['actual_holder'],
                         owner_type=typ, governance_ok=ok, reason=why,
                         source='eastmoney:F10', fetched_at=today))
        if verbose:
            flag = {True: 'PASS', False: 'REJECT', None: 'MISSING'}[ok]
            print(f'  {flag:<7} {code} {got["name"]:<6} {got["actual_holder"] or "—":<24} {why}')
        if i < len(codes) - 1:
            time.sleep(random.uniform(*sleep))           # 随机延时，避免被限流

    df = pd.DataFrame(rows, columns=COLS)
    # 合并写回：本次未涉及的代码保留旧记录，否则逐批抓取会互相覆盖缓存。
    # （第一次实现是直接整表覆盖，第二批一跑，第一批的结论就没了。）
    if len(old):
        keep = old[~old.code.isin(set(df.code))]
        df = pd.concat([keep[COLS], df], ignore_index=True)
    df = df.drop_duplicates('code', keep='last').sort_values('code').reset_index(drop=True)
    df = reclassify(df)        # 命中当日缓存的行也要按当前规则重判
    df.to_csv(cache, index=False, encoding='utf-8-sig')
    save_to_lake(df)          # 每抓一次就回写一次，湖里的结论不会落后于 CSV
    pd.DataFrame(bad, columns=['code', 'name', 'issue', 'detail', 'fetched_at']) \
        .to_csv(exc_path, index=False, encoding='utf-8-sig')
    if bad:
        print(f'  异常 {len(bad)} 条 → {exc_path}（缺失不视为通过）')
    return df


def load_cached(cache: str = CACHE) -> pd.DataFrame:
    """读核查结论：湖优先，CSV 只在湖里没有时兜底。

    顺序不能反。CSV 是「这台机器上某次跑到哪儿了」的本地痕迹，湖才是
    全市场共享的结论；按 CSV 优先，换台机器就会得出「没有缓存」然后重抓，
    同一家公司被两套抓取时点判两次。
    """
    lake = load_from_lake()
    if len(lake):
        if os.path.exists(cache):
            csv = pd.read_csv(cache, encoding='utf-8-sig')
            # CSV 里更新的那几行（fetched_at 更晚）覆盖湖内同代码结论
            if len(csv):
                newer = csv[~csv.code.isin(lake.code) | (
                    csv.fetched_at.astype(str) > csv.code.map(
                        lake.set_index('code').fetched_at.astype(str)).fillna(''))]
                if len(newer):
                    lake = pd.concat([lake[~lake.code.isin(newer.code)], newer],
                                     ignore_index=True)
        # 无论结论来自湖还是 CSV，都按当前规则重判一遍：规则版本不属于数据，
        # 它属于代码，读的时候才应该被应用。
        return reclassify(lake).reset_index(drop=True)
    if not os.path.exists(cache):
        return pd.DataFrame(columns=COLS)
    return reclassify(pd.read_csv(cache, encoding='utf-8-sig'))


def load_from_lake() -> pd.DataFrame:
    """读湖内 derived/ownership_check。没有就返回空表，不抛异常。"""
    if not os.path.exists(LAKE_PART):
        return pd.DataFrame(columns=COLS)
    try:
        d = pd.read_parquet(LAKE_PART)
    except Exception as exc:                                   # noqa: BLE001
        print(f'  [WARN] 湖内实控人数据损坏（{type(exc).__name__}），回退 CSV')
        return pd.DataFrame(columns=COLS)
    return d


def save_to_lake(df: pd.DataFrame) -> int:
    """把核查结论写回湖内 derived/ownership_check（整表覆盖 + 元数据）。"""
    if not len(df):
        return 0
    os.makedirs(LAKE_DERIVED, exist_ok=True)
    d = df.reindex(columns=COLS).drop_duplicates('code', keep='last').sort_values('code')
    import uuid
    tmp = LAKE_PART + '.' + uuid.uuid4().hex + '.tmp'
    try:
        d.to_parquet(tmp, index=False)
        os.replace(tmp, LAKE_PART)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    from external_lake import snapshot_governance
    snapshot_governance(d)
    ok = int((d.governance_ok == True).sum())                  # noqa: E712
    meta = {
        'dataset': 'ownership_check',
        'layer': 'derived',
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'rows': int(len(d)),
        'columns': COLS,
        'source': 'eastmoney:F10 RPT_F10_BASIC_ORGINFO（实控人字段）',
        'decision_basis': '国资主体白名单.csv / 非国资法人名单.csv + 名称标记',
        'governance_ok_true': ok,
        'governance_ok_false': int((d.governance_ok == False).sum()),  # noqa: E712
        'governance_ok_null': int(d.governance_ok.isna().sum()),
        'fetched_at_range': [str(d.fetched_at.min()), str(d.fetched_at.max())],
        'note': ('未注册为 cne 数据集（数据集清单在包内，外部无法追加）；'
                 '用 duckdb read_parquet 直接查，或 ownership.load_from_lake()。'),
    }
    with open(LAKE_META, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f'  已回写湖内 {LAKE_PART}（{len(d)} 行，通过 {ok}）')
    return len(d)


def gate(df: pd.DataFrame) -> dict:
    """汇总成 {code: (ok, owner_type, reason)}，供准入/复核快速取用。"""
    return {r['code']: (r['governance_ok'] if r['governance_ok'] in (True, False) else None,
                        r['owner_type'], r['reason'], r['actual_holder'])
            for _, r in df.iterrows()}


def main():
    ap = argparse.ArgumentParser(description='实控人性质定向抓取与判定')
    ap.add_argument('--codes', default='', help='逗号分隔，如 000651,000895')
    ap.add_argument('--from-csv', default='', help='从 CSV 的 symbol 列取代码')
    ap.add_argument('--refresh', action='store_true', help='忽略当日缓存重抓')
    ap.add_argument('--to-lake', action='store_true',
                    help='把现有 CSV 全量回写湖内 derived/ownership_check（不联网）')
    args = ap.parse_args()

    if args.to_lake:
        d = load_cached(CACHE)
        if not len(d):
            sys.exit(f'{CACHE} 不存在或为空，无可回写')
        n = save_to_lake(d)
        print(f'回写完成：{n} 行 → {LAKE_PART}')
        return 0

    codes = [c for c in args.codes.split(',') if c.strip()]
    if args.from_csv:
        d = pd.read_csv(args.from_csv, encoding='utf-8-sig')
        col = 'symbol' if 'symbol' in d.columns else ('sym' if 'sym' in d.columns else 'code')
        codes += [str(x) for x in d[col].tolist()]
    codes = list(dict.fromkeys(codes))
    if not codes:
        sys.exit('需给 --codes 或 --from-csv')

    print(f'核查 {len(codes)} 只 · 源 东财 F10 ACTUAL_HOLDER')
    df = fetch_many(codes, refresh=args.refresh)
    n_ok = int((df.governance_ok == True).sum())          # noqa: E712
    n_no = int((df.governance_ok == False).sum())         # noqa: E712
    n_ms = int(df.governance_ok.isna().sum())
    print(f'\n通过 {n_ok} / 否决 {n_no} / 缺失 {n_ms} → {CACHE}')
    return 0 if n_ms == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())

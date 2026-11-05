# -*- coding: utf-8 -*-
"""银行专项门槛（全市场统一，沪深同一标准）。

为什么要单独一个模块：
  沪深两市筛选主流程都只做「7 条 / 11 条」财务漏斗，银行在其中靠「豁免」
  绕过资产负债率(④)和支付率区间(⑤)。豁免一条就必须补一条更专业的——
  否则等于对银行放水。银行的风险不在负债率（存款是经营原料），而在资本充足。

统一门槛（沪深一致，不做市场例外）：
  核心一级资本充足率 HXYJBCZL  >= 9.5%
  不良贷款率       NONPERLOAN <= 1.5%
  拨备覆盖率       BLDKBBL    >= 180%

口径：取 bank_risk_metrics 每只最新报告期；非银行标的直接视为通过。
数据缺失的银行：不静默通过（缺数据≠达标），标记为不通过并在调用方记录原因。
"""
from __future__ import annotations
import duckdb

HX_MIN = 9.5      # 核心一级资本充足率下限 %
NPL_MAX = 1.5     # 不良贷款率上限 %
PROV_MIN = 180.0  # 拨备覆盖率下限 %
_LAKE = 'D:/repository/数据湖/data/cnequity/duckdb/cnequity.duckdb'


def load_bank_metrics(lake: str = _LAKE) -> 'object':
    """返回 index=SECUCODE 的最新报告期银行指标 DataFrame（HXYJBCZL/NONPERLOAN/BLDKBBL/REPORT_DATE_NAME）。"""
    con = duckdb.connect(lake, read_only=True)
    try:
        df = con.execute("""
          select SECUCODE, REPORT_DATE_NAME, HXYJBCZL, NONPERLOAN, BLDKBBL
          from bank_risk_metrics
          qualify row_number() over (partition by SECUCODE order by REPORT_DATE desc) = 1
        """).df().set_index('SECUCODE')
    finally:
        con.close()
    return df


def bank_gate(symbols, is_bank, metrics=None):
    """对给定 symbols 判定银行专项。

    symbols: iterable of symbol（如 '600919.SH'）
    is_bank: dict/Series: symbol -> bool（是否银行；由调用方用名称/行业识别）
    metrics: load_bank_metrics() 结果，可复用避免重复读
    返回 dict: symbol -> (ok: bool|None, reason: str)
      非银行 -> (True, '非银行')
      银行达标 -> (True, '核心X/不良Y/拨备Z 达标')
      银行不达标 -> (False, '...不达标')
      银行无数据 -> (None, '缺银行指标，不通过')
    """
    if metrics is None:
        metrics = load_bank_metrics()
    out = {}
    for s in symbols:
        if not bool(is_bank.get(s, False)):
            out[s] = (True, '非银行')
            continue
        if s not in metrics.index:
            out[s] = (None, '缺银行指标，不通过')
            continue
        r = metrics.loc[s]
        ok = bool(r.HXYJBCZL >= HX_MIN and r.NONPERLOAN <= NPL_MAX and r.BLDKBBL >= PROV_MIN)
        reason = f"核心一级{r.HXYJBCZL:.2f}%/不良{r.NONPERLOAN:.2f}%/拨备{r.BLDKBBL:.0f}%"
        out[s] = (ok, reason + (' 达标' if ok else ' → 不达标'))
    return out


if __name__ == '__main__':
    m = load_bank_metrics()
    print('银行专项门槛：核心一级>=%.1f%% 不良<=%.1f%% 拨备>=%.0f%%' % (HX_MIN, NPL_MAX, PROV_MIN))
    print('库内银行数：', len(m))

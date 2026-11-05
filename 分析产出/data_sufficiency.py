# -*- coding: utf-8 -*-
"""数据充足性前置检查：样本不足就不给数值，而不是给一个带缺陷的数字。

用法（在筛选脚本里）：

    from data_sufficiency import Sufficiency
    suf = Sufficiency(con, base)

    R = rebuilt_returns(...)                 # 原有复权收益
    ok = suf.check_returns(syms, R)           # 返回 bool Series
    idx['vol'] = idx['vol'].where(ok)         # 不足 → NaN
    idx['vol_reason'] = suf.reasons_for(idx.index)   # 缺失原因，进输出列

为什么单独一个模块：三个脚本（screen_sh / screen_sz / annual_review）都要
用同一套口径。写在其中一个里，另外两个迟早会不一致——而"样本够不够"
这种判定一旦不一致，比没有判定更糟：同一个标的会在一张表里入选、
在另一张表里落选，且没人知道为什么。
"""
from __future__ import annotations
import pandas as pd

#: 复权收益最少观测数。低于此值，std 与年化都是噪声。
MIN_RETURN_OBS = 200
#: 波动窗口（交易日）。
VOL_WINDOW = 252
#: 流动性窗口（交易日）。
AMT_WINDOW = 63
#: 流动性窗口内最少有成交的交易日。
MIN_AMT_DAYS = 60
#: 单只标的在窗口内至少要有这些行情天数，否则波动/回撤不参与判定。
MIN_BAR_DAYS = VOL_WINDOW


class Sufficiency:
    """按标的判定窗口内数据是否足以支撑指标计算。"""

    def __init__(self, con, base: str, *, vol_win: int = VOL_WINDOW):
        self.con = con
        self.base = str(base)[:10]
        self.vol_win = vol_win
        self._bar_counts = None
        self._amt_counts = None

    # ---------- 行情充足性 ----------
    def _load_bar_counts(self) -> pd.Series:
        if self._bar_counts is None:
            days = int(self.vol_win * 1.7)
            rows = self.con.execute(f"""
                select symbol, count(*) n
                from src.daily_bars
                where trade_date <= DATE '{self.base}'
                  and trade_date >= DATE '{self.base}' - INTERVAL {days} DAY
                group by 1
            """).df()
            self._bar_counts = rows.set_index('symbol').n
        return self._bar_counts

    def check_returns(self, syms, returns: pd.DataFrame) -> pd.Series:
        """复权收益观测数达标，且窗口内行情天数也达标。

        两个条件都要：观测数可以由 ffill 撑起来，但 ffill 补不出真实价格，
        一个停牌 200 天的标的照样能凑出 200 个非空值。所以行情天数必须单独查。
        """
        counts = self._load_bar_counts()
        obs = returns.notna().sum()
        enough_obs = obs.reindex(syms).fillna(0) >= MIN_RETURN_OBS
        enough_bars = counts.reindex(syms).fillna(0) >= MIN_BAR_DAYS
        return enough_obs & enough_bars

    def check_amount(self, syms, frame: pd.DataFrame | None = None,
                     col: str = 'amt63') -> pd.Series:
        """流动性：至少 MIN_AMT_DAYS 个交易日有成交，且均值可算。"""
        if self._amt_counts is None:
            rows = self.con.execute(f"""
                select symbol, count(*) n
                from (
                    select symbol, trade_date,
                           row_number() over (partition by symbol order by trade_date desc) rk
                    from src.daily_bars
                    where trade_date <= DATE '{self.base}' and amount is not null and amount > 0
                ) where rk <= {AMT_WINDOW} group by 1
            """).df()
            self._amt_counts = rows.set_index('symbol').n
        enough = self._amt_counts.reindex(syms).fillna(0) >= MIN_AMT_DAYS
        if frame is not None and col in frame.columns:
            enough = enough & frame[col].reindex(syms).notna()
        return enough

    # ---------- 财务充足性 ----------
    @staticmethod
    def check_financial(frame: pd.DataFrame, items: list[str]) -> pd.Series:
        """财报科目：任一年缺失即整项不成立，不用其余年份冒充三年。

        这与脚本里既有的 skipna=False 一致；这里把它集中，让"缺一年"
        在每个脚本里都是同一种处理，而不是各写一遍 where。
        """
        if frame is None or not len(frame):
            return pd.Series(dtype=bool)
        if isinstance(frame.columns, pd.MultiIndex):
            cols = [c for c in frame.columns if c[0] in items]
            if not cols:
                return pd.Series(False, index=frame.index)
            return frame[cols].notna().all(axis=1)
        return frame.notna().all(axis=1)

    # ---------- 分红充足性 ----------
    @staticmethod
    def check_dividend_years(n_years: pd.Series, need: int = 3) -> pd.Series:
        return n_years.fillna(0) >= need

    # ---------- 原因 ----------
    def reasons_for(self, syms) -> dict:
        """逐标的中文化原因，供报告直接引用。"""
        counts = self._load_bar_counts()
        amt = self._amt_counts if self._amt_counts is not None else pd.Series(dtype=int)
        out = {}
        for s in syms:
            b = int(counts.get(s, 0))
            a = int(amt.get(s, 0))
            why = []
            if b < MIN_BAR_DAYS:
                why.append(f'窗口内仅{b}个交易日行情（需≥{MIN_BAR_DAYS}）')
            if a < MIN_AMT_DAYS:
                why.append(f'近{AMT_WINDOW}日仅{a}日有成交（需≥{MIN_AMT_DAYS}）')
            if why:
                out[s] = '；'.join(why)
        return out

    def report(self, syms) -> pd.DataFrame:
        """供人读的充足性明细。"""
        counts = self._load_bar_counts()
        amt = self._amt_counts if self._amt_counts is not None else pd.Series(dtype=int)
        return pd.DataFrame({
            'symbol': list(syms),
            'window_bar_days': [int(counts.get(s, 0)) for s in syms],
            'amt_days': [int(amt.get(s, 0)) for s in syms],
            'reason': [self.reasons_for([s]).get(s, '') for s in syms],
        })

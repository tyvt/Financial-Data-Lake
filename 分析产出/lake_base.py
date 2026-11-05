# -*- coding: utf-8 -*-
"""数据湖基准日选择：唯一实现。

数据湖的 max(trade_date) 不保证是完整交易日。2026-10-16 是一次真实案例：
该日全市场平均成交额 863.8 万，仅为此前 20 日中位数（1,913 万）的 45%；
粤高速A 单只成交量 3.63 万手（平日 1,379 万手），收盘价却跳涨 7.1%。
这是盘中/残缺快照被写库，不是行情。

若拿它当基准日，会同时污染三个口径，而且错得「看起来很保守」，不会有人怀疑：
  · 波动率：粤高速A 23.50% → 24.51%（多了一个假的 7% 跳空）
  · 股息率：4.45% → 4.15%（分母用了错的价）
  · 回撤：假跳涨进入净值序列，最大回撤被削平

判据用成交额而非交易日历：日历只知道哪天开市，不知道数据是否收全。
"""
import pandas as pd


def pick_base(con, ratio=0.80, lookback=20, verbose=True, table='src.daily_bars'):
    """返回最近一个「成交额达标」的交易日（str）。

    某日全市场平均个股成交额低于前 lookback 日中位数的 ratio 倍，判为不完整，
    继续回退。没有两天连续残缺的正常情况下，退一天就够。
    """
    days = con.execute(f"""
        select trade_date, avg(amount) amt from {table}
        where amount is not null and amount > 0
        group by 1 order by 1 desc limit ?
    """, [lookback + 5]).df()
    if not len(days):
        raise RuntimeError('数据湖无可用交易日')
    days = days.sort_values('trade_date').reset_index(drop=True)
    for i in range(len(days) - 1, 0, -1):
        full = days.amt.iloc[max(0, i - lookback):i]
        med = float(full.median())
        if med > 0 and days.amt.iloc[i] >= ratio * med:
            if i < len(days) - 1 and verbose:
                print(f'[基准日校正] {str(days.trade_date.iloc[-1])[:10]} 全市场个股平均成交额 '
                      f'仅为前 {len(full)} 日中位数 {med/1e4:.0f} 万的 '
                      f'{days.amt.iloc[i]/med*100:.0f}%，判为不完整快照，'
                      f'基准日回退至 {str(days.trade_date.iloc[i])[:10]}')
            return str(days.trade_date.iloc[i])
    return str(days.trade_date.iloc[-1])

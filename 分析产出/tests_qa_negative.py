# -*- coding: utf-8 -*-
"""
校验器自身的负向测试

为什么需要这个文件
------------------
校验机制的失效方式不是「报错」，而是「静默通过」——它看起来在拦，
实际什么都没拦。本轮就真实发生过：第一次做负向测试时拿修正后的值
（本来就合计 100）去测，结果当然 PASS，差点误判「机制失效」。反过来
也可能拿一组必然失败的数据测出 PASS 却以为正常。

所以每个检查项都必须有一组「已知会失败」的输入，确认它真的会 FAIL；
同时有一组「已知会通过」的输入，确认它没有过度敏感。两边都测，才算
验证过。

覆盖
  A. qa_checks 的硬断言（出表闸门）
  B. qa_checks 的分组统计（观察池与入选资格不计入需处理）
  C. annual_review 的专项触发线（双汇单年可容忍 / 连续两年移出；
     云南白药按「最新年度」判定，历史触发只写进明细、不降级）
  D. annual_review 的持仓判定边界（入选门槛上下沿）
  E. 分组统计：只有「卖出触发」计入需处理
  F. 卖出触发线边界（银行缓冲带、拨备+不良双条件、连续两年、波动红线）
  G. consec 自身：缺失值不得拼成「连续两年」
  H. 集中度上限：单只 25%、行业 35% 的上下沿
  I. 黑天鹅：登记即触发、状态留空视为有效
  J. 替补资格：任一项失效即不可递补
  O. 支付率门禁（含 100.00% 边界、缺失值）
  P. 治理三态：供销社体系归集体所有制（待确认），不误伤真国资

用法：python tests_qa_negative.py   全通过退出码 0，否则 1
"""
import sys

from qa_checks import (weights_sum_100, conservation, single_source, weight_cap,
                       residue_free, absent, audit, audit_report, AUDIT, pay_gate)
import annual_review as ar
import ownership as gov

RESULTS = []


def expect(name, should_fail, fn, *a, **kw):
    """执行一个检查，断言它按预期失败/通过。"""
    try:
        fn(*a, **kw)
        got = False
        err = ''
    except AssertionError as e:
        got = True
        err = str(e)
    ok = (got == should_fail)
    RESULTS.append((name, ok, '预期' + ('FAIL' if should_fail else 'PASS')
                    + '，实际' + ('FAIL' if got else 'PASS')
                    + (f'（{err[:60]}）' if err else '')))
    print(f"  {'✓' if ok else '✗'} {name}  "
          f"{'预期FAIL' if should_fail else '预期PASS'}"
          f"→{'FAIL' if got else 'PASS'}")
    return ok


print('=== A. 硬断言：权重合计 ===')
# 旧版真实缺陷：wm 组内提前 round(1) → 沪市合计 99.9
expect('旧版沪市 wm 合计 99.9 被拦截', True,
       weights_sum_100, [23.8, 22.7, 19.7, 18.9, 14.8], '沪市(旧版)')
expect('旧版 60/40 组合 99.94 被拦截', True,
       weights_sum_100, [round(x * 0.6, 2) for x in [23.8, 22.7, 19.7, 18.9, 14.8]]
       + [19.20, 10.00, 10.80], '组合(旧版)')
expect('当前 60/40 正确表 100.00 通过', False,
       weights_sum_100, [14.28, 13.65, 11.83, 11.36, 8.88, 19.20, 10.80, 10.00], '60/40')
expect('多 0.01pct 被拦截', True,
       weights_sum_100, [50.0, 50.01], '测试')

print('\n=== B. 硬断言：其余闸门 ===')
expect('权重不守恒被拦截', True,
       conservation, [1.62, 1.08, 0, 0, 0, 2.70, -5.40, 0.14], '降级方案')
expect('权重守恒通过', False,
       conservation, [1.62, 1.08, 0, 0, 0, 2.70, -5.40, 0], '降级方案')
expect('手写变异系数 25.6 vs 重算 23.1 被拦截', True,
       single_source, 25.6, 23.1, '云南白药CV', tol=1e-4)
expect('变异系数一致通过', False,
       single_source, 23.1, 23.1, '云南白药CV')
expect('模板残留被拦截', True,
       residue_free, '<div>{met.loc[0,"x"]}</div>', 'HTML')
expect('无残留通过', False,
       residue_free, '<div>5.11%</div>', 'HTML')
expect('过期硬编码被拦截', True,
       absent, '变异系数 25.6%，股息率 5.30%', ['25.6%', '5.30%'], 'HTML')
expect('无过期硬编码通过', False,
       absent, '变异系数 23.1%', ['25.6%', '5.30%'], 'HTML')

print('\n=== C. 深市分红触发线（单年可容忍 / 连续两年移出）===')
cases = [
    ('真实数据 [0.67,1.72,1.46] 不触发', [99.4, 97.9, 98.4], [0.67, 1.72, 1.46], (False, False)),
    ('支付率 101% 触发降级', [98.0, 101.0, 99.0], [1.5, 1.5, 1.5], (True, False)),
    ('支付率恰好 100% 不触发（>100 才算超）', [98.0, 100.0, 99.0], [1.5, 1.5, 1.5], (False, False)),
    ('OCF 连续两年 0.8 触发移出', [98.0, 99.0, 99.0], [0.8, 0.9, 1.2], (False, True)),
    ('OCF 隔年低于 1 不触发（非连续）', [98.0, 99.0, 99.0], [0.8, 1.2, 0.9], (False, False)),
    ('OCF 两年 0.8 但中间隔一年正常', [98.0] * 4, [0.8, 1.2, 0.8, 1.5], (False, False)),
]
for nm, ps, os_, want in cases:
    got = ar.sw_triggers(ps, os_)
    ok = got == want
    RESULTS.append((f'深市分红线 {nm}', ok, f'预期{want} 实际{got}'))
    print(f"  {'✓' if ok else '✗'} {nm}  预期{want} 实际{got}")

print('\n=== D. 持仓判定边界 ===')
BASE = dict(name='测试标的', market='沪', years_ok=True, pay_series=[1, 2, 3],
            pay_max=50.0, ocfd3=2.0, vol=20.0, vol_h=20.0, mdd=-10.0, amt_wan=10000.0)


def judge(**kw):
    r = dict(BASE); r.update(kw)
    return {x[0]: x[1] for x in ar.judge_holding(r)}


def one(r, key):
    return [v for k, v in r.items() if key in k][0]


def _assert(m, key, want):
    got = [v for k, v in m.items() if key in k][0]
    if got != want:
        raise AssertionError(f'{key} 判定 {got}，预期 {want}')


print('  波动率阈值 25%：')
expect('24.99% 通过', False, lambda: _assert(judge(vol=24.99), '年化波动', True))
expect('25.00% 失败（不含等于）', True, lambda: _assert(judge(vol=25.00), '年化波动', True))
expect('25.01% 失败', True, lambda: _assert(judge(vol=25.01), '年化波动', True))


print('  支付率上限（沪 80% / 深 100%）：')
expect('沪 79.9% 通过', False, lambda: _assert(judge(pay_max=79.9), '支付率', True))
expect('沪 80.0% 失败（30–80 含上界应下降）', True, lambda: _assert(judge(pay_max=80.0), '支付率', True))
expect('深 99.9% 通过', False, lambda: _assert(judge(market='深', pay_max=99.9), '支付率', True))
expect('深 100.1% 失败', True, lambda: _assert(judge(market='深', pay_max=100.1), '支付率', True))

print('  流动性与现金流：')
expect('成交额 5000 万整通过', False, lambda: _assert(judge(amt_wan=5000.0), '日均成交', True))
expect('成交额 4999 万失败', True, lambda: _assert(judge(amt_wan=4999.0), '日均成交', True))
expect('OCF/分红 1.01 通过', False, lambda: _assert(judge(ocfd3=1.01), 'OCF', True))
expect('OCF/分红 1.00 失败（需 >1）', True, lambda: _assert(judge(ocfd3=1.00), 'OCF', True))
expect('缺一年分红数据失败', True, lambda: _assert(judge(years_ok=False), '连续三年', True))

print('\n=== E. 分组统计：只有卖出触发计入「需处理」 ===')
AUDIT.clear()
audit('卖出项A 正常', True, group='卖出触发')
audit('卖出项B 触发', False, 'x', '动作', group='卖出触发')
audit('资格项C 偏离', False, 'y', '', group='入选资格')
audit('观察池未达标', False, 'z', '', group='观察池')
_n, _txt = audit_report('_test_audit.txt', '测试报告')
_reopen = open('_test_audit.txt', encoding='utf-8').read()
import os
os.remove('_test_audit.txt')
ok = (_n == 1 and '需处理 1 项' in _reopen and '卖出触发 1' in _reopen
      and '入选资格偏离 1 项' in _reopen and '观察池：0/1' in _reopen)
RESULTS.append(('分组统计（卖出1/资格1/观察0）', ok, f'需处理 {_n} 项'))
print(f"  {'✓' if ok else '✗'} 卖出触发 {_n} 项计入需处理（预期 1）；资格偏离与观察池不计入")

AUDIT.clear()
audit('资格项偏而不卖', False, 'w', '', group='入选资格')
_n2, _ = audit_report('_test_audit2.txt', '测试报告')
_t2 = open('_test_audit2.txt', encoding='utf-8').read()
os.remove('_test_audit2.txt')
ok2 = (_n2 == 0) and ('无卖出触发' in _t2)
RESULTS.append(('资格偏离不产生卖出触发', ok2, f'需处理 {_n2} 项'))
print(f"  {'✓' if ok2 else '✗'} 仅资格偏离时 → 需处理 0 项、提示「无卖出触发」")

print('\n=== F. 卖出触发线边界 ===')


def same(name, got, want):
    """断言级别判定符合预期（直接记结果，语义比 expect 的「预期失败」清晰）。"""
    ok = (got == want)
    RESULTS.append((name, ok, f'预期{want} 实际{got}'))
    print(f"  {'✓' if ok else '✗'} {name}  预期{want} 实际{got}")
    return ok


def exit_of(**kw):
    """构造一只标的并返回 {检查项: 级别}。"""
    bank = kw.pop('bank', None)
    r = dict(name='测试标的', ind='', pay_series=[50.0, 50.0, 50.0],
             ocfd_series=[2.0, 2.0, 2.0], dps_series=[1.0, 1.0, 1.0], vol=20.0)
    r.update(kw)
    return {x[0]: x[1] for x in ar.judge_exit(r, bank)}


def lv(m, key):
    return [v for k, v in m.items() if key in k][0]


print('  银行：核心一级（入选 9.5 / 卖出 9.0）')
for c, want, lab in [(9.60, 'OK', '9.60 正常'), (9.50, 'OK', '9.50 恰在入选线'),
                     (9.30, 'TRIM', '9.30 缓冲带降权'), (9.00, 'TRIM', '9.00 恰在卖出线不清仓'),
                     (8.99, 'EXIT', '8.99 跌破卖出线')]:
    m = exit_of(ind='银行', bank=dict(cet1=[c], npl=[0.90, 0.95], cover=[300.0]))
    same(f'核心一级 {lab}', lv(m, '核心一级'), want)

print('  银行：拨备+不良 必须双条件才清仓')
for lab, cov, npl, want in [
        ('拨备 179 且不良上升', [170.0], [1.20, 1.00], 'EXIT'),
        ('拨备 179 但不良下降', [170.0], [1.00, 1.20], 'TRIM'),
        ('拨备 181 不良上升未超限', [181.0], [1.20, 1.00], 'OK'),
        ('拨备 181 不良上升且超 1.5', [181.0], [1.60, 1.40], 'TRIM'),
        ('拨备 300 不良上升未超限', [300.0], [1.20, 1.00], 'OK')]:
    m = exit_of(ind='银行', bank=dict(cet1=[10.0], npl=npl, cover=cov))
    same(lab, lv(m, '拨备'), want)

print('  公用/交运：连续两年才触发')
for ps, want, lab in [([79.0, 81.0, 82.0], 'EXIT', '后两年 81/82 连续'),
                      ([81.0, 79.0, 82.0], 'OK', '81/79/82 不连续不触发'),
                      ([80.0, 80.0, 80.0], 'OK', '恰好 80 不算超'),
                      ([81.0, 81.0, 50.0], 'EXIT', '前两年连续也算')]:
    same(f'支付率 {lab}', lv(exit_of(ind='交运', pay_series=ps), '支付率未连续两年'), want)

print('  深市三只：各自触发线')
same('格力 76/77 连续超 75 → 降权',
     lv(exit_of(name='格力电器', ind='家电', pay_series=[76.0, 77.0, 50.0]), '支付率'), 'TRIM')
same('格力 76/74 不连续 → 不触发',
     lv(exit_of(name='格力电器', ind='家电', pay_series=[76.0, 74.0, 50.0]), '支付率'), 'OK')
same('云南白药 分红 2.00→1.35（-32.5%）→ 降权',
     lv(exit_of(name='云南白药', ind='医药', dps_series=[2.0, 2.0, 1.35]), '每股分红'), 'TRIM')
same('云南白药 分红 2.00→1.45（-27.5%）→ 不触发',
     lv(exit_of(name='云南白药', ind='医药', dps_series=[2.0, 2.0, 1.45]), '每股分红'), 'OK')
same('云南白药 OCF 0.9 且支付率 81 → 移出',
     lv(exit_of(name='云南白药', ind='医药', ocfd_series=[2.0, 2.0, 0.9],
                pay_series=[50.0, 60.0, 81.0]), '同时恶化'), 'EXIT')

print('  云南白药：口径 = 最新年度，历史触发只提示、不降级')
# 三个真实年度数据：2023 同时满足（0.95 / 90.5%），2024 分红砍 43% 自我修正，
# 2025 修复。锁定四件事——不降级、写明触发年份、写明修正幅度、规则没被放宽。
_gc = ar.judge_exit(dict(name='云南白药', ind='医药', vol=13.78,
                         pay_series=[90.5, 44.5, 54.8],
                         ocfd_series=[0.95, 1.90, 1.63],
                         dps_series=[2.077, 1.185, 1.583],
                         years=[2023, 2024, 2025]))
_c = [x for x in _gc if '同时恶化' in x[0]][0]
same('2023 曾触发、2025 已修复 → 不降级', _c[1], 'OK')
same('明细写明触发年份', '2023 年曾同时满足' in _c[2], True)
same('明细写明修正幅度', '2024 年分红 −43% 已自我修正' in _c[2], True)
same('明细声明当前不触发', '当前不触发' in _c[2], True)
_gc2 = ar.judge_exit(dict(name='云南白药', ind='医药', vol=13.78,
                          pay_series=[50.0, 60.0, 91.0],
                          ocfd_series=[2.0, 2.0, 0.9],
                          dps_series=[1.5, 1.5, 1.5], years=[2023, 2024, 2025]))
same('最新年度再次同时满足 → 仍 EXIT（规则未被放宽）',
     [x for x in _gc2 if '同时恶化' in x[0]][0][1], 'EXIT')

# 2026-10-19 双汇清仓后，这套规则由粤高速A 继承。断言不变、主体换人——
# 规则是随标的走的，不是随标的名写死的。
same('粤高速A 支付率 100.0 不触发（需 >100）',
     lv(exit_of(name='粤高速A', ind='交运', pay_series=[99.0, 100.0, 98.0]), '支付率'), 'OK')
same('粤高速A 支付率 100.1 → 权重减半',
     lv(exit_of(name='粤高速A', ind='交运', pay_series=[99.0, 100.1, 98.0]), '支付率'), 'TRIM')
same('粤高速A OCF 连续两年 0.9 → 移出',
     lv(exit_of(name='粤高速A', ind='交运', ocfd_series=[0.9, 0.9, 1.5]), 'OCF'), 'EXIT')
same('粤高速A 每股分红 −31% → 分红被砍，触发复核',
     lv(exit_of(name='粤高速A', ind='交运', pay_series=[70.0, 70.0, 70.0],
                ocfd_series=[3.3, 3.0, 2.8], dps_series=[0.60, 0.60, 0.41]), '每股分红'), 'TRIM')
same('粤高速A 净利口径：分红 −29% 不触发（阈值 30%）',
     lv(exit_of(name='粤高速A', ind='交运', pay_series=[70.0, 70.0, 70.0],
                ocfd_series=[3.3, 3.0, 2.8], dps_series=[0.60, 0.60, 0.426]), '每股分红'), 'OK')

print('  全类共用：波动率红线对分红好的标的同样生效')
same('波动 24.99 不触发', lv(exit_of(vol=24.99), '年化波动'), 'OK')
same('波动 25.00 触发移出（不含等于）', lv(exit_of(vol=25.00), '年化波动'), 'EXIT')
same('波动 25.01 触发移出', lv(exit_of(vol=25.01), '年化波动'), 'EXIT')
same('银行波动超标也移出（不因银行身份豁免）',
     lv(exit_of(ind='银行', vol=26.0, bank=dict(cet1=[12.0], npl=[0.9, 0.9], cover=[300.0])),
        '年化波动'), 'EXIT')
same('银行指标缺失 → SKIP 而非 PASS（不静默通过）',
     lv(exit_of(ind='银行', bank=None), '核心一级'), 'SKIP')

print('\n=== G. consec 自身：窗口与缺失值 ===')
same('consec 连续两个 True → 触发', ar.consec([1, 1, 0], lambda v: v == 1), True)
same('consec 中间缺失不拼接成连续', ar.consec([1, None, 1], lambda v: v == 1), False)
same('consec 长度不足 2 不触发', ar.consec([1], lambda v: v == 1), False)


print('\n=== H. 集中度上限 ===')
expect('单只 24.99 通过', False, weight_cap, [19.20, 24.99], 25.0, '单只')
expect('单只 25.00 恰在上限通过（≤ 含等号）', False, weight_cap, [25.00, 10.0], 25.0, '单只')
expect('单只 25.01 被拦截', True, weight_cap, [25.01, 10.0], 25.0, '单只')
expect('行业 35.00 恰在上限通过', False, weight_cap, [25.48, 35.00], 35.0, '行业')
expect('行业 35.01 被拦截', True, weight_cap, [35.01, 20.0], 35.0, '行业')
expect('空序列视为无法判断、必须拦截', True, weight_cap, [], 25.0, '单只')
expect('报错文本指出超标标的', True, weight_cap, [12.0, 30.0], 25.0, '单只', ['甲', '乙'])

print('\n=== I. 黑天鹅：登记即触发，状态留空视为有效 ===')
import pandas as pd

def _sw(rows):
    return pd.DataFrame(rows, columns=['symbol', 'name', 'date', 'type', 'detail',
                                       'status', 'clear_date'])

_n = ar.active_swans(_sw([]))
same('空登记簿 → 0 条有效', len(_n), 0)
_n = ar.active_swans(_sw([['000895.SZ', '双汇', '2026-10-19', '审计意见', 'x', '', '']]))
same('状态留空视为有效（宁可误报）', len(_n), 1)
_n = ar.active_swans(_sw([['000895.SZ', '双汇', '2026-10-19', '审计意见', 'x', '已解除', '2026-11-01']]))
same('已解除不计入有效', len(_n), 0)
_n = ar.active_swans(_sw([
    ['000895.SZ', '双汇', '2026-10-19', '审计意见', 'x', '', ''],
    ['600461.SH', '洪城', '2026-10-19', '安全事故', 'y', '已解除', '2026-10-20'],
    ['000651.SZ', '格力', '2026-10-19', '立案调查', 'z', 'closed', '']]))
same('混合 3 条只算未解除的 1 条', len(_n), 1)
same('空表不报错且类型一致', len(ar.active_swans(_sw([])).columns), 7)

print('\n=== J. 替补资格：任一项失效即不可递补 ===')
same('美的当前指标 → 可递补', ar.backup_ok(19.95, 74.6, 2.13), True)
same('波动 25.00 → 失效（<25 不含等号）', ar.backup_ok(25.00, 74.6, 2.13), False)
same('支付率 100.0 → 失效（<100 不含等号）', ar.backup_ok(19.95, 100.0, 2.13), False)
same('OCF/分红 1.00 → 失效（>1 不含等号）', ar.backup_ok(19.95, 74.6, 1.00), False)
same('三项全优 → 仍可递补', ar.backup_ok(20.0, 70.0, 3.0), True)


print('\n=== K. 治理底线：实控人性质判定 ===')
_WL = {'招商局集团有限公司': '中央', '中国华润有限公司': '中央'}
_NEG = {'兴泰集团有限公司': '万洲国际控股股东，万隆家族控制'}
# 三态：通过 / 否决 / 缺失
same('中央国有企业 → 通过', gov.check_ownership('中央国有企业', ''), True)
same('地方国有企业 → 通过', gov.check_ownership('地方国有企业', ''), True)
same('无实际控制人 → 通过', gov.check_ownership('无实际控制人', '无'), True)
same('个人或家族 → 否决', gov.check_ownership('个人或家族', '何享健'), False)
same('民营（非国资法人）→ 否决', gov.check_ownership('民营（非国资法人）', '兴泰集团有限公司'), False)
same('外资企业 → 否决', gov.check_ownership('外资企业', 'SEB S.A.'), False)
same('性质与名称均空 → 缺失（None，不是 False）', gov.check_ownership('', ''), None)

# 名称回落判定：性质字段缺失时，按实控人文本判
cases = [
    ('广东省人民政府国有资产监督管理委员会', True, '省国资委 → 地方国企'),
    ('苏州市财政局', True, '财政局 → 地方国企'),
    ('招商局集团有限公司', True, '央企，名称无「国资」→ 靠白名单'),
    ('无', True, '披露为无'),
    ('何享健', False, '自然人（3 字）'),
    ('兴泰集团有限公司', False, '命中非国资法人名单 → 否决（双汇这一例）'),
    ('SEB S.A.', False, '境外主体'),
    ('某某投资有限公司', None, '法人但名录无记录 → 待确认，不当成否决'),
    ('刘德铭,刘子斌', False, '「张三,李四」家族写法 → 个人或家族'),
]
for name, want, lab in cases:
    same(lab, gov.classify_owner(name, _WL, _NEG)[1], want)

same('白名单命中 → 中央国有企业',
     gov.classify_owner('招商局集团有限公司', _WL, _NEG)[0], '中央国有企业')
same('不在白名单 → 待确认（不是民营，也不是国资）',
     gov.classify_owner('招商局集团有限公司', {}, _NEG)[0], '待确认（法人，未列名录）')
same('央企名录里没有「国资」二字也要认得出',
     gov.classify_owner('中国华润有限公司', _WL, _NEG)[1], True)
same('黑名单优先于「法人待确认」',
     gov.classify_owner('兴泰集团有限公司', _WL, _NEG)[1], False)
same('无法归类的短词 → 缺失', gov.classify_owner('abc-def', {}, _NEG)[1], False)  # 含字母 → 外资
same('空文本 → 无实际控制人', gov.classify_owner('', {}, _NEG)[0], '无实际控制人')

print('\n=== L. 治理底线接入复核：分组与动作 ===')
_o, _d, _a, _g = ar.judge_governance('双汇发展', None, market='深')
same('未核查 → 计数据完整性', _g, '数据完整性')
same('未核查 → FAIL', _o, False)
_o, _d, _a, _g = ar.judge_governance('双汇发展', (False, '民营（非国资法人）', 'x', '兴泰'), market='深')
same('深市否决 → 治理底线组', _g, '治理底线')
same('深市否决 → 动作含清仓换仓', '清仓换仓' in _a, True)
same('深市否决 → 动作含【移出】', '【移出】' in _a, True)
_o, _d, _a, _g = ar.judge_governance('某沪市标的', (False, '个人或家族', 'x', '张三'), market='沪')
same('沪市否决 → 只警示不硬移出', '【警示】' in _a, True)
_o, _d, _a, _g = ar.judge_governance('格力电器', (True, '无实际控制人', '披露为无', '无'), market='深')
same('通过 → ok True', _o, True)
same('通过 → 无动作', _a, '')

print('\n=== M. 基准日完整性 / 替补标记 / 记录一致性 ===')
# M1 基准日：最后一天成交额只有前 20 日中位数的 45%（2026-10-16 的真实形态）
import duckdb as _dd
from lake_base import pick_base as _pb
_c = _dd.connect(':memory:')
_c.execute("create table daily_bars(trade_date date, amount double)")
for _i in range(25):
    _c.execute("insert into daily_bars values (DATE '2026-10-01' + ?, ?)",
               [_i, 1.0e8 if _i < 24 else 4.5e7])
_b = _pb(_c, verbose=False, table='daily_bars')
same('M1 快照日（成交额 45%）→ 基准日回退一天', _b[:10], '2026-10-24')
_c.execute("update daily_bars set amount = 1.0e8 where trade_date = DATE '2026-10-25'")
same('M1b 成交额正常 → 取最后一天', _pb(_c, verbose=False, table='daily_bars')[:10], '2026-10-25')
# 阈值边界：恰好 80% 应通过，79% 应回退
_c.execute("update daily_bars set amount = 8.0e7 where trade_date = DATE '2026-10-25'")
same('M1c 恰好 80% → 视为完整', _pb(_c, verbose=False, table='daily_bars')[:10], '2026-10-25')
_c.execute("update daily_bars set amount = 7.9e7 where trade_date = DATE '2026-10-25'")
same('M1d 79% → 回退', _pb(_c, verbose=False, table='daily_bars')[:10], '2026-10-24')

# M2 替补席：候选表里不得出现任何不合格行
# 以前的设计是「合格 3 行 + 不可递补的留痕行（order=9x）」。留痕行会让读者
# 在一张候选表里逐行判断哪行能用，等于把筛选责任推回给人。现在改为：不符合
# 条件的不进表，理由写在文档里。这条测试守住这个边界。
_bs = pd.read_csv('深市替补席.csv', encoding='utf-8-sig')
_ok = _bs[_bs.eligible.astype(str).str.lower() == 'true']
_no = _bs[_bs.eligible.astype(str).str.lower() == 'false']
same('M2 可递补替补 ≥1 只', len(_ok) >= 1, True)
same('M2b 候选表内不得有 eligible=False 的行', len(_no), 0)
same('M2c 合格行的顺位必须是连续 1..n',
     _ok.order.astype(int).tolist(), list(range(1, len(_ok) + 1)))
same('M2d 每只替补都必须带建仓参考价（60 日均价 > 0）',
     bool((pd.to_numeric(_ok.vwap60, errors='coerce') > 0).all()), True)

# M3 最终配置 MD vs 执行清单 CSV：两份文件逐项相等
# 换仓记录取消后，剩下的一对「人读的 md / 机器读的 csv」必须仍然一致——
# 这正是最容易腐烂的地方：改了脚本重生成 csv，md 却是手抄的旧版。
_hp = pd.read_csv('打新底仓_最终8只组合_v2.csv', encoding='utf-8-sig')
_md_cfg = open('打新底仓_最终配置.md', encoding='utf-8').read().splitlines()
_diff = [r['name'] for _, r in _hp.iterrows()
         if not any(r['name'] in ln and f"**{r['w60']:.2f}%**" in ln for ln in _md_cfg)]
same('M3 最终配置 md 与执行清单逐项一致', _diff, [])
same('M3b md 权重合计 100%', round(float(_hp.w60.sum()), 2), 100.0)
# 反向：把 md 里某只的权重改错，必须能被发现
_bad_md = [ln.replace('**9.52%**', '**11.73%**') for ln in _md_cfg]
_d2 = [r['name'] for _, r in _hp.iterrows()
       if not any(r['name'] in ln and f"**{r['w60']:.2f}%**" in ln for ln in _bad_md)]
same('M3d 反向：md 写 11.73 而清单是 9.52 → 必须报不符', _d2, ['粤高速A'])

# M4 行业上限真的咬住了 score
_jt = float(_hp.loc[_hp.ind == '交运', 'w60'].sum())
same('M4 交运合计 ≤35%（行业上限优先于 score）', _jt <= 35.0001, True)
same('M4b 交运合计未被压到明显低于上限（说明是贴着约束解的）', _jt > 34.5, True)

print('\n=== N. 复权重建：除权日停牌 ===')
# N 除权日停牌时，分红必须落在「复牌首日」，不能因为当天没有行情就整条丢掉。
# 构造：除权日 06-18 无成交（06-08 停牌至 06-22），06-23 复牌 4.37，前收 4.31，
# 每股分红 0.102 → 真实收益 4.37/(4.31-0.102)-1 = +3.85%。
# 丢掉分红则算成 4.37/4.31-1 = +1.39%，把分红当成了下跌。
from screen_sz import rebuilt_returns as _rr
_cn = _dd.connect(':memory:')
_cn.execute("create schema src")
_cn.execute("create table src.daily_bars(symbol varchar, trade_date date, close double)")
for _d, _c in [('2026-06-05',4.43),('2026-06-08',4.31),('2026-06-23',4.37),('2026-06-24',4.15)]:
    _cn.execute("insert into src.daily_bars values ('X.SH', ?, ?)", [_d, _c])
_cn.execute("""create table src.corporate_actions(symbol varchar, ex_date date, cash_dividend double,
  bonus_ratio double, transfer_ratio double)""")
_cn.execute("insert into src.corporate_actions values ('X.SH', DATE '2026-06-18', 0.102, 0, 0)")
_R = _rr(_cn, ['X.SH'], '2026-06-24', win=30)['X.SH']
same('N1 除权日停牌 → 分红落到复牌日（+3.85%，非 +1.39%）',
     round(float(_R.loc['2026-06-23']) * 100, 2), 3.85)
same('N2 停牌日不产生假收益', round(float(_R.loc['2026-06-08']) * 100, 2), -2.71)
# 反向：若把分红记成 0，应当还原成 +1.39%，证明这条测试确实在测分红
_cn.execute("update src.corporate_actions set cash_dividend = 0")
_R0 = _rr(_cn, ['X.SH'], '2026-06-24', win=30)['X.SH']
same('N3 去掉分红后复牌日回到 +1.39%（证伪对照）',
     round(float(_R0.loc['2026-06-23']) * 100, 2), 1.39)

print('\n=== O. 支付率门禁（⑥）===')
# 2026-10-20 审计：支付率 <100% 只在文档里、代码没实现，东阿阿胶 119.41%、
# 华特达因 100.27% 留在名单里而校验器 16/16 PASS。下面四条锁死这个门禁：
# 边界（99.99 放 / 100.00 拦）+ 真实值 + 缺失值（缺失按不合规报出，不静默通过）。
same('O1 支付率 99.99% → 放行', pay_gate([('甲', 99.99)]), [])
same('O2 支付率 100.00% → 拦截（含等号）', pay_gate([('甲', 100.00)]), ['甲 100.00%'])
same('O3 东阿阿胶 119.41% → 拦截', pay_gate([('东阿阿胶', 119.41)]), ['东阿阿胶 119.41%'])
same('O4 缺失值 → 报出而非放行',
     pay_gate([('甲', float('nan'))]), ['甲 支付率缺失'])

print('\n=== P. 治理三态：供销社体系 ≠ 国资 ===')
# 「供销」原先写在 _LOCAL 里，永新股份（黄山供销集团）被判地方国企直接放行。
# 误判方向是「放过非国资」，比误杀危险，所以单列集体所有制并归入待确认。
same('P1 黄山供销集团 → 待确认（None），不是通过',
     gov.classify_owner('黄山供销集团有限公司')[1], None)
same('P2 中华全国供销合作总社 → 待确认',
     gov.classify_owner('中华全国供销合作总社')[1], None)
same('P3 成都市国资委 → 仍判地方国企（未误伤真国资）',
     gov.classify_owner('成都市国有资产监督管理委员会')[1], True)
same('P4 兴泰集团（双汇）→ 仍否决（未因改规则放行）',
     gov.classify_owner('兴泰集团有限公司')[1], False)

# ---------------- 汇总 ----------------
bad = [r for r in RESULTS if not r[1]]
# 落盘项数：报表要引用「N 组测试」这个数字。写死会在下次加测试后过期，
# 和之前 HTML 硬编码复核项数是同一类错误，所以让测试自己产出这个数。
with open('校验器自测结果.txt', 'w', encoding='utf-8') as f:
    f.write(f'共 {len(RESULTS)} 项，通过 {len(RESULTS)-len(bad)}，失败 {len(bad)}\n')
print('\n' + '=' * 62)
print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS)-len(bad)}，失败 {len(bad)}")
if bad:
    print('\n失败项：')
    for n, _, m in bad:
        print(f'  · {n}  {m}')
    print('\n注意：这里是「校验器的测试」失败，不是业务数据失败。')
    sys.exit(1)
print('全部符合预期。校验器已验证：该拦的拦得住，该放的不误伤。')
sys.exit(0)

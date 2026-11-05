# -*- coding: utf-8 -*-
"""把 深市打新底仓专用标准.md 的漏斗 / 入选 / 待确认 / 否决 / 对照 五张表
从 CSV 重生成，避免手抄——文档里 27 只的数字曾经与 CSV 差一个基准日，
根因就是手抄。

用法：screen_sz.py 跑完后执行本脚本。
"""
import pandas as pd, re

MD = '深市打新底仓专用标准.md'
res = pd.read_csv('深市专用标准_筛选结果.csv', encoding='utf-8-sig')
pend = pd.read_csv('深市专用标准_待确认.csv', encoding='utf-8-sig')
bad = pd.read_csv('深市专用标准_治理否决.csv', encoding='utf-8-sig')
fun = pd.read_csv('深市专用标准_漏斗.csv', encoding='utf-8-sig')

def nm(x):
    return str(x).replace(' ', '')

# ---- 漏斗 ----
frows = '\n'.join(f'| {r.步骤} | {r.条件} | {r.剩余} |' for _, r in fun.iterrows())

# ---- 入选 ----
# 列名必须与 screen_sz.py 输出的 CSV 表头逐字一致：这里曾读「股息率TTM%」，
# 而 CSV 里根本没有这一列（只有 股息率% / 财年口径%），KeyError 让文档同步
# 整段失效、而旧文档看上去仍完好——这是最坏的一种失败：不报错给读者看。
rrows = []
for i, r in res.iterrows():
    rrows.append(
        f"| {i+1} | {nm(r['名称'])} {r['代码']} | {r['板块']} | {float(r['年化波动%']):.2f}% | "
        f"**{float(r['股息率%']):.2f}%** | {float(r['股息率TTM%']):.2f}% | "
        f"{float(r['三年支付率%']):.1f}% | {float(r['OCF/分红']):.2f} | {r['所有制性质']} |")
rrows = '\n'.join(rrows)

# ---- 待确认 ----
prows = '\n'.join(
    f"| {nm(r['名称'])} {r['代码']} | {float(r['年化波动%']):.2f}% | {float(r['股息率%']):.2f}% | "
    f"{float(r['OCF/分红']):.2f} | {r['实际控制人']} |" for _, r in pend.iterrows())

# ---- 否决 ----
brows = '\n'.join(
    f"| {nm(r['名称'])} {r['代码']} | {r['实际控制人']} | {r['所有制性质']} |"
    for _, r in bad.sort_values('名称').iterrows())

s = open(MD, encoding='utf-8').read()

# 漏斗表：按表头定位、只替换数据行。不锚定「## 三」标题——标题与表之间常插
# 一段口径说明，锚定标题的正则会在那次插话之后静默失配（文档保持旧表而脚本报成功）。
s = re.sub(r'(\| 步骤 \| 条件 \| 剩余 \|\n\|[-\|]+\n)(?:\|[^\n]*\n)*',
           lambda m: m.group(1) + frows + '\n', s, count=1)
s = re.sub(r'## 四、入选名单（\d+ 只）\n\n\| # \| 标的 \| 板块 \| 年化波动 \| 股息率 \| 股息率TTM \| [^\n]*\n\|[-\|]+\n(?:.*\n)*?\n',
           f'## 四、入选名单（{len(res)} 只）\n\n| # | 标的 | 板块 | 年化波动 | 股息率 | 股息率TTM | 三年支付率 | OCF/分红 | 实控人性质 |\n'
           '|---|---|---|---|---|---|---|---|---|\n' + rrows + '\n\n', s)
s = re.sub(r'## 五、待确认（\d+ 只，不计入入选）\n\n\| 标的 \| 年化波动 \| 股息率 \| OCF/分红 \| 实际控制人 \|\n\|---\|---\|---\|---\|---\|\n(?:.*\n)*?\n',
           f'## 五、待确认（{len(pend)} 只，不计入入选）\n\n| 标的 | 年化波动 | 股息率 | OCF/分红 | 实际控制人 |\n'
           '|---|---|---|---|---|\n' + prows + '\n\n', s)
s = re.sub(r'## 六、被治理底线剔除的（\d+ 只）\n\n\| 标的 \| 实际控制人 \| 判定 \|\n\|---\|---\|---\|\n(?:.*\n)*?\n',
           f'## 六、被治理底线剔除的（{len(bad)} 只）\n\n| 标的 | 实际控制人 | 判定 |\n|---|---|---|\n' + brows + '\n\n', s)

# 待确认段的引导句（三只→N 只）
s = s.replace('三只的实控人都是**县级/省级国资平台**',
              f'这 {len(pend)} 只的实控人都是**县级/省级国资平台**')

open(MD, 'w', encoding='utf-8').write(s)
print(f'已重生成：入选 {len(res)} / 待确认 {len(pend)} / 否决 {len(bad)}')

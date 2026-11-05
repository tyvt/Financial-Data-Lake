# -*- coding: utf-8 -*-
"""
报表口径校验（单一真源防线）

背景：本轮复算暴露了两类同源错误——
  1) 权重归一化时提前 round(1)，导致组合合计 99.94%，按下单表会系统性少配 0.06%；
  2) 变异系数在正文手写 23.1%、报表手写 25.6%，两处独立产生、无单一真源。
共同根因：同一个数字在多处独立生成，靠人工复核不可靠。

本模块把校验做成硬断言：任一不满足直接 AssertionError 中断出表，
杜绝"带病报表"流到执行环节。

────────────────────────────────────────────────────────────────
⚠ 能力边界（使用前必读）
────────────────────────────────────────────────────────────────
本模块保证的是「数字自洽」，不保证「口径正确」。这是必要条件，
不是充分条件。

  · 能防住：同一个数字在多处独立产生导致的不一致（工程问题）。
    例：权重归一化提前舍入 → 合计 99.94%；变异系数正文 23.1%、
    报表 25.6% 两处手写。这类错误本模块一定拦得住。

  · 防不住：源头数据或口径本身错了（研究问题）。
    反例：若分红序列漏掉 2024 年那次 −43% 的下调，变异系数会
    算成另一个值——源与报表用的是同一份错数据，single_source()
    照样 PASS。权重合计 100% 也不能保证权重模型本身合理。

因此「断言 + 报表脚注的口径声明」必须同时存在，缺一不可：
  断言守住一致性，脚注守住口径的可追溯性（分红按报告期归因、
  波动率取 252 个交易日复权收益率、回撤用复权净值、变异系数用
  2022–2025 四年 ddof=1 等）。只有前者没有后者，错的结论会以
  完全自洽的形式流到执行环节，比明显的不一致更危险。

另：校验器自身也需要被验证。新增检查项时，应构造一组已知错误
的输入做负向测试，确认它真的会 FAIL——否则它可能只是「看起来
在拦」。见 tests_qa_negative.py。
────────────────────────────────────────────────────────────────

用法：
    from qa_checks import (check, weights_sum_100, conservation,
                           single_source, residue_free, absent, summary)
"""
import numpy as np

TOL_PCT = 1e-6          # 百分比口径容差（0.000001 pct）
TOL_EXACT = 1e-9        # 浮点重算一致性
LOG = []


def check(name, cond, msg=''):
    """通用断言：失败即中断。"""
    if not cond:
        LOG.append((name, False, msg))
        raise AssertionError(f'[FAIL] {name}：{msg}')
    LOG.append((name, True, ''))
    print(f'  [PASS] {name}')
    return True


def weight_cap(values, cap, label='权重', who=None):
    """断言一组权重（单只或单行业）均不超过上限。

    values: 数值序列；who: 与 values 等长的名称序列，用于报出是谁超标。
    返回最大权重，便于调用方复用。

    为什么单独成函数而不是内联：上限是硬约束，必须能被负向测试覆盖。
    内联的 check 只能证明"当前值没超"，证明不了"超了会拦"。
    """
    vals = [float(v) for v in values]
    if not vals:
        check(f'{label} ≤{cap:.0f}%（无数据）', False, '权重序列为空，无法判断上限')
    mx = max(vals)
    i = vals.index(mx)
    name = who[i] if who else f'第 {i + 1} 项'
    return check(f'{label} ≤{cap:.0f}%（实际最大 {mx:.2f}%：{name}）', mx <= cap,
                 f'{name} 权重 {mx:.2f}% 超过上限 {cap:.0f}%，'
                 f'超出部分按比例分配给未超标标的，并重查另一级约束')


def pay_gate(rows, limit=100.0):
    """支付率门禁：返回三年合计支付率 ≥ limit 的标的描述列表（空 = 全部合规）。

    rows: [(名称, 支付率%)] 或 [(名称, 支付率%, 代码)]。
    单独成函数是为了能被负向测试覆盖——内联在出表脚本里的断言只能证明
    「当前这批数据没超」，证明不了「超了会拦」。2026-10-20 的教训：支付率
    上限写在文档里、代码没实现，名单里躺着 119% 的东阿阿胶，而校验器全 PASS。
    """
    bad = []
    for r in rows:
        name, v = r[0], r[1]
        if v is None or v != v:            # NaN：无法判定，按不合规报出
            bad.append(f'{name} 支付率缺失')
            continue
        if float(v) >= limit:
            bad.append(f'{name} {float(v):.2f}%')
    return bad


# ---------------- 软检查：用于周期性复核（不中断） ----------------
AUDIT = []

# 分组语义：只有「黑天鹅」「治理底线」「卖出触发」「数据完整性」计入需处理。
# 「入选资格」组回答的是"当初为什么选它、理由还在不在"，破了不等于要卖；
# 「观察池」组的未达标属于预期状态（维持观察）；「替补席」失效只影响预案。
# 这几类都只记录、不报警，避免把"资格偏离"误读成"必须下单"。
# 「治理底线」计需处理，是因为它和政治/资本背景有关且不可随时间自愈——
# 实控人性质不会因为业绩好转变回国资，属于「要么留、要么换」的判断。
RISK_GROUPS = ('黑天鹅', '治理底线', '卖出触发', '数据完整性')


def audit(name, ok, detail='', action='', group='入选资格'):
    """记录一条复核结果但不中断流程。

    与 check() 的区别：check() 用于出表前的硬闸门（失败即停），
    audit() 用于年度复核这类「多标的 × 多指标」的巡检——单项
    不达标不应中断整轮复核，需要看到全貌后再决定动作。

    group: 结果分组，决定它是否计入「需处理」——
      · '黑天鹅'     人工登记的风险事件，登记即需动仓；
      · '治理底线'   实控人性质（个人/家族、非国资法人、外资），一票否决；
      · '卖出触发'   触发即需动仓，计入需处理，影响退出码；
      · '数据完整性' 指标取不到，计入需处理（缺数据≠达标）；
      · '入选资格'   入选标准是否仍满足，仅记录偏离，不计需处理；
      · '观察池'     回补条件是否满足，仅记录。
    action: 触发后的建议动作，会写进报告。
    """
    msg = f'[FAIL] {name}' if not ok else f'[PASS] {name}'
    if detail:
        msg += f'  {detail}'
    if not ok and action:
        msg += f'  → {action}'
    AUDIT.append(dict(name=name, ok=bool(ok), detail=detail, action=action, group=group))
    print(msg)
    return bool(ok)


def weights_sum_100(series, name, total=100.0):
    """权重必须精确合计到 total（默认 100.00%）。"""
    s = float(np.sum(np.asarray(series, dtype=float)))
    return check(f'{name} 合计={total:.2f}%', abs(s - total) < TOL_PCT,
                 f'实际 {s:.10f}，偏差 {s-total:+.10f}')


def conservation(deltas, name):
    """权重调整必须守恒：各标的变动之和为 0。"""
    net = float(np.sum(np.asarray(deltas, dtype=float)))
    return check(f'{name} 权重守恒', abs(net) < TOL_PCT, f'净变动 {net:+.10f}')


def single_source(reported, recomputed, name, tol=TOL_EXACT):
    """报表展示值必须等于从源数据重算的值（防止手写残留）。"""
    return check(f'{name} 单一真源', abs(float(reported) - float(recomputed)) < tol,
                 f'报表 {reported} vs 重算 {recomputed:.10f}')


def residue_free(text, name, markers=('{met.', '{dps_', '{sw_', '{deg', '{r[', '{sh_deg', '{sz_deg')):
    """模板占位符必须全部替换完毕。"""
    bad = [m for m in markers if m in text]
    return check(f'{name} 无模板残留', not bad, f'发现残留 {bad}')


def absent(text, keywords, name):
    """已废弃的硬编码数字不得残留（如旧口径的 25.6%、5.30%）。

    已知局限：纯字符串匹配无法区分「把旧值当现值展示」和「在文档里
    引用这段历史缺陷」——两者都含同样的字面。因此 keywords 只应放
    「绝不该出现在报表现值区的精确数值」（如 99.94、25.6%），不要放
    「合计」「约」这类宽泛前缀；正文若要复盘历史缺陷，改用描述性
    表述（如「因提前舍入而系统性少配」），不要写具体旧数字。
    """
    hit = [k for k in keywords if k in text]
    return check(f'{name} 无过期硬编码', not hit, f'发现 {hit}')


def summary(path='校验结果.txt'):
    lines = ['报表口径校验结果', '=' * 46, '']
    for n, ok, msg in LOG:
        lines.append(f"{'PASS' if ok else 'FAIL':<5} {n}" + (f'  -> {msg}' if msg else ''))
    lines += ['', f'通过 {sum(1 for _, ok, _ in LOG if ok)} / {len(LOG)} 项']
    txt = '\n'.join(lines)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(txt + '\n')
    print('\n' + txt.split('通过')[0].strip()[:0] + f"校验：通过 {sum(1 for _,ok,_ in LOG if ok)}/{len(LOG)}，已写 {path}")
    return txt


def audit_report(path='年度复核结果.txt', title='打新底仓年度复核', risk_groups=RISK_GROUPS):
    """把 audit() 累积的结果写成文本报告，返回 (需处理数, 文本)。

    只有 risk_groups（默认「黑天鹅」「治理底线」「卖出触发」「数据完整性」）
    的 FAIL 计入「需处理」并影响退出码；「入选资格」偏离与「观察池」未达标
    单独统计，避免把"当年入选理由松动"直接当成"该卖了"。
    """
    groups = []
    for a in AUDIT:
        if a['group'] not in groups:
            groups.append(a['group'])
    risk = [a for a in AUDIT if not a['ok'] and a['group'] in risk_groups]
    watch = [a for a in AUDIT if a['group'] == '观察池']
    watch_ok = [a for a in watch if a['ok']]
    gate_bad = [a for a in AUDIT
                if not a['ok'] and a['group'] not in risk_groups and a['group'] != '观察池']
    n_bad = len(risk) + len(gate_bad) + len([a for a in watch if not a['ok']])

    lines = [title, '=' * 62, '']
    for g in groups:
        items = [a for a in AUDIT if a['group'] == g]
        tag = '动作组' if g in risk_groups else ('观察组' if g == '观察池' else '资格组')
        lines.append(f'【{g}】（{tag}）')
        for a in items:
            lines.append(f"{'FAIL' if not a['ok'] else 'PASS':<5} {a['name']}"
                         + (f"  {a['detail']}" if a['detail'] else '')
                         + (f"  → {a['action']}" if a['action'] and not a['ok'] else ''))
        lines.append('')

    # 汇总行不能只写「卖出触发 N 项」：治理底线、黑天鹅同样计入需处理，
    # 混在一个数字里会让读报告的人以为只有卖出线被触发。分开列。
    cnt = {g: sum(1 for a in risk if a['group'] == g) for g in risk_groups}
    parts = ' / '.join(f'{g} {cnt[g]}' for g in risk_groups if cnt[g]) or '无'
    lines += ['-' * 62,
              f"共 {len(AUDIT)} 项，通过 {len(AUDIT)-n_bad}，"
              f"需处理 {len(risk)} 项（{parts}），入选资格偏离 {len(gate_bad)} 项"]
    if watch:
        lines.append(f"观察池：{len(watch_ok)}/{len(watch)} 项已满足回补条件")
    if risk:
        lines.append('\n需处理（按分组，均需动作）：')
        for a in risk:
            lines.append(f"  · {a['name']}" + (f" → {a['action']}" if a['action'] else ''))
    elif gate_bad:
        lines.append('\n无卖出触发。以下项入选资格已松动，仅需记录，不动仓：')
        for a in gate_bad:
            lines.append(f"  · {a['name']}  {a['detail']}")
    else:
        lines.append('\n组合全部达标，维持当前配置。')

    txt = '\n'.join(lines)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(txt + '\n')
    print('\n' + f'复核完成：卖出触发 {len(risk)} 项，已写 {path}')
    return len(risk), txt

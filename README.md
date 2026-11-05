# Financial-Data-Lake

A 股打新底仓选股用的本地数据湖与筛选工程。

## 结构

- `scripts/` — 数据湖更新与维护脚本（`update_selection_lake.py`、`audit_lake_public.py`、`prune_selection_history.py` 等）
- `configs/` — 数据湖策略配置（`selection_lake_policy.json`）
- `data/cnequity/` — 数据湖本体（行情、财务、分红、银行专项、治理、行业等，parquet + duckdb）
- `分析产出/` — 选股脚本、筛选标准、结果与报告（沪市 11 条 / 深市专用 7 条 → score → 组合）
- `logs/` — 运行日志

## 选股流程（简要）

1. `python scripts/update_selection_lake.py` 更新数据湖
2. `python 分析产出/screen_sh.py --base <日期>` 沪市 11 条筛选
3. `python 分析产出/screen_sz.py --base <日期>` 深市专用 7 条筛选
4. `python 分析产出/reselect_portfolio.py <日期>` score 选股 + 权重
5. `python 分析产出/final_portfolio_v3.py` 出组合报表 + 校验

> 注：数据湖依赖本地 HTTP 代理出口抓取东财等数据源。

# 数据湖 B 阶段执行报告

- 状态：完成，清空15个已确认B组数据集；保留零行Parquet结构及有效版本指针。
- 删除逻辑文件体积：1.106 GiB；清理后目录约 2.805 GiB（包含审计记录）。
- 旧版本清理：26 个；另有15个无收据的 `legacy` 基线目录，已确认均为空目录、总计0字节，保留其目录身份。
- 已更新B组统计索引：15张表共0行，结构文件合计约27 KiB。
- 已移除 configs/cnequity.toml 中B组日常采集步骤，配置校验通过。
- 采集配置按现有调度组控制，不是全局禁用机制；显式 backfill、init 或不限定组的 stale-only 修复仍可能重新采集，今后不要对B组执行这些补采。
- B组保留空表视图，查询返回0行，不使现有视图因路径消失而报错。
- 核心和C组19个保留查询的行数清理前后一致；保留层5,537个Parquet文件SHA256逐个一致。
- 版本收据、删除审计、任务历史、混合来源快照及共享缓存保留。C组、核心数据、字段未裁剪。

## 清理数据集

- `analyst_consensus`
- `block_trades`
- `commodity_bars`
- `dragon_tiger`
- `etf_profiles`
- `fund_flow`
- `macro_indicators`
- `margin_trading`
- `market_breadth`
- `northbound_flows`
- `northbound_holdings`
- `sector_bars`
- `sector_members`
- `sentiment_scores`
- `valuation_metrics`

## 保留数据核验

|表|行数|
|---|---:|
|`announcement_index`|8,012|
|`corporate_actions`|69,976|
|`daily_bars`|11,187,520|
|`earnings_disclosure_schedule`|11,120|
|`financial_statement_items`|2,791,767|
|`index_bars`|19,978|
|`index_constituents`|6,284|
|`industry_members`|437,058|
|`institutional_holdings`|20,647|
|`instruments`|8,082|
|`news_headlines`|1,588|
|`regulatory_events`|23|
|`shareholder_counts`|2,839|
|`share_structure`|62,182|
|`share_unlock_schedule`|813|
|`top_holders`|1,022,791|
|`trading_calendar`|4,310|
|`trading_status`|49,027|
|`daily_bars_adj`|11,187,520|

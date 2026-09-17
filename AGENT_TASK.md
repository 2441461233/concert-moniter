# 分层手动刷新任务书

> 2026-08-24 更新：旧的“Kimi 全量生成后自动进 inbox/覆盖”契约已废止。
> 当前真实 benchmark 中没有任何模型通过生产硬门禁，不得恢复旧路径。
> 详细操作、预算和验收标准以 [README.md](README.md) 与 [VALIDATION.md](VALIDATION.md) 为准。

## 当前不可变契约

1. 网页按钮调度 GitHub Actions；Vercel API 不运行采集或模型请求。
2. `scripts/full_refresh.py` 先运行 ShowStart 确定性采集，使用
   `monitor.py check --force --no-inbox --strict-sources`。2026-09-17 起同一步还采集
   `official_sources` 显式配置的官方页面；不走 LLM candidate promotion。
3. 已存在的 `events.json`、`rumors.json`、`meta.json` 必须存在、可读且
   根类型正确；不得把损坏/缺失生产库当成空库重建。
4. ShowStart 任一应采艺人或任一配置官方来源失败、HTTP 200 挑战/空页/DOM 漂移、merge、build 或
   metadata 失败，整轮返回非 0，不写新 `full_refresh_id`，workflow 不提交。
5. `LLM_ENRICH_PROVIDER` 默认 `none`；没 Key、没余额或可选参数误配不得
   阻断免费确定性快照。
6. 显式 `kimi` 只允许在预算/上下文硬上限内生成仓库外 candidate artifact。
   candidate 在首个付费请求前独占预留，拒绝仓内路径、symlink escape 和覆盖。
7. 所有付费 candidate 都写入 `production_write=false`、
   `promotion_status=candidate_only_pending_manual_validation` 与 blocked quality gate。
   `monitor.py ingest` 必须拒绝这类文件。当前没有 promotion 开关或自动写路径。
8. 模型不输出 URL；只能引用程序从 Formula reference catalog 生成的固定
   `source_id`，由程序回填原 URL。这仅证明“引用属于搜索结果”，不证明
   页面语义支持每个字段，因此不构成生产授权。
9. `doors_time`、`show_time`、`show_end_time`、`curfew_time` 分开存储；含义
   不明或冲突时留空，禁止互填。
10. workflow 只提交确定性 `config/data/site` 产物。telemetry 和 candidate 位于
    runner temp 并上传 artifact，绝不 `git add` `research/archive` 或 candidate。

## 状态语义

- `deterministic_completed_enrichment_stale`：确定性快照已完成；模型未运行或
  candidate 已隔离生成，线上 research 没有更改。
- `deterministic_completed_enrichment_failed`：确定性快照已完成；候选层失败，
  线上 research 仍没有更改。
- `deterministic_completed_enrichment_completed` 仅作为旧数据 UI 兼容文案。当前主链、
  metadata finalizer 都不得产生该状态。

## 未来 promotion

只有版本化 contract/manifest 经多艺人、同证据、至少 3 轮人工金标验收，并通过
`VALIDATION.md` 的召回、confirmed precision、字段准确/完整、稳定性、逐字段来源支持、
冲突披露与完整账单门禁后，才能另立可回退的 promotion 设计。不得复用普通
`monitor.py ingest` 或删除 candidate marker 来绕过门禁。

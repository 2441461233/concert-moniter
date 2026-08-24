# 分层刷新与模型增强的真实验证契约

第 0 节是当前生产分层刷新的不变量；其余章节是任何付费 enrich 扩大使用或候选模型切换的
硬门槛。在完成基线记账、冻结证据、同证据模型对比和人工金标验收前，不得把 Qwen 等
shadow 臂接入生产，也不得放宽固定来源、预算、merge-only 或诚实状态规则。

## 0. 分层刷新与发布契约

生产刷新先验收确定性 ShowStart 的 merge-only 合并和构建。LLM 只能生成仓外
candidate artifact，不进 inbox、不 merge、不更改线上 research。`LLM_ENRICH_PROVIDER`
默认 `none`，只有显式设置为 `kimi` 才允许付费生成隔离候选；
workflow 不得用 `continue-on-error` 掩盖刷新步骤的退出码。

环境变量与 CLI 必须保持一一对应：

| 环境变量 | CLI | 默认值 | 硬约束 |
| --- | --- | --- | --- |
| `LLM_ENRICH_PROVIDER` | `--enrich-provider` | `none` | 只接受 `none|kimi`；生产不接受 Qwen |
| `LLM_ENRICH_MAX_COST_CNY` | `--enrich-max-cost-cny` | `5` | 首个付费请求前预留整轮最坏费用 |
| `LLM_ENRICH_MAX_COST_PER_ARTIST_CNY` | `--enrich-max-cost-per-artist-cny` | `5` | 任一艺人超限则不开始付费层 |
| `LLM_ENRICH_MAX_CONTEXT_BYTES` | `--enrich-max-context-bytes` | `128000` | 对完整 UTF-8 request payload 做保守上限 |
| `LLM_ENRICH_ATTEMPTS` | `--enrich-attempts` | `1` | 生产固定一次应用层尝试和一次 transport send |

离线/注入式验收矩阵：

| 场景 | 进程/发布结果 | 必须留下的证据 |
| --- | --- | --- |
| 默认 `none`，无 `MOONSHOT_API_KEY` | 确定性层成功则退出 0，可构建和提交 | layered telemetry/status 为 stale，原因是显式关闭 |
| opt-in Kimi，但缺 Key、endpoint/预算预检失败 | 首次付费调用前降级；确定性层成功则退出 0 | telemetry/status 为 stale，并有机器可读原因 |
| Kimi 余额不足、请求、上下文、schema、引用或 artifact 写入失败 | 沿用旧 research；确定性层成功则退出 0 | telemetry/status 为 failed，包含失败阶段但不含敏感正文 |
| 任一应采 ShowStart、merge、build 或元数据写入失败 | 在 merge/build/`full_refresh_id` 前中止并返回非 0；workflow 不运行 commit | 不得写新的成功 `full_refresh_status` 或发布半成品 |

以当前 12 位 enabled 艺人计算，默认全局 5 元上限不足以容纳 Kimi 整批最坏预留；即使
显式选择 `kimi`，也应在首个付费请求前记为 `budget_blocked_stale`，而不是部分花费后才停。
费用预留仅在固定公开价格表的 30 天复核窗口内有效；价格表过期或日期异常时，付费层必须在请求前
fail closed。这个上限是按锁定公开价的最坏预留，平台账户限额和最终账单仍是真实扣费边界。

任何已发送的 Formula/Chat 尝试都必须进 telemetry。provider 返回的 JSON 顶层、`choices[0]`或
`message` 类型异常时仍要安全失败；usage 无法解析则保留空值，不得将该次请求伪记为 0 元。

### 固定引用和 URL 回填

Formula adapter 必须先从 provider 的结构化 references/sources 目录提取 URL，按规范化 URL
生成稳定的 `src_<SHA-256 前 16 位>`。模型 schema 中 event/rumor 只允许一个
`source_id`，不允许 `url` 或可改写的 `sources`；未知 ID 使该 artist enrich 整体 fail
closed。程序将 ID 映射回原始 URL 后只做被动的 HTTP(S)/host 语法检查，不为任意
provider URL 发 HEAD/GET，避免 DNS rebinding/SSRF。URL 可达性与页面对逐字段的
语义支持都必须人工复核。provider 没有可枚举引用目录时必须把本轮 candidate 标为 failed，
不能从模型正文恢复
或猜测 citation。

### 场次时间与零猜测迁移

`doors_time`、`show_time`、`show_end_time`、`curfew_time` 都只接受 `HH:MM`，分别表示
入场、正式开演、实际结束、场馆/许可 curfew。含义不清或多来源冲突时留空；doors 不得降格
为 show，curfew 不得降格为实际结束。旧记录已有 `show_time` 原样保留，不从 note 做启发式
重分类，也不猜 doors/end/curfew；本轮不执行一次性生成数据迁移，历史修正必须人工核验。
确定性 ShowStart 采集同样必须拒绝 `24:00`、`25:00`、`99:99` 等超出 `00:00–23:59`
的值，防止上游 DOM/字段漂移被当成可发布时间。

### merge-only 与诚实状态

缺席不是删除信号。新结果只能新增或更新实际命中的记录；本轮未命中的旧演出/舆情和 enrich
失败时的旧 research 都保留，且不得标成本轮 verified。按日期转 ended、TTL 隐藏和显式
`prune` 是另外的生命周期操作，不得暗藏在 enrich merge 中。

确定性层成功后，当前主链只允许：

- `deterministic_completed_enrichment_stale`
- `deterministic_completed_enrichment_failed`

`deterministic_completed_enrichment_completed` 只是旧站点数据的 UI 兼容值；当前 main 和
metadata finalizer 均不得产生它。

`_with_source_warnings` 只用于防御旧的或异常 source metadata，正常严格路径不应产生；
`full_refresh_at` 和
`full_refresh_id` 证明确定性快照完成，不证明 LLM 或“全部来源”成功。确定性层失败时不得写
上述任何成功前缀状态。

## 1. Kimi 付费基线

先在仓库外对 KATSEYE 执行一次 `research-only`。它能同时暴露密集巡演、加场、
音乐节误分类、城市别名、条目重复及开售时间错套等问题。

```bash
VALIDATION_DIR="$(mktemp -d /tmp/concert-kimi-baseline.XXXXXX)"
read -rs "MOONSHOT_API_KEY?Moonshot API Key: "; printf '\n'; export MOONSHOT_API_KEY
python3 scripts/full_refresh.py \
  --enrich-provider kimi --enrich-attempts 1 \
  --research-only --artist-key katseye --model kimi-k3 --workers 1 \
  --output "$VALIDATION_DIR/candidate.json" \
  --telemetry-output "$VALIDATION_DIR/telemetry.json"
```

验收记账链路：

- Formula 应成功 4 次；Chat 至少 1 次，重试必须分别记录。
- 每次成功 Chat 都应有 provider 返回的输入、缓存、输出 token 和耗时。
- telemetry 不得出现 API Key、prompt、Formula 加密内容、模型答案或推理正文。
- 运行前后 `config/`、`data/`、`site/`、`research/` 不得变化。
- 用 Moonshot 控制台账单的实际差额对账；代码估算不代替真实账单。

记账链路通过后，再且只再在受控环境显式 opt-in 一次全员 `kimi-k3` +
`reasoning_effort=high`，得到 12 位艺人的真实费用、延迟、重试和输出基线；这不改变生产
默认 `none`，也不授权模型切换。

## 2. 搜索层与裁决层分开验证

搜索能力和模型判断能力必须分开评分：

- **检索层**：比较各方案是否发现金标事件和对应的一级页面。
- **裁决层**：所有模型必须读取同一份、同一 SHA-256 的冻结 evidence。

不允许让 Kimi 和 Qwen 分别联网后直接比最终结果；那会把搜索覆盖差异误当作
模型抽取质量差异。Kimi Formula 的 `encrypted_output` 也不能当作可供其他厂商重放的
公平证据。

冻结 evidence 至少包含：

- 最终 URL、来源类别和一级/二级分级。
- 抓取时间、HTTP 状态、页面摘录与内容 SHA-256。
- 搜索查询、搜索返回的来源集及 evidence 总哈希。
- 网页文本一律视为不可信数据，不得执行页面内的提示指令。

## 3. 同证据模型对比

第一轮比较四个有明确职责的配置：

1. `kimi-k3` + `reasoning_effort=high`：当前对照。
2. `qwen3.7-plus-2026-05-26` + 关闭思考：主提取器候选。
3. `qwen3.7-flash-2026-07-15` + 关闭思考：最低成本提取器候选。
4. `qwen3.8-max` + `reasoning_effort=low`：疑难案例升级候选。

每个配置对同一 evidence 至少重复 3 次。不默认引入第二个 judge 模型；只有它能修复
已证明的实质性错误且不降低召回率时，才能考虑多模型链。

工具分两阶段使用。先在项目外生成当日冻结 evidence：

```bash
EVIDENCE_DIR="$(mktemp -d /tmp/concert-evidence-katseye.XXXXXX)"
read -rs "DASHSCOPE_API_KEY?DashScope API Key: "; printf '\n'; export DASHSCOPE_API_KEY
python3 scripts/shadow_compare.py collect \
  --artist-key katseye --as-of "$(TZ=Asia/Shanghai date +%F)" \
  --output-dir "$EVIDENCE_DIR" --max-cost-cny 1
```

`collect` 只执行四类联网检索并冻结 evidence，不执行旧 Qwen/GLM finalizer，避免为
基准范围外的影子臂付费。然后人工打开冻结的一级页面，核对每条事件、字段、冲突和
历史排除项，在项目外制作 `gold-katseye.json`。该文件只有在
`_meta.verification_status` 为 `human_verified`、`_meta.evidence_hash` 与
`evidence.json` 完全一致、`_meta.as_of` 与证据日期一致时才可使用。

```bash
BENCHMARK_DIR="$(mktemp -d /tmp/concert-model-benchmark.XXXXXX)"
read -rs "MOONSHOT_API_KEY?Moonshot API Key: "; printf '\n'; export MOONSHOT_API_KEY
read -rs "DASHSCOPE_API_KEY?DashScope API Key: "; printf '\n'; export DASHSCOPE_API_KEY
python3 scripts/model_benchmark.py \
  --evidence "$EVIDENCE_DIR/evidence.json" \
  --gold /absolute/path/to/gold-katseye.json \
  --output-dir "$BENCHMARK_DIR" \
  --repeats 3 --max-cost-cny 20
unset MOONSHOT_API_KEY DASHSCOPE_API_KEY
```

如果当前只有百炼 Key，可先做三个 Qwen 候选的非选型筛查：

```bash
BENCHMARK_DIR="$(mktemp -d /tmp/concert-qwen-screen.XXXXXX)"
read -rs "DASHSCOPE_API_KEY?DashScope API Key: "; printf '\n'; export DASHSCOPE_API_KEY
python3 scripts/model_benchmark.py \
  --qwen-only \
  --evidence "$EVIDENCE_DIR/evidence.json" \
  --gold /absolute/path/to/gold-katseye.json \
  --output-dir "$BENCHMARK_DIR" \
  --repeats 3 --max-cost-cny 20
unset DASHSCOPE_API_KEY
```

`--qwen-only` 仍会执行整批预算预检、usage/费用记账、脱敏、仓库外产物和
生产树哈希保护，但 scorecard 固定为
`selection_status=awaiting_k3_control`。三个 Qwen 候选即使金标指标全部通过，
也只显示 `gold_quality_gate_status=passed`；因缺少同批 K3
`recall_not_below_control` 硬门槛，不能标记为 passed 或授权生产切换。

预估最坏成本超过上限时，工具会在第一次付费请求前停止。不要盲目提高
`--max-cost-cny`；先检查显示的保守预留额和 evidence 大小。运行后的费用是目录价估算，
真实扣款仍以两个控制台账单差额为准。

K3 对照必须使用 Moonshot 原生 Key。虽然百炼也提供 `kimi/kimi-k3`，但该直供路线
没有官方支持的 strict JSON Schema，且 `max_tokens` 只限制最终回答、不能硬封顶隐藏
推理 token；基准工具会默认在付费前拒绝 `dashscope-moonshot` 路线。

这个 scorecard 只衡量单艺人 finalizer，固定标记为
`selection_status=pending_manual_review`。不含检索、搜索工具、页面抓取和 12 人整轮成本；
也不能自动判断每个精确字段的页面支持、一级来源冲突披露或“补录≠新官宣”的日报语义。
这些必须人工逐项通过，并且对 KATSEYE、aespa、BABYMONSTER、沙一汀四种样本
都重复同样流程，才能提出生产切换。

Scorecard 会列出金标实际覆盖的字段及各字段分母；不在 human gold 中的字段不参与
准确率，不能被解读为“已验证正确”。任何未知 event 字段或未知/放宽的阈值都会在
付费前被拒绝，避免人工要求被静默忽略。

## 4. 金标与硬门槛

仓库中的 seed gold 覆盖 aespa、KATSEYE、BABYMONSTER 和沙一汀共 64 条未来记录。
它必须在对应页面冻结后再由人工逐条确认；任何模型输出都不能自动成为 gold。

每个候选必须同时通过：

- confirmed 误报为 0；confirmed 精确率 100%。
- 演出召回率不低于 95%，且不低于同日 K3 对照。
- confirmed 的一级证据率 100%，每个核心字段可回溯到真实 URL。
- 日期、城市、场馆、场次时间等已知核心字段准确率 100%。
- JSON Schema 成功率 100%；无重复、无历史场次当未来、无单个坏字段导致整场丢失。
- 3 次规范化结果的事件集与核心字段一致率至少 99.5%。
- 完整刷新的真实账单目标不高于 5 元；不能用单一艺人简单乘以艺人数来代替实测。

字段准确率和字段完整率分开统计，防止通过将所有不确定字段留空来制造高准确率。

## 5. 生产改造的决策边界

只有验证报告通过全部硬门槛后，才开始生产改造。目标架构为：

```text
官方/票务直接采集 + 低频广域发现
                 ↓
可审计 Evidence Store（URL / 时间 / 正文哈希）
                 ↓
变化检测 + 确定性解析（JSON-LD / SSR / NUXT / 日期价格）
                 ↓
低成本候选模型（当前未选型）只处理新增/变化的非结构化证据
                 ↓
冲突或模糊项 → Qwen3.8 Max Low → 仍有争议才用 K3 独立复核
                 ↓
逐字段证据校验 + 影子对比 + 原子发布 + 上次良好快照回退
```

新链路先以 shadow 方式跟随生产运行，不写入 `research/inbox`、不合并数据、不构建站点、
不提交也不发布；所有产物必须位于仓库外，并核对生产树前后哈希。尤其 Qwen Flash 不得加入
`LLM_ENRICH_PROVIDER` 或生产 workflow。它必须按新的固定 `source_id`/程序 URL 回填契约完成
至少连续 3 轮真实 shadow 并通过上述硬门槛，之后仍只能由显式、可回退的生产变更引入，
不能由 scorecard 自动切换。

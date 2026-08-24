# KATSEYE 刷新模型真实验证报告（2026-08-24）

> 状态：单艺人、finalizer-only pilot；截至 2026-08-24（Asia/Shanghai）。本报告只记录可复核的测试产物与公开来源，不记录任何 API 凭证，也未修改、提交或发布生产数据。

## 结论先行

1. 当前人工核验的正确结果是 **32 场**，不是线上/本地旧快照中的 31 场。新增的是 **2026-11-28 Mexico City**；Irvine 应是 festival，演出为 14:15–14:45，不应沿用巡演名称。
2. **本轮没有任何模型达到无人值守发布标准。** Qwen3.8-Max 的单轮最好结果虽然达到 32/32 事件召回，但仍有 3 个关键字段错误；三轮之间波动明显。Qwen3.7-Flash 最便宜、最快，但三轮有效性和召回波动过大。Qwen3.7-Plus 本轮最差，三轮只有一轮通过基础校验。阿里云部署的 Kimi K3 三次返回只有 2 次 valid，有效轮召回从 37.5% 跳到 100%，期间还经历一次超时和一次 429，也被 rejected。
3. 程序已重新设计为：**默认 0 元的确定性刷新 + 显式 opt-in 的仓外模型 candidate + 严格验证器 + 原子发布**。未来可让 Flash 只影子处理变更证据、Max 升级处理冲突项，但它们当前都不是生产默认；K3 只作少量跨供应商仲裁。
4. 本次已能按目录价核算的支出为 **¥8.5727146**：Qwen 搜索/证据 ¥0.1451600、Qwen 9 次 finalizer ¥2.9762106、阿里云 K3 三次有 usage 的响应 ¥5.4513440。**另有一次 K3 超时未返回 usage，是否计费未知；目录价估算不是实际扣款，必须以控制台账单为准。**

## 1. 验证方法、范围与限制

### 1.1 固定比较合同

- 艺人：KATSEYE，仅比较 2026-08-24 之后的未来演出。
- 任务边界：只测试“读取冻结证据并产出结构化事件”的 finalizer；搜索/证据构建单独计费、单独统计。
- 冻结证据：人工补齐官方缺口后共覆盖 32 场，证据内容 hash 为 `8341b49ba12d64abd1831b182214e4f96e79e0e78dd438978b5c1443c1e10a95`；文件 SHA-256 为 `2b5939890b33ca0ed10804c2aaa6d712b57b0528f694351990aa893fefacb6a5`。
- Qwen3.7-Flash 真实搜索阶段先得到 33 个来源（6 primary / 27 secondary；26 可抓取、5 受限、2 失败），覆盖 31/32 个演出身份，漏了 11 月 28 日 Mexico City。因此人工再用 Ticketmaster MX 官方独立活动页补齐；这是“搜索很便宜”不等于“搜索必然完整”的直接证据。
- 人工金标：`human_verified`，`as_of=2026-08-24`，profile 为 `katseye_finalizer_v1`；文件 SHA-256 为 `344c94f1ff78d9db08e888cfa0fa612a91b92c8583903ff4435bc74d87283abc`，绑定上述证据 hash。
- 核心字段：每场比较 `date/city/venue/event_type/tour_name/show_time/show_end_time`，即 32 × 7 = 224 个字段槽位；金标不评价 sale/price，避免把未经证据支持的售票状态或价格当成“正确答案”。
- Qwen 三个候选与 K3 在同一份冻结证据、同一提示词、同一 schema、同一金标上各跑 3 次；K3 超时/429 审计历史保留，续跑没有重复已计费的 R1。
- `valid` 只表示通过 JSON/schema/安全检查，并不等于质量合格。任何不可访问、非冻结证据中的来源 URL 都会使整轮 invalid；逐事件中改写或伪造 URL 的记录会被丢弃。
- 所有模型均使用供应商默认采样行为；不同模型的 reasoning 配置并不完全等价，因此本报告比较的是“实际可部署配置的端到端表现”，不是纯粹的学术模型能力排行。

### 1.2 单艺人 pilot 的边界

这次只覆盖 KATSEYE。它验证了多国家票务页、日期加场、doors/show time 混淆、官方来源冲突和 festival/tour 分类，但**不能代表生产中的全部 12 位艺人**，尤其不能覆盖中文票务、亚洲预售、多日音乐节、取消/延期、价格币种、传闻升级等所有情况。搜索完整率、12 艺人总成本和全流程发布稳定性也不能从一个艺人线性外推。

## 2. 线上旧快照核验

生产站点为 [concertmoniter.buaichiyu.com](https://concertmoniter.buaichiyu.com)。截至核验时，公开站点的数据 hash 与仓库旧快照一致：

| 项目 | 核验结果 |
|---|---:|
| 线上/本地 KATSEYE 未来事件 | 31 场 |
| 其中已有 `show_time` | 20 场 |
| `full_refresh_at` | 2026-08-13 11:51:26 |
| 最新数据提交 | `3b4465d`，2026-08-13 11:51（+08） |
| `origin/main` 核验时最新提交 | `9e1dcc4`，2026-08-13 12:15（+08） |
| 最新成功刷新 | [GitHub Actions #31662797705](https://github.com/2441461233/concert-moniter/actions/runs/31662797705)，2026-08-13 |
| 后续失败刷新 | [GitHub Actions #32222572647](https://github.com/2441461233/concert-moniter/actions/runs/32222572647)，2026-08-19，约 6 秒即因 429/账户余额或状态失败 |

因此，“昨晚已经把最好结果更新到平台”在公开数据层面**没有发生**：当时看到的是旧的 8 月 13 日数据快照被再次提供/部署，而不是 8 月 24 日新模型结果写入。这个判断来自数据内容与 hash，不依赖网页显示的部署时间。

## 3. 正确 32 场相对旧 31 场的关键差异

### 3.1 新增与分类修正

- **新增** 2026-11-28 Mexico City，Palacio de los Deportes，20:00，THE WILDWORLD TOUR；官方 Ticketmaster Mexico 独立事件页见附录。
- **Irvine 修正**：旧数据把 2026-08-29 写成 THE WILDWORLD TOUR、时间为空；金标依据 Daisy Chain Fields 官方日程和 Weverse 官方公告，改为 festival、`tour_name` 留空、场地归一为 Great Park、`show_time=14:15`、`show_end_time=14:45`。
- 旧 31 场中新增 7 个可证实时间：Irvine 14:15、Cologne 20:30、Antwerp 18:30、Copenhagen 20:30、Montreal 20:00、Hamilton 20:00、Mexico City 11/27 20:00；再加新增的 11/28 20:00，非空时间从 20 增至 28。
- 城市/场地只做可逆归一，如 `Belmont Park, NY → Belmont Park`、`Moody Center ATX → Moody Center`；不把这种展示名差异误判为新增/删除事件。

### 3.2 必须留空的四个 `show_time`

- London 9/3、9/4：可见的 18:30 是 doors，不足以证明艺人开演时间。
- Manchester 9/6：18:30 是 general admission/doors，不足以证明艺人开演时间。
- Amsterdam 9/11：Ticketmaster NL 标 20:00，而 Live Nation NL 标 `Show 20:30`，两个官方来源冲突；单一 `show_time` 字段无法无损表达，故严格金标留空。

Amsterdam 的空值不是漏标。K3/部分 Qwen 选择 20:30 会被记为字段错误，因为另一官方票务页同时给出 20:00。下一版 schema 应拆成 `ticket_time`、`doors_time`、`show_time`、`curfew/show_end_time`，并保留 `time_conflict` 及逐来源值。

## 4. Qwen 三模型逐轮结果

### 4.1 逐轮明细

目录价成本按返回 usage 与华北 2（北京）公开原价计算；时间为单次端到端延迟。`—` 表示该轮在基础校验阶段即 invalid，不能给它伪造召回率。

| 模型 / 轮次 | 基础状态 | 匹配召回 | 接受记录 | schema 丢弃 | 目录价估算 | 延迟 | 主要错误 |
|---|---|---:|---:|---:|---:|---:|---|
| Qwen3.7-Plus R1 | valid | 6/32 = 18.75% | 6 | 28 | ¥0.1811020 | 146.512s | 26 个事件 URL 无法绑定证据，另有 2 个 rumor 记录被丢弃；London/Amsterdam 时间误填 |
| Qwen3.7-Plus R2 | invalid | — | — | — | ¥0.1930620 | 166.105s | `source[5]` 不在可访问冻结证据中 |
| Qwen3.7-Plus R3 | invalid | — | — | — | ¥0.2102060 | 202.515s | `source[1]` 不在可访问冻结证据中 |
| Qwen3.7-Flash R1 | invalid | — | — | — | ¥0.0653298 | 49.680s | `source[7]` 不在可访问冻结证据中 |
| Qwen3.7-Flash R2 | valid | 5/32 = 15.625% | 5 | 29 | ¥0.0559410 | 41.757s | 27 个事件 URL 丢弃、2 个 rumor 丢弃；Irvine 分类和 London/Amsterdam 时间错误 |
| Qwen3.7-Flash R3 | valid | 30/32 = 93.75% | 31 | 1 | ¥0.0614538 | 57.582s | 漏 11/28；Copenhagen 丢失已知时间而无法匹配；Irvine 仍误分为 tour，Manchester/Amsterdam 猜时间；1 个 rumor 被丢弃 |
| Qwen3.8-Max R1 | valid | 12/32 = 37.50% | 12 | 20 | ¥1.1044800 | 187.381s | 20 个事件 URL 丢弃；London 两场把 23:00 curfew 当结束时间；Amsterdam 取 20:30 |
| Qwen3.8-Max R2 | valid | **32/32 = 100%** | 32 | 0 | ¥0.5166240 | 198.920s | 仍有 3/224 字段错误：London 两场错误写 `show_end_time=23:00`，Amsterdam 错选 20:30；核心字段准确率 98.6607%，不是全对 |
| Qwen3.8-Max R3 | valid | 31/32 = 96.875% | 32 | 0 | ¥0.5880120 | 236.789s | Paris 丢失 20:30 而成为未匹配记录；London×2、Manchester、Amsterdam 出现时间/结束时间误读；1 个 false confirmed |

Max R2/R3 各有 57,344 个缓存命中输入 token，因此目录价低于 R1；这只是本次返回的 usage 事实，不应当作每次必然缓存或固定折扣。

### 4.2 逐轮 token 与计费明细

`output` 为 provider 返回的总输出 token；Max 的 `reasoning` 是其中可单列的推理 token，不在费用公式中再重复相加。

| 模型 / 轮次 | input | cached input | output | reasoning⊆output | total | 计费公式（元/百万 token） | 目录价 |
|---|---:|---:|---:|---:|---:|---|---:|
| Plus R1 | 58,179 | 0 | 8,093 | 0 | 66,272 | 58,179×2 + 8,093×8 | ¥0.1811020 |
| Plus R2 | 58,179 | 0 | 9,588 | 0 | 67,767 | 58,179×2 + 9,588×8 | ¥0.1930620 |
| Plus R3 | 58,179 | 0 | 11,731 | 0 | 69,910 | 58,179×2 + 11,731×8 | ¥0.2102060 |
| Flash R1 | 58,179 | 0 | 12,676 | 0 | 70,855 | 58,179×0.6 + 12,676×2.4 | ¥0.0653298 |
| Flash R2 | 58,179 | 0 | 8,764 | 0 | 66,943 | 58,179×0.6 + 8,764×2.4 | ¥0.0559410 |
| Flash R3 | 58,179 | 0 | 11,061 | 0 | 69,240 | 58,179×0.6 + 11,061×2.4 | ¥0.0614538 |
| Max R1 | 58,203 | 0 | 11,279 | 3,442 | 69,482 | 58,203×12 + 11,279×36 | ¥1.1044800 |
| Max R2 | 58,203 | 57,344 | 11,675 | 4,398 | 69,878 | 859×12 + 57,344×1.5 + 11,675×36 | ¥0.5166240 |
| Max R3 | 58,203 | 57,344 | 13,658 | 3,805 | 71,861 | 859×12 + 57,344×1.5 + 13,658×36 | ¥0.5880120 |

实际执行窗口：Qwen 证据搜索于 14:54:38 开始、14:57:40 完成；9 次 Qwen finalizer 于 15:18:20–15:39:48 执行（Asia/Shanghai）。控制台对账可使用这些时间、模型名和 ApiKeyID 过滤。

### 4.3 三轮汇总

| 模型 | 有效轮次 | 有效轮次召回均值（范围） | 有效性调整后的规范化稳定度 | 三轮目录价 | 单轮均价 | 平均延迟 | 结论 |
|---|---:|---:|---:|---:|---:|---:|---|
| Qwen3.7-Plus（非思考） | 1/3 | 18.75%（18.75%–18.75%） | 0.333333 | ¥0.5843700 | ¥0.1947900 | 171.711s | rejected |
| Qwen3.7-Flash（非思考） | 2/3 | 54.6875%（15.625%–93.75%） | 0.060606 | **¥0.1827246** | **¥0.0609082** | **49.673s** | rejected |
| Qwen3.8-Max（低推理） | 3/3 | 78.125%（37.50%–100%） | 0.302687 | ¥2.2091160 | ¥0.7363720 | 207.697s | rejected |

三模型共 9 次 finalizer：目录价合计 **¥2.9762106**，若顺序执行，模型调用延迟合计 **1,287.242 秒（约 21 分 27 秒）**。稳定度同时惩罚 invalid 轮次与不同轮次的事件/字段不一致，越接近 1 越稳定；本轮三者均远未达到发布门槛。

### 4.4 为什么不能挑“最好的一次”上线

- 定时任务运行时没有人工金标，无法事先知道哪次是 Max R2、哪次是 Max R1；事后挑最好是利用答案的 cherry-pick，不是可部署策略。
- 三次都跑再择优会直接增加成本和延迟，而且仍需要一个更可靠的裁判模型或人工金标；否则只是把错误选择推迟一层。
- “事件召回 100%”不等于字段 100% 正确。Max R2 仍把 doors/curfew/冲突时间写成确定值，若自动发布会污染数据。
- Flash 从 15.625% 跳到 93.75%，Max 从 37.5% 跳到 100% 再回落到 96.875%，说明单次幸运结果不能代表稳定性。

## 5. Kimi K3 三轮完整结果

本次实际可调用的是阿里云百炼部署 `kimi-k3`、高推理配置，通过 DashScope/百炼 API 路由。它**不是**现有生产所用的 Moonshot 原生 K3 High 的完全等价对照；供应商、路由、隐式推理上限与服务行为可能不同。另一路径 `kimi/kimi-k3` 在百炼返回 HTTP 400“未开通/未激活”，没有模型响应，估算成本为 ¥0。

| 轮次 | 状态 | usage | 目录价估算 | 延迟 | 质量结果 |
|---|---|---|---:|---:|---|
| R1 | valid | input 53,135；cached 0；output 9,692；reasoning 534；total 62,827 | ¥2.0319000 | 296.684s | 接受 13、匹配 12，召回 37.5%，primary source ratio 46.1538%，19 个 schema drops，1 个 false confirmed |
| R2（续跑返回） | **invalid** | input 53,135；cached 52,992；output 12,604；reasoning 3,620；total 65,739 | ¥1.3692440 | 530.693s | 返回了付费响应，但 `research` 缺少 events/rumors/sources/coverage 必填字段，不可评分也不可上线 |
| R3 | valid | input 53,135；cached 0；output 9,875；reasoning 3,259；total 63,010 | ¥2.0502000 | 480.729s | 32/32 召回，但 224 个核心字段中仍错 4 个：London 两场和 Manchester 把 18:30 doors 当 show，Amsterdam 在冲突证据中猜 20:30；primary source ratio 仅 6.25% |

K3 计费复算（元/百万 token）：R1 = `(53,135×20 + 9,692×100)/1e6 = ¥2.031900`；R2 = `(143×20 + 52,992×2 + 12,604×100)/1e6 = ¥1.369244`；R3 = `(53,135×20 + 9,875×100)/1e6 = ¥2.050200`。reasoning 已包在 output token 中，不重复加价。

R1 的 12/32 召回并不表示模型只“看见”12场；它的原始结构里有 32 场，但模型重写/生成了不在证据白名单中的 URL，严格验证器只能安全丢弃 19 场。这说明主要工程故障之一是“让模型复制 URL”，不是单纯搜索能力不足。

## 6. K3 稳定性、服务可用性与 Qwen 对比

K3 三个有 usage 的响应合计 input 159,405、cached input 52,992、output 32,171（其中 reasoning 7,413），目录价 **¥5.4513440**。三调用平均 ¥1.8171147、平均延迟 436.035s；仅 2/3 valid，有效轮召回均值 68.75%（37.5%–100%），有效性调整后的规范化稳定度仅 0.047619，最终 **rejected**。

| 模型 | valid | 有效轮召回均值（范围） | 规范化稳定度 | 单次均价 | 平均延迟 | 结论 |
|---|---:|---:|---:|---:|---:|---|
| Qwen3.7-Flash | 2/3 | 54.6875%（15.625%–93.75%） | 0.060606 | **¥0.0609082** | **49.673s** | rejected；最便宜但波动太大 |
| Qwen3.7-Plus | 1/3 | 18.75% | 0.333333 | ¥0.1947900 | 171.711s | rejected；本轮无性价比优势 |
| Qwen3.8-Max | **3/3** | **78.125%（37.5%–100%）** | **0.302687** | ¥0.7363720 | 207.697s | rejected；四者中完整性最好 |
| 阿里云 Kimi K3 | 2/3 | 68.75%（37.5%–100%） | 0.047619 | ¥1.8171147 | 436.035s | rejected；更贵、更慢、也不稳定 |

可用性审计还保留了两次非评分尝试：原 R2 在约 300s 后 read timeout，无 usage，可能已计费；第一次续跑立即返回 HTTP 429，无模型响应/usage。第二次续跑才完成逻辑 R2/R3。完整 K3 执行窗口为 15:49:08–17:25:40，续跑窗口为 17:08:48–17:25:40（Asia/Shanghai）。

相对 K3，Max 的平均单次目录价低约 59.5%、平均延迟低约 52.4%，且 valid rate 更高；但 Max 仍未过字段准确性和稳定性门禁。因此结论是“当前不自动用任何模型”，而不是“盲选 Max”。

## 7. 官方目录价与本次已知支出

### 7.1 华北 2（北京）公开原价

单位均为人民币元/百万 tokens；官方页面明确说明这些是原价，不含限时优惠，实际扣款以百炼控制台为准。

| 模型 | 输入区间 | 输入 | 缓存命中输入 | 输出 | 官方页 |
|---|---:|---:|---:|---:|---|
| Qwen3.7-Flash | ≤32K | ¥0.2 | ¥0.04 | ¥0.8 | [阿里云官方](https://help.aliyun.com/zh/model-studio/qwen3-7-flash) |
| Qwen3.7-Flash | 32K–256K | ¥0.6 | ¥0.12 | ¥2.4 | [阿里云官方](https://help.aliyun.com/zh/model-studio/qwen3-7-flash) |
| Qwen3.7-Flash | 256K–1M | ¥1.2 | ¥0.24 | ¥4.8 | [阿里云官方](https://help.aliyun.com/zh/model-studio/qwen3-7-flash) |
| Qwen3.7-Plus | ≤256K | ¥2 | ¥0.4 | ¥8 | [阿里云官方](https://help.aliyun.com/zh/model-studio/qwen3-7-plus) |
| Qwen3.7-Plus | 256K–1M | ¥6 | ¥1.2 | ¥24 | [阿里云官方](https://help.aliyun.com/zh/model-studio/qwen3-7-plus) |
| Qwen3.8-Max | 全档 | ¥12 | ¥1.5 | ¥36 | [阿里云官方](https://help.aliyun.com/zh/model-studio/qwen3-8-max) |
| 阿里云 `kimi-k3` | 全档 | ¥20 | ¥2 | **¥100** | [阿里云官方](https://help.aliyun.com/zh/model-studio/aliyun-kimi-k3) |

联网搜索是额外计费项。Qwen 本次采用 agent 搜索策略，公开价为 ¥4/千次，即 ¥0.004/次；见[阿里云联网搜索文档](https://help.aliyun.com/zh/model-studio/web-search/)。Moonshot 原生 Kimi 的 `$web_search` 每次实际触发收 ¥0.03，搜索结果 token 在后续模型调用中仍计 token 费；见 [Kimi 官方联网搜索定价](https://platform.kimi.com/docs/pricing/tools)。两条路由不可混为同一个价格。

### 7.2 Qwen 证据搜索逐类明细

| 搜索类别 | input | output | 实际搜索次数 | token 档位 | 目录价 |
|---|---:|---:|---:|---|---:|
| ticketing | 24,004 | 1,396 | 3 | Flash ≤32K | ¥0.017918 |
| official | 5,760 | 1,302 | 1 | Flash ≤32K | ¥0.006194 |
| china_region | 123,789 | 1,374 | 8 | Flash 32K–256K | ¥0.109571 |
| rumors | 12,372 | 1,253 | 2 | Flash ≤32K | ¥0.011477 |
| **合计** | **165,925** | **5,325** | **14** | — | **¥0.145160** |

### 7.3 本次目录价估算与控制台账单分离

| 阶段 | 实际调用 | usage 目录价估算 | 控制台实际账单 |
|---|---|---:|---|
| Qwen 证据搜索 | 4 个高层查询，内部实际触发 14 次搜索；Qwen3.7-Flash | ¥0.1451600（其中单纯工具费 14×¥0.004=¥0.056，其余为输入/输出 token） | 未核对/待填 |
| Qwen finalizer | 3 模型 × 3 轮 = 9 次 | ¥2.9762106 | 未核对/待填 |
| 阿里云 K3 评分响应 | 3 次有 usage 的响应（其中 1 次 schema invalid） | ¥5.4513440 | 未核对/待填 |
| 阿里云 K3 原 R2 | 超时、未返回 usage | **未知，不能记为 ¥0** | **必须查账单** |
| 阿里云 K3 续跑限流 | HTTP 429，无模型响应/usage | ¥0 的目录价假设 | 待控制台确认无扣款 |
| 百炼 `kimi/kimi-k3` 未激活路由 | HTTP 400，无模型响应 | ¥0 | 待控制台确认无扣款 |
| **可核算小计** | 不含 K3 超时的未知账单 | **¥8.5727146** | **当前未知，不能用左栏替代** |

其中 Qwen 搜索 + Qwen finalizer 的目录价小计为 **¥3.1213706**。目录价估算来自返回 usage 和公开费率，可能因促销、免费额度、缓存记账、超时后服务端继续执行、账单粒度或四舍五入而与控制台扣款不同。百炼文档说明推理账单通常在调用后 2–10 分钟出现；请在控制台按 ApiKeyID、上述时间窗口和模型过滤对账。

## 8. 为什么原方案一次刷新可能花十几元甚至更多

当前生产思路不是“只搜一次然后让 K3 写几句总结”，而是按艺人独立执行搜索、长证据汇总和结构化 finalizer：

- 12 位艺人 × 4 类搜索组，基线就是约 48 组搜索；若使用 Moonshot 原生 Kimi 且每组触发一次，单纯工具费下限约 48×¥0.03=¥1.44，尚未包含任何 token。
- 每位艺人的 finalizer 会收到多个搜索正文、最多约 40 条旧事件和 25 条传闻；这不是一个无限增长的全局会话，但每位艺人的单次上下文仍可能达到数万 token。
- K3 的输出价远高于输入价。阿里云 K3 公开输出价为 ¥100/百万 token；12 位艺人若每位产生约 8K 输出/推理 token，仅输出侧示意值就约 ¥9.60。
- 验证失败后的重试同样会计 token；超时不代表供应商一定没有完成和计费。
- 本次 KATSEYE 单次阿里云 K3 R1 的目录价已经是 ¥2.0319。机械乘以 12 得 ¥24.3828，只能作为“为什么风险很高”的量级示意，**不是正式预测**，因为各艺人的证据长度、缓存命中和输出长度不同。

所以高费用的主因是“全量重复搜索 + 长上下文 + 昂贵输出/推理 + 重试”，而不仅是最终摘要文字的长度。若搜索已经拿到可靠官方结构，很多字段根本不需要大模型再读写一次。

## 9. 推荐的新架构

1. **官方优先直采**：Ticketmaster/Live Nation/场馆/艺人公告做定向 collector；广域搜索只用于发现新 URL，降低频率。
2. **证据仓与内容 hash**：保存来源正文、抓取时间、HTTP 状态、内容 hash 和 source ID；无变化的艺人直接跳过 finalizer。
3. **确定性事件主键与归一化**：日期、城市别名、场馆别名先用代码合并，模型不负责事件去重主键。
4. **模型只返回 `source_id`**：URL 由程序从白名单映射回去，禁止模型复制或改写 URL。这直接消除本轮最主要的 schema drops。
5. **Flash 只做未来增量候选**：Qwen3.7-Flash 可影子抽取变化过的证据或少量新记录；它便宜且快，但本轮已 rejected，在更大样本过门禁前不是生产默认。
6. **Max 条件升级**：只有来源冲突、复杂日期/时间、Flash schema 失败或字段不一致时才调用 Qwen3.8-Max；不要每位艺人都跑 Max。
7. **K3 稀有仲裁**：仅在 Max 后仍有冲突时进行跨供应商复核；当前数据不支持把 K3 设为默认模型。
8. **时间字段拆分**：`doors_time`、`ticket_time`、`show_time`、`curfew`、`show_end_time` 分开，并记录冲突来源；证据冲突时宁可空值也不猜。
9. **差量与缓存**：固定提示前缀、来源摘要和旧事件快照，只有 hash 变化才重新抽取；缓存命中是优化，不写成成本保证。
10. **预算与遥测**：每次调用、每艺人、整批三层预算；记录 input/cached/output/reasoning、搜索次数、超时是否产生账单；重试次数有上限。
11. **严格门禁与原子发布**：事件召回、primary evidence、0 URL drops、关键字段正确率、重复稳定度全部过线才生成候选快照；整批验证成功后一次性替换，失败保留旧站并支持回滚。
12. **扩大 shadow test**：先覆盖至少数个差异明显的艺人和取消/延期/中文票务样本，再决定生产默认模型。

如果现在必须给模型排序：**默认增量抽取优先 Flash，复杂项升级 Max，K3 不做默认**。如果被迫只选一个模型完成当前固定 finalizer，Max 的完整率最高，但本轮稳定性和字段错误仍不足以授权无人值守发布。

## 10. 本轮为什么不发布

- 只是单艺人 pilot，不能证明 12 艺人的端到端稳定性。
- Qwen 三个候选全部未通过 hard gates；最好单轮仍有关键时间字段错误。
- K3 只有 2/3 valid，有效轮召回从 37.5% 到 100%，服务还出现超时与 429，且阿里云部署不等价于现有 Moonshot 原生生产配置。
- 当前主要失败来自 URL 绑定设计；在改为 `source_id → URL` 的确定性映射前，换模型仍可能大量丢记录。
- 本次评分只覆盖 finalizer；未把 12 艺人真实检索召回、全流程成本、取消/延期和 sale/price 正确性纳入验收。
- 人工金标明确不包含 sale/price，不能用本轮结果宣称这些字段已验证。

因此报告生成时保持生产数据与站点快照不变，不 commit、不 push、不 deploy；本地程序已按下述架构重新设计。旧线上快照虽不完整，但比未经门禁的新模型结果更可恢复。

## 11. 重新设计后的真实无 Key 刷新

在一份隔离仓库副本中，显式移除 Moonshot/DashScope/Qwen/OpenAI Key，以 `LLM_ENRICH_PROVIDER=none` 运行了真实的 12 艺人全量刷新（最终代码复跑完成时间 2026-08-24 17:17:13）：

| 验证项 | 真实结果 |
|---|---|
| 进程结果 | 退出 0，`deterministic_completed_enrichment_stale` |
| ShowStart | 3 位有确定性主键的艺人全部成功；沙一汀 12 场、门尼 1 场、加木 12 场 |
| 付费调用 | Formula 0，Chat 0，candidate 0，估算成本 ¥0 |
| 旧 research | 原样保留 73 条，其中 KATSEYE 31 条；诚实标记为 `disabled_stale` |
| 生产树 | 主仓 `data/`/`site/data.*`/`research/` 无差异；没有 commit/push/deploy |

该次确定性刷新在隔离副本中发现两条相对旧快照的新演出：

| 艺人 | 日期/时间 | 城市/场馆 | 演出 | 状态 |
|---|---|---|---|---|
| 加木 | 2026-09-13 20:00 | 广州 / MAO Livehouse广州（太古仓店） | 掂过碌蔗-yamy2026巡演广州站 | on_sale |
| 沙一汀 | 2026-09-26 13:30 | 成都 / 国际非物质文化遗产博览园 | 2026成都葫芦果音乐节 | on_sale |

这次验证证明“没钱也能刷新”不是纸面方案：确定性新数据可正常产生，模型只是显式 opt-in 的仓外 candidate，不得自动覆盖生产。异常已付费响应会进 telemetry，固定价格表超过 30 天会阻断付费层，ShowStart 非法时刻会 fail closed。最终回归为 **144/144** 测试通过，全仓 Python 编译和 `git diff --check` 通过。

## 附录 A：人工核验的 32 场正确结果

说明：`—` 表示金标有意留空；除 Irvine 外，tour name 均为 `THE WILDWORLD TOUR`；所有日期/时间为场馆当地日期/时间。每行链接均为能语义支持该事件的官方票务、场馆、主办方或艺人平台页面。

| # | 日期 | 城市 | 场馆 | 类型 | tour name | show | end | 主要官方证据 |
|---:|---|---|---|---|---|---:|---:|---|
| 1 | 2026-08-29 | Irvine | Great Park | festival | — | 14:15 | 14:45 | [Daisy Chain Fields schedule](https://www.daisychainfields.com/schedule)、[Weverse 公告](https://weverse.io/katseye/notice/36941) |
| 2 | 2026-09-01 | Dublin | 3Arena | tour | THE WILDWORLD TOUR | 18:30 | — | [Ticketmaster IE](https://www.ticketmaster.ie/katseye-the-wildworld-tour-dublin-01-09-2026/event/180064AFAB8713FC) |
| 3 | 2026-09-03 | London | The O2 | tour | THE WILDWORLD TOUR | — | — | [Ticketmaster UK](https://www.ticketmaster.co.uk/katseye-the-wildworld-tour-london-03-09-2026/event/350064A893811E60)、[The O2](https://www.theo2.co.uk/events/detail/katseye) |
| 4 | 2026-09-04 | London | The O2 | tour | THE WILDWORLD TOUR | — | — | [Ticketmaster UK](https://www.ticketmaster.co.uk/katseye-the-wildworld-tour-london-04-09-2026/event/350064ADB7514235)、[The O2](https://www.theo2.co.uk/events/detail/katseye) |
| 5 | 2026-09-06 | Manchester | Co-op Live | tour | THE WILDWORLD TOUR | — | — | [Ticketmaster UK](https://www.ticketmaster.co.uk/katseye-the-wildworld-tour-manchester-06-09-2026/event/370064A8CEFC63DD)、[Co-op Live](https://www.cooplive.com/events/katseye-fjfq) |
| 6 | 2026-09-09 | Paris | Accor Arena | tour | THE WILDWORLD TOUR | 20:30 | — | [Accor Arena](https://www.accorarena.com/en/events-and-tickets/katseye-the-wildworld-tour--e0745) |
| 7 | 2026-09-11 | Amsterdam | Ziggo Dome | tour | THE WILDWORLD TOUR | — | — | [Ticketmaster NL：20:00](https://www.ticketmaster.nl/event/katseye-the-wildworld-tour-tickets/611093913)、[Live Nation NL：Show 20:30](https://www.livenation.nl/en/event/katseye-the-wildworld-tour-amsterdam-tickets-edp1673721) |
| 8 | 2026-09-13 | Cologne | Lanxess Arena | tour | THE WILDWORLD TOUR | 20:30 | — | [Ticketmaster DE](https://www.ticketmaster.de/event/katseye-the-wildworld-tour-tickets/212333631)、[Lanxess Arena](https://www.lanxess-arena.de/eventdetail/1390) |
| 9 | 2026-09-15 | Antwerp | AFAS Dome | tour | THE WILDWORLD TOUR | 18:30 | — | [Ticketmaster BE](https://www.ticketmaster.be/event/katseye-the-wildworld-tour-tickets/1384692019) |
| 10 | 2026-09-17 | Copenhagen | Royal Arena | tour | THE WILDWORLD TOUR | 20:30 | — | [Ticketmaster DK](https://www.ticketmaster.dk/event/katseye-the-wildworld-tour-billetter/1691215729) |
| 11 | 2026-10-13 | Miami | Kaseya Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-miami-florida-10-13-2026/event/0D0064ABEED4E240) |
| 12 | 2026-10-15 | Atlanta | State Farm Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-atlanta-georgia-10-15-2026/event/0E0064ABD8F32445) |
| 13 | 2026-10-20 | Charlotte | Spectrum Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-charlotte-north-carolina-10-20-2026/event/2D0064ABE62DF032) |
| 14 | 2026-10-22 | Washington, DC | Capital One Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-washington-district-of-columbia-10-22-2026/event/150064A5EBAC0D86) |
| 15 | 2026-10-24 | Belmont Park | UBS Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-belmont-park-new-york-10-24-2026/event/300064AB9AB430EA) |
| 16 | 2026-10-25 | Belmont Park | UBS Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-belmont-park-new-york-10-25-2026/event/300064AB9AB930F5) |
| 17 | 2026-10-28 | Boston | TD Garden | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-boston-massachusetts-10-28-2026/event/010064A9ECAFEAEC) |
| 18 | 2026-10-30 | Montreal | Bell Centre | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster CA](https://www.ticketmaster.ca/katseye-the-wildworld-tour-montreal-quebec-10-30-2026/event/310064A8A00972D3) |
| 19 | 2026-11-01 | Hamilton | TD Coliseum | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster CA](https://www.ticketmaster.ca/katseye-the-wildworld-tour-hamilton-ontario-11-01-2026/event/100064AB07DF2C82) |
| 20 | 2026-11-03 | Detroit | Little Caesars Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-detroit-michigan-11-03-2026/event/080064A9C8CDBB55) |
| 21 | 2026-11-05 | Chicago | United Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-chicago-illinois-11-05-2026/event/040064ABAC947B12) |
| 22 | 2026-11-07 | Minneapolis | Target Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-minneapolis-minnesota-11-07-2026/event/060064ACF981D91C) |
| 23 | 2026-11-10 | Austin | Moody Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-austin-texas-11-10-2026/event/3A0064AAC1419703) |
| 24 | 2026-11-11 | Dallas | American Airlines Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-dallas-texas-11-11-2026/event/0C0064A99EAF7251) |
| 25 | 2026-11-14 | Las Vegas | MGM Grand Garden Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/event/Z7r9jZ1A70OuV) |
| 26 | 2026-11-17 | Seattle | Climate Pledge Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-seattle-washington-11-17-2026/event/0F0064A9D030C333) |
| 27 | 2026-11-19 | Oakland | Oakland Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-oakland-california-11-19-2026/event/1C0064ABA10BD29D) |
| 28 | 2026-11-21 | Los Angeles | Crypto.com Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-los-angeles-california-11-21-2026/event/2C0064ABC62217BE) |
| 29 | 2026-11-22 | Los Angeles | Crypto.com Arena | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-los-angeles-california-11-22-2026/event/2C0064B3CEC7151F) |
| 30 | 2026-11-24 | Phoenix | Mortgage Matchup Center | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster](https://www.ticketmaster.com/katseye-the-wildworld-tour-phoenix-arizona-11-24-2026/event/190064AB95A545E9) |
| 31 | 2026-11-27 | Mexico City | Palacio de los Deportes | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster Mexico](https://www.ticketmaster.com.mx/katseye-the-wildworld-tour-ciudad-de-mexico-27-11-2026/event/140064AB8D982D1C)、[Live Nation 新闻稿](https://newsroom.livenation.com/news/katseye-announces-2026-headline-arena-tour-of-uk-eu-and-north-america/) |
| 32 | 2026-11-28 | Mexico City | Palacio de los Deportes | tour | THE WILDWORLD TOUR | 20:00 | — | [Ticketmaster Mexico](https://www.ticketmaster.com.mx/katseye-the-wildworld-tour-ciudad-de-mexico-28-11-2026/event/140064AB8F152FA7) |

补充聚合入口：[Live Nation KATSEYE 事件聚合](https://www.livenation.asia/katseye-tickets-adp1609340)、[Ticketmaster KATSEYE 艺人页](https://www.ticketmaster.com/katseye-tickets/artist/3259344)。它们用于交叉检查巡演范围，单场字段仍以附录中的对应事件/场馆页为准。

## 附录 B：审计产物位置

这些是本机临时验证产物，用于复核本报告，不是生产输入：

- 冻结证据：`/tmp/concert-model-validation.3hLXWR/manual_augmentation_v3/evidence.json`
- 人工金标：`/tmp/concert-model-validation.3hLXWR/manual_augmentation_v3/human_verified_gold.json`
- Qwen 逐轮：`/tmp/concert-model-validation.3hLXWR/qwen_finalizer_20260824/runs.json`
- Qwen 汇总：`/tmp/concert-model-validation.3hLXWR/qwen_finalizer_20260824/scorecard.json`
- 阿里云 K3 完整批次：`/tmp/concert-model-validation.3hLXWR/aliyun_kimi_k3_finalizer_20260824/`

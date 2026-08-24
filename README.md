# 演唱会监控器

聚合关注艺人的演出、开票与舆情线索，页面按 **正在售卖**、**已官宣 / 待开票**、**舆情监控** 和 **已结束** 展示。

生产地址：[`concertmoniter.buaichiyu.com`](https://concertmoniter.buaichiyu.com)

GitHub：[`2441461233/concert-moniter`](https://github.com/2441461233/concert-moniter)

## 手动刷新现在做什么

网页右上角的刷新按钮执行一轮**分层刷新**，不是重新加载页面。可发布的确定性主链不依赖
LLM Key 或余额；联网 LLM 只是显式 opt-in 的仓外 candidate 层：

```text
管理员点击刷新并输入口令
        │
        ▼
Vercel /api/refresh
  · 校验同源请求和管理员口令
  · 通过 GitHub API 创建或复用 full-refresh 工作流
        │
        ▼
GitHub Actions（最长 90 分钟）
  · 读取 config/artists.json 中全部 enabled 艺人
  · 确定性层：强制重新采集 ShowStart；任一应采源失败即停止
  · 可选 candidate：默认关闭；显式选择 Kimi 也只写 runner temp artifact
  · 只合并 ShowStart 确定性结果、构建 site/data.js 与 site/data.json
  · 如实写入分层状态并提交确定性快照到 main
        │
        ▼
Vercel 根据 main 的新提交创建生产部署
        │
        ▼
网页确认本次 full_refresh_id 已上线，自动载入新数据
```

Vercel 在这里是轻量调度器，不承担采集或联网调研。任务一旦进入 GitHub Actions，即使关闭网页或本地电脑关机也会继续执行。网页每 5 秒查询一次状态；重开同一浏览器后会继续跟踪原任务。执行窗口最长 90 分钟，Actions 完成后再给 Vercel 15 分钟独立发布窗口；超时只停止高频轮询，不会忘记后台任务或重复扣费。

同一时间 GitHub API 已能看到完整刷新任务时，再次点击会接入该任务。GitHub Actions 还配置了单任务并发锁，不会并行写同一份数据；在极少数调度竞态下，第二个请求可能进入等待队列，但不会并发发布。

workflow 不使用 `continue-on-error` 包住刷新脚本。脚本只会把**可选 enrich** 的失败转换为
退出码 0，并先在脱敏 telemetry 中记录 `stale`/`failed`；ShowStart、合并、构建或元数据
写入等确定性故障仍返回非 0，后续提交步骤不会运行。

### 两层的覆盖范围

确定性层强制重新请求每个应采艺人的 ShowStart 数据。任一应采源失败都会在 merge、build 和
`full_refresh_id` 写入前中止；采集、合并或构建的故障返回非 0，不允许 workflow 越过失败提交。
`region: "kpop"` 且没有 `showstart_artist_id` 的艺人没有可采的 ShowStart 主键，会明确跳过，
而不是伪报采集成功。

可选 candidate 默认 provider 为 `none`，不会产生付费请求。显式选择 `kimi`，且 Key 与整批预算
预检允许时，才会对
`config/artists.json` 中全部启用艺人调用 Kimi 官方 `moonshot/web-search:latest`
Formula，覆盖四类信息：

1. 票务平台：大麦、秀动、票星球、猫眼、摩天轮、Cityline、拓元、Interpark/NOL、Ticketmaster、Live Nation 等公开页面。
2. 官方渠道：艺人、事务所、Weverse、官方微博/X、主办方、场馆公告，包含 fanclub presale 与公售时间。
3. 中国内地及港澳台：新增站、加场、补票、取消；KPop 艺人同时复核官方亚洲及世界巡演安排。
4. 近期舆情：只保留与未来演出或开票有关、仍可能变化的线索，并标记可信度。

现有记录只作为待复核线索，不会被当成新事实直接复制。代码会为每位艺人分别执行票务、
官方、中国区域/完整巡演、舆情四个查询并记录 category、query 和 Fiber ID。某位艺人的
Formula、结构化汇总、来源校验或余额检查失败时，candidate fail closed。
即使成功，也固定记为 `candidate_ready_stale`：产物位于仓库外，不进
`research/inbox`、不自动 merge，线上旧 research 始终保留。

#### 固定来源引用

程序从 Formula 返回的结构化 reference/source 目录提取原始 URL，并按规范化 URL 生成稳定的
`src_<SHA-256 前 16 位>`。模型只能给每条 event/rumor 输出一个 `source_id`，不能自行输出
URL 或重写来源目录；未知 `source_id` 会让该艺人的 enrich 整体 fail closed。程序随后对固定
引用只做被动的 HTTP(S) URL 语法/主机边界检查后回填原 URL。候选校验不会为任意
provider URL 发 HEAD/GET；可达性与页面是否支持每个字段都待人工复核。若
provider 不返回可枚举的引用目录，本轮 candidate 会安全标为 failed。

#### 场次时间

时间字段各有唯一含义，格式均为 `HH:MM`：`doors_time` 是入场/开门时间，`show_time`
是正式开演时间，`show_end_time` 是演出实际结束时间，`curfew_time` 是场馆或许可要求的最晚
结束时间。来源含义不清或互相冲突时留空；严禁把 doors 填进 `show_time`，也严禁把 curfew
填进 `show_end_time`。

迁移采用零猜测策略：已有记录的旧 `show_time` 原样保留，不从旧 note 推断或启发式重分类为
doors/end/curfew；新四字段只有在来源明确时才写入。本轮不做一次性生成数据迁移，历史纠错必须
等人工核验后单独处理。

#### merge-only

确定性刷新只把 ShowStart 新记录或明确更新合并进现有库存。本轮未命中的旧演出/舆情与
research 记录都不会因为“缺席”被删除，也不会冒充本轮重新验证；日期推进仍可把演出转为已结束，
真正清理只能走显式 `prune`。这避免一次漏搜、余额不足或 provider 故障抹掉线上历史。

### 前端更新时间

页面右上角会显示：

```text
确定性快照生成于 2026-08-12 20:24 · 6分钟前
```

这个时间来自 `site/data.json` 的 `full_refresh_at`，只表示本次确定性 ShowStart、合并和构建
已经成功形成可发布快照；普通 `monitor.py check`、单独 `ingest` 或仅重新生成页面不会冒充
按钮刷新。它不再暗示可选 LLM enrich 必然成功。

`full_refresh_status` 只表达当前 candidate-only 主链的两种生产结果：

- `deterministic_completed_enrichment_stale`：确定性层完成；模型关闭、未生成候选或候选已写仓外，线上 research 未变。
- `deterministic_completed_enrichment_failed`：确定性层完成，但候选请求/校验/写 artifact 失败；线上 research 仍未变。

旧数据中的 `deterministic_completed_enrichment_completed` 只是兼容显示，当前
`scripts/full_refresh.py` 不会产生该状态。

严格生产路径不接受 ShowStart source warning；`_with_source_warnings` 后缀只用于防御旧的或
异常 metadata，正常刷新不应产生。不得把 enrich
成功状态或 `full_refresh_at` 当作“所有来源均已复核”。确定性层若整体失败则不写新的成功
`full_refresh_status`，也不提交半成品。merge-only 不会因为本轮缺席而批量改写旧记录的历史
验证字段；只有与当前刷新 ID/时间明确绑定的字段才能证明本轮命中，不能拿旧的裸
`verification_status` 推断本轮 enrich 已复核。

`full_refresh_id` 对应本次按钮任务。网页只有看到这个 ID 随新部署上线，才会宣布完成并重载，因此 GitHub Actions 完成但 Vercel 尚未发布时，页面仍显示“正在发布”。最新时间较近时没有必要反复点击。

完整刷新产生的“本轮降级提示”只在真正创建该任务的浏览器中展示：页面会在任务成功发布后，把该次 `full_refresh_id` 作为不含密钥的本地完成标记保存；其他访客、只接入已有任务的浏览器以及标记不匹配的新一轮页面都不会显示这块验收信息。演出与舆情仍是全站共享数据；这项限制仅针对刷新执行结果提示。

艺人筛选标签右侧的数字只统计当前活跃的演唱会场次（正在售卖 + 待开票），不包含舆情条数，也不包含已结束场次。点击艺人标签后仍会同时筛选该艺人的演唱会和舆情内容。

## 首次配置

完整刷新涉及两组服务端配置。不要把任何密钥写进仓库或前端代码。

### 1. GitHub 配置

在仓库 **Settings → Secrets and variables → Actions** 中配置：

| 类型 | 名称 | 必需 | 用途 |
| --- | --- | --- | --- |
| Repository secret | `MOONSHOT_API_KEY` | 否 | 可选 Kimi enrich；缺失、无效或余额不足不阻断确定性刷新 |
| Repository variable | `LLM_ENRICH_PROVIDER` | 否 | `none` 或 `kimi`；默认 `none`，必须显式 opt-in 才付费 |
| Repository variable | `LLM_ENRICH_MAX_COST_CNY` | 否 | enrich 全局最坏费用上限；默认 `5` 元 |
| Repository variable | `LLM_ENRICH_MAX_COST_PER_ARTIST_CNY` | 否 | enrich 单艺人最坏费用上限；默认 `5` 元 |
| Repository variable | `LLM_ENRICH_MAX_CONTEXT_BYTES` | 否 | 单次汇总请求的 UTF-8 字节上限；默认 `128000` |
| Repository variable | `LLM_ENRICH_ATTEMPTS` | 否 | 每个付费阶段的应用层尝试数；默认且生产要求为 `1` |
| Repository variable | `KIMI_RESEARCH_MODEL` | 否 | 调研模型；未设置时使用 `kimi-k3` |

选择 `kimi` 后，预算预检按所有启用艺人、四次 Formula、最大上下文和最大输出做最坏预留，
并在首次付费请求前一次性通过。以当前 12 位艺人和默认上限，整批会被全局 5 元上限拦截并
记为 `budget_blocked_stale`；确定性快照照常构建和提交。需要真的运行全员 Kimi 时，应先
根据 telemetry/控制台基线显式设置经过审核的上限，不能靠重试绕过预算。预留依赖已
标注日期的公开价格表；价格表超过 30 天未复核时付费层会 fail closed，确定性刷新不受影响。

权限要求：

- 仓库必须允许 GitHub Actions 运行。
- [`.github/workflows/full-refresh.yml`](.github/workflows/full-refresh.yml) 已声明 `contents: write`，使用 Actions 自动提供的 `GITHUB_TOKEN` 提交刷新快照，不需要再给工作流配置一个 GitHub PAT。
- 如果 `main` 有分支保护，需允许 `github-actions[bot]` 直接写入，或相应调整发布流程；否则调研会完成但推送步骤会失败。
- 工作流运行期间若 `main` 有新提交，快照 push 会以 non-fast-forward 失败并要求重跑，不会将基于旧配置生成的数据 rebase 后冒充完整快照。

### 2. Vercel 配置

在 Vercel 项目 **Settings → Environment Variables** 中配置：

| 名称 | 必需 | 用途 |
| --- | --- | --- |
| `GITHUB_ACTIONS_TOKEN` | 是 | Vercel 查询并触发 GitHub Actions |
| `REFRESH_SECRET` | 是 | 网页按钮的管理员刷新口令 |
| `GITHUB_REPOSITORY` | 否 | 默认 `2441461233/concert-moniter`；仅 fork/迁移时修改 |
| `GITHUB_WORKFLOW_FILE` | 否 | 默认 `full-refresh.yml`；仅工作流改名时修改 |

`GITHUB_ACTIONS_TOKEN` 推荐使用 fine-grained personal access token：

- Repository access 只选择 `2441461233/concert-moniter`。
- Repository permissions 将 **Actions** 设为 **Read and write**；Metadata 的只读权限会自动包含。
- 设置合理的有效期，到期前轮换。它只用于查询运行记录和调用 `workflow_dispatch`，不用于提交代码。

`REFRESH_SECRET` 使用独立的高强度随机口令，例如：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

至少配置到 Production 环境；如果需要在预览部署测试，也配置到 Preview。环境变量新增或修改后要重新部署，旧 Deployment 不会自动获得新变量。

点击按钮时口令通过同源 `Authorization: Bearer …` 请求发送，只保存在当前标签页的 `sessionStorage`；它不是 Moonshot Key，也不会出现在站点数据中。Vercel 为每个任务返回独立 HMAC 只读状态令牌；`localStorage` 只保存该任务的状态 URL，不保存管理员口令，也不能触发新任务。

## 本地运行

项目运行代码只依赖 Python 3 标准库。

### 完整刷新

```bash
python3 scripts/full_refresh.py
```

默认 `LLM_ENRICH_PROVIDER=none`，上面的命令只跑确定性层。显式 opt-in Kimi 时再安全读入 Key；
隐藏输入不会把 Key 写进 zsh history，完成后执行 `unset MOONSHOT_API_KEY`：

可选参数和环境变量：

```bash
# 显式启用 Kimi；预算须能容纳整批最坏预留，否则首次付费调用前降级
read -rs "MOONSHOT_API_KEY?Moonshot API Key: "; printf '\n'; export MOONSHOT_API_KEY
LLM_ENRICH_PROVIDER='kimi' \
LLM_ENRICH_MAX_COST_CNY='20' \
LLM_ENRICH_MAX_COST_PER_ARTIST_CNY='5' \
LLM_ENRICH_MAX_CONTEXT_BYTES='128000' \
LLM_ENRICH_ATTEMPTS='1' \
KIMI_RESEARCH_MODEL='kimi-k3' \
python3 scripts/full_refresh.py --workers 1 --showstart-sleep 0.15

# 只调研并输出经过校验的 JSON，不采集、合并或改站点数据
python3 scripts/full_refresh.py --enrich-provider kimi \
  --research-only --output /tmp/concert-research.json

# 只用一位艺人验证 Kimi Formula 真实端到端契约
python3 scripts/full_refresh.py --enrich-provider kimi \
  --research-only --artist-key menni \
  --output /tmp/concert-kimi-smoke.json
```

### 真实用量与费用基线

完整刷新会额外写一份脱敏 telemetry JSON。即使 enrich 关闭、缺 Key、预算拦截或失败，
也会记录 provider、`stale`/`failed` 原因和分层结果。真正调用 Kimi 时，它还记录每次
Formula/Chat 尝试、provider 返回的 `prompt_tokens`、`cached_tokens`、包含隐藏推理的
`completion_tokens`、耗时和估算费用。模型结果在业务校验前就记账，失败调用也不会从账本
消失；即使 provider 返回了顶层数组/字符串等异常 JSON，已发送的尝试也会留下记录，无法
解析 usage 时费用置空并要求对账。该文件不保存 API Key、prompt、Formula 加密输出、模型答案或推理正文。

GitHub Actions 无论成功或失败，都会上传
`full-refresh-telemetry-<request_id>` artifact；若候选成功，另上传
`full-refresh-candidate-<request_id>`，两者都不加入 Git。工具单价及 token 单价是按标注日期的公开价估算，
真实扣费仍必须与 Moonshot 控制台账单差额对账。

充值后不要立即重复全员刷新。先用长巡演样本 KATSEYE 校验记账链路：

```bash
VALIDATION_DIR="$(mktemp -d /tmp/concert-kimi-baseline.XXXXXX)"
read -rs "MOONSHOT_API_KEY?Moonshot API Key: "; printf '\n'; export MOONSHOT_API_KEY
python3 scripts/full_refresh.py \
  --enrich-provider kimi --enrich-attempts 1 \
  --research-only --artist-key katseye --model kimi-k3 --workers 1 \
  --output "$VALIDATION_DIR/candidate.json" \
  --telemetry-output "$VALIDATION_DIR/telemetry.json"
```

确认 telemetry 里有 4 次 Formula、1 次 Chat 和完整 usage，并与控制台账单对上后，
才在受控环境显式 opt-in 一次 `kimi-k3` + `reasoning_effort=high` 全员流程作为基线。
这两次运行都不改变生产默认 `none`，目的只是量出真实成本、延迟和质量。

同证据模型对比使用 `scripts/model_benchmark.py`。它将同一份冻结 evidence
分别交给 K3 High、Qwen3.7 Plus 非思考、Qwen3.7 Flash 非思考和
Qwen3.8 Max Low 四个配置，每个配置默认重复 3 次。Gold 必须经人工核验并绑定
该 evidence 的精确 SHA-256；
仓库里的 64 条 fixture 只是 seed，工具会拒绝直接将它当真值付费运行。
详细步骤、硬门槛和人工验收项见 `VALIDATION.md`。
只有 DashScope Key 时可显式使用 `--qwen-only` 先跑三个 Qwen 臂；该模式仍至少
重复 3 次并保留预算、记账、脱敏和生产树保护，但因缺少同批 K3 召回率
对照，scorecard 固定为 `awaiting_k3_control`，不能授权生产切换。

这些 Qwen 臂与生产刷新严格隔离：`LLM_ENRICH_PROVIDER` 只接受 `none|kimi`，Qwen
Flash 只能把 shadow/benchmark 产物写到仓库外，不能进入 `research/inbox`、合并、构建、
提交或发布。它必须使用新的固定 `source_id` 契约完成至少连续 3 轮真实 shadow 并通过
`VALIDATION.md` 全部门槛，之后也只能由显式生产变更引入，不能由 scorecard 自动切换。

K3 对照只允许走 Moonshot 原生接口。百炼直供的 `kimi/kimi-k3` 仅支持
JSON Object，且 `max_tokens` 不能封顶隐藏推理 token；因此工具会在任何付费调用前
拒绝该路线，不能为了复用一个 DashScope Key 放弃 schema 与预算保护。

脚本还支持：

- `LLM_ENRICH_PROVIDER` / `--enrich-provider`：`none` 或 `kimi`；默认 `none`。
- `LLM_ENRICH_MAX_COST_CNY` / `--enrich-max-cost-cny`：整轮 enrich 最坏费用上限，默认 5 元。
- `LLM_ENRICH_MAX_COST_PER_ARTIST_CNY` / `--enrich-max-cost-per-artist-cny`：单艺人 enrich 最坏费用上限，默认 5 元。
- `LLM_ENRICH_MAX_CONTEXT_BYTES` / `--enrich-max-context-bytes`：单次汇总 payload 的 UTF-8 字节上限，默认 128000。
- `LLM_ENRICH_ATTEMPTS` / `--enrich-attempts`：每个付费阶段的应用层尝试数，默认且生产要求为 1。
- `MOONSHOT_API_BASE`：默认 `https://api.moonshot.cn/v1`。
- `MOONSHOT_REQUEST_INTERVAL`：Moonshot 请求的全局最小间隔，默认 21 秒以适配 Tier 0 的 3 RPM；仅在账户限额更高时调小。
- `KIMI_RESEARCH_MODEL`：默认 `kimi-k3`。
- `--model`：覆盖模型。
- `--workers`：艺人调研并发数，默认 1（适配 Moonshot Tier 0）。
- `--output`：仅供 `--research-only` 的仓外 JSON；拒绝仓内、symlink escape 和覆盖。
- `--candidate-output`：分层刷新的仓外 candidate artifact；首个付费请求前独占预留。
- `--telemetry-output`：脱敏用量/费用 JSON；默认写到系统临时目录，必须位于项目目录之外。

本地执行完整管线会更新数据文件，但不会自动提交或部署；请检查差异后自行提交到 `main`。

### 单项维护命令

```bash
./check.sh                              # 秀动采集 + 现有 inbox 合并 + 重建站点
python3 monitor.py check                # 与上面相同
python3 monitor.py check --force        # 忽略秀动 HTTP 缓存
python3 monitor.py status               # 查看当前概览
python3 monitor.py build                # 只重建 site/data.js / site/data.json
python3 monitor.py ingest <file.json>   # 单独并入一份调研 JSON
python3 monitor.py prune --days 60      # 清理 60 天前的数据
```

这些命令适合调试和维护，**不等于按钮分层刷新**；只有 `scripts/full_refresh.py` 的确定性
发布主链成功后才会更新 `full_refresh_at`。是否完成 LLM enrich 必须另看
`full_refresh_status` 与 telemetry。

只预览静态页面：

```bash
python3 -m http.server 8000 --directory site
open http://localhost:8000
```

静态服务器没有 `/api/refresh`。若要本地调试 Vercel 调度接口，可安装并运行 Vercel CLI，同时提供所需环境变量：

```bash
npm install -g vercel
vercel dev
```

## 加人 / 改人

编辑 `config/artists.json`：

```json
{
  "key": "xxx",
  "name": "艺人名",
  "region": "cn",
  "aliases": ["艺人名", "英文名", "韩文名"],
  "showstart_artist_id": "",
  "search_terms": ["艺人名 演唱会 开票"],
  "enabled": true
}
```

`key` 必须唯一且稳定。`aliases` 用于秀动结果匹配；`search_terms` 会在显式启用 Kimi candidate
时作为基础搜索词。`region: "kpop"` 且没有秀动艺人 ID 时，确定性秀动采集会明确跳过；
未通过门禁前，Kimi 四类调研只供仓外候选复核，不补全线上数据。设置 `enabled: false` 才会从下一次刷新中排除。

## GitHub、Vercel 与域名

Vercel 项目从 GitHub `main` 部署，Framework Preset 为 `Other`，Root Directory 为仓库根目录。`vercel.json` 已配置静态页面、数据文件、Python 调度函数、函数时长和禁止缓存的数据响应头。

工作流提交新的站点快照后，Vercel Git 集成会创建生产部署。自定义域名 `concertmoniter.buaichiyu.com` 在 Cloudflare 使用 Vercel 提供的 CNAME，DNS 验证期间保持 **DNS only**；Vercel 控制台显示的实际目标值优先于通用示例。

## 数据文件与运行产物

```text
api/refresh.py                       Vercel 调度与任务状态接口
.github/workflows/full-refresh.yml  GitHub Actions 完整刷新与发布
scripts/full_refresh.py              确定性刷新主链与可选 Kimi enrich
monitor.py                           秀动采集、调研合并与站点构建入口
AGENT_TASK.md                        完整刷新的规则与运维任务书
config/artists.json                  关注名单和基础搜索词
lib/showstart.py                     秀动确定性采集器
lib/store.py                         合并、去重、状态与变更追踪
data/                                持久化演出、舆情、元数据与变更日志
research/inbox/                      待并入调研数据
research/archive/                    已校验并并入的调研归档
site/data.js                         前端加载的生产快照
site/data.json                       同内容 JSON，供刷新发布状态核验
```

## 已知边界

- 当前所有实测模型都未通过硬门禁；付费 candidate 成功也不等于线上数据已更新。
- 大麦直接请求容易遇到阿里风控，因此由联网搜索和公开页面交叉确认；秀动仍有单独的确定性采集器。
- 默认不会调用 LLM。显式 opt-in Kimi 后，每位艺人计划四次 Formula 和一次汇总，生产尝试数固定为 1；余额/额度不足不会无意义重试。
- Kimi enrich 对来源和单艺人输出 fail closed，但对确定性发布 fail open：enrich 失败沿用旧 research 并如实标 stale/failed；任何应采 ShowStart 源、合并或构建失败仍会阻止发布。
- 合并策略是保守的：本轮没搜索到或 enrich 失败时，旧记录不会仅因此被删除、降级或冒充本轮已复核；它保留既有 `verification_status`。过期演出按日期转入已结束；舆情超过 90 天会从页面隐藏。
- 同一艺人、同一天、同一城市的两场不同演出目前可能被合并。
- 如果确定性刷新、Git push 或 Vercel 部署失败，前端不会获得本轮新快照；Kimi/Formula 单独失败时仍可发布确定性快照，必须从 `full_refresh_status` 和 telemetry 看见降级，不能误读为全源成功。

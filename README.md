# 演唱会监控器

聚合关注艺人的演出、开票与舆情线索，页面按 **正在售卖**、**已官宣 / 待开票**、**舆情监控** 和 **已结束** 展示。

生产地址：[`concertmoniter.buaichiyu.com`](https://concertmoniter.buaichiyu.com)

GitHub：[`2441461233/concert-moniter`](https://github.com/2441461233/concert-moniter)

## 手动刷新现在做什么

网页右上角的刷新按钮执行的是一轮**完整刷新**，不是重新加载页面，也不是只抓秀动：

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
  · Kimi K3 + 官方 Formula 为每位艺人执行四类联网搜索
  · 强制重新采集秀动
  · 校验、合并、去重并生成 site/data.js 与 site/data.json
  · 写入 full_refresh_at / full_refresh_id 并提交到 main
        │
        ▼
Vercel 根据 main 的新提交创建生产部署
        │
        ▼
网页确认本次 full_refresh_id 已上线，自动载入新数据
```

Vercel 在这里是轻量调度器，不承担几十分钟的联网调研。任务一旦进入 GitHub Actions，即使关闭网页或本地电脑关机也会继续执行。网页每 5 秒查询一次状态；重开同一浏览器后会继续跟踪原任务。执行窗口最长 90 分钟，Actions 完成后再给 Vercel 15 分钟独立发布窗口；超时只停止高频轮询，不会忘记后台任务或重复扣费。

同一时间 GitHub API 已能看到完整刷新任务时，再次点击会接入该任务。GitHub Actions 还配置了单任务并发锁，不会并行写同一份数据；在极少数调度竞态下，第二个请求可能进入等待队列，但不会并发发布。

### “完整”的范围

每次都会对 `config/artists.json` 中**全部启用艺人**由代码明确调用四次 Kimi 官方 `moonshot/web-search:latest` Formula，并要求每位艺人覆盖四类信息：

1. 票务平台：大麦、秀动、票星球、猫眼、摩天轮、Cityline、拓元、Interpark/NOL、Ticketmaster、Live Nation 等公开页面。
2. 官方渠道：艺人、事务所、Weverse、官方微博/X、主办方、场馆公告，包含 fanclub presale 与公售时间。
3. 中国内地及港澳台：新增站、加场、补票、取消；KPop 艺人同时复核官方亚洲及世界巡演安排。
4. 近期舆情：只保留与未来演出或开票有关、仍可能变化的线索，并标记可信度。

现有记录只作为待复核线索，不会被当成事实直接复制。代码会为每位艺人分别执行票务、官方、中国区域/完整巡演、舆情四个查询并记录 category、query 和 Fiber ID；任一调用失败，整轮都不会发布。Kimi K3 读取四个搜索结果后以严格 JSON Schema 汇总，每条 event/rumor URL 必须匹配本轮 `sources`，且经过公开域名和 HEAD/GET 可达性校验；日期或来源不合格的候选会被丢弃。

Formula 的 Web Search 返回受保护的 `encrypted_output`，API 不提供可由项目代码解密并逐条对白名单的 citation 列表。因此这里能证明“四类搜索确实由代码执行”，也能验证 Kimi 给出的来源 URL 是公开且可访问的；但不能声称项目代码已将每个 URL 与加密搜索结果逐字比对。原始加密内容不会写入公开仓库。

联网调研完成后，工作流还会运行 `monitor.py check --force`，绕过本地 HTTP 缓存重新抓取可直接采集的秀动数据，并合并本轮调研结果。

### 前端更新时间

页面右上角会显示：

```text
全源采集完成于 2026-08-12 20:24 · 6分钟前
```

这个时间来自 `site/data.json` 的 `full_refresh_at`。它只在“全部启用艺人调研 + 秀动采集 + 合并构建”完成后更新；普通 `monitor.py check`、单独 `ingest` 或仅重新生成页面不会冒充完整刷新。每条活跃记录还有 `verification_status`：本轮再次命中为 `verified`；本轮没搜到的旧记录保守留存但标记为 `unverified`，避免一次搜索漏检就盲删，也不会将旧数据伪装成本轮已复核。

`full_refresh_id` 对应本次按钮任务。网页只有看到这个 ID 随新部署上线，才会宣布完成并重载，因此 GitHub Actions 完成但 Vercel 尚未发布时，页面仍显示“正在发布”。最新时间较近时没有必要反复点击。

## 首次配置

完整刷新涉及两组服务端配置。不要把任何密钥写进仓库或前端代码。

### 1. GitHub 配置

在仓库 **Settings → Secrets and variables → Actions** 中配置：

| 类型 | 名称 | 必需 | 用途 |
| --- | --- | --- | --- |
| Repository secret | `MOONSHOT_API_KEY` | 是 | GitHub Actions 调用 Kimi K3 与官方 Web Search Formula |
| Repository variable | `KIMI_RESEARCH_MODEL` | 否 | 调研模型；未设置时使用 `kimi-k3` |

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
MOONSHOT_API_KEY='sk-...' python3 scripts/full_refresh.py
```

可选参数和环境变量：

```bash
# 默认按 Moonshot Tier 0（3 RPM）串行执行；指定 Kimi 模型
MOONSHOT_API_KEY='sk-...' \
KIMI_RESEARCH_MODEL='kimi-k3' \
python3 scripts/full_refresh.py --workers 1 --showstart-sleep 0.15

# 只调研并输出经过校验的 JSON，不采集、合并或改站点数据
MOONSHOT_API_KEY='sk-...' \
python3 scripts/full_refresh.py \
  --research-only --output /tmp/concert-research.json

# 只用一位艺人验证 Kimi Formula 真实端到端契约
MOONSHOT_API_KEY='sk-...' \
python3 scripts/full_refresh.py --research-only --artist-key menni \
  --output /tmp/concert-kimi-smoke.json
```

脚本还支持：

- `MOONSHOT_API_BASE`：默认 `https://api.moonshot.cn/v1`。
- `MOONSHOT_REQUEST_INTERVAL`：Moonshot 请求的全局最小间隔，默认 21 秒以适配 Tier 0 的 3 RPM；仅在账户限额更高时调小。
- `KIMI_RESEARCH_MODEL`：默认 `kimi-k3`。
- `--model`：覆盖模型。
- `--workers`：艺人调研并发数，默认 1（适配 Moonshot Tier 0）。
- `--output`：调研 JSON 输出位置；执行完整管线时必须位于项目目录内，以便 `monitor.py check` 自动并入。

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

这些命令适合调试和维护，**不等于完整刷新**；只有 `scripts/full_refresh.py` 成功走完全员调研和采集合并后才会更新 `full_refresh_at`。

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

`key` 必须唯一且稳定。`aliases` 用于秀动结果匹配；`search_terms` 会交给联网调研作为基础搜索词。`region: "kpop"` 且没有秀动艺人 ID 时，确定性秀动采集会跳过该艺人，但 Kimi 四类调研仍会完整执行。设置 `enabled: false` 才会从下一次完整刷新中排除。

## GitHub、Vercel 与域名

Vercel 项目从 GitHub `main` 部署，Framework Preset 为 `Other`，Root Directory 为仓库根目录。`vercel.json` 已配置静态页面、数据文件、Python 调度函数、函数时长和禁止缓存的数据响应头。

工作流提交新的站点快照后，Vercel Git 集成会创建生产部署。自定义域名 `concertmoniter.buaichiyu.com` 在 Cloudflare 使用 Vercel 提供的 CNAME，DNS 验证期间保持 **DNS only**；Vercel 控制台显示的实际目标值优先于通用示例。

## 数据文件与运行产物

```text
api/refresh.py                       Vercel 调度与任务状态接口
.github/workflows/full-refresh.yml  GitHub Actions 完整刷新与发布
scripts/full_refresh.py              全员 Kimi Formula Web Search 调研管线
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

- “全部来源”指对全部启用艺人完整执行上述四类公开网络检索，并非拥有每个平台的官方 API。Kimi Web Search 只能检索它可访问的公开页面，登录墙、私域群、未收录社交内容和地区限制页面可能遗漏。
- 大麦直接请求容易遇到阿里风控，因此由联网搜索和公开页面交叉确认；秀动仍有单独的确定性采集器。
- 一轮每位艺人至少调用四次 Formula 和一次 Kimi 汇总。默认单路执行且请求间隔 21 秒，12 位艺人通常需要二十多分钟，并可能因限流重试，因此网页不应被当作高频刷新按钮使用。
- Kimi 官方目前提示 K3 联网搜索通道仍在更新，不建议作为无保护的生产依赖。本项目采取 fail-closed：任一艺人的四类 Formula 调用、结构化汇总或任何应采秀动源最终失败，本轮不发布，线上数据保持不变；429 余额/额度不足不会无意义重试。
- 合并策略是保守的：本轮没搜索到的旧记录不会仅因此被删除，但会标记为 `unverified` 并累计连续未命中轮数。过期演出按日期转入已结束；舆情超过 90 天会从页面隐藏。
- 同一艺人、同一天、同一城市的两场不同演出目前可能被合并。
- 如果 Actions、Kimi/Formula、Git push 或 Vercel 部署失败，前端不会更新 `full_refresh_at`，也不会用半成品覆盖当前生产页面；到 GitHub Actions 日志排查具体阶段。

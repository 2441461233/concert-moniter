# 完整手动刷新任务书

这份文件定义“完整刷新”的执行契约和运维方式。生产环境的首选入口是网页右上角的刷新按钮；自动化实现位于：

- `api/refresh.py`：Vercel 上的认证、GitHub Actions 调度和状态查询。
- `.github/workflows/full-refresh.yml`：后台运行、提交数据和触发生产部署。
- `scripts/full_refresh.py`：全员 Kimi Formula Web Search 调研、校验与数据管线。

不要再把网页按钮解释成“临时刷新秀动”。它会创建一轮可持久化、供所有访客共享的完整数据更新。

## 完整刷新的定义

一次合格的完整刷新必须同时满足：

1. 读取 `config/artists.json`，覆盖每一位 `enabled` 艺人。
2. 每位艺人都由代码直接调用四次 Kimi 官方 `moonshot/web-search:latest` Formula，重新获取下面四类当前信息。
3. 强制重新请求可直接采集的秀动页面。
4. 校验来源、合并去重、更新 `data/`、归档调研文件，并重建 `site/data.js` 和 `site/data.json`。
5. 只有管线完成后才写入新的 `full_refresh_at`、`full_refresh_id` 和 `full_refresh_status`。
6. 将刷新快照提交到 GitHub `main`，由 Vercel Git 集成发布为新的生产站点。

四类联网调研是：

### A. 票务

- 国内重点查大麦、秀动、票星球、猫眼、摩天轮以及主办方正式售票页。
- 港澳台及海外同时查 Cityline、拓元、Interpark/NOL、Ticketmaster、Live Nation 等当地正式票务。
- 重点字段：演出日期、城市、场馆、票价、开票时间、fanclub presale、公售时间和购票 URL。

### B. 官方公告

- 艺人及事务所官网、Weverse、官方微博/X、主办方、场馆公告、官方巡演海报。
- 官方或正式票务可查才标为 `confirmed`，不能把媒体转述或论坛消息伪装成官宣。

### C. 中国内地及港澳台 / 完整巡演

- 每轮都重新确认新增站、加场、补票、延期和取消。
- KPop 艺人除中国内地及港澳台外，还要复核完整的官方亚洲与世界巡演安排，不能只查中文搜索结果。

### D. 近期舆情

- 只收录与未来演出或开票相关、未来几周仍可能变化的新线索。
- 场馆档期泄露、票务页面提前挂出、行程线索、加场传闻可以收录，但必须标 `high` / `medium` / `low` 可信度。
- 不收录新歌、综艺、历史战绩或已经结束演出的回顾。

## 生产执行链路

### 第 1 步：Vercel 调度

网页以 `POST /api/refresh` 启动任务：

- 请求必须同源，并携带 `Authorization: Bearer <REFRESH_SECRET>`。
- Vercel 使用 `GITHUB_ACTIONS_TOKEN` 查询 `.github/workflows/full-refresh.yml` 的近期运行。
- 已有 `queued` 或 `in_progress` 任务时直接复用；否则生成 24 位十六进制 `job_id` 并调用 GitHub `workflow_dispatch`。
- Vercel 不运行完整调研，也不把结果写在临时文件系统里。浏览器关闭不影响 GitHub Actions 继续运行。

网页用 `GET /api/refresh?job_id=…` 每 5 秒轮询。Actions 成功后，它继续读取禁用缓存的 `/data.json`；只有 `full_refresh_id` 等于本次 `job_id` 才重载页面。

### 第 2 步：GitHub Actions 全员调研

工作流先运行离线测试，再执行：

```bash
python3 scripts/full_refresh.py
```

脚本对全部启用艺人创建独立调研单元。为适配 Moonshot Tier 0 的并发 1 / 3 RPM，默认单路执行，所有 Moonshot API 请求之间至少间隔 21 秒。每个单元：

- 使用 `KIMI_RESEARCH_MODEL`，默认 `kimi-k3`，`reasoning_effort=high`。
- 代码固定生成 ticketing、official、china_region、rumors 四类查询，并分别直接调用 Formula Fiber；每类都记录 category、query 和 Fiber ID。
- 将四个受保护搜索结果作为匹配的 assistant `tool_calls` / `role=tool` 消息交给 Kimi K3，再使用严格 JSON Schema 汇总。
- event 和 rumor 的 URL 必须匹配 Kimi 本轮输出的 `sources`，并通过公开域名及 HEAD/GET 可达性校验；只校验实际被条目引用的前 40 个来源。
- 拒绝不精确的日期、无依据 URL 和结构错误；单元失败最多重试 3 次。

所有单元都会跑完以给出完整失败清单。只要仍有一位艺人失败，整个调研抛错，并且不会把半份结果写进 `research/inbox/`。

### 第 3 步：秀动、合并与元数据

全部调研成功后，脚本原子写入一份带时间戳的 `research/inbox/*-full-refresh.json`，再调用：

```bash
python3 monitor.py check --force --sleep 0.15
```

`monitor.py` 会重新采集秀动、自动并入并归档刚生成的调研文件、跨来源合并事件和舆情、更新变更日志并构建站点。海外艺人没有 `showstart_artist_id` 时只跳过秀动确定性采集，不会跳过 Kimi 调研；任何应采秀动源失败都会阻止本轮发布。

随后脚本写入：

- `full_refresh_at`：整轮完成时间，Asia/Shanghai。
- `full_refresh_id`：网页任务 ID；本地执行时生成 `local-*` ID。
- `full_refresh_status`：无警告为 `completed`，有可接受降级为 `completed_with_warnings`。
- `full_refresh`：模型、艺人数、找到的演出/舆情/来源数和警告数摘要。

写完元数据后再构建一次站点，保证这些字段进入 `site/data.js` 与 `site/data.json`。普通 `monitor.py check`、单独 `ingest` 或 `build` 不能修改 `full_refresh_at`。

### 第 4 步：提交与发布

工作流提交以下持久化产物：

```text
config/
data/
research/archive/
site/data.js
site/data.json
```

若运行期间 `main` 有其他提交，工作流先 rebase 到最新 `main`，再以 `github-actions[bot]` 推送。Vercel 的 Git 集成看到新提交后创建生产部署。前端在对应 `full_refresh_id` 真正上线前不会显示新的完整更新时间。

## 必需配置和权限

### GitHub Actions

- Repository secret：`MOONSHOT_API_KEY`。
- Repository variable（可选）：`KIMI_RESEARCH_MODEL`。
- 工作流权限：`contents: write`，已经在 YAML 中声明。
- 仓库必须允许 Actions；分支保护不能阻止 `github-actions[bot]` 的刷新提交。

Actions 使用自动提供的 `GITHUB_TOKEN` 推送，不要把 `GITHUB_ACTIONS_TOKEN` 再复制到 GitHub Secrets。

### Vercel

- `REFRESH_SECRET`：管理员按钮口令。
- `GITHUB_ACTIONS_TOKEN`：fine-grained PAT，只授权该仓库，Repository permissions 的 **Actions: Read and write**。
- `GITHUB_REPOSITORY`（可选）：默认 `2441461233/concert-moniter`。
- `GITHUB_WORKFLOW_FILE`（可选）：默认 `full-refresh.yml`。

变量至少配置到 Production 并重新部署。Moonshot Key 只放在 GitHub Actions；GitHub PAT 和管理员口令只放在 Vercel。

## 本地完整刷新

在仓库根目录执行：

```bash
MOONSHOT_API_KEY='sk-...' python3 scripts/full_refresh.py
```

常用调试形式：

```bash
# 只产生经过校验的调研 JSON，不触碰项目数据
MOONSHOT_API_KEY='sk-...' \
python3 scripts/full_refresh.py \
  --research-only --output /tmp/concert-research.json

# 指定模型、并发数和秀动请求间隔
MOONSHOT_API_KEY='sk-...' \
python3 scripts/full_refresh.py \
  --model kimi-k3 --workers 1 --showstart-sleep 0.15
```

可用环境变量：`KIMI_RESEARCH_MODEL`、`MOONSHOT_API_BASE`，以及 `MOONSHOT_REQUEST_INTERVAL`（默认 21 秒；只有账户限额高于 Tier 0 时才调小）。本地完整刷新会改数据与站点文件，但不会代替你执行 Git commit/push，也不会自动触发 Vercel。

`python3 monitor.py check --force` 只负责秀动和已经存在的 inbox，不会调用 Kimi，也不算完整刷新。

## 结果契约

每个艺人单元的模型输出由严格 Schema 限制，合并后的调研文件核心结构如下：

```jsonc
{
  "_meta": {
    "researched_at": "2026-08-12",
    "by": "kimi-k3-formula-web-search",
    "model": "kimi-k3",
    "artists_total": 12,
    "artists_succeeded": 12,
    "coverage": {},
    "queries": {
      "menni": [
        {"category": "ticketing", "query": "…", "fiber_id": "fiber-…"},
        {"category": "official", "query": "…", "fiber_id": "fiber-…"},
        {"category": "china_region", "query": "…", "fiber_id": "fiber-…"},
        {"category": "rumors", "query": "…", "fiber_id": "fiber-…"}
      ]
    },
    "warnings": []
  },
  "events": [
    {
      "source": "research",
      "url": "https://tickets.example.com/event/123",
      "artist_key": "menni",
      "artist_name": "门尼",
      "tour_name": "2026 巡回演唱会",
      "title": "门尼 2026 巡回演唱会 · 北京站",
      "city": "北京",
      "country": "China",
      "venue": "北京某场馆",
      "show_date": "2026-11-08",
      "show_time": "19:30",
      "price": "¥380-1280",
      "ticket_tiers": ["¥380 看台", "¥1280 内场"],
      "sale_status": "upcoming",
      "sale_time": "2026-09-20 12:00",
      "confidence": "confirmed",
      "note": "公售时间来自正式票务页"
    }
  ],
  "rumors": [
    {
      "artist_key": "menni",
      "artist_name": "门尼",
      "headline": "可能新增巡演城市",
      "detail": "来源与交叉验证情况",
      "source_name": "公开信息源名称",
      "url": "https://example.com/source",
      "credibility": "medium",
      "posted_at": "2026-08-12"
    }
  ],
  "sources": [
    {
      "artist_key": "menni",
      "category": "ticketing",
      "title": "公开票务页面",
      "url": "https://tickets.example.com/event/123"
    }
  ]
}
```

硬规则：

1. `artist_key` 必须来自 `config/artists.json`；脚本会按当前艺人单元注入，不能由模型自由指定。
2. event 和 rumor 必须带本轮可核验的 HTTP(S) 来源 URL。
3. `show_date` 必须为 `YYYY-MM-DD`；不确定就留空，不能猜。
4. `sale_time` 只能是 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`。
5. `posted_at` 必须精确到 `YYYY-MM-DD`。
6. 官方或正式票务可查才是 `confirmed`；论坛、搬运和曝光应放进 rumor 并标可信度。
7. 不报无关艺人动态、历史战绩和已经结束的演出。

## 失败与边界

- Kimi/Formula 调研任一艺人连续失败：整轮失败，不落半份调研结果，线上数据不变。Moonshot 返回余额或额度不足的 429 时立即失败，不反复消耗时间。
- 候选项来源无法对应或字段格式不合格：丢弃该项并记录 warning；其他全员覆盖仍可完成。
- 任何应采秀动源失败：整轮失败，不发布半份快照。
- Git push 失败：Actions 工作区中的数据不会成为生产快照，`full_refresh_at` 不会上线。
- Vercel 部署失败或尚未完成：GitHub 已有新数据，但网页继续显示旧时间并等待发布。
- “完整来源”是四类公开网络信息的完整检索，不是对所有票务/社交平台的私有 API 直连。登录墙、私域信息、未被搜索引擎收录或受地区限制的页面无法保证覆盖。
- 合并是保守增量合并。本轮未搜到的旧记录不会立刻删除，但会标为 `unverified` 并累计连续未命中轮数；过期演出按日期结束，舆情超过 90 天从页面隐藏。
- Kimi Formula Web Search 的输出是受保护的 `encrypted_output`，API 不暴露可供代码逐条核对的 citation 白名单。项目能验证四类搜索确实由代码执行、Kimi 输出 URL 与本轮 sources 一致且公开可达，但不能将 URL 与加密结果逐字比对。加密输出不会写入仓库。
- Kimi 官方目前提示 K3 联网搜索通道仍在更新，不建议作为无保护生产依赖；本项目以严格校验、重试及 fail-closed 降低风险，但仍需关注官方状态。
- GitHub Actions 最长运行 90 分钟；Actions 成功后前端再给 Vercel 15 分钟独立发布窗口。前端超时不等于任务已被取消，任务跟踪记录不得因网络错误或超时被删除。

## 运维检查清单

完整刷新失败时按顺序检查：

1. Vercel Function 日志：是否缺 `REFRESH_SECRET` / `GITHUB_ACTIONS_TOKEN`，GitHub API 是否返回 401/403。
2. GitHub Actions 是否生成 `Full refresh · <job_id>` 运行，`MOONSHOT_API_KEY` 是否有效，账户余额/限额是否足够，Kimi Formula 服务是否可用。
3. 调研日志是否有某位艺人 coverage、来源 URL 或日期校验失败。
4. `monitor.py check` 是否出现秀动降级提示。
5. commit/push 是否被分支保护拒绝。
6. Vercel 是否从最新 `main` 创建生产 Deployment，线上 `/data.json` 的 `full_refresh_id` 是否与任务一致。

如果只是浏览页面发现时间很近，不要为了“确认按钮能否工作”重复启动付费调研；以页面展示的 `full_refresh_at` 为准。

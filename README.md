# 演唱会监控器

聚合关注艺人的演出、开票与舆情线索，页面按 **正在售卖**、**已官宣 / 待开票**、**舆情监控** 和 **已结束** 展示。国内场次以秀动的确定性采集为主；大麦、KPop 海外场与舆情由联网调研补全。

## 数据怎么刷新

平台有两条刷新链路：

### 可持久化的项目快照

```text
config/artists.json ──┐
                      ├─ monitor.py check ─→ data/*.json ─→ site/data.js + site/data.json
research/inbox/*.json ┘
```

- `monitor.py check` 抓取秀动，并入 `research/inbox/` 中待处理的调研 JSON，再更新演出库、变更日志与站点数据。
- 同一场演出按“艺人 + 日期 + 城市”跨来源合并；同一秀动 `source_id` 会优先归到已有记录。
- `first_seen` 保持不变，供页面的“新增”角标和“本轮变化”使用；过期场次自动归入“已结束”。
- `site/data.js` 是页面直接加载的部署快照，`site/data.json` 是同内容的 JSON 版本。提交并推送这些数据后，Vercel 的新部署才会永久更新线上基线。

### 网页右上角的手动刷新

```text
刷新按钮 → GET /api/refresh → 绕过落盘缓存，实时请求秀动
                              ↓
                  合并当前部署自带的数据快照
                              ↓
                    返回结果并在当前标签页重绘
```

手动刷新按钮刻意做得较轻，只补充“现在想看一眼”的即时数据：

- 只实时抓取秀动；不会联网调研大麦、KPop 海外场或舆情。
- 函数在临时目录复制部署时的 `config/` 与 `data/`，跳过采集缓存和调研 inbox，合并后返回 `{ok, cached, data}`。
- 结果只存入当前浏览器标签页的 `sessionStorage`，有效 30 分钟；不会写回 Git 仓库，也不会成为下一次部署的数据。
- 3 位国内艺人会受控并发采集；单次请求和整轮刷新都有时间预算，避免慢响应长期占住按钮。
- 完整结果有约一分钟的短缓存（CDN 重验期间最多约 90 秒），连续点击可能返回 `cached: true`，用于避免重复请求秀动。
- 某一来源降级时会沿用部署快照并显示提示，降级结果不缓存；全部实时来源失败时不会替换当前页面数据。
- 需要永久保存一次刷新结果时，请在本地运行 `check`，检查数据文件后提交到 GitHub。

## 本地运行

项目的数据脚本只依赖 Python 3 标准库。

```bash
./check.sh                              # 秀动采集 + inbox 合并 + 重建站点
python3 monitor.py check                # 与上面相同
python3 monitor.py check --force        # 忽略本地 HTTP 缓存，强制重新请求秀动
python3 monitor.py status               # 查看当前概览
python3 monitor.py build                # 只重建 site/data.js / data.json
python3 monitor.py ingest <file.json>   # 单独并入一份调研 JSON
python3 monitor.py prune --days 60      # 清理 60 天前的数据
```

只预览静态页面：

```bash
python3 -m http.server 8000 --directory site
open http://localhost:8000
```

静态预览不包含 `/api/refresh`。要连同手动刷新接口一起调试，请先安装 [Vercel CLI](https://vercel.com/docs/cli)，然后从仓库根目录运行：

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

`key` 必须唯一且稳定。`aliases` 用于过滤秀动的模糊搜索结果；`region: "kpop"` 且没有秀动艺人 ID 时，会跳过秀动，交由调研流程覆盖。

## GitHub 与 Vercel 部署

仓库：[`2441461233/concert-moniter`](https://github.com/2441461233/concert-moniter)

首次推送：

```bash
git init
git branch -M main
git remote add origin https://github.com/2441461233/concert-moniter.git
git add .
git commit -m "Initial deployment"
git push -u origin main
```

在 Vercel 中选择 **Add New Project → Import Git Repository**，导入上述仓库即可：

- Framework Preset 选 `Other`。
- Root Directory 保持仓库根目录。
- 不填写 Build Command 和 Output Directory。
- `vercel.json` 已负责首页及数据文件路由、Python 刷新函数、函数超时和缓存响应头。

也可以用 CLI 部署：

```bash
vercel          # 预览部署 / 首次关联项目
vercel --prod   # 生产部署
```

GitHub 的 `main` 分支后续有新提交时，Vercel 会自动创建生产部署。

### 自定义域名与 Cloudflare DNS

生产域名：`concertmoniter.buaichiyu.com`

1. 在 Vercel 项目的 **Settings → Domains** 添加 `concertmoniter.buaichiyu.com`。
2. 在 Cloudflare 的 `buaichiyu.com` DNS 中添加一条 CNAME：名称 `concertmoniter`，目标使用 Vercel 域名页给出的值（通常是 `cname.vercel-dns.com`）。
3. 初次验证时将 Cloudflare Proxy status 设为 **DNS only**；等待 Vercel 显示配置有效并签发 HTTPS 证书。
4. 如需开启 Cloudflare 代理，待验证成功后再切为 **Proxied**，并确认 SSL/TLS 模式为 `Full (strict)`。

Vercel 控制台给出的 DNS 记录始终优先于上面的常见示例。不要把 Cloudflare Pages 配到这个子域名；Cloudflare 在这里仅管理 DNS。

## 每日调研

完整的一轮数据更新是“秀动采集 + 大麦 / KPop / 舆情联网调研”。调研指令见 `AGENT_TASK.md`；产物放进 `research/inbox/` 后，下次 `check` 会自动并入并归档。

只想更新秀动时运行 `./check.sh` 即可。网页按钮同样只更新秀动，但它的结果是临时快照，不替代每日调研和 Git 部署。

## 目录

```text
api/refresh.py          Vercel 手动刷新接口
monitor.py              命令行入口与站点构建
check.sh                本地跑一轮检查
AGENT_TASK.md           联网调研任务书
config/artists.json     关注名单
lib/showstart.py        秀动采集器
lib/store.py            合并、去重、状态与变更追踪
lib/http.py             HTTP 请求与本地缓存
data/                   持久化演出、舆情、元数据与变更日志
research/inbox/         待并入调研数据
research/archive/       已并入调研归档
site/                   前端与部署数据快照
vercel.json             Vercel 路由、函数与响应头配置
```

## 已知边界

- 大麦请求会遇到阿里风控，目前依赖联网调研补全。
- 秀动的 SSR 页面通常不提供准确开售时间，因此秀动场次主要能确定在售、暂停、售罄或结束状态；`sale_time` 仍需调研补充。
- 舆情依赖公开搜索，天然晚于微博超话、粉丝群等实时渠道。
- 当前去重模型会把同一艺人、同一天、同一城市的两场不同演出合并。

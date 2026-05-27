# Chip Webapp — Planning Doc

**项目**：`02_work_chip_availability/webapp/`（Phase 2，Versuni 业务自助查询 webapp）
**仓库形式**：**monorepo 子目录**，与 Phase 1 pipeline 同一 git 仓库（`Chip_stock_info_collection_agent`）
**关联**：父目录 `02_work_chip_availability/`（Phase 1，CLI pipeline，**只读依赖**）
**起始日期**：2026-05-27
**当前状态**：设计阶段（块 1-6）完成；Step 1 风险验证完成；**M0 + M1 + M2 完成**（email 通知留到 M3）；M3 待开始

---

## 1. 项目定位

把 Phase 1 已经跑通的 chip-availability pipeline 包装成 webapp，让 Versuni 业务人员自助输入 MPN 查询、所见即所得地在浏览器看结果、并可下载完整 xlsx。

- **不重写 pipeline**：webapp 是壳子，pipeline 是核。
- **不与 Phase 1 源代码耦合**：webapp 通过 `subprocess` 调老 pipeline CLI；老 pipeline 源代码零修改。webapp 用相对路径 `../common/run_pipeline.py` 访问。
- **网页直接渲染关键列**（A2 决策），同时保留 xlsx 下载，邮件通知双通道。

---

## 2. 已确定的架构决策（45 项）

| # | 决策项 | 选择 |
|---|---|---|
| 1 | **项目隔离** | **C：monorepo 子目录（`02_work_chip_availability/webapp/`），同 repo。** 便于 pipeline CLI + webapp consumer 在同一 commit 同步演进；未来真要拆，`git subtree split` 即可。 |
| 2 | 与老 pipeline 耦合方式 | iii：subprocess 调 CLI，老 pipeline 源代码零修改 |
| 3 | Pipeline 改造 | webapp 端加 run_id namespace，不动老 pipeline |
| 4 | 并发/配额 | 全局单 worker 队列 + 24h (MPN, source) 缓存 |
| 5 | bom2buy | MVP 跳过（云上 Opera + captcha 跑不动） |
| 6 | Scraper 并发 | 默认 1 串行 (`--sequential`)，留口子未来扩 |
| 7 | 前端入口 | A 网页表单 + B 邮件触发，**不做钉钉** |
| 8 | DB | SQLite |
| 9 | 文件存储 | 阿里云本地磁盘；30 天 retention + 每日快照（细节见决策 #32） |
| 10 | 认证 | Magic Link（邮箱 allowlist + 一次性登录链接） |
| 11 | 域名/HTTPS | MVP 裸奔 IP + HTTP |
| 12 | Email | MVP `wlnyyaa@hotmail.com` SMTP；未来切 Versuni 邮箱 |
| 13 | 部署方式 | Claude Code 通过 SSH 直接操作阿里云 |
| 14 | xlsx 消化方式 | A2：webapp 解析 xlsx 渲染关键列 + 保留下载 |
| 15 | 进程模型 | 合一：FastAPI + worker 在同一个进程 |
| 16 | run_id 实现归属 | Option β：webapp 维护 `run_id ↔ batch_dir` 映射；pipeline 零修改 |
| 17 | 前端形态 | server-rendered HTML（Jinja2 模板，零构建步骤） |
| 18 | 缓存机制 | Option (a) 全 run 级 hash + 强制重新跑按钮 |
| 19 | 输入方式 | Mode A 粘贴（MPN only，**仅 newline 分隔** —— `,`/`-`/空格 在真 MPN 中都出现：NXP `BT168GW,115`、`BD18333EUV-ME2`、`BD18333EUV-M E2`；强制 newline 避免误拆；超长单条 >50 字符触发"是否一行一个"友好提示）；Mode B Excel 上传（4 列，仅 MPN 必填）；模板下载 |
| 20 | 结果交付 | Option-2 双通道：网页 poll + 邮件附 HTML summary + xlsx 附件 |
| 21 | parsed.json 缓存 | T4 一次性解析写盘 `webapp/runs/<run_id>/parsed.json` |
| 22 | MPN cleaning UX | (c) 智能 review：机械规则改动时才弹 review 页；不应用 `MANUAL_OVERRIDES` |
| 23 | 等待页进度 | 显示 Phase 1/2/3 进度条（数据源 `.pipeline_state.json`） |
| 24 | 历史可见范围 | (a) 只看自己（owner_email 过滤） |
| 25 | **in_stock 过滤** | **web + 邮件正文仅显示 in_stock=True 行**；xlsx 下载保留全部行（含 lead-time only） |
| 26 | **web/邮件排序** | **risk(high→low→other→null) → Type(asc) → MPN_cleaned_byAgent(asc) → Broker name(asc) → Available Quantity(desc)** |
| 27 | **T1/T2 列归属** | **T1（10 列）**：Type, risk, MPN_cleaned_byAgent, Manufacture, in_stock, Broker name, **Warehouse/vender**, Available Quantity, Unit price w/o VAT (max qty), **Trade Currency**, ship infor after order placed。**T2（4 列）**：Is_orig_manufacture, Is_cheapest, packaging, **Lead Time (Week)** |
| 28 | T2 默认展开 | web 默认显示 T1+T2 ≈ 15 列；T3 在"显示更多列"按钮后 |
| 29 | **Type/risk overlay** | **用户上传值覆盖 chip-list join 值**（Mode B 优先于 master chip list） |
| 30 | **webapp xlsx 下载** | **单 sheet（仅 All_data）**，不带 Sheet 1/3/Data dictionary/Source Availability。webapp 自己生成精简版 xlsx，pipeline 原文件留作内部审计 |
| 31 | **输入新鲜度** | **每次查询必须由用户主动上传或粘贴**；/query 页面在无输入时显眼提示；history "重新跑" = redirect /query + MPN 列表预填到粘贴框，**用户仍需点提交** |
| 32 | **文件保留期** | raw 上传 .xlsx 每日清理；webapp/runs/<run_id>/{input.csv, parsed.json, state_snapshot.json} 30 天；pipeline BatchTest 30 天；SQLite runs 行永久 |
| 33 | **Schema drift handling** | webapp 按**列名**识别（不按列序）；未知列**静默忽略** + 日志记录；缺列渲染为破折号 + warning 日志；列 schema 升级 = 改 `WEBAPP_SCHEMA_v2` + 重新部署 |
| 34 | **安装路径** | **`/opt/chip-project/`**（FHS 标准）；从 github.com/Guoh05/Chip_stock_info_collection_agent clone |
| 35 | **进程管理** | **systemd**：1 个 service（`chip-webapp.service` 运行 uvicorn）+ 2 个 timer（retention 每天 03:00，backup 每天 04:00） |
| 36 | **端口** | **8000**（原计划 8080，但发现 8080 已被 OpenClaw 套件的 searxng 占用 127.0.0.1:8080 ）；业务 URL = `http://101.133.151.21:8000/` |
| 37 | **反向代理** | **MVP 不装 nginx**；uvicorn 直接监听 8080；未来加 TLS 时再引入 nginx + Let's Encrypt |
| 38 | **进程身份** | **root**（MVP 减少权限调试）；未来生产强化时切非 root user |
| 39 | Venv 共享 | pipeline + webapp **共用** `/opt/chip-project/.venv/`（云端用 **Python 3.11**——al8 仓库 `python3.11.x86_64 3.11.13-7.0.1.al8`；本机 pipeline 用 3.10.9 没变化）；M1 阶段需测 pipeline 在 3.11 下能否跑通；新增 `requirements_webapp.txt` 单独维护 webapp deps |
| 40 | SQLite 模式 | **WAL**（`PRAGMA journal_mode=WAL`）；单进程写、多线程读不冲突 |
| 41 | 内存限制 | systemd `MemoryMax=2G` 给 `chip-webapp.service`，防 OOM 误杀 OpenClaw。**实测 OpenClaw 全套占 ~800MB，不是先前估的 1.8GB**：openclaw-gateway 484MB + searxng 163MB + dockerd/containerd 80MB + cloudmonitor 40MB；webapp + pipeline 实际可用 ~2.5-2.7GB |
| 42 | 配置 / 秘密 | **`/opt/chip-project/.env`**（chmod 600，gitignore）+ python-dotenv 加载；SMTP / FastAPI key / allowlist / pipeline 路径全在此 |
| 43 | **备份策略** | **(a) 双重保险**：①SQLite 每日 `.backup` 保留 7 份；②Alibaba 云平台磁盘快照每日 |
| 44 | 日志策略 | 多层：①uvicorn 请求 → `webapp/logs/access.log`（rotating 7d）；②FastAPI 应用 → `webapp/logs/app.log`（7d）；③pipeline subprocess → `webapp/runs/<run_id>/pipeline.log`（随 run 30d）；④systemd → journald |
| 45 | 部署 workflow | Claude Code 通过 SSH 操作：初次 `git clone + venv 创建 + playwright install + init_db + systemctl enable`；日常升级 `git pull + pip install -r requirements_webapp.txt（如有新dep）+ systemctl restart chip-webapp` |

> 注：决策 #9 早期描述的 `production/runs/<run_id>/` 路径已被决策 #16 修正。pipeline 写到 `production/{api,scraper,merged}/BatchTest_<ts>/`，webapp 在 SQLite 维护 `run_id ↔ batch_dir` 映射；webapp 自己的快照可以放 `webapp/runs/<run_id>/`。

---

## 3. 规划进度（6 大块）

| # | 块 | 状态 | 备注 |
|---|---|---|---|
| 1 | 整体架构图 | ✅ 完成（见 §3.1） | UI ↔ backend ↔ pipeline subprocess ↔ 存储 ↔ 认证 |
| 2 | 数据流 | ✅ 完成（见 §3.2） | 端到端：业务点查询 → 看到结果 |
| 3 | 用户流程 | ✅ 完成（见 §3.3） | 登录、提交、等待、查看历史、下载、cleaning UX |
| 4 | A2 解析契约 | ✅ 完成（见 §3.4） | webapp 解析哪几列、in_stock 过滤、schema drift |
| 5 | 部署形态 | ✅ 完成（见 §3.5） | 阿里云 webapp 进程、systemd / 文件路径 / 内存 / cron / 备份 |
| 6 | 实施顺序 | ✅ 完成 | 5 个里程碑 M0-M4；Step 1 风险验证完成 |

---

## 3.1 块 1 详情：整体架构

### 架构图

```
┌─ Outside cloud ───────────────────────────────────┐
│   User Browser          User Email Inbox           │
│        │                       ▲                   │
│        │ HTTP                  │ SMTP              │
└────────┼───────────────────────┼───────────────────┘
         │                       │
┌─ Alibaba Cloud (al8) ──────────┼───────────────────┐
│        ▼                       │                   │
│   ┌──────────────────────┐     │                   │
│   │ FastAPI 进程         │─────┘                   │
│   │  · API / 静态前端    │                          │
│   │  · Magic Link 认证   │                          │
│   │  · Job queue+worker  │                          │
│   │  · xlsx 解析 + 下载  │                          │
│   └──┬─────────┬─────────┘                          │
│      │         │ subprocess                         │
│      │         ▼                                    │
│      │   ┌────────────────────┐                     │
│      │   │ Old Pipeline       │                     │
│      │   │ (../common/        │                     │
│      │   │  run_pipeline.py)  │                     │
│      │   └─────────┬──────────┘                     │
│      │             │ writes                         │
│      ▼             ▼                                │
│   ┌────────┐   ┌──────────────────┐                 │
│   │ SQLite │   │ Disk             │                 │
│   │ users  │   │ production/      │                 │
│   │ runs   │   │  api/scraper/    │                 │
│   │ cache  │   │  merged/         │                 │
│   │ tokens │   │  BatchTest_<ts>/ │                 │
│   └────────┘   └──────────────────┘                 │
└─────────────────────────────────────────────────────┘
```

### 6 个组件 + 3 类通信

| 组件 | 角色 |
|---|---|
| User Browser | 业务入口；提交查询、看结果、下载 xlsx |
| FastAPI 进程 | webapp 全部：HTTP API + 前端 + 认证 + job 队列 + worker + xlsx 解析（决策 #15 合一） |
| SQLite | 状态：用户白名单、run 历史（含 `run_id ↔ batch_dir` 映射）、Magic Link token、24h MPN 缓存 |
| Old Pipeline | 子进程被调用，webapp **不 import**，只通过 CLI |
| Disk (production/) | pipeline 写入 `production/{api,scraper,merged}/BatchTest_<ts>/`；webapp 读取并管理 30 天 retention |
| Hotmail SMTP | 出站，发 Magic Link 邮件 + 结果通知邮件 |

**3 类通信**：

1. **HTTP**：浏览器 ↔ FastAPI
2. **Subprocess**：FastAPI fork → 老 pipeline；pipeline 写 disk 后 exit，留下 `.pipeline_state.json` 供 webapp 解析
3. **File IO**：FastAPI 读 disk（解析 merged xlsx + 下载分发）；SQLite 同进程读写

### 边界

- webapp 代码住在 `02_work_chip_availability/webapp/`
- 老 pipeline 代码住在 `02_work_chip_availability/{common,scraper,api}/`
- 两者通过 **subprocess + 磁盘文件** 两个接口通信，**绝不互相 import**
- pipeline 不知道 webapp 的存在；webapp 把 pipeline 当外部 CLI 工具

### run_id ↔ pipeline batch_dir 映射（Option β 细节）

```
webapp 端流程：
1. 用户提交查询 → webapp 生成 run_id（如 r_20260527_a1b2）
2. webapp INSERT 一行到 runs 表：(run_id, status=queued, started_at=now, mpns=[...])
3. worker 从队列取出，调：
     subprocess.run(
       ["python", "../common/run_pipeline.py",
        "--env", "prod",
        "--mpns-file", "/tmp/<run_id>_mpns.tsv",
        "--skip-bom2buy",
        "--scraper-args", "--sequential",
        "--merge-args", "--chip-list ../ref/Raw_chip_list_<...>.xlsx"],
       cwd="../")
4. 子进程退出后，webapp 读 production/.pipeline_state.json
5. 提取 phases.{api,scraper_main,merge}.batch_dir 三个路径
6. UPDATE runs SET api_batch=..., scraper_batch=..., merge_batch=...,
                  status=done WHERE run_id=...
7. webapp 把 .pipeline_state.json 拷贝到
   webapp/runs/<run_id>/state_snapshot.json 留底
```

**为什么这不动 pipeline**：步骤 4 的 `.pipeline_state.json` 是 pipeline **已有**的状态文件（本为 `--resume` 而写）。webapp 只是把它从"重启工具"复用为"结果追溯工具"。

### 关键约束

- pipeline `.pipeline_state.json` 在 `<env_root>/` 是单文件、不带 run_id → **webapp 必须串行调 pipeline**（决策 #4 单 worker 队列已保证）
- pipeline 默认产生的 `BatchTest_<ts>/` 不会自我清理 → **webapp 负责 30 天 retention 清理**（决策 #9）

---

## 3.2 块 2 详情：数据流

### 主流程时间线（happy path）

```
T0  已登录用户打开 /query 页面
    数据：浏览器持有 session cookie
    状态：SQLite users 表有这个邮箱
    /query 强制提示：**必须上传文件或粘贴 MPN 才能查询**（决策 #31）

T1  用户提交 MPN 列表（Mode A 粘贴 或 Mode B Excel 上传，决策 #19）
    数据流：浏览器 POST → FastAPI
    webapp 动作：
      · 校验输入 + 邮箱权限
      · 解析输入 → 抽出 MPN 列表 + (可选) 业务元数据 (Manufacture/Type/risk)
      · 应用 mechanical cleaning（决策 #22）
      · 若有改动 → 弹清洗 review 页（决策 #22 智能 review）
      · 生成 run_id（如 r_20260527_a1b2）
      · INSERT runs (run_id, owner_email, status=queued, mpns,
                     mpns_hash, submitted_at)
      · 如 Mode B：把完整元数据写到 webapp/runs/<run_id>/input.csv
      · 把 run_id 丢进 in-memory 队列
    用户看到：HTTP 302 → /r/<run_id>，页面显示"Agent 正在运行 + 3 phase 进度条"

T1.5 缓存检查（决策 #18 Option a）
     · webapp 用 mpns_hash 查 runs 表，找 24h 内 status=done 的匹配
     · 命中：UPDATE runs 把当前 run_id 标 status=cache_hit, points_to=<old_run_id>
            → 直接跳到 T5，不调 pipeline
     · 未命中或用户点"强制重新跑"：继续 T2

T2  Worker 从队列取出 job
    动作：
      · 把 mpns 写到 /tmp/<run_id>_mpns.tsv（MPN<TAB>Mfr，Mfr 可空）
      · subprocess.run([python, ../common/run_pipeline.py,
                        --env prod, --mpns-file /tmp/<run_id>_mpns.tsv,
                        --skip-bom2buy, --scraper-args "--sequential",
                        --merge-args "--chip-list <...>"])
    webapp 状态：runs.status = running

T3  Pipeline 跑 3 phase（API → scraper → merge，serial，5~30 分钟）
    pipeline 动作：写 production/{api,scraper,merged}/BatchTest_<ts>/ +
                  增量更新 production/.pipeline_state.json
    webapp 状态：worker 阻塞在 subprocess.wait()；FastAPI 主线程继续响应
                等待页前端每 5~10 秒 poll status 端点，
                webapp 读 .pipeline_state.json 把 phase 状态返回前端（决策 #23）

T4  Pipeline 退出
    webapp 动作：
      · 读 production/.pipeline_state.json
      · 提取 phases.{api, scraper_main, merge}.batch_dir 三个路径
      · UPDATE runs SET ..._batch=..., status=done, finished_at=now
      · 拷贝 .pipeline_state.json → webapp/runs/<run_id>/state_snapshot.json
      · **解析 merged xlsx（决策 #21）→ 应用 webapp schema（决策 #33）
        → 应用 in_stock 过滤（决策 #25）→ 应用排序（决策 #26）
        → join input.csv 元数据（决策 #29 用户值优先）
        → 写 webapp/runs/<run_id>/parsed.json**
      · **生成精简 xlsx（决策 #30）：仅 All_data sheet，全部行
        → 写 webapp/runs/<run_id>/Versuni_chip_stock_<run_id>.xlsx**
      · 发邮件给 owner_email（决策 #20）：
          正文 = HTML summary 表（与网页同一渲染，已过滤 in_stock=True）
          附件 = 精简 xlsx（如 <20MB；否则只放查看链接）

T5  用户查看结果
    用户动作：页面 poll 到 done → 自动刷新（或从通知邮件点过来）
    webapp 动作：读 parsed.json → 渲染 Jinja2 模板
    用户看到：浏览器内 summary 表（仅 in_stock=True 行）+ "下载完整 xlsx" 链接
            若过滤后无任何行 → 显示空态"本次查询无现货可用，请下载完整 xlsx 查看 Lead Time"

T6  用户下载 xlsx
    用户动作：点页面或邮件里的下载链接
    webapp 动作：serve webapp/runs/<run_id>/Versuni_chip_stock_<run_id>.xlsx
    用户得到：精简版（仅 All_data sheet，全部行；含 in_stock=False 和 lead-time only 数据）
```

### 关键数据存放位置（含 retention，决策 #32）

| 数据 | 位置 | 生命周期 |
|---|---|---|
| Session token | browser cookie + SQLite `sessions` 表 | 7 天 |
| Run 元数据 + 状态 + batch_dir 映射 + mpns_hash | SQLite `runs` 表 | 永久 |
| **用户上传的 raw .xlsx 文件** | `webapp/runs/<run_id>/upload_raw.xlsx` | **每日清理**（仅当天） |
| 用户上传的业务元数据（Mfr/Type/risk） | `webapp/runs/<run_id>/input.csv` | 30 天 retention |
| Pipeline 状态快照 | `webapp/runs/<run_id>/state_snapshot.json` | 30 天 retention |
| 解析后的 xlsx (parsed JSON) | `webapp/runs/<run_id>/parsed.json` | 30 天 retention |
| webapp 生成的精简 xlsx | `webapp/runs/<run_id>/Versuni_chip_stock_<run_id>.xlsx` | 30 天 retention |
| Pipeline 原始 batch + 完整 xlsx | `production/{api,scraper,merged}/BatchTest_<ts>/` | 30 天 retention（webapp 来清） |
| 24h (MPN, source) 缓存 | SQLite `mpn_cache` 表 | 24 小时 TTL |
| Magic Link token | SQLite `magic_links` 表 | 15 分钟 TTL，消费后立即标记 |

### 分支：非 happy path

| 分支 | 触发点 | webapp 行为 |
|---|---|---|
| Pipeline exit ≠ 0 | T4 | runs.status = failed；页面显示错误 + .pipeline_state.json 摘要；可下载 partial xlsx |
| API 限额（LCSC 200/day） | T3 内部 | pipeline 自己降级；webapp 状态仍 done，页面 banner "部分 source 数据缺失" |
| State 文件读不到 | T4 | runs.status = failed；展示 stderr 末尾 |
| 用户中途关浏览器 | T1 后任意时刻 | job 已入队，pipeline 照跑；用户回来访问 /r/<run_id> 或点邮件回来 |
| 缓存命中 | T1.5 | 跳过 T2-T4；T5 直接渲染 points_to 的 parsed.json；banner "24h 内同查询，复用结果" |
| Excel 附件 >20MB | T4 邮件 | 邮件只放正文 summary + 查看链接，不附 xlsx |
| 过滤后 0 行（无现货） | T5 | 空态页面，提示业务下载完整 xlsx 看 Lead Time（决策 #25） |
| /query 页面用户未上传/粘贴就点提交 | T1 | 显眼红色提示框 "请先上传文件或粘贴 MPN"（决策 #31） |

### Auth 流程

```
A1  未登录用户打开 / → 跳 /login
A2  填邮箱 → POST /login {email}
A3  webapp 检 allowlist → 生成 token → INSERT magic_links (15 min TTL)
A4  Hotmail SMTP 发邮件："点这里登录 https://<ip>/auth/<token>"
A5  用户点链接 → GET /auth/<token>
A6  webapp 验 token → SET session cookie → INSERT sessions
                    → 标记 magic_link consumed → redirect /query
```

### 输入方式（决策 #19 详情）

**Mode A — 文本框粘贴**：MPN only，**仅 newline 分隔**（一行一个），trim + dedupe。理由：`,`/`-`/空格/`;` 都在真 MPN 中出现（NXP `,118` 系列、ubiquitous dashes、`BD18333EUV-M E2` 等），强制 newline 是唯一零冲突的分隔约定，且与 Excel 列复制粘贴的天然格式一致。超长单条 (>50 字符) 触发"看起来是分隔列表请改成一行一个"友好提示。

**Mode B — Excel 上传**：webapp 提供"下载模板.xlsx"按钮（仅含表头）。

| 列名 | 必填 | 用途 | 流到哪 |
|---|---|---|---|
| Manufacture Part Number | ✅ | 主键 | → pipeline `--mpns-file` |
| Manufacture | ❌ | 厂商提示 | → pipeline `--mpns-file` |
| Type | ❌ | 业务自分类 | 仅 webapp 用，**用户值优先覆盖 chip-list join**（决策 #29） |
| risk | ❌ | 业务风险等级 | 仅 webapp 用，**用户值优先覆盖 chip-list join**（决策 #29） |

### 结果交付（决策 #20 + #25 + #26）

| 通道 | 时点 | 内容 |
|---|---|---|
| 网页 poll | T1 后每 5~10 秒 | 状态查询 GET /r/<run_id>/status |
| 网页渲染 | done 触发自动刷新 | summary 表（仅 in_stock=True 行 + 15 列 T1+T2） + 下载链接 |
| 邮件通知 | T4 完成时 | 标题"查询结果就绪 - run XYZ"；正文 HTML summary（仅 in_stock=True + T1 10 列）；附件精简 xlsx |

---

## 3.3 块 3 详情：用户流程

### 7 个流程总览

| # | 流程 | 触发 | 终点 |
|---|---|---|---|
| 1 | 首次登录 | 打开 `/` 未登录 | 登录后落到 `/query` |
| 2 | 提交查询（Mode A 粘贴） | `/query` 页面 | 跳到 `/r/<run_id>` 等待页 |
| 3 | 提交查询（Mode B 上传） | `/query` 页面 | （可能经"清洗预览"页）跳到 `/r/<run_id>` |
| 4 | 等待结果 | 已提交，停在 `/r/<run_id>` | 自动刷新成结果页 |
| 5 | 查看结果（从邮件回来） | 点邮件链接 | 落到 `/r/<run_id>` 结果页 |
| 6 | 查看历史 | 点导航 `/history` | 看到自己的 run 列表 |
| 7 | 下载 xlsx | 结果页或邮件附件 | 浏览器下载 |

### Flow 1 — 登录

```
1. 访问 / → 未登录 → 302 /login
2. /login 显示邮箱输入框 + "发送登录链接"按钮
3. 提交 → "我们发了一封邮件到 X，15 分钟内点链接登录"
4. 用户去邮箱点链接 → /auth/<token>
5. token 验证通过 → 设 cookie → 302 /query
6. 失败：邮箱不在 allowlist / token 过期-已用 → 对应错误提示
```

### Flow 2 — 提交查询（Mode A 粘贴）

```
1. /query 显示两个 tab：[粘贴 MPN] [上传 Excel]
   · 页面顶部明显提示："每次查询都需要新上传文件或粘贴 MPN"（决策 #31）
2. 切到[粘贴 MPN]：
   · textarea，placeholder："每行一个 MPN，或用 , | ; 分隔"
   · 提交按钮（disabled 当输入为空）
3. 输入为空就点提交 → 红色提示框"请先粘贴 MPN 列表"（决策 #31）
4. 提交 → split + trim + dedupe → 机械清洗（决策 #22）
5. 若清洗有改动 → 弹清洗 review 页
6. 若清洗无改动 → "识别到 N 个 MPN，开始查询"确认条
7. 确认 → POST → 跳 /r/<run_id>
```

### Flow 3 — 提交查询（Mode B 上传）

```
1. /query 切到[上传 Excel]：
   · 顶部明显提示同上
   · "下载模板.xlsx"按钮 → 只含表头的模板
   · 文件选择框（限 .xlsx；预估 ≤5MB）
2. 上传后 webapp 解析：
   · 校验 `Manufacture Part Number` 列存在 → 否则"模板格式不对"
   · 抽出 4 列（缺失可选列填空）
   · 把原 .xlsx 存到 webapp/runs/<run_id>/upload_raw.xlsx（**当天清理**，决策 #32）
3. 应用 mechanical cleaning（决策 #22）
4. 若有改动 → 弹清洗 review 页
5. 若无改动 → 直接跑 → 跳 /r/<run_id>
6. 用户在 /query 不上传任何文件就提交 → 红色提示框"请先上传文件"（决策 #31）
```

### 清洗 review 页

仅在机械清洗**有改动**时弹出。

```
┌────────────────────────────────────────────────┐
│ 自动清洗预览 (10 个改动，2 个原样)              │
│                                                  │
│ 原始 MPN                  →  清洗后             │
│ ──────────────────────────────────────────────  │
│ MCU-STM32F103C8T6        →  STM32F103C8T6       │
│ NRF52840(QIAA)           →  NRF52840            │
│ STM32G030F6P6 LQFP32     →  STM32G030F6P6       │
│ BD18333EUV-M E2          →  ⚠️ 未识别，请人工    │
│                                                  │
│ [✓ 确认开始查询] [手动编辑] [取消]               │
└────────────────────────────────────────────────┘
```

规则：仅应用 mechanical rules（参考 Phase 1 `_build_cleaned_input.py`），**不应用** `MANUAL_OVERRIDES`；无法识别标 ⚠️ 让业务自己决定。

### Flow 4 — 等待结果（决策 #23）

```
┌─────────────────────────────────────┐
│  查询 run_id: r_20260527_a1b2        │
│  状态：Agent 正在运行                 │
│                                       │
│  ▣ Phase 1: API sweep      [运行中]   │
│  ▢ Phase 2: Web scraper    [等待]     │
│  ▢ Phase 3: 合并结果        [等待]     │
│                                       │
│  预计完成：约 8 分钟                  │
│  完成后会自动刷新本页 + 发邮件通知    │
└─────────────────────────────────────┘
```

前端每 5~10 秒 poll；status=done 时整页 reload。

### Flow 5 — 从邮件回访 = Flow 4 的 done 状态

### Flow 6 — 查看历史（决策 #24 + #31）

```
┌────────────────────────────────────────────────┐
│ 我的查询历史                                    │
│                                                  │
│ ▢ 2026-05-27 14:32  r_..._a1b2  5 MPNs  ✅ Done   │
│   [查看结果] [重新跑] [下载 xlsx]                │
│ ▢ 2026-05-26 10:11  r_..._3f4e  120 MPNs ✅ Done  │
│   [查看结果] [重新跑] [下载 xlsx]                │
│ ▢ 2026-05-25 17:00  r_..._9b8c  8 MPNs   ❌ Failed │
│   [查看错误] [重新跑]                            │
└────────────────────────────────────────────────┘
```

- 默认只列 owner_email = 当前用户的 run
- **"重新跑" 不立即触发**：跳到 /query + 把 MPN 列表预填到 Mode A 粘贴框，**用户必须再点提交**（决策 #31）

### Flow 7 — 下载 xlsx

任何 done 状态页面 → 点"下载完整 xlsx" → 浏览器获取 `Versuni_chip_stock_<run_id>.xlsx`（**仅 All_data 一个 sheet，全部行**，决策 #30）。

### Error UX（Flow 4 的 failed 分支）

```
┌─────────────────────────────────────┐
│  查询 r_..._9b8c 失败                  │
│  ❌ Phase 2 (web scraper) 报错        │
│  错误：Playwright 超时                │
│  [重新跑] [查看日志摘要] [联系管理员]   │
└─────────────────────────────────────┘
```

日志摘要 = pipeline stderr 末尾 50 行；重新跑同 Flow 6 规则。

---

## 3.4 块 4 详情：A2 解析契约

### Pipeline 输出 xlsx 摘要

3 数据 sheet + 2 参考 sheet：

| Sheet | 默认可见 | 行 | 列数 |
|---|---|---|---|
| `High_risk_positive_stock` | 隐藏 | risk=high & in_stock 子集 | 43 |
| **`All_data`** | **默认打开** | 所有 merge 后行 | **43** |
| `ref_scraper_api_diff` | 隐藏 | QA mismatch | 24 |
| `Data dictionary` | 可见 | 列说明 | 3 |
| `Source Availability` | 可见 | 数据源能力表 | ~5 |

webapp **只消化 `All_data`**，其他 sheet 忽略。

### 43 列三色带分布（背景知识，不需要 webapp 全显示）

```
A–L (1-12) 深蓝底白字：业务元数据 + 双 MPN + Manufacture..risk
M–AH (13-34) 浅橙底黑字：分销商数据 + 计算列 + packaging
AI+ (35+) 深灰底白字：ref_* 审计字段（9 列）

8 个深红色高亮列（procurement 一眼扫描）：
  in_stock, Broker name, Warehouse/vender, Is_orig_manufacture,
  Is_cheapest, Available Quantity, ship infor after order placed,
  Unit price w/o VAT (max qty)
```

### A2 列分层（决策 #27 + #28，T1+T2 默认展开）

**T1 必显（10 列）— 网页和邮件 summary 都显**：

| 列名 | 中文标签建议 | 渲染提示 |
|---|---|---|
| Type | 类型 | 文本 |
| risk | 风险 | badge（high=红/low=灰） |
| MPN_cleaned_byAgent | MPN（清洗后） | 文本（左对齐） |
| Manufacture | 厂商 | 文本 |
| in_stock | 现货 | bool（绿✓） |
| Broker name | 分销商 | 文本 |
| Warehouse/vender | 仓库/供应商 | 文本 |
| Available Quantity | 可用数量 | 数字（千分位） |
| Unit price w/o VAT (max qty) | 单价（不含税，大批量） | 数字 4 位小数 |
| Trade Currency | 结算币种 | 文本（短码） |
| ship infor after order placed | 下单后发货 | 文本 |

**T2 推荐显（4 列）— 网页显，邮件不显**：

| 列名 | 中文标签建议 |
|---|---|
| Is_orig_manufacture | 原厂仓? |
| Is_cheapest | 最低价? |
| packaging | 包装 |
| Lead Time (Week) | Lead Time (周) |

**T3 高级显（"显示更多列"按钮后才展开）**：

```
Manufacture Part Number (raw), Stock Location, MOQ, Maximum order qty,
Date of Code, Reel/Cut Reel, Certificate of Conformity(Yes/No),
Category, Project, EMS/Finish Goods, 12NC_PCBA,
Quantity, Currency, Current Price, Data collect method
```

**T4 不显示（仅在下载的精简 xlsx 里有，网页永不渲染）**：

```
9 个 ref_*, Minimum order qty, Unit price w/o VAT (min qty),
Number of price tiers, price_rank
```

### in_stock 过滤 + 排序（决策 #25 + #26）

**过滤**：仅渲染 `in_stock=True` 的行。其他行（in_stock=False / lead-time only）网页和邮件都不显示，但保留在下载 xlsx 里。

**排序**：

```
risk(high→low→other→null) 
  → Type(asc)
    → MPN_cleaned_byAgent(asc)
      → Broker name(asc)
        → Available Quantity(desc)
```

risk 排序细节：sort_key `{"high":0, "low":1, "other":2, "":3, None:3}`。Type / MPN / Broker 字符串自然顺序。Available Quantity 降序（多的在前）。

### 空态处理

过滤后行数 = 0 时：

```
┌─────────────────────────────────────┐
│  本次查询无现货可用                    │
│                                       │
│  你查询的 N 个 MPN 在 M 个 source 都  │
│  没有现货库存。可下载完整 xlsx 查看：  │
│   · 工厂订货 Lead Time 选项           │
│   · 历史/未来到货时间                │
│   · 全部 source 的尝试详情            │
│                                       │
│  [下载完整 xlsx]                       │
└─────────────────────────────────────┘
```

### webapp 下载 xlsx（决策 #30）

webapp 在 T4 用 openpyxl 生成精简版：

- **只一个 sheet：`All_data`**
- 列：43 列全部保留（不删 ref_*，让需要审计的人能看到）
- 行：**全部保留**（含 in_stock=False，给业务做 lead-time 决策）
- 表头视觉：沿用 pipeline 同款（3 色带 + 8 深红高亮 + Calibri）
- 表体视觉：沿用 pipeline 同款（in_stock=True 浅绿，qty=0 浅灰）
- 文件名：`Versuni_chip_stock_<run_id>.xlsx`
- AutoFilter + freeze panes A2 同 pipeline

**pipeline 原 xlsx 不动**，保留在 `production/merged/Merge_*/` 作内部审计；webapp 用户拿到的是精简版。

### Type/risk overlay（决策 #29）

```
parsed.json 一行 = pipeline 输出行
  ├─ Pipeline 已 join 的 Type/risk（来自 master chip list）作为兜底
  └─ 如 Mode B 且 webapp/runs/<run_id>/input.csv 有该 MPN
        → Type/risk 用 input.csv 值覆盖（用户上传值优先）
```

理由：业务知道这次查询的当前上下文（Type 可能临时改了，risk 可能升级了），master chip list 可能滞后几个月。

### Schema drift handling（决策 #33）

**核心原则**：按列名识别，不按列序。

```python
WEBAPP_SCHEMA_v1 = {
    # T1 (10)
    "Type": {"tier": 1, "label": "类型", "render": "text"},
    "risk": {"tier": 1, "label": "风险", "render": "badge"},
    "MPN_cleaned_byAgent": {"tier": 1, "label": "MPN（清洗后）", "render": "text"},
    "Manufacture": {"tier": 1, "label": "厂商", "render": "text"},
    "in_stock": {"tier": 1, "label": "现货", "render": "bool"},
    "Broker name": {"tier": 1, "label": "分销商", "render": "text"},
    "Warehouse/vender": {"tier": 1, "label": "仓库/供应商", "render": "text"},
    "Available Quantity": {"tier": 1, "label": "可用数量", "render": "qty"},
    "Unit price w/o VAT (max qty)": {"tier": 1, "label": "单价（不含税，大批量）", "render": "price"},
    "Trade Currency": {"tier": 1, "label": "结算币种", "render": "text"},
    "ship infor after order placed": {"tier": 1, "label": "下单后发货", "render": "text"},
    # T2 (4)
    "Is_orig_manufacture": {"tier": 2, "label": "原厂仓?", "render": "bool"},
    "Is_cheapest": {"tier": 2, "label": "最低价?", "render": "bool"},
    "packaging": {"tier": 2, "label": "包装", "render": "text"},
    "Lead Time (Week)": {"tier": 2, "label": "Lead Time (周)", "render": "num1"},
    # T3 (15) ...
}
```

**Drift 规则**：

| 场景 | webapp 行为 |
|---|---|
| xlsx 有 schema 没列的列 | 忽略 + 日志 `[schema] unknown col 'X' in run Y` |
| xlsx 缺 schema 期待的列 | 该单元格渲染为 `—` + 日志 warning |
| 列序乱了 | 不影响（按列名取） |
| 列名大小写变化 | 视作"缺列"（不做模糊匹配，避免误伤） |

**升级路径**：

1. pipeline 加新列 → webapp 日志报 unknown col → 管理员评估是否对业务有价值
2. 有价值 → 在 `WEBAPP_SCHEMA_v2` 添加该列条目（指定 tier + label + render）
3. 重新部署 webapp
4. 已 done 的 run **重新解析一次**（webapp 提供管理员命令 `webapp re-parse <run_id>` 或全量重解析）→ parsed.json 重建 → 历史 run 也能展示新列

---

## 3.5 块 5 详情：部署形态

### 部署组件总览

```
┌─ Alibaba Cloud (al8, 2vCPU/3.5GiB) ─────────────────────────────┐
│                                                                   │
│  systemd                                                          │
│  ├─ chip-webapp.service (核心，决策 #35)                          │
│  │   └─ uvicorn app.main:app --host 0.0.0.0 --port 8080            │
│  │       └─ (运行时) subprocess: python ../common/run_pipeline.py │
│  │                                                                 │
│  ├─ chip-webapp-retention.timer (每天 03:00)                      │
│  │   └─ 清 upload_raw.xlsx + 清 30 天前 webapp/runs/<id>/ + 清    │
│  │      pipeline BatchTest_* + 清过期 magic_links + 清 24h cache │
│  │      + VACUUM SQLite                                            │
│  │                                                                 │
│  └─ chip-webapp-backup.timer (每天 04:00)                         │
│      └─ SQLite .backup → keep 7 份                                 │
│                                                                    │
│  OpenClaw (常驻 ~1.8GiB)                                          │
│                                                                    │
│  Alibaba 云端磁盘快照 (每天，平台自带，决策 #43)                    │
└────────────────────────────────────────────────────────────────────┘
```

### 文件系统布局（决策 #34）

```
/opt/chip-project/                       ← git clone github.com/Guoh05/...
├── .git/
├── .venv/                                ← Python 3.10.9，pipeline + webapp 共享（决策 #39）
├── .env                                  ← 秘密（chmod 600；gitignore；决策 #42）
│   · SMTP_HOST / SMTP_USER / SMTP_PASS
│   · FASTAPI_SECRET_KEY
│   · ALLOWLIST_EMAILS（逗号分隔）
│   · WEBAPP_BASE_URL（http://101.133.151.21:8080）
│   · PIPELINE_ROOT / PIPELINE_PYTHON / PIPELINE_CHIP_LIST / PIPELINE_ENV
│   · 各种 retention 阈值
│
├── common/, scraper/, api/, ref/         ← pipeline 代码（不动）
├── production/                           ← pipeline 输出（不动）
│   ├── api/BatchTest_<ts>/
│   ├── scraper/BatchTest_<ts>/
│   ├── merged/Merge_*/
│   └── .pipeline_state.json
│
├── webapp/                               ← Phase 2 新代码
│   ├── app/
│   │   ├── main.py                       ← FastAPI 入口
│   │   ├── routers/ (auth, query, runs, history, download)
│   │   ├── services/ (pipeline_runner, xlsx_parser, emailer, cleaner)
│   │   ├── models.py                     ← SQLAlchemy / sqlite3
│   │   └── schemas.py                    ← WEBAPP_SCHEMA_v1（决策 #33）
│   ├── templates/                        ← Jinja2: login, query, run, history
│   ├── static/                           ← CSS / JS（poll script 等）
│   ├── docs/planning.md                  ← 本文档
│   ├── scripts/
│   │   ├── retention_cleanup.py          ← retention timer 调用
│   │   ├── sqlite_backup.sh              ← backup timer 调用
│   │   └── deploy.sh                     ← SSH 部署脚本
│   ├── webapp.db                         ← SQLite（WAL 模式，决策 #40）
│   ├── webapp.db-wal, webapp.db-shm      ← WAL 配套
│   ├── backups/                          ← SQLite 每日备份 7 份（决策 #43）
│   │   └── webapp_YYYYMMDD.db
│   ├── runs/<run_id>/                    ← 每个 run 一个文件夹
│   │   ├── upload_raw.xlsx               ← 当天清（决策 #32）
│   │   ├── input.csv                     ← 30 天
│   │   ├── state_snapshot.json           ← 30 天
│   │   ├── parsed.json                   ← 30 天
│   │   ├── Versuni_chip_stock_<id>.xlsx  ← 30 天
│   │   └── pipeline.log                  ← pipeline subprocess 输出，30 天（决策 #44）
│   ├── tmp/                              ← /tmp/<run_id>_mpns.tsv 等中间文件
│   └── logs/
│       ├── app.log                       ← FastAPI 应用日志（rotating 7 天）
│       └── access.log                    ← uvicorn 请求日志（rotating 7 天）
│
├── requirements.txt                      ← pipeline deps（已有）
└── requirements_webapp.txt               ← webapp 新增 deps（FastAPI / uvicorn / jinja2 /
                                              python-dotenv / openpyxl / email-validator / ...）
```

### 网络 / 防火墙（决策 #36 + #37）

| 入站 | 端口 | 来源 | 用途 |
|---|---|---|---|
| SSH | 22 | claude code 本机 IP | 部署 / 调试 |
| HTTP | **8080** | 0.0.0.0 | webapp |
| | （未来） 443 | 0.0.0.0 | TLS 后切换 |

| 出站 | 端口 | 目的地 | 用途 |
|---|---|---|---|
| SMTP | 587 | smtp-mail.outlook.com | Magic Link + 结果通知邮件 |
| HTTPS | 443 | API source / scraper site | pipeline 跑的时候 |
| HTTPS | 443 | github.com | git pull 部署 |

**反向代理**：MVP 不装 nginx，uvicorn 直接监听 8080。

### Cron / Timer 设计（决策 #35 + #32 + #43）

| 任务 | 时点 | 内容 | 实现 |
|---|---|---|---|
| 清 upload_raw.xlsx | 每天 03:00 | 删除所有 `webapp/runs/*/upload_raw.xlsx`（决策 #32） | `chip-webapp-retention.timer` → `retention_cleanup.py` |
| 清 30 天前 derivatives | 每天 03:00（同上） | 删 `webapp/runs/<id>/`（按 ctime > 30 天） + 删 `production/{api,scraper,merged}/BatchTest_*`（同 30 天） | 同上 |
| 清过期 magic_links | 每天 03:00（同上） | `DELETE FROM magic_links WHERE expires_at < now()` | 同上 |
| 清 24h MPN cache | 每天 03:00（同上） | `DELETE FROM mpn_cache WHERE created_at < now() - 24h` | 同上 |
| SQLite VACUUM | 每天 03:00（同上） | 收缩文件 + 优化 | 同上 |
| SQLite 备份 | 每天 04:00 | `sqlite3 webapp.db ".backup webapp_YYYYMMDD.db"` → 保留 7 份 | `chip-webapp-backup.timer` → `sqlite_backup.sh` |
| Alibaba 磁盘快照 | 每天 02:00 | 整盘 snapshot，保留 7 天 | Alibaba 云控制台配置（决策 #43） |

### 日志策略（决策 #44）

| 日志 | 位置 | 保留 |
|---|---|---|
| FastAPI uvicorn 请求 | `webapp/logs/access.log`（rotating） | 7 天 |
| FastAPI 应用日志（schema drift / 业务错误等） | `webapp/logs/app.log`（rotating） | 7 天 |
| Pipeline subprocess stdout/stderr | `webapp/runs/<run_id>/pipeline.log` | 30 天（随 run 一起清） |
| systemd 服务日志 | `journalctl -u chip-webapp` | journald 默认（~1 月） |
| Cron timer 输出 | journald | 同上 |

调试 `journalctl -u chip-webapp -f` 看实时；查历史 `journalctl -u chip-webapp --since "1 hour ago"`。

### 内存预算（决策 #41）

```
3.5 GiB 总内存
├─ OS + 内核 buffer ........... ~300 MiB
├─ OpenClaw 常驻 .............. ~1.8 GiB
├─ FastAPI uvicorn idle ....... ~100 MiB
├─ Pipeline 跑时（playwright）  ~500-800 MiB
├─ SQLite cache + buffer ...... ~100 MiB
└─ 剩余 cushion ............... ~200-400 MiB
```

**对策**：systemd unit `MemoryMax=2G` 给 chip-webapp，若超限则 systemd 杀 webapp（包括 pipeline subprocess），保护 OpenClaw。监控由人工通过 `journalctl` 抽查；MVP 不做自动告警。

### 配置 / 秘密（决策 #42）

`/opt/chip-project/.env`（chmod 600，gitignore）：

```dotenv
# SMTP
SMTP_HOST=smtp-mail.outlook.com
SMTP_PORT=587
SMTP_USER=wlnyyaa@hotmail.com
SMTP_PASS=<...>

# FastAPI
FASTAPI_SECRET_KEY=<random 32 bytes for session signing>
WEBAPP_BASE_URL=http://101.133.151.21:8080

# Auth
ALLOWLIST_EMAILS=ext_hao.guo@versuni.com,wlnyyaa@hotmail.com,...

# Pipeline 调用
PIPELINE_ROOT=/opt/chip-project
PIPELINE_PYTHON=/opt/chip-project/.venv/bin/python
PIPELINE_CHIP_LIST=/opt/chip-project/ref/Raw_chip_list_<latest>_cleaned.xlsx
PIPELINE_ENV=prod

# Retention
RAW_UPLOAD_RETENTION_HOURS=24
DERIVED_RETENTION_DAYS=30
SQLITE_BACKUP_KEEP=7
```

python-dotenv 加载。管理员通过 SSH 编辑此文件即可改秘密 / 切邮箱 / 加 allowlist。

### 部署 / 升级 workflow（决策 #45）

**初次部署**（Claude Code 通过 SSH 操作）：

```bash
ssh root@101.133.151.21
git clone https://github.com/Guoh05/Chip_stock_info_collection_agent.git /opt/chip-project
cd /opt/chip-project
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # pipeline deps
.venv/bin/pip install -r requirements_webapp.txt # webapp deps
.venv/bin/playwright install chromium firefox    # pipeline scraper
# 初始化 SQLite schema
.venv/bin/python -m webapp.app.scripts.init_db
# 写 .env（手动 vim 填秘密）
vim /opt/chip-project/.env && chmod 600 /opt/chip-project/.env
# 安装 systemd unit + timer
cp webapp/deploy/systemd/chip-webapp.service /etc/systemd/system/
cp webapp/deploy/systemd/chip-webapp-retention.timer /etc/systemd/system/
cp webapp/deploy/systemd/chip-webapp-retention.service /etc/systemd/system/
cp webapp/deploy/systemd/chip-webapp-backup.timer /etc/systemd/system/
cp webapp/deploy/systemd/chip-webapp-backup.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now chip-webapp.service \
                       chip-webapp-retention.timer \
                       chip-webapp-backup.timer
# Alibaba 控制台开 8080 安全组 + 配每日盘快照
journalctl -u chip-webapp -f   # 看服务起来了
```

**日常升级**：

```bash
ssh root@101.133.151.21
cd /opt/chip-project
git pull
.venv/bin/pip install -r requirements_webapp.txt  # 如果加了新 dep
systemctl restart chip-webapp.service
journalctl -u chip-webapp -n 50
```

整个流程**只动 webapp/**，pipeline 代码不动（除非真的要升 pipeline 版本，那是 merge 窗口的事）。

### 关键的"和老 pipeline 共存"约定

- `.venv/` 共用，但**装包前先在干净 venv 试装**确认 webapp deps 不破坏 pipeline deps。`requirements_webapp.txt` pin 版本号
- `production/` 目录 pipeline + webapp 共写共读，但 pipeline **只写 `BatchTest_<ts>/` 时间戳目录**，webapp 只**读** `.pipeline_state.json` 和 batch 目录，**写**自己的 `webapp/runs/<run_id>/`，互不踩
- `ref/` 目录 pipeline + webapp 共读；webapp 通过 `.env` 的 `PIPELINE_CHIP_LIST` 指定当前用哪个 chip-list 文件，不在代码里硬编码

---

## 4. 老 pipeline 已支持的 CLI flag（webapp 会依赖）

webapp 显式传所有 flag，**不依赖 pipeline 默认值**。这是 defensive subprocess invocation 原则。

- `--env {test,prod}`
- `--mpns-file PATH` / `--xlsx PATH`
- `--skip-bom2buy`
- scraper: `--sequential`（只支持 1 或全并行，无中间值）
- API: `--max-workers N`（1~N 任意）
- merge: `--chip-list PATH`
- 43 列输出 schema（v1.12，含 `MPN_cleaned_byAgent` + `packaging`），见 Phase 1 `doc/merge_for_procurement_rules.md`

补充（块 1 + 块 4 调研发现）：

- pipeline orchestrator 退出后会写 `<env_root>/.pipeline_state.json`，含 `phases.{api,scraper_main,merge}.batch_dir` 路径 → webapp 用此反查产生的 batch 目录
- pipeline 所有 driver 都用 `BatchTest_<ts>/` 时间戳命名，**无 `--out-dir` flag**；webapp 不要尝试覆盖路径，靠 SQLite 映射处理
- pipeline 在跑的过程中也会增量更新 `.pipeline_state.json`，phases.<phase>.status 字段实时反映当前进度 → webapp 等待页 poll 此文件给前端进度条数据源（决策 #23）
- pipeline `All_data` sheet 43 列（v1.12）含 `MPN_cleaned_byAgent` + `packaging` + `Is_orig_manufacture` + `Is_cheapest`；按列名取，schema drift 不破坏 webapp（决策 #33）

---

## 5. 阿里云服务器现状

- IP `101.133.151.21`, user `root`
- SSH key: `C:\Users\wlnyy\Documents\aliyun_openclaw_private_key.pem`
- OS: Alibaba Linux 3 (al8, RHEL 衍生, dnf/yum)
- 2 vCPU / 3.5 GiB RAM / 50 GiB 系统盘
- OpenClaw 常驻占 ~1.8 GiB → 实际可用 ~1.7 GiB

---

## 6. 待决问题（TBD）

- Mode B 上传文件大小上限（典型 200 行 xlsx <1MB，应该不是问题）
- 老 pipeline `--max-channel-workers N` 是否需要补一个（MVP 不需要）
- pipeline CLI 契约治理：是否在 Phase 1 docs 里加一份"webapp-dependent flags"列表
- webapp 子目录是否需要自己的 sub-`CLAUDE.md`
- Excel 模板的 schema 版本号机制（防业务用旧模板上传）
- 等待页"预计完成时间"算法（基于历史 run 时长 / MPN 数量？）
- T3 "显示更多列"展开形态（折叠面板 / 横向滚动 / 弹窗）—— 块 5 部署 / 实施时再定
- Mode A 粘贴模式下空态提示的具体文案
- WEBAPP_SCHEMA 升级时是否自动重解析所有 30 天内 run（vs 按需手动）

---

## 7. 变更日志

| 日期 | 变更 |
|---|---|
| 2026-05-27 | 文档创建（原位置 `03_chip_webapp/docs/planning.md`）。确认 A2 方向。开始块 1 架构图讨论。 |
| 2026-05-27 | **决策 #1 从 A（sibling 独立 repo）切到 C（monorepo 子目录）**。文档迁移到 `02_work_chip_availability/webapp/docs/planning.md`，sibling `03_chip_webapp/` 目录删除。 |
| 2026-05-27 | **块 1（整体架构）完成**：架构图 + 6 个组件 + 3 类通信 + run_id 映射方案（Option β，pipeline 零修改）。新增决策 #15-#17。 |
| 2026-05-27 | **块 2（数据流）完成**：T0-T6 时间线 + 数据存放位置 + 分支表 + Auth 流程 + Mode A/B 输入方式 + 结果交付双通道。新增决策 #18-#21。 |
| 2026-05-27 | **块 3（用户流程）完成**：7 个流程 + 清洗 review 页 + 等待页 phase 进度 + 历史页 + Error UX。新增决策 #22-#24。 |
| 2026-05-27 | **块 4（A2 解析契约）完成**：A2 列分层（T1+T2 = 15 列默认显，T3 折叠，T4 不显）+ in_stock 过滤 + 自定义排序 + 空态 + webapp 精简 xlsx 生成 + Type/risk overlay + schema drift 按列名识别。新增决策 #25-#33（in_stock 过滤、排序、T1/T2 列归属、T2 默认展开、Type/risk overlay、精简 xlsx 单 sheet、输入新鲜度、文件保留期、schema drift）。 |
| 2026-05-27 | **块 5（部署形态）完成**：部署组件图（systemd 1 service + 2 timer）+ `/opt/chip-project/` 文件布局 + 网络/防火墙 + cron 设计 + 日志多层 + 内存预算（systemd MemoryMax=2G）+ `.env` 配置 + 部署/升级 workflow。新增决策 #34-#45（安装路径 / systemd / 8080 端口 / 不装 nginx / root 身份 / 共享 venv / SQLite WAL / 内存限制 / .env 配置 / SQLite+Alibaba 双重备份 / 多层日志 / SSH+git pull 部署）。 |
| 2026-05-27 | **块 6（实施顺序）确认 + Step 1 风险验证完成**：5 个里程碑 M0-M4 计划。SSH 验证阿里云：✅ Playwright 15 个系统依赖全部已装 / ✅ SMTP 587 出站通 / ✅ 主要 API endpoints 通 / ✅ 端口 8000 空闲 / ✅ /opt 可写。**调整**：决策 #36 端口 8080→8000（被 OpenClaw searxng 占用）；决策 #39 云端 Python 用 3.11（dnf 仓库无 3.10）；决策 #41 OpenClaw 实测 ~800MB（不是 1.8GB），可用内存宽裕。**已知非阻塞问题**：www.mouser.com CN 区被封（但 api.mouser.com 通，API track 不受影响）。 |
| 2026-05-27 | **M0（骨架 + 视觉 demo）完成**：建 `webapp/` 完整子目录骨架 + `requirements_webapp.txt`（FastAPI 0.115.5 / uvicorn 0.32.1 / jinja2 3.1.4 / python-multipart / python-dotenv / openpyxl / email-validator）+ `app/main.py` / `config.py` / `schemas.py`（WEBAPP_SCHEMA_v1 实例 + 5 行 fake data + render_cell filter）/ `storage.py`（in-memory RUNS dict）/ `routers/{query,runs,history}.py` + 4 个 Jinja2 模板 + `static/css/style.css`（视觉同步 xlsx：T1 蓝/T2 橙/highlight 深红/in_stock 浅绿）。本机 `uvicorn webapp.app.main:app --port 8000` 跑通；7 个路由 smoke 全 pass。结果页 15 列正确渲染（T1+T2 默认展开，决策 #28），8 个深红 highlight class 正确（决策 #14）。**注意**：pip install 期间 jinja2 从 3.1.6 降到 3.1.4、python-dotenv 从 1.2.2 降到 1.0.1（pin 在 requirements_webapp.txt 中）；M1 阶段需快速 smoke pipeline 确认不破。 |
| 2026-05-27 | **决策 #19 修订**：Mode A 粘贴改为**仅 newline 分隔**。原 `,`/`\|`/`;` 分隔不可行——真 MPN 中`,`（NXP `BT168GW,115`）、`-`（绝大多数）、空格（`BD18333EUV-M E2`）都会出现。Newline 是唯一零冲突分隔字符，且与 Excel 列复制粘贴的天然格式一致。补加超长单条 (>50 字符) 触发友好警告。 |
| 2026-05-27 | **M1（本机 happy path）完成**：从内存 dict → SQLite (WAL)；`app/services/{pipeline_runner,xlsx_parser,xlsx_writer}.py`；FastAPI lifespan 启动 single worker thread；queue 串行调老 pipeline subprocess；`Path.as_posix()` 转 chip-list 路径绕开 Windows shlex 反斜杠 bug；读 `<env>/.pipeline_state.json` 反查 api/scraper batch_dir；glob 找最新 `Merge_*/`；in_stock 过滤 + 自定义排序（risk → Type → MPN → Broker → qty desc）→ `parsed.json`；生成精简 xlsx（单 sheet All_data，43 列全保留，全部行）。端到端真 pipeline 跑通：`STM32G030F6P6` 单 MPN ~44 秒（DigiKey API ok + DigiKey scraper blocked + merge ok），webapp 拿到 2 行现货数据。**已知非阻塞**：`STM32G030F6P6` 不在 `Raw_chip_list_20260523_cleaned.xlsx` 内 → Type/risk/Manufacture 列空（chip-list join 无匹配，符合 pipeline 行为）。 |
| 2026-05-27 | **M2（本机 feature 完整）完成**（email 留到 M3）：① `app/services/mpn_cleaner.py` —— port Phase 1 6 条机械规则 + suspicious-pattern warning（中文/内部空格/过短/过长），不应用 `MANUAL_OVERRIDES`（决策 #22）。② `app/services/excel_input.py` —— `make_template_xlsx()` 生成 4 列模板 + `parse_upload()` 解析上传文件 + 智能跳过模板示例行 + `write_input_csv()`。③ /query 重构：Mode A 干净→直接 enqueue；Mode A 脏→review.html；Mode B 必走 review.html；/query/template 模板下载；/query/confirm 处理 review 提交。④ review.html：变化标黄、警告标红、并列展示 metadata；textarea 可编辑最终 MPN；强制重新跑 checkbox（决策 #18 缓存绕过）。⑤ 24h cache check（按 mpns_hash + owner_email + status=done + 24h 窗口）→ 命中 redirect 到原 run_id?cache_hit=1 + 结果页 banner。⑥ `_apply_metadata_overlay()` in pipeline_runner —— 决策 #29：用户上传的 Type/risk/Manufacture 覆盖 chip-list join 值（按 MPN_cleaned_byAgent match）。⑦ 端到端验证：Mode B 上传 `BD18333EUV-ME2`（chip-list 里 Type=Non-prescribed/risk=Middle）+ 用户值 Type=电源 IC/risk=low → parsed.json 显示 Type=电源 IC/risk=low ✅ overlay 生效。**未做（留 M3）**：邮件通知（需 Hotmail SMTP 凭证 + 模板）；history 页用 force=1 重新跑链接（review 页 checkbox 已够用）。 |

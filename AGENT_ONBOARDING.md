# Paper Reader · Agent 对接文档

> 版本：2026-11-26（丙午年孟冬之十八）
> 适用对象：接手本项目的 AI Agent / 高级工程师
> 目的：让一个「顶级 Agent」在 **30 分钟内** 交接清楚：这是什么、现在跑在哪、代码怎么读、怎么改、怎么验证、哪些绝对不能碰，以及一份带验收标准的优化路线图。
> 约定：本文不写任何密钥/密码明文；凭据获取方式见第 3 节。

---

## 0. 一页摘要

- **产品**：自托管的「论文双语阅读器」——上传 PDF → 版面感知切片 → 调用大模型逐段流式翻译 → 中英对照阅读，附带 TTS 朗读、术语表、论文推荐、双语 Markdown 导出。
- **技术栈**：Python 3.11 / FastAPI / uvicorn / PyMuPDF / httpx；原生 JS 前端（无构建）；SQLite（WAL）；Docker；nginx；Cloudflare 代理；Let's Encrypt。
- **线上状态**：已部署在海外 VPS（香港），**https://paper.lushixiao.cn**，nginx Basic Auth 保护，两个共享账号（`paper`、`guest`），每日 04:17 自动备份数据库。
- **代码规模**：后端约 12k 行 Python + 前端约 4 万字符 JS，结构扁平、可读性好。
- **当前主要短板**：单书库多人共享、Basic Auth 无用户体系、无自动化测试/CI、无成本配额、代码仓库落后于线上版本。
- **接手建议顺序**：读 README → 读本文 → 本地跑起来 → 复现问题 → 领任务（第 12 节）→ 小步改 + 验证 → 发布（第 8 节）。
- **三件事不能碰**：① 同机代理服务（sing-box）；② 证书/Cloudflare 配置（除非任务明确要求）；③ 生产数据库（改前必须备份）。

---

## 1. 运行拓扑

```
                          用户浏览器（电脑 / 手机）
                                   │  HTTPS
                                   ▼
                      ┌──────────────────────────┐
                      │  Cloudflare（代理/加速）    │  zone: lushixiao.cn
                      │  SSL 模式: Full           │  DNS: paper → A 23.27.0.190（已代理）
                      └────────────┬─────────────┘
                                   │  HTTPS（源站 Let's Encrypt 证书）
                                   ▼
   ┌───────────────────────────  VPS 23.27.0.190（Evoxt 香港）  ───────────────────────────┐
   │                                                                                       │
   │  nginx :80   → ACME 验证 + 301                                                        │
   │  nginx :443  → Basic Auth（realm "Paper Reader"）+ 限速 + 反向代理                      │
   │  nginx :8010 → 301 到主域名（旧链接兼容）                                              │
   │  nginx :2083 → 代理订阅（proxy-sub，与本项目无关，勿动）                                │
   │        │ proxy_pass http://127.0.0.1:18010                                            │
   │        ▼                                                                              │
   │  Docker 容器 paper-reader（FastAPI/uvicorn :8000，非 root 用户 uid 10001）             │
   │        │  读写 /data（bind mount → 宿主机 /opt/paper-reader-data）                     │
   │        │  外呼 https://api.deepseek.com（翻译）/ https://api.xiaomimimo.com（TTS）      │
   │        ▼                                                                              │
   │  SQLite（WAL）：/opt/paper-reader-data/paper_reader.db                                │
   │                                                                                       │
   │  同机常驻（与本项目无关，禁止改动）：                                                    │
   │    sing-box：Hysteria2 443/udp、VLESS Reality 8443/tcp  ← 用户的主力代理                │
   │    sshd :22、ufw、certbot.timer、每日 DB 备份 cron                                      │
   └───────────────────────────────────────────────────────────────────────────────────────┘
```

- **备用访问地址**：`https://23.27.0.190.sslip.io`（直连 VPS，独立证书；主域名/Cloudflare 故障时使用）。
- **注意**：项目流量走 Cloudflare，所以 nginx 看到的客户端 IP 是 Cloudflare 的 IP；按 IP 限速目前等于「所有用户共享一个额度」（见第 11 节技术债）。
- **管理入口**：Evoxt 控制台（开关机/重装/救援/续费）→ `https://console.evoxt.com/vmcontrolpanel.php?id=958593`。

---

## 2. 环境与资产清单

### 2.1 服务器

| 项 | 值 |
|---|---|
| 供应商 / 编号 | Evoxt，VM #958593，宿主节点 Evoxt-HK-17 |
| 位置 | 香港 |
| 规格 | 1 vCPU（AMD EPYC-Genoa 4.19GHz）/ 2GB 内存 / 20GB 磁盘 / 500GB 月流量 / 1Gbps |
| 系统 | Ubuntu 26.04 LTS，内核 7.0.0-14，KVM，BBR 已开 |
| 公网 IP | IPv4 `23.27.0.190`；IPv6 `2400:8d60:6::f395:b3b3`；内网 `10.5.15.199` |
| Docker | Engine 29.1.3 + Compose 2.40.3（apt 源安装） |
| 磁盘占用 | 约 2.3GB / 20GB（剩余充足） |
| 内存占用 | 约 480MB / 2GB（其中本项目容器约 56MB） |

### 2.2 域名与证书

| 项 | 值 |
|---|---|
| 主域名 | `paper.lushixiao.cn`（Cloudflare 托管；A 记录 → 23.27.0.190，**已开启代理**） |
| Cloudflare 设置 | SSL/TLS 加密模式 = **完全（Full）**；已不再使用 Cloudflare Access（无邮箱验证） |
| 备用域名 | `23.27.0.190.sslip.io`（免费解析，**DNS only**，直连） |
| 源站证书 | Let's Encrypt：`/etc/letsencrypt/live/paper.lushixiao.cn/`、`/etc/letsencrypt/live/23.27.0.190.sslip.io/` |
| 续期 | `certbot.timer`（systemd 自动），ACME webroot：`/var/www/certbot`；有效期至 2027-02-24，已 dry-run 验证 |
| 访问控制 | nginx Basic Auth，用户文件 `/etc/nginx/.paper-reader.htpasswd`（root:www-data 640），realm `Paper Reader`，账号：`paper`、`guest` |
| 限速 | `/etc/nginx/conf.d/paper-reader-limits.conf`：`pr_req` 600 次/分 burst 100；`pr_tts` 60 次/分 burst 10；`pr_conn` 25 |
| 请求体上限 | nginx `client_max_body_size 100m`；应用 `MAX_UPLOAD_MB=80` |

### 2.3 防火墙（ufw，仅本机防火墙；Evoxt 面板侧防火墙关闭）

放行：`22/tcp`、`80/tcp`、`443/tcp`、`443/udp`（代理）、`8010/tcp`（旧链接跳转）、`8443/tcp`（代理）、`2083/tcp`（订阅）。
**除本项目外，以上端口均属于同一台机器上的代理/订阅服务，切勿关闭或改写。**

### 2.4 部署文件位置

| 路径 | 内容 |
|---|---|
| `/opt/paper-reader/` | 项目源码 + `.env` + compose（约 3.9MB） |
| `/opt/paper-reader-data/` | **生产数据**：SQLite、PDF、TTS 缓存（约 6.4MB，含 backups 子目录） |
| `/opt/paper-reader-data/backups/` | 每日 DB 自动备份（保留 14 天） |
| `/opt/paper-reader-backups/` | 历史备份：修复前的损坏库、迁移前整目录备份 |
| `/etc/nginx/sites-available/paper-reader` | nginx 站点配置（已 enable） |
| `/etc/nginx/conf.d/paper-reader-limits.conf` | 限速规则 |
| `/usr/local/bin/paper-reader-backup.sh` + `/etc/cron.d/paper-reader-backup` | 每日 04:17 数据库备份 |
| `/etc/letsencrypt/` | 证书与续期配置 |
| `/var/log/nginx/paper-reader.access.log` | 访问日志（含 Basic Auth 用户名） |

### 2.5 密钥/凭据在哪里（本文不写明文）

| 凭据 | 存放/获取方式 |
|---|---|
| 网站登录账号 | `paper`（所有者）/ `guest`（访客）；密码由用户提供，或见用户桌面《paper-reader-VPS-接入信息.md》 |
| VPS root 密码 | Evoxt 控制台 → VM 面板 → 「显示密码」按钮 |
| LLM Key（DeepSeek） | 服务器 `/opt/paper-reader/.env` 的 `LLM_API_KEY`（**禁止复制进仓库/聊天/文档**） |
| TTS Key（小米 MiMo） | 同上，`TTS_API_KEY` |
| Cloudflare 账号 | 用户个人账号（邮箱 15527759825@163.com）；Agent 未获授权时不得改 DNS/证书策略 |

---

## 3. 代码地图（`/opt/paper-reader`，本地 Mac 同一份）

```
paper-reader/
├── Dockerfile              # python:3.11-slim + pip 依赖；非 root 用户 reader(10001)；HEALTHCHECK
├── docker-compose.yml      # 单服务；端口 ${HOST_PORT}=127.0.0.1:18010:8000；数据卷 ${DATA_HOST_DIR}:/data
├── Makefile                # up/build/down/logs/test/sample 等便捷命令
├── requirements.txt        # fastapi 0.115.6 / uvicorn 0.34.0 / python-multipart / httpx / pymupdf 1.25.2
├── .env                    # 全部运行配置（含密钥；权限 600，不入库）
├── README.md               # 功能、配置、切片机制、提示词约束、TTS 说明、已知边界
├── app/
│   ├── main.py             # FastAPI 入口：全部 HTTP 路由（上传/翻译/朗读/导出/推荐/缓存）
│   ├── pdf_parser.py       # PDF 版面解析与段落切片（PyMuPDF）
│   ├── store.py            # SQLite 存储层：docs/blocks/cache/glossary/picks；WAL；线程锁
│   ├── translator.py       # LLM 翻译引擎（OpenAI 兼容：DeepSeek/OpenAI/Kimi/Ollama…），并发与重试
│   ├── protect.py          # 占位符保护：公式、引用、编号、代码标识符挖空后再翻译
│   ├── discovery.py        # 论文发现：让 LLM 推荐论文 + 中文概括
│   ├── tts.py              # 小米 MiMo TTS：分段合成、磁盘缓存、探测
│   ├── config.py           # 环境变量解析（ALL CAPS 常量）
│   └── static/             # 前端（无构建）：index.html + app.js(41KB) + style.css
├── scripts/serve_local_llm.sh   # 可选：本地 Ollama 小模型（Mac 专用，生产不用）
└── tools/                  # make_sample_pdf.py、smoke_test.py、样例 PDF
```

**Git**：本地目录带 `.git`，远端 `https://github.com/lsx3320/paper-reader.git`（公开仓库，**最后推送 2026-09-15**）。
⚠️ 当前本地/线上代码包含 2026 年 9 月下旬～10 月上旬的改动，**比远端仓库新**；接手时必须以本站部署版本为基线，并建议先做一次代码归档/推送（见 12 节 P0-7）。

---

## 4. 数据模型（SQLite，WAL 模式）

文件：`/opt/paper-reader-data/paper_reader.db`（容器内 `/data/paper_reader.db`）。
当前数据量：docs=2、blocks=368、cache=1698、glossary=61、picks=62、pick_docs=0。

| 表 | 字段 | 说明 |
|---|---|---|
| `docs` | id, filename, title, num_pages, n_blocks, meta(JSON), created_at | 一篇论文一行 |
| `blocks` | doc_id, idx, page, kind, level, cont, text, zh, status, hash | 段落块；`kind`=title/heading/para/caption/ref/meta/refhead；`status`=pending/done/error/skip；`zh` 为译文；`hash` 用于译文缓存 |
| `cache` | hash, zh, model, created_at | 段落级翻译缓存（跨文档复用，省 token） |
| `glossary` | en, zh, note, builtin | 术语表（翻译时注入约束） |
| `picks` | id, oa_id, category, title, authors, year, venue, doi, landing_url, pdf_url, pdf_ok, zh, why, prompt_ver, used_at, created_at | 「论文发现」推荐池 |
| `pick_docs` | pick_id, doc_id, created_at | 推荐与已导入文档的关联 |

**数据目录刻意放在项目外**（`/opt/paper-reader-data`）。原因：SQLite 的 `-wal/-shm` 三件套必须与主库同生共死，项目目录曾被同步/清理工具碰到，历史上两次数据库损坏（2026-09-20、2026-10-08，见 README 注释）。**不要**把 `paper_reader.db` 移回项目目录，也不要单拷 `.db` 而丢掉 `-wal/-shm`（正确做法：停容器后再拷，或用 SQLite backup API）。

### 4.1 数据库损坏的识别与修复（重要运维知识）

- **现象**：页面保存失败、接口 500，容器日志出现 `sqlite3.DatabaseError: database disk image is malformed`。
- **检查**：`sqlite3 paper_reader.db "PRAGMA integrity_check;"`（正常返回 `ok`）。
- **修复流程（本次 2026-11-26 实战验证过）**：
  1. `cd /opt/paper-reader && docker compose stop`
  2. 整目录备份：`cp -a /opt/paper-reader-data /opt/paper-reader-backups/paper-reader-data-backup-$(date +%Y%m%d-%H%M%S)`
  3. `cp -a /opt/paper-reader-data/paper_reader.db /tmp/repair/`（停服后 `-wal/-shm` 已合并，单文件即可）
  4. `sqlite3 /tmp/repair/paper_reader.db ".recover" | sqlite3 /tmp/repair/recovered.db`
  5. `sqlite3 /tmp/repair/recovered.db "PRAGMA integrity_check;"` 应为 `ok`；核对各表行数
  6. 替换：停服 → `cp recovered.db /opt/paper-reader-data/paper_reader.db` → `chown 10001:10001` → 启动容器
  7. 验证：`/api/health`、`/api/docs`、翻译一篇验证写库
- **预防**：已有每日备份（04:17）；重启/升级前先手动备份；不要用 cp 拷贝运行中的库。

---

## 5. 接口清单（调试与自动化用）

鉴权：nginx Basic Auth。Base：`https://paper.lushixiao.cn`。示例：`curl -u paper:**** https://paper.lushixiao.cn/api/health`。

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/health` | `probe=1` 顺带真实探测 TTS | 健康检查（含 llm_ready/tts_ready） |
| GET | `/api/stats` | — | 用量/缓存统计 |
| GET | `/api/config` | — | 生效配置（脱敏后的公开字段） |
| GET | `/api/docs` | — | 书库列表（含翻译进度） |
| POST | `/api/docs` | multipart `file` | 上传 PDF（≤80MB），返回解析结果 |
| GET | `/api/docs/{id}` | — | 文档详情 + 全部段落 |
| DELETE | `/api/docs/{id}` | — | 删除文档（同时清理语音缓存） |
| GET | `/api/docs/{id}/file` | — | 原始 PDF |
| POST | `/api/docs/{id}/reset` | `keep_done=true/false` | 重置译文 |
| GET | `/api/docs/{id}/translate` | `idx=1,2,3`、`force=1` | **SSE 流式翻译**（前端主要用法） |
| POST | `/api/docs/{id}/blocks/{idx}/translate` | `force=true` | 单段翻译（省流量模式用） |
| GET | `/api/docs/{id}/blocks/{idx}/speech` | `voice` | 朗读某段中文 |
| GET | `/api/tts/config` / `GET /api/tts/probe` | `force=1` | TTS 配置 / 真实探测 |
| POST | `/api/tts` | JSON `{text, voice?}` | 任意文本合成（≤6000 字，返回音频字节） |
| GET/DELETE | `/api/tts/cache` | `doc_id?` | 语音缓存查询/清理 |
| POST | `/api/tts/cache/purge` | `doc_id?` | 关闭页面时上报清理（beacon） |
| GET/DELETE | `/api/cache` | `doc_id?` | 译文缓存查询/清理 |
| GET | `/api/docs/{id}/export.md` | — | 导出双语 Markdown |
| GET/POST/DELETE | `/api/glossary`、`/api/glossary/{en}` | — | 术语表增删改查 |
| GET | `/api/picks/next` | — | 取下一篇推荐；池空时 `{"pick":null,"generating":true}` |

**验证脚本常用命令**（SSE 用 `curl -N`）：
```bash
curl -u paper:**** https://paper.lushixiao.cn/api/health
curl -u paper:**** -F "file=@tools/out/sample_paper.pdf" https://paper.lushixiao.cn/api/docs
curl -u paper:**** -N "https://paper.lushixiao.cn/api/docs/<doc_id>/translate?idx=10"
curl -u paper:**** -X POST -H 'Content-Type: application/json' \
     -d '{"text":"测试语音"}' https://paper.lushixiao.cn/api/tts -o /tmp/t.mp3
```

---

## 6. 配置项（`.env`）

规则：所有配置走环境变量（`app/config.py`）；**改 `.env` 后必须 `docker compose up -d --force-recreate`**（restart 不会重新读 env_file 之外的变化）。

关键项（VPS 现值）：

| 变量 | 现值/说明 |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek key（密钥）；`https://api.deepseek.com/v1`；`deepseek-chat` |
| `LLM_TEMPERATURE` / `LLM_CONCURRENCY` / `LLM_TIMEOUT` / `LLM_RETRIES` | 1.3（DeepSeek 推荐翻译温度）/ 6 / 120 / 2 |
| `LLM_MOCK` | 0（1 = 不调模型，本地演示） |
| `TRANSLATE_MAX_CHARS` / `TRANSLATE_REFS` | 1800（单段上限）/ 0（不翻译参考文献） |
| `GLOSSARY_ENABLED` / `GLOSSARY_MODE` | 1 / match |
| `MAX_UPLOAD_MB` | 80 |
| `TTS_API_KEY` / `TTS_BASE_URL` / `TTS_MODEL` / `TTS_VOICE` / `TTS_FORMAT` | 小米 MiMo（密钥）/ `https://api.xiaomimimo.com/v1` / `mimo-v2.5-tts` / `mimo_default` / `mp3` |
| `TTS_TIMEOUT` / `TTS_CACHE_ON_SHUTDOWN` / `TTS_CACHE_MAX_MB` | 180 / 0 / 500 |
| `HOST_PORT`（仅 compose 用） | `127.0.0.1:18010`（只绑本机，公网入口由 nginx 提供） |
| `DATA_HOST_DIR`（仅 compose 用） | `/opt/paper-reader-data` |

论文发现（`PICK_*`）与本地模型（`LLM_BASE_URL` 指向 Ollama）等完整说明见 README。

---

## 7. 本地开发与测试

本地仓库路径（Mac）：`~/Desktop/01_Agent开发/插件与技能/dph插件/paper-reader`
本地数据目录：`~/paper-reader-data`（已同步为修复后的库）
本地端口：`8010`（`.env` 的 `HOST_PORT=8010`）

```bash
cd <repo>
docker compose up -d --build      # 构建并启动 → http://localhost:8010
docker compose logs -f            # 看日志
make test                         # 生成样例 PDF + 切片自检
make sample                       # 只生成样例 PDF
docker compose down               # 停止

# 不想花钱、只想看流程：.env 里设 LLM_MOCK=1
```

**架构注意**：本地 Mac 是 `arm64`，VPS 是 `amd64` —— **本地构建的镜像不能直接拷到 VPS**，必须在 VPS 上重新 `docker compose build`（构建约 10～30 秒，依赖已缓存）。

前端是原生静态文件（无构建步骤），改 `app/static/*` 后重启容器即可生效。

---

## 8. 部署与发布流程（当前 + 建议改进）

### 8.1 当前流程（手工）

1. 在本地仓库改代码，本地验证（第 7 节）。
2. 备份：数据库 + 代码目录（见 9.3）。
3. 把改动同步到 VPS（SFTP/rsync；**不要覆盖 VPS 的 `.env`**，它含生产密钥和端口映射）。
4. 在 VPS 构建并重启：
   ```bash
   cd /opt/paper-reader
   docker compose build
   docker compose up -d
   docker compose ps            # healthy
   ```
5. 跑验证清单（8.2）。
6. 记录变更（建议写入 `CHANGELOG.md`）。

### 8.2 发布后验证清单（每次必做）

- [ ] `curl http://127.0.0.1:18010/api/health` → `ok:true, llm_ready:true, tts_ready:true`（TTS 可用 `probe=1` 真探）
- [ ] `https://paper.lushixiao.cn/` 无密码返回 401，有密码返回 200
- [ ] `/api/docs` 返回书库 2 篇（不丢数据）
- [ ] 挑一段未翻译的段落 → `POST .../translate?force=true` → 200 且 `zh` 为中文、状态 `done`
- [ ] `POST /api/tts` 返回音频字节
- [ ] SSE：`curl -N .../translate?idx=<已译段>` 返回 `text/event-stream`
- [ ] `docker stats` 内存正常（<300MB）；`df -h` 磁盘正常
- [ ] 代理没被影响：`systemctl is-active sing-box` = active

### 8.3 回滚

- 代码：保留上一份 `/opt/paper-reader` 目录副本（如 `/opt/paper-reader.prev/`），出问题整目录换回并重建。
- 镜像：`docker tag paper-reader:latest paper-reader:prev`（发布前打标签），回滚时 `docker tag paper-reader:prev paper-reader:latest` 再 `up -d`。
- 数据：用 `/opt/paper-reader-data/backups/paper_reader-*.db` 恢复（停容器 → 替换 → 启动）。
- 兜底：本地 Mac 上仍保留一份可运行的数据/代码。

### 8.4 建议改进（P0-7）

建立 Git 工作流 + 一键部署脚本：
```bash
# 示例（待实现）
./scripts/deploy.sh  # 本地推代码 → VPS 备份 → build → health 检查 → 失败自动回滚
```

---

## 9. 运维手册

### 9.1 常用命令

```bash
cd /opt/paper-reader
docker compose ps                     # 状态
docker compose logs -f --tail=100     # 日志
docker compose restart                # 重启
docker compose stop / up -d           # 停止/启动
docker stats --no-stream              # 资源
systemctl status nginx certbot.timer
nginx -t && systemctl reload nginx    # 改 nginx 后
```

### 9.2 证书续期

```bash
certbot certificates                  # 查看两份证书与到期时间
certbot renew --dry-run --no-random-sleep-on-renew   # 演练（已通过）
systemctl list-timers | grep certbot  # 自动续期定时器
```
续期走 HTTP-01：`/var/www/certbot` + nginx :80 的 `/.well-known/acme-challenge/`。**不要删掉 nginx :80 的 ACME location**。

### 9.3 数据备份与恢复

- 自动：`/etc/cron.d/paper-reader-backup` → 每天 04:17 调用 `/usr/local/bin/paper-reader-backup.sh`，用 SQLite backup API 生成一致性备份到 `/opt/paper-reader-data/backups/`，保留 14 天；日志 `/var/log/paper-reader-backup.log`。
- 手动：`/usr/local/bin/paper-reader-backup.sh`
- 恢复：
  ```bash
  cd /opt/paper-reader && docker compose stop
  cp /opt/paper-reader-data/backups/paper_reader-YYYY-MM-DD_HHMM.db /opt/paper-reader-data/paper_reader.db
  chown 10001:10001 /opt/paper-reader-data/paper_reader.db
  rm -f /opt/paper-reader-data/paper_reader.db-wal /opt/paper-reader-data/paper_reader.db-shm
  docker compose up -d
  ```
- 建议（P1）：把备份再同步到对象存储（Cloudflare R2 已有账号）或用户 Mac。

### 9.4 监控阈值

| 指标 | 当前 | 告警建议 |
|---|---|---|
| 内存 | 480/1961MB | >1600MB 告警 |
| 磁盘 | 2.3/20GB | >15GB 告警（PDF 增长/备份堆积） |
| 容器内存 | 56MB | >500MB 检查泄漏（PDF 解析峰值短时高属正常） |
| 证书 | 2027-02-24 到期 | 剩余 <21 天检查续期 |
| 备份 | 每日 1 份 | 连续 2 天缺失告警 |

### 9.5 与代理服务共存（红线）

同机跑着用户主力代理（sing-box）和订阅服务（nginx:2083）。**禁止**：重启/停止/修改 `sing-box`、占用 `443/udp`、`8443/tcp`、`2083/tcp`、修改 ufw 已有规则、改动 `proxy-sub` 站点。改 nginx 时只动 `sites-available/paper-reader`。

---

## 10. 安全现状与风险

**现状**：HTTPS（CF + 源站 LE 证书）、Basic Auth（2 个共享账号）、按 IP 限速、容器只监听 127.0.0.1、数据/密钥仅服务器可读、Docker 非 root 用户运行、每日备份。

**风险清单（对应优化项）**：
1. 共享账号：无法按人踢出、无审计；密码较弱（用户指定）。
2. Cloudflare 后限速按 CF 边缘 IP 统计 → 多用户共享额度、无法按真实用户限速（缺 `real_ip`/`CF-Connecting-IP` 处理）。
3. 应用无用户体系/无配额 → 任何登录者可消耗用户的 DeepSeek/TTS 额度。
4. 书库全局共享，任何登录者可删除任何论文。
5. 无 CSRF/会话概念（Basic Auth + 简单 JSON API，风险低但需注意）。
6. `.env` 明文密钥（服务器文件 600，可接受；建议后续接密钥管理）。
7. Docker 日志无大小限制（未配 `daemon.json` log-opts），长期会增长。
8. PDF 上传仅按大小限制，无内容校验/杀毒。

---

## 11. 已知问题与技术债

| # | 问题 | 影响 | 备注 |
|---|---|---|---|
| 1 | SQLite 曾损坏（2026-11-26 已修复） | 翻译/推荐报 500 | 历史遗留（2026-09-20、10-08 亦有记录）；已加每日备份 |
| 2 | 代码仓库落后线上版本 | Agent 拿不到最新代码 | GitHub 最后推送 2026-09-15；线上含 9 月下旬~10 月上旬改动 |
| 3 | 单书库、单租户 | 多人共享/互删 | 需要多租户改造 |
| 4 | Basic Auth 无用户体系 | 无法按人管理/审计/限流 | 见优化 P0 |
| 5 | CF 代理导致限速粒度失真 | 限速上限变成全局 | 需 nginx real_ip + CF-Connecting-IP |
| 6 | 无任何自动化测试/CI | 改动风险高 | 只有 tools/smoke_test.py（切片自检） |
| 7 | 无部署脚本 | 手工同步易漏/易错 | 见 8.4 |
| 8 | 「论文发现」偶发为空 | 体验不稳定 | 依赖 LLM 生成，失败时池空；可加降级/重试 |
| 9 | 无成本配额/用量告警 | 可能超支 LLM/TTS | 可做 token 统计与额度 |
| 10 | Docker 日志未限制大小 | 长期磁盘增长 | 建议 daemon.json log-opts |
| 11 | 单实例 1 vCPU | 同时上传/解析大 PDF 会慢 | PyMuPDF 解析 CPU 密集 |
| 12 | 前端无构建/无类型 | 维护成本 | 保持轻量可接受 |

---

## 12. 全面优化路线图（带验收标准）

> 建议按 P0 → P1 → P2 顺序；每项独立可验收；改前备份、改后跑第 8.2 清单。

### P0（安全/稳定/工程基线）

| # | 任务 | 验收标准 |
|---|---|---|
| P0-1 | **用户体系**：用应用内登录/会话替代共享 Basic Auth（至少支持多账号、退出、密码修改、禁用） | 每个账号独立密码；停用某账号后其请求 401；不影响现有 paper/guest 登录 |
| P0-2 | **多租户/独立书库**：每用户独立书库与删除权限 | 用户 A 看不到/删不掉用户 B 的论文；owner 可管理全部 |
| P0-3 | **真实客户端 IP**：nginx 接入 `real_ip`（信任 CF 网段）+ 限速按真实 IP | `$remote_addr` 为真实 IP；不同用户互不挤占限速额度 |
| P0-4 | **数据库自检**：启动时 `PRAGMA integrity_check` 失败告警；WAL 定期 checkpoint；升级前自动备份 | 人为制造损坏可被检测并拒绝启动/告警；备份文件自动生成 |
| P0-5 | **成本控制**：按用户统计 token/字符用量 + 月额度 + 超额拒绝；TTS 同理 | `/api/stats` 可按用户展示；超限返回明确错误 |
| P0-6 | **密钥管理**：`.env` 轮换流程文档化；禁止密钥进入仓库/日志 | `git log -S` 无密钥；日志无密钥明文 |
| P0-7 | **代码归档 + 部署脚本**：把线上版本推到 Git（建议私有仓库或确认公开范围）；`scripts/deploy.sh` 一键备份→构建→健康检查→失败回滚 | 新环境按文档可复现部署；部署失败自动回滚 |

### P1（功能/体验/可观测）

| # | 任务 | 验收标准 |
|---|---|---|
| P1-1 | 自动化测试：pytest（API/存储/占位符）+ 扩展 smoke_test + CI（GitHub Actions） | 核心接口测试通过；PR 自动跑 |
| P1-2 | 监控与告警：健康检查、证书到期、备份缺失、磁盘/内存阈值（Uptime Kuma 或脚本+Telegram） | 故意停容器可收到告警 |
| P1-3 | 移动端体验与登录页：原生登录页替代浏览器 Basic Auth 弹窗；手机适配检查 | 手机上可正常上传/阅读/朗读 |
| P1-4 | 「论文发现」稳定性：失败重试、降级提示、池空补货策略 | 连续 10 次请求至少 9 次有结果或明确提示 |
| P1-5 | WAL/日志/缓存治理：Docker log 轮转、TTS/译文缓存定期清理策略 | 运行 30 天磁盘增长可控 |
| P1-6 | 备份上云：每日备份同步到 R2/S3，保留策略 | 本机磁盘损坏可从云端恢复 |

### P2（性能/架构/长期）

| # | 任务 | 验收标准 |
|---|---|---|
| P2-1 | 解析/翻译并发治理：解析线程池、上传队列、超时与友好报错 | 5 人同时上传 20MB PDF 不崩、不互相阻塞 |
| P2-2 | 存储演进：数据量增大后迁移 PostgreSQL（或每用户独立 SQLite） | 迁移脚本 + 回滚方案 + 数据校验 |
| P2-3 | 前端工程化（可选）：轻量构建/组件化，保留无构建兜底 | 功能不回退 |
| P2-4 | Cloudflare 加固：WAF 规则、Bot 防护、缓存规则（静态资源）| 攻击/扫描流量被拦截且不影响正常用户 |
| P2-5 | 可观测性：结构化日志 + 请求 ID + 慢请求统计 | 可定位单次失败链路 |

---

## 13. Agent 接手 Checklist

**第 0 步：拿到访问**
- 网站账号：`paper` / `guest`（密码向用户要，见 2.5）
- VPS：Evoxt 控制台取 root 密码；`ssh root@23.27.0.190`
- 代码：本地 Mac 仓库目录（或先从 GitHub clone，再与线上对比）

**第 1 步：读懂（不修改）**
1. `README.md`（功能/配置/切片/提示词/已知边界）
2. 本文（部署/运维/红线/路线图）
3. `app/main.py` 路由 → `app/store.py` 数据层 → `app/translator.py` 翻译链路
4. `docker-compose.yml`、`Dockerfile`、`.env`（只看键名，值脱敏）

**第 2 步：本地跑通**
```bash
docker compose up -d --build
curl http://127.0.0.1:8010/api/health
# 浏览器打开 http://localhost:8010（或 curl -u 测试）
make test
```

**第 3 步：复现现状**
- 上传一篇小 PDF，翻译一段，确认 200 且中文正确
- 检查 `/api/picks/next`、TTS 是否正常
- 跑一遍第 8.2 验证清单（先记录基线）

**第 4 步：领任务**
- 从第 12 节按顺序领；每项先写 3～5 行方案（改哪些文件、如何验证、回滚方式），与用户确认后再动手
- 每次只做一项，小步提交

**第 5 步：改 + 验证**
- 本地验证 → 备份 → 同步到 VPS → build → 第 8.2 清单
- 用可复现证据说话（curl 输出、日志、截图），不要只说「改好了」

**第 6 步：收尾**
- 更新 README/本文档的变更记录
- 保留回滚包与备份
- 向用户说明：改了什么、怎么验证、风险与后续

**红线（违反即回滚）**
- ❌ 不动 `sing-box` / 代理端口 / `proxy-sub` / ufw 既有规则 / 证书配置（除非任务明确）
- ❌ 不删 `/opt/paper-reader-data`、不覆盖 VPS 的 `.env`、不把密钥写进代码/日志/仓库
- ❌ 不在未备份的情况下动生产数据库
- ❌ 不跳过更新后的验证清单
- ❌ 不把「用户可见行为」的改动（登录、共享、删除权限）在未确认的情况下直接上线

---

## 14. 可直接粘贴给接手 Agent 的提示词模板

```text
你是这个项目的接手指工程师。请先完整阅读项目根目录的 README.md 和 AGENT_ONBOARDING.md，
然后按其中「Agent 接手 Checklist」执行：

1) 先不要改任何代码。用 5 分钟复述：项目是什么、线上拓扑、数据在哪、有哪些红线；
2) 在本地把项目跑起来（docker compose up -d --build），验证 /api/health、上传一篇小 PDF、
   翻译一段、TTS 一段，全部通过后把结果贴给我；
3) 从 AGENT_ONBOARDING.md 第 12 节路线图按 P0→P1 顺序选一项，先给我 3~5 行方案
  （改哪些文件、如何验证、如何回滚），我确认后你才动手；
4) 每次改动必须：本地验证 → 备份 → 部署 → 跑第 8.2 验证清单 → 给我可复现证据；
5) 严禁：改动 sing-box/代理/证书/Cloudflare DNS、删除数据、把密钥写进代码或日志、
   未备份就动生产数据库。

你现在的任务：<在这里写具体任务，例如「实现 P0-1 用户体系」>。
```

---

## 15. 附录

### A. 常用命令速查

```bash
# 状态
cd /opt/paper-reader && docker compose ps && docker stats --no-stream
curl -s http://127.0.0.1:18010/api/health
curl -s -o /dev/null -w '%{http_code}\n' https://paper.lushixiao.cn/            # 期望 401
curl -s -o /dev/null -w '%{http_code}\n' -u paper:'密码' https://paper.lushixiao.cn/  # 期望 200
# 日志
docker compose logs -f --tail=200 | grep -iE 'error|Traceback|malformed'
journalctl -u sing-box -n 50          # 代理（只读）
tail -50 /var/log/nginx/paper-reader.access.log
# 备份/恢复/证书/DB 修复：见 9.3、9.2、4.1
```

### B. 关键路径速查

| 用途 | 路径 |
|---|---|
| 源码 | `/opt/paper-reader`（本地：`~/Desktop/01_Agent开发/插件与技能/dph插件/paper-reader`） |
| 生产数据 | `/opt/paper-reader-data`（容器内 `/data`） |
| 备份 | `/opt/paper-reader-data/backups/`、`/opt/paper-reader-backups/` |
| nginx 站点 | `/etc/nginx/sites-available/paper-reader` |
| 限速 | `/etc/nginx/conf.d/paper-reader-limits.conf` |
| 证书 | `/etc/letsencrypt/live/{paper.lushixiao.cn, 23.27.0.190.sslip.io}/` |
| Basic Auth | `/etc/nginx/.paper-reader.htpasswd` |
| 备份脚本 | `/usr/local/bin/paper-reader-backup.sh` |
| 接入信息（含密码，用户侧） | 用户桌面《paper-reader-VPS-接入信息.md》 |

### C. 变更记录（本机）

| 日期（农历） | 变更 |
|---|---|
| 丙午年孟冬之十七（2026-11-25） | 用户购入 VPS；由外部助手部署 sing-box 代理 + nginx 订阅（与本项目无关） |
| 丙午年孟冬之十八（2026-11-26） | 项目从 Mac 迁移到 VPS（Docker）；配置 HTTPS、Cloudflare、Basic Auth、限速、每日备份；域名切换为 `paper.lushixiao.cn`；修复 SQLite 损坏；删除旧 Cloudflare Access 邮箱登录 |

### D. 名词表

- **SSE**：Server-Sent Events，本项目翻译用「逐段流式推送」的方式（`GET /api/docs/{id}/translate`）。
- **WAL**：SQLite 预写日志模式（`-wal`/`-shm` 文件），提高并发读写；备份/拷贝时必须整体处理。
- **占位符保护（protect.py）**：把公式/引用/编号挖空再送模型翻译，避免模型破坏原文结构。
- **Basic Auth**：浏览器原生账号密码框，本项目当前用它做访问控制。
- **CF 代理（橙云）**：Cloudflare 反向代理，隐藏源站 IP、提供 TLS 与加速；源站用 Full 模式校验。
- **多租户**：多个用户各自独立书库/权限，是当前最大的功能升级方向。

---

*本文档由当前会话生成并同步到 VPS `/opt/paper-reader/AGENT_ONBOARDING.md`；随代码变更请同步更新。*

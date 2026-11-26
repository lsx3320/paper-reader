# CLAUDE.md

完整交接文档见 **`AGENT_ONBOARDING.md`**；Agent 入口速览见 **`AGENTS.md`**。

简要：论文双语阅读器（FastAPI + SQLite + 原生前端，Docker）。
线上 https://paper.lushixiao.cn ；生产机 VPS 23.27.0.190（代码 /opt/paper-reader，数据 /opt/paper-reader-data）。

红线：不改同机代理 sing-box/代理端口/证书/Cloudflare DNS；不删生产数据；密钥不入库；动数据库前先备份；改完跑验证清单。

发布用 `./scripts/deploy.sh`（先 `--dry-run`）。不覆盖 VPS `.env`，不从 Mac 拷镜像；失败会回滚。详见 `AGENT_ONBOARDING.md` 第 8.4 节。

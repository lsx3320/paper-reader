# AGENTS.md · 给 AI Agent 的入口

> 如果你是接手本项目的 AI Agent（Claude Code / Codex / Cursor / DSH / 其它），**先读 `AGENT_ONBOARDING.md`**（完整交接文档：拓扑、代码地图、数据模型、接口、部署、运维、红线、优化路线图）。

## 30 秒速览

- 项目：论文双语阅读器（FastAPI + SQLite + 原生前端，Docker 部署）。
- 线上：https://paper.lushixiao.cn（Cloudflare 代理 + nginx Basic Auth）。
- 生产机：VPS 23.27.0.190，代码 `/opt/paper-reader`，数据 `/opt/paper-reader-data`。
- 本地开发：`docker compose up -d --build` → http://localhost:8010；`make test` 自检。
- 发布：本地验证 → 备份 → 同步到 VPS（保留 VPS 的 `.env`）→ `docker compose up -d` → 跑《AGENT_ONBOARDING.md》第 8.2 节验证清单。

## 红线（违反即回滚）

1. 不改动同机代理 `sing-box`、代理端口（443/udp、8443/tcp、2083/tcp）和 `proxy-sub` 站点。
2. 不动证书/Cloudflare DNS（除非任务明确要求）。
3. 不删 `/opt/paper-reader-data`、不覆盖 VPS 的 `.env`。
4. 密钥（LLM/TTS）不得写入代码、日志、仓库、聊天。
5. 动生产数据库前必须备份；改完必须跑验证清单再宣称完成。

## 工作方式

- 每项任务先给 3～5 行方案（改哪些文件、如何验证、如何回滚），确认后动手。
- 小步提交，每步给可复现证据（curl 输出 / 日志 / 截图）。
- 优化任务按 `AGENT_ONBOARDING.md` 第 12 节路线图（P0 → P1 → P2）领。

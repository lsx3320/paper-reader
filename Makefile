# 便捷命令：make up / make logs / make test ...
IMAGE ?= paper-reader:latest

.PHONY: help up build down logs restart sample test shell clean llm-up llm-down llm-status use-local use-cloud

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s $$'\t'

up: ## 构建并后台启动（http://localhost:8000）
	docker compose up -d --build

build: ## 只构建镜像
	docker compose build

down: ## 停止并移除容器
	docker compose down

restart: ## 重启容器（改 .env 后用）
	docker compose restart

logs: ## 跟踪日志
	docker compose logs -f

sample: ## 生成样例双栏论文 PDF
	python3 tools/make_sample_pdf.py tools/out/sample_paper.pdf

test: sample ## 切片自检（打印段落块并断言）
	python3 tools/smoke_test.py

shell: ## 进入容器
	docker compose exec paper-reader sh

clean: ## 清空运行时数据（PDF 与译文缓存）
	rm -rf data/*.db data/*.db-wal data/*.db-shm data/pdfs/*

llm-up: ## 启动本地小模型服务（Ollama + Metal，内存调优）
	@nohup ./scripts/serve_local_llm.sh > /tmp/local-llm.log 2>&1 & sleep 4; \
	 curl -s http://127.0.0.1:11434/api/version && echo " ← 本地模型服务已就绪"

llm-down: ## 停止本地模型服务
	@./scripts/serve_local_llm.sh --stop

llm-status: ## 查看本地模型状态与内存占用
	@curl -s http://127.0.0.1:11434/api/version >/dev/null && echo "服务: 运行中" || echo "服务: 未运行"
	@ollama ps

use-local: ## 切换为本地模型（Ollama + Qwen3-1.7B）
	@cp .env.local-llm.example .env && docker compose up -d --force-recreate >/dev/null && echo "已切换为本地模型，见 http://localhost:$$(grep -oE '[0-9]+' <<< $$(grep HOST_PORT .env | cut -d= -f2))"

use-cloud: ## 切换为云端 DeepSeek
	@cp .env.cloud.example .env && echo "已切到云端配置，请把 LLM_API_KEY 填进 .env 后执行：docker compose up -d --force-recreate"

clean-tts: ## 清空已合成的语音缓存（下次朗读会重新合成并重新计费）
	rm -f data/tts/*.wav && echo "语音缓存已清空"

clean-cache: ## 清空翻译缓存（注意：之后重新翻译会产生真实费用）
	@printf '这会清掉所有已翻译段落的缓存，之后重新导入论文将重新调用大模型计费。\n确认请输入 yes：' && read ans && [ "$$ans" = "yes" ] && \
	 curl -s -X DELETE http://127.0.0.1:$$(grep HOST_PORT .env | cut -d= -f2)/api/cache | python3 -m json.tool || echo "已取消"

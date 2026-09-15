#!/usr/bin/env bash
# ⚠️ 注意：本地模型权重已按你的要求清理，本脚本当前不可直接运行。
# 需要时按下面三步重建（约 1.1 GB，走 hf-mirror 镜像，实测 6 MB/s）：
#
#   1) 下载 GGUF（Qwen3-1.7B Q4_K_M，1056 MB）
#      curl -L -o /Users/Zhuanz1/Desktop/models/llm/Qwen3-1.7B-Q4_K_M.gguf \
#        "https://hf-mirror.com/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf"
#   2) 写 Modelfile（关键：num_thread 3 / num_batch 128 控制发热，模板内预置空 think 块关闭思考链）
#      ollama create qwen3-1.7b-zh -f Modelfile.qwen3
#   3) cp .env.local-llm.example .env && docker compose up -d --force-recreate
#
# 本地小模型推理服务（Ollama + Apple Metal），按 8GB 内存机器做极限瘦身。
#
#   ./scripts/serve_local_llm.sh          # 前台启动
#   nohup ./scripts/serve_local_llm.sh > /tmp/local-llm.log 2>&1 &   # 后台常驻
#   ./scripts/serve_local_llm.sh --stop   # 停掉
#
# 资源优化点（相对 Ollama 默认）：
#   FLASH_ATTENTION=1     Metal 上开 flash attention，显存/内存更省、速度更快
#   KV_CACHE_TYPE=q8_0    KV 缓存从 f16 压到 q8_0，4096 上下文省约一半内存
#   NUM_PARALLEL=1        单槽位，避免按槽位复制 KV 缓存（8GB 机器必须）
#   CONTEXT_LENGTH=4096   论文段落级翻译够用，比默认 8192 省一半
#   MAX_LOADED_MODELS=1   同时只驻留一个模型
#   KEEP_ALIVE=2m         空闲 2 分钟卸载模型 —— 无风扇 MacBook Air 的关键：待机不发热
#   线程/批次限制在模型侧（Modelfile: num_thread 3 / num_batch 128），压低持续功耗

set -euo pipefail

export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-4096}"
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-2m}"   # 空闲 2 分钟即卸载，无风扇机器不长期积热

if [[ "${1:-}" == "--stop" ]]; then
  pkill -f "ollama serve" && echo "已停止本地模型服务" || echo "服务未在运行"
  exit 0
fi

# Ollama.app 自带的服务是默认参数（f16 KV），先让它退出，避免抢端口
if pgrep -f "Ollama.app/Contents/MacOS/Ollama" >/dev/null 2>&1; then
  echo "退出 Ollama.app（它的服务用的是未调优参数）…"
  osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
  sleep 3
fi
pkill -f "Resources/ollama serve" >/dev/null 2>&1 || true
sleep 1

echo "启动调优后的 ollama serve："
echo "  HOST=$OLLAMA_HOST  KV=$OLLAMA_KV_CACHE_TYPE  PARALLEL=$OLLAMA_NUM_PARALLEL  CTX=$OLLAMA_CONTEXT_LENGTH"
exec ollama serve

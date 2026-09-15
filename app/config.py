"""运行配置：全部通过环境变量注入，便于 Docker 部署。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# 提示词版本号：修改提示词后 +1，可让翻译缓存自动失效
PROMPT_VERSION = "v3"

# 内置学术术语表（可在界面中增删改）
DEFAULT_GLOSSARY: list[tuple[str, str]] = [
    ("Transformer", "Transformer"),
    ("attention mechanism", "注意力机制"),
    ("self-attention", "自注意力"),
    ("cross-attention", "交叉注意力"),
    ("large language model", "大语言模型"),
    ("foundation model", "基础模型"),
    ("pre-training", "预训练"),
    ("fine-tuning", "微调"),
    ("prompt", "提示"),
    ("token", "词元"),
    ("embedding", "嵌入"),
    ("latent space", "潜在空间"),
    ("ablation study", "消融实验"),
    ("baseline", "基线"),
    ("overfitting", "过拟合"),
    ("underfitting", "欠拟合"),
    ("gradient descent", "梯度下降"),
    ("loss function", "损失函数"),
    ("objective function", "目标函数"),
    ("benchmark", "基准测试"),
    ("dataset", "数据集"),
    ("inference", "推理"),
    ("downstream task", "下游任务"),
    ("state-of-the-art", "当前最优"),
    ("neural network", "神经网络"),
    ("convolutional neural network", "卷积神经网络"),
    ("recurrent neural network", "循环神经网络"),
    ("reinforcement learning", "强化学习"),
    ("supervised learning", "监督学习"),
    ("unsupervised learning", "无监督学习"),
    ("zero-shot", "零样本"),
    ("few-shot", "少样本"),
    ("chain-of-thought", "思维链"),
    ("retrieval-augmented generation", "检索增强生成"),
    ("hallucination", "幻觉"),
    ("robustness", "鲁棒性"),
    ("generalization", "泛化能力"),
    ("throughput", "吞吐量"),
    ("latency", "延迟"),
    ("quantization", "量化"),
    ("knowledge distillation", "知识蒸馏"),
    ("multimodal", "多模态"),
    ("encoder", "编码器"),
    ("decoder", "解码器"),
    ("layer normalization", "层归一化"),
    ("residual connection", "残差连接"),
    ("pooling", "池化"),
    ("batch size", "批大小"),
    ("learning rate", "学习速率"),
    ("hyperparameter", "超参数"),
    ("validation set", "验证集"),
    ("test set", "测试集"),
    ("recall", "召回率"),
    ("precision", "精确率"),
    ("accuracy", "准确率"),
    ("cross-entropy", "交叉熵"),
    ("regularization", "正则化"),
    ("ground truth", "真实标签"),
    ("scaling law", "缩放定律"),
    ("emergent ability", "涌现能力"),
    ("contrastive learning", "对比学习"),
]


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：不覆盖已存在的环境变量。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(_env(key) or default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    upload_dir: Path

    # LLM（OpenAI 兼容协议，可用于 DeepSeek / OpenAI / Kimi / Ollama / vLLM 等）
    api_key: str
    base_url: str
    model: str
    temperature: float
    concurrency: int
    timeout: float
    mock: bool
    mock_delay: float
    retries: int
    max_tokens: int
    no_think: bool
    glossary_mode: str
    request_interval: float

    # 切片与翻译策略
    max_chars: int
    max_upload_mb: int
    translate_refs: bool
    glossary_enabled: bool
    prompt_version: str

    # 语音合成（小米 MiMo mimo-v2.5-tts）
    tts_api_key: str
    tts_base_url: str
    tts_model: str
    tts_voice: str
    tts_format: str
    tts_timeout: float
    tts_dir: Path
    tts_cache_on_shutdown: bool
    tts_concurrency: int
    tts_chunk_chars: int
    tts_cache_max_mb: int
    app_password: str

    default_glossary: list[tuple[str, str]] = field(default_factory=lambda: list(DEFAULT_GLOSSARY))

    @property
    def llm_ready(self) -> bool:
        return self.mock or bool(self.api_key)

    @property
    def tts_ready(self) -> bool:
        return bool(self.tts_api_key)

    def public(self) -> dict:
        return {
            "model": "mock" if self.mock else self.model,
            "base_url": self.base_url,
            "concurrency": self.concurrency,
            "temperature": self.temperature,
            "translate_refs": self.translate_refs,
            "max_chars": self.max_chars,
            "mock": self.mock,
            "max_tokens": self.max_tokens,
            "no_think": self.no_think,
            "glossary_mode": self.glossary_mode,
            "request_interval": self.request_interval,
            "llm_ready": self.llm_ready,
            "tts_ready": self.tts_ready,
            "tts_model": self.tts_model,
            "tts_voice": self.tts_voice,
            "tts_cache_on_shutdown": self.tts_cache_on_shutdown,
            "max_upload_mb": self.max_upload_mb,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    _load_dotenv(root / ".env")

    data_dir = Path(_env("DATA_DIR", str(root / "data"))).expanduser().resolve()
    upload_dir = data_dir / "pdfs"
    upload_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "paper_reader.db",
        upload_dir=upload_dir,
        api_key=_env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY") or _env("OPENAI_API_KEY"),
        base_url=_env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        model=_env("LLM_MODEL", "deepseek-chat"),
        temperature=_env_float("LLM_TEMPERATURE", 1.3),
        concurrency=max(1, _env_int("LLM_CONCURRENCY", 6)),
        timeout=_env_float("LLM_TIMEOUT", 120.0),
        mock=_env_bool("LLM_MOCK", False),
        mock_delay=_env_float("LLM_MOCK_DELAY", 0.25),
        retries=max(0, _env_int("LLM_RETRIES", 2)),
        max_tokens=max(0, _env_int("LLM_MAX_TOKENS", 0)),
        no_think=_env_bool("LLM_NO_THINK", False),
        glossary_mode=(_env("GLOSSARY_MODE", "match").lower() or "match"),
        # 每段翻译之间的强制间隔（秒）：本地模型用它压低 CPU 占空比，控制发热
        request_interval=max(0.0, _env_float("LLM_REQUEST_INTERVAL", 0.0)),
        max_chars=max(200, _env_int("TRANSLATE_MAX_CHARS", 1800)),
        max_upload_mb=max(1, _env_int("MAX_UPLOAD_MB", 80)),
        translate_refs=_env_bool("TRANSLATE_REFS", False),
        glossary_enabled=_env_bool("GLOSSARY_ENABLED", True),
        prompt_version=PROMPT_VERSION,
        tts_api_key=_env("TTS_API_KEY") or _env("MIMO_API_KEY"),
        tts_base_url=_env("TTS_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/"),
        tts_model=_env("TTS_MODEL", "mimo-v2.5-tts"),
        tts_voice=_env("TTS_VOICE", "mimo_default"),
        tts_format=_env("TTS_FORMAT", "wav").lower(),
        tts_timeout=_env_float("TTS_TIMEOUT", 180.0),
        tts_dir=data_dir / "tts",
        # 服务停止（docker compose stop/down/重启）时清空语音缓存，默认开启
        tts_cache_on_shutdown=_env_bool("TTS_CACHE_ON_SHUTDOWN", True),
        tts_concurrency=max(1, _env_int("TTS_CONCURRENCY", 4)),
        # 单次请求的字数上限：越小并发片数越多、首段等待越短；180 字约 9 秒音频
        tts_chunk_chars=max(60, _env_int("TTS_CHUNK_CHARS", 180)),
        # 语音缓存容量上限（MB）：超出后按最久未使用淘汰；0 表示不限制
        tts_cache_max_mb=max(0, _env_int("TTS_CACHE_MAX_MB", 500)),
        # 访问口令：设置后整个站点需要登录（公网暴露时必填）
        app_password=_env("APP_PASSWORD") or _env("PAPER_PASSWORD") or "",
    )

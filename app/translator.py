"""翻译引擎：OpenAI 兼容协议（DeepSeek / OpenAI / Kimi / Ollama / vLLM…）。"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from .config import Settings
from .protect import (clean_output, looks_like_thinking, looks_untranslated,
                      mask, restore, select_glossary)

log = logging.getLogger("translator")

SYSTEM_PROMPT = """你是一名计算机科学与人工智能领域的专业学术论文翻译专家。你的任务是把英文论文片段翻译成地道、规范的简体中文。

必须严格遵守以下规则：
1. 学术风格：语言客观严谨、通顺凝练，符合中文学术期刊规范，坚决杜绝生硬的机器翻译腔。不要逐词直译，按中文语序重组句子。
2. 占位符绝对保护：文本中形如 [[0]]、[[12]] 的标记代表公式、引用、图表编号、代码标识符等不可翻译内容。必须原样保留，不得翻译、改写、增删、调换顺序，也不得在其内部添加空格或标点。
3. 引用与标记：文献引用符号（如 [1]、(Vaswani et al., 2017)）与图表编号（如 Figure 2、Table 1）保持原样。
4. 专业术语规范：采用学术界公认译法（例如：Latent Space → 潜在空间；Ablation Study → 消融实验；Overfitting → 过拟合；Baseline → 基线）。无通用中文对照的专有名词（模型名、数据集名、方法名）保留英文原文。
5. 输出要求：只输出译文正文本身。不要输出任何解释、前言、总结、Markdown 代码块、标题或引号包裹。
6. 段落结构：保持原文的段落与句序，不合并、不拆分、不添加小标题。
7. 若原文本身已是中文，原样返回。"""

STRICT_REMINDER = (
    "注意：上一次输出丢失了占位符。请重新翻译，并确保所有形如 [[0]] 的占位符"
    "都被原样、完整、按序保留（数量必须与原文一致）。只输出译文。"
)


class TranslationError(RuntimeError):
    pass


class Translator:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(settings.concurrency)
        self.stats = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "errors": 0}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.s.base_url,
                timeout=httpx.Timeout(self.s.timeout, connect=20.0),
                limits=httpx.Limits(max_connections=self.s.concurrency * 2,
                                    max_keepalive_connections=self.s.concurrency),
                headers={"Authorization": f"Bearer {self.s.api_key}",
                         "Content-Type": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ 主入口
    async def translate(self, text: str, *, title: str = "", section: str = "",
                        glossary: dict[str, str] | None = None) -> str:
        """翻译单个段落，返回已还原占位符的中文。"""
        masked, tokens = mask(text)
        if not masked.strip():
            return text

        system = self._build_system(glossary or {}, masked)
        user = self._build_user(masked, title, section)

        last_err: Exception | None = None
        for attempt in range(self.s.retries + 1):
            try:
                raw = await self._chat(system, user if attempt == 0
                                       else f"{user}\n\n{STRICT_REMINDER}")
                # 没有闭合标记的思考链无法安全剥离（不知道译文从哪开始），直接重试
                if looks_like_thinking(raw) and "</think" not in raw and attempt < self.s.retries:
                    last_err = TranslationError("输出混入未闭合的思考链")
                    continue
                cleaned = clean_output(raw)
                zh, missing = restore(cleaned, tokens)
                if missing and attempt < self.s.retries:
                    last_err = TranslationError(f"占位符丢失 {missing[:5]}")
                    continue
                if missing:
                    log.warning("占位符未完全还原，缺失=%s，段落=%r", missing[:5], text[:60])
                if (looks_untranslated(zh) or looks_like_thinking(zh)) and attempt < self.s.retries:
                    last_err = TranslationError("输出疑似未翻译或混入思考链")
                    continue
                return zh
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                await asyncio.sleep(min(2 ** attempt, 6))
        raise TranslationError(f"翻译失败：{last_err}")

    # ------------------------------------------------------------------ 提示词
    def _build_system(self, glossary: dict[str, str], text: str) -> str:
        parts = [SYSTEM_PROMPT]
        rules = (select_glossary(text, glossary, mode=self.s.glossary_mode)
                 if self.s.glossary_enabled else [])
        if rules:
            lines = "；".join(f"{en} → {zh}" for en, zh in rules)
            parts.append(f"\n术语表（必须严格遵守以下统一译法）：{lines}")
        if self.s.no_think:
            # Qwen3 系列软开关：在系统提示里声明不思考，配合模板可省掉整段推理链
            parts.append("/no_think")
        return "\n".join(parts)

    @staticmethod
    def _build_user(masked: str, title: str, section: str) -> str:
        head = []
        if title:
            head.append(f"论文标题：{title}")
        if section:
            head.append(f"所在章节：{section}")
        prefix = ("\n".join(head) + "\n\n") if head else ""
        return f"{prefix}请把下面的英文段落翻译为简体中文：\n\n{masked}"

    # ------------------------------------------------------------------ 调用
    async def _chat(self, system: str, user: str) -> str:
        if self.s.mock:
            await asyncio.sleep(self.s.mock_delay)
            return self._mock_output(user)

        payload = {
            "model": self.s.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.s.temperature,
            "stream": False,
        }
        if self.s.max_tokens > 0:      # 0 = 不限制，交给服务端默认
            payload["max_tokens"] = self.s.max_tokens
        client = await self._get_client()
        async with self._sem:
            self.stats["requests"] += 1
            try:
                resp = await client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                self.stats["errors"] += 1
                raise TranslationError(f"网络错误：{exc}") from exc
        if resp.status_code >= 400:
            self.stats["errors"] += 1
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self.stats["errors"] += 1
            raise TranslationError(f"响应解析失败：{resp.text[:300]}") from exc
        usage = data.get("usage") or {}
        self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        return content or ""

    @staticmethod
    def _mock_output(user: str) -> str:
        """离线自测：保留占位符原样，生成可辨识的假译文。"""
        body = user.split("：\n\n")[-1]
        body = body.split("\n\n注意：上一次输出")[0]
        return "【模拟译文】" + body + "（模拟模式，仅供流程验证）"

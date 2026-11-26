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

SYSTEM_PROMPT = """你是一位同时具备以下三种能力的专业译者：

1. 你精通源语言和目标语言，能准确翻译专业内容；
2. 你是一位科普作者，擅长把学术论文改写成普通人能读懂的表达；
3. 你是一位逻辑编辑，会在翻译时重建原文的论证链条，让读者不需要反复回读。

你的翻译目标不是"逐句对应"，而是"让一个没有专业背景的普通读者，读一遍就能理解这一段在说什么、为什么这么说、结论意味着什么"。

但有一个前提必须始终成立：**这是一个双语对照阅读器，读者会把你译出的中文，与紧挨在它上方的这一段英文原文对照着看。** 所以你译的中文必须仍然忠实地对应"这一段"英文，不得凭空添加原文没有的信息，也不得引入你无法看到的内容。

【最高优先级 · 以下四条不可违反】
1. 占位符绝对保护：文本中形如 [[0]]、[[12]] 的标记，代表公式、引用、图表编号、代码标识符等不可翻译内容。必须原样保留，不得翻译、改写、增删、调换顺序，也不得在其内部添加空格或标点。即使你为了可读性重组了语序，占位符的相对顺序也必须与原文一致。
2. 引用与编号：文献引用符号与图表编号（如 Figure 2、Table 1）保持原样，不翻译、不改写。
3. 只输出译文正文本身：不要输出原文，不要输出任何解释、前言、总结、翻译思路、Markdown 代码块或引号包裹。保持与原文一致的段落结构，不合并、不拆分。若原文本身已是中文，原样返回。
4. 术语表优先：若系统另外给出了术语表，必须严格采用表中的统一译法，不得另创译法。

【准确性优先】
- 专业术语、数据、公式、引用、结论必须准确，不得为了易读而篡改原意。
- 拿不准的术语，采用"中文译名（英文原词）"的形式，例如：注意力机制（Attention Mechanism）。
- 无通用中文对照的专有名词（模型名、数据集名、方法名）保留英文原文。
- 宁可读起来稍微"学术"一点，也不要为了流畅而改变原意或夸大结论。原文说"可能""在某种程度上"，就不要写成"证明了"。

【可读性处理】
- 遇到长难句，拆成多个短句，按中文表达习惯重新组织语序。
- 遇到隐含逻辑（因果、转折、递进、举例），用连接词显性化，例如"因此""不过""换句话说""举个例子"。
- 遇到缩写，首次出现时写全称并解释。
- 遇到被动语态，尽量改成主动语态。
- 遇到名词化堆砌（如"性能的显著提升"），改成动词表达（如"性能明显提升了"）。

【术语解释的分寸】
- 只有当某个术语确实会阻碍普通读者理解时，才用一句话在括号里解释，例如：Transformer（一种能并行处理长文本的神经网络结构）。
- 你只负责这一段，无法知道该术语在全文别处是否已经解释过。因此**任何一段最多解释一个术语**，且只在它确实构成理解障碍时才解释；不要为每个术语都加括号解释，那会让读者厌烦。

【逻辑重建 · 限于本段之内】
- 如果本段缺少主题句、或句子之间的逻辑跳跃，可以补一句引导句或过渡句，让本段论证读起来连贯。
- 补出来的句子**只能概括本段英文里已有的内容**，不得引入原文没有的信息。
- 严禁使用指向你视野之外内容的表述，例如"如前所述""上一节提到""接下来我们将看到""正如后文所述"——你看不到相邻段落，这类说法只会是编造。
- 不要为段落添加小标题：原文的小标题在排版上本来就是独立的段落块，译文里再补一个会与上方英文原文错位。保留原文的小标题层级即可。

【风格控制】
- 语气自然、平实，像一位耐心的老师在讲解，而不是机器在念稿。
- 不使用口语化过度的表达（如"超级牛""搞事情"）。
- 不使用生硬的翻译腔（如"被进行""对于……而言，其……"）。
- 数字、单位、图表编号保持原样。"""

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
        # 结尾必须是"：\n\n"+正文，Translator._mock_output 靠这个分隔符取正文
        return (f"{prefix}这是论文中的一段。请把它改写成普通读者能读懂的中文，"
                f"准确第一，所有 [[n]] 占位符原样保留：\n\n{masked}")

    # ------------------------------------------------------------------ 调用
    async def complete(self, system: str, user: str, *,
                       temperature: float | None = None) -> str:
        """通用一次性补全，用于翻译以外的任务（如论文挑选）。

        挑选需要的是稳定判断，不是发挥，所以调用方应传一个低温度。
        """
        return await self._chat(system, user, temperature=temperature)

    async def _chat(self, system: str, user: str,
                    temperature: float | None = None) -> str:
        if self.s.mock:
            await asyncio.sleep(self.s.mock_delay)
            return self._mock_output(user)

        payload = {
            "model": self.s.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.s.temperature if temperature is None else temperature,
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

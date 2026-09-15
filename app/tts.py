"""小米 MiMo 语音合成（mimo-v2.5-tts）。

接口形态是 OpenAI 兼容的 chat/completions：把要朗读的文本放进 assistant 消息，
音频以 base64 放在 choices[0].message.audio.data 里返回。

本模块负责：
1. 朗读文本清洗：去掉占位符、引用编号、LaTeX 残片——这些念出来只会干扰听感；
2. 长段落自动按句子切成多次请求，再把 WAV 拼回一个文件；
3. 磁盘缓存：同一段文本（同模型同音色）只合成一次，重听不重复计费。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import re
import wave
from pathlib import Path

import httpx

log = logging.getLogger("tts")

# 官方内置音色（mimo-v2.5-tts 只支持内置音色）
VOICES: list[dict[str, str]] = [
    {"id": "mimo_default", "label": "默认（推荐）"},
    {"id": "冰糖", "label": "冰糖"},
    {"id": "茉莉", "label": "茉莉"},
    {"id": "苏打", "label": "苏打"},
    {"id": "白桦", "label": "白桦"},
    {"id": "Mia", "label": "Mia（女声）"},
    {"id": "Chloe", "label": "Chloe（女声）"},
    {"id": "Milo", "label": "Milo（男声）"},
    {"id": "Dean", "label": "Dean（男声）"},
]

CITATION_RE = re.compile(r"\[\s*\d{1,4}(?:\s*[-,–—]\s*\d{1,4})*\s*\]")
PLACEHOLDER_RE = re.compile(r"\[\s*\[\s*\d{1,3}\s*\]\s*\]")
LATEX_RE = re.compile(r"\$[^$\n]{0,200}\$|\\[a-zA-Z]+(?:\{[^{}]{0,80}\})*")
PAREN_EN_RE = re.compile(r"[\(\[]\s*[A-Z][A-Za-z\-']+(?:\s+(?:and|&|,)\s+[A-Z][A-Za-z\-']+)*"
                         r"(?:\s+et\s+al\.?)?\s*,?\s*(?:19|20)\d{2}[a-z]?\s*[\)\]]")
# 括号内的交叉引用：念出来只会打断听感，例如 "(Section 3.2)"、"(see Figure 2)"
PAREN_REF_RE = re.compile(
    r"[\(（]\s*(?:see\s+|cf\.\s*)?(?:Section|Sec\.|Figure|Fig\.|Table|Tab\.|Equation|Eq\.|"
    r"Appendix|App\.|Algorithm|Alg\.|Chapter)\s*[\dIVXivx.]+[^\)）]{0,40}[\)）]")
URL_RE = re.compile(r"https?://\S+|\bwww\.[^\s,，。]+")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
# 通讯作者/邮箱这类联系方式不适合朗读
CONTACT_RE = re.compile(r"(?:通讯作者|通信作者|Corresponding author)[：:]?\s*\S*")
# 百分号要按中文语序前置：12.5% → 百分之12.5
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
SYMBOL_MAP = {
    "→": "到", "←": "来自", "≥": "大于等于", "≤": "小于等于", "≈": "约等于",
    "≠": "不等于", "×": "乘", "&": "和", "±": "正负",
    "∑": "求和", "∈": "属于", "∞": "无穷",
}


class SpeechError(RuntimeError):
    pass


def clean_for_speech(text: str) -> str:
    """把书面译文转成适合朗读的文本。"""
    t = text or ""
    t = PLACEHOLDER_RE.sub("", t)
    t = CONTACT_RE.sub("", t)
    t = URL_RE.sub("", t)
    t = EMAIL_RE.sub("", t)
    t = PAREN_REF_RE.sub("", t)
    t = PAREN_EN_RE.sub("", t)
    t = CITATION_RE.sub("", t)
    t = LATEX_RE.sub("", t)
    t = re.sub(r"\[[^\]]{0,40}\]", "", t)          # 残留的方括号内容（图表编号等）
    t = re.sub(r"^\s*(?:Figure|Table|图|表)\s*\d+[:：]?", "", t)
    t = PERCENT_RE.sub(r"百分之\1", t)
    for a, b in SYMBOL_MAP.items():
        t = t.replace(a, b)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    # 引用被删掉后常留下"与 … 以及"这类悬空连接词，读出来很怪
    t = re.sub(r"(?:与|和|以及|及|并且|、、)\s*(?=[，。、；：！？,.;:!?])", "", t)
    t = re.sub(r"(?:与|和|以及|及)\s*(?:与|和|以及|及)", "以及", t)
    t = re.sub(r"\s+([，。、；：！？])", r"\1", t)                 # 标点前的空格
    t = re.sub(r"([，,、])\s*([，,、。；;])", r"\2", t)            # 重复标点
    t = re.sub(r"^[\s，。、；：,.。;:!?！？—\-–]+", "", t)      # 开头残留标点
    t = re.sub(r"[\s，。、；：,.。;:—\-–]+$", "", t)           # 结尾残留标点
    return t.strip()


def split_for_speech(text: str, max_chars: int = 400) -> list[str]:
    """按句子切分，避免单次请求过长导致音质下降或超限。"""
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r"(?<=[。！？；!?;])\s*", text)
    chunks: list[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if buf and len(buf) + len(p) > max_chars:
            chunks.append(buf)
            buf = p
        else:
            buf += p
    if buf:
        chunks.append(buf)
    # 兜底：单句超长时硬切
    out: list[str] = []
    for c in chunks:
        while len(c) > max_chars * 1.5:
            out.append(c[:max_chars])
            c = c[max_chars:]
        out.append(c)
    return [c.strip() for c in out if c.strip()]


def concat_wav(chunks: list[bytes]) -> bytes:
    """把多段 WAV 拼成一个（同采样率/位深，直接接数据段）。"""
    if len(chunks) == 1:
        return chunks[0]
    out = io.BytesIO()
    with wave.open(out, "wb") as wout:
        params_set = False
        for data in chunks:
            with wave.open(io.BytesIO(data), "rb") as win:
                if not params_set:
                    wout.setnchannels(win.getnchannels())
                    wout.setsampwidth(win.getsampwidth())
                    wout.setframerate(win.getframerate())
                    params_set = True
                wout.writeframes(win.readframes(win.getnframes()))
    return out.getvalue()

def _strip_id3(data: bytes) -> bytes:
    """去掉 MP3 开头的 ID3v2 标签：多片拼接时避免播放器解析异常。"""
    if data[:3] == b"ID3" and len(data) > 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        return data[10 + size:]
    return data


def concat_audio(chunks: list[bytes], audio_format: str) -> bytes:
    """按格式拼接分片音频：wav 重组 RIFF 头，mp3 直接接帧。"""
    if len(chunks) == 1:
        return chunks[0]
    if (audio_format or "wav").lower() == "wav":
        return concat_wav(chunks)
    out = bytearray()
    for data in chunks:
        out += _strip_id3(data)
    return bytes(out)


class Synthesizer:
    """MiMo TTS 客户端 + 磁盘缓存。"""

    def __init__(self, api_key: str, base_url: str, model: str, voice: str,
                 audio_format: str, timeout: float, cache_dir: Path,
                 max_chunk: int = 180, concurrency: int = 4,
                 cache_max_mb: int = 500) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.audio_format = audio_format
        self.timeout = timeout
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_chunk = max_chunk
        self.concurrency = max(1, concurrency)
        self._sem = asyncio.Semaphore(self.concurrency)   # 全局限流，避免把接口打爆
        self._client: httpx.AsyncClient | None = None
        self.stats = {"requests": 0, "chars": 0, "cache_hits": 0, "errors": 0}
        self.cache_max_mb = max(0, int(cache_max_mb))
        self._evict()   # 启动时先按上限收敛一次

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                headers={"api-key": self.api_key, "Content-Type": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 缓存
    def cache_path(self, text: str, voice: str | None = None) -> Path:
        key = f"{self.model}|{voice or self.voice}|{self.audio_format}|{text}"
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{h}.{self.audio_format}"

    # ---------------------------------------------------------------- 合成
    async def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, bool]:
        """返回 (音频字节, 是否命中缓存)。"""
        spoken = clean_for_speech(text)
        if not spoken:
            raise SpeechError("没有可朗读的文本")
        voice = voice or self.voice
        path = self.cache_path(spoken, voice)
        if path.is_file() and path.stat().st_size > 1024:
            self.stats["cache_hits"] += 1
            return path.read_bytes(), True

        # 分片并行合成：实测接口无并发限制（6 路并发同样 6.7~8.8s 全部返回），
        # 因此长段落切成多片同时合成，墙钟时间从"线性累加"变成"最慢那一片"。
        chunks = split_for_speech(spoken, self.max_chunk)
        if len(chunks) == 1:
            data = await self._request(chunks[0], voice)
        else:
            audios = await asyncio.gather(*(self._request(c, voice) for c in chunks))
            data = concat_audio(list(audios), self.audio_format)
        self.stats["chars"] += len(spoken)
        try:
            path.write_bytes(data)
        except OSError as exc:  # noqa: BLE001
            log.warning("音频缓存写入失败：%s", exc)
        self._evict()   # 写盘后按容量上限淘汰
        return data, False

    # ---------------------------------------------------------------- 缓存容量
    def cache_files(self) -> list[Path]:
        return [p for p in self.cache_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".wav", ".mp3")]

    def cache_bytes(self) -> int:
        total = 0
        for p in self.cache_files():
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    def _evict(self) -> None:
        """清掉非当前格式的遗留缓存；超出上限时按最久未使用淘汰（保留 90%）。"""
        keep_ext = "." + (self.audio_format or "wav").lower()
        entries: list[tuple[float, int, Path]] = []
        for p in self.cache_files():
            try:
                if p.suffix.lower() != keep_ext:
                    p.unlink(missing_ok=True)   # 换格式后遗留的旧文件
                    continue
                st = p.stat()
                entries.append((max(st.st_atime, st.st_mtime), st.st_size, p))
            except OSError:
                continue
        if self.cache_max_mb <= 0:
            return
        cap = self.cache_max_mb * 1024 * 1024
        total = sum(e[1] for e in entries)
        if total <= cap:
            return
        target = int(cap * 0.9)
        entries.sort(key=lambda e: e[0])        # 最久未使用的排最前
        removed = 0
        for _, size, path in entries:
            if total <= target:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        if removed:
            log.info("语音缓存超限：淘汰 %d 个最久未用文件，当前 %.1f MB（上限 %d MB）",
                     removed, total / 1048576, self.cache_max_mb)

    async def _request(self, text: str, voice: str, retries: int = 2) -> bytes:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": "请用自然、清晰的学术口吻朗读下面这段文字。"},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": self.audio_format, "voice": voice},
        }
        client = await self._get_client()
        resp = None
        for attempt in range(retries + 1):
            self.stats["requests"] += 1
            try:
                async with self._sem:
                    resp = await client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                if attempt == retries:
                    self.stats["errors"] += 1
                    raise SpeechError(f"网络错误：{exc}") from exc
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            if resp.status_code < 400:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                log.warning("语音合成临时失败 HTTP %s，%.1fs 后重试", resp.status_code, 0.8 * (attempt + 1))
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            self.stats["errors"] += 1
            raise SpeechError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        assert resp is not None
        try:
            data = resp.json()
            b64 = data["choices"][0]["message"]["audio"]["data"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self.stats["errors"] += 1
            raise SpeechError(f"响应解析失败：{resp.text[:200]}") from exc
        raw = base64.b64decode(b64)
        if not raw:
            self.stats["errors"] += 1
            raise SpeechError("返回音频为空")
        return raw

    async def synthesize_paragraphs(self, texts: list[str], voice: str | None = None) -> bytes:
        """一次性合成多段并拼接（暂未使用，留给导出整篇音频）。"""
        out: list[bytes] = []
        for t in texts:
            data, _ = await self.synthesize(t, voice)
            out.append(data)
            await asyncio.sleep(0)
        return concat_wav(out)

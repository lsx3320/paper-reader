"""译文占位符保护：把公式、引用、编号、代码标识符等不可翻译片段挖空后再送翻译。

思路：翻译前用 [[0]] [[1]] … 形式的占位符替换"必须原样保留"的片段，
翻译后再按序还原。这样即使模型试图改写，也无法破坏公式与引用。
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable

PLACEHOLDER_RE = re.compile(r"\[\s*\[\s*(\d{1,3})\s*\]\s*\]")

# 注意：顺序敏感，越具体的模式越靠前
MASK_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("url", re.compile(r"https?://[^\s)]+|\bwww\.[^\s)]+")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("latex_math", re.compile(r"\${1,2}[^$\n]{1,300}\${1,2}")),
    ("latex_cmd", re.compile(r"\\[a-zA-Z]+(?:\{[^{}]{0,120}\})*")),
    ("cite_num", re.compile(r"\[\s*\d{1,4}(?:\s*[-,–—]\s*\d{1,4})*\s*\]")),
    ("cite_author", re.compile(
        r"[\(\[]\s*[A-Z][A-Za-z\-']+(?:\s+(?:and|&|,)\s+[A-Z][A-Za-z\-']+)*"
        r"(?:\s+et\s+al\.?)?\s*,?\s*(?:19|20)\d{2}[a-z]?\s*[\)\]]")),
    ("etal", re.compile(r"\b[A-Z][A-Za-z\-']+\s+et\s+al\.?")),
    ("crossref", re.compile(
        r"\b(?:Figs?\.|Figures?|Tabs?\.|Tables?|Eqs?\.|Equations?|Algs?\.|Algorithms?|"
        r"Secs?\.|Sections?|Apps?\.|Appendices|Appendix|Theorems?|Lemmas?|Corollaries?|"
        r"Propositions?|Definitions?|Assumptions?|Remarks?|Chapters?)\s*\(?\d+(?:\.\d+)*[a-z]?\)?")),
    ("bigO", re.compile(r"\b[Oo]\s*\(\s*[A-Za-z0-9\s\^\\{}_+\-*/.,]{1,24}\)")),
    ("measure", re.compile(r"\d+(?:\.\d+)?\s*(?:[×x]\s*10\s*\^?\s*-?\d+|[KMGTP]?B\b|\bms\b|\bs\b|\bHz\b)")),
    ("percent", re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("identifier", re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")),
    ("attr", re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*){1,3}\b")),
    ("mathvar", re.compile(r"\b[A-Za-z]\s*[_^]\s*\{?[A-Za-z0-9]{1,3}\}?")),
    ("acronym", re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:[-/][A-Z0-9]+)*\b")),
    ("version", re.compile(r"\bv?\d+(?:\.\d+){1,3}\b")),
]

MATH_SYMBOLS = set("=≈≤≥≠±→←↔⇒⇔∝∞∫∑∏√∂∇∈∉⊂⊆∪∩∀∃⋅×÷^_{}|<>~")


# 规则之间用私有区哨兵占位，避免后一条规则去匹配前一条规则生成的占位符
# （例如 latex_cmd 先把 \sum 挖成占位符，cite_num 又会去匹配占位符里的 [3]）。
_SENT_OPEN = "\ue000"
_SENT_CLOSE = "\ue001"
_SENT_RE = re.compile(_SENT_OPEN + r"(\d{1,3})" + _SENT_CLOSE)


def mask(text: str) -> tuple[str, dict[int, str]]:
    """挖空不可翻译片段，返回 (占位化文本, {序号: 原文})。

    过程：所有规则依次用哨兵占位 → 最后统一转成 [[n]] 形式交给模型。
    """
    tokens: dict[int, str] = {}

    def repl(m: re.Match[str]) -> str:
        i = len(tokens)
        tokens[i] = m.group(0)
        return f"{_SENT_OPEN}{i}{_SENT_CLOSE}"

    out = text
    for _, pattern in MASK_RULES:
        out = pattern.sub(repl, out)
    out = _SENT_RE.sub(lambda m: f"[[{m.group(1)}]]", out)
    return out, tokens


def restore(text: str, tokens: dict[int, str]) -> tuple[str, list[int]]:
    """还原占位符，返回 (还原后文本, 丢失的序号列表)。"""
    used: set[int] = set()

    def repl(m: re.Match[str]) -> str:
        i = int(m.group(1))
        if i in tokens:
            used.add(i)
            return tokens[i]
        return m.group(0)

    out = PLACEHOLDER_RE.sub(repl, text)
    missing = sorted(set(tokens) - used)
    return out, missing


def clean_output(text: str) -> str:
    """清理模型可能附带的思考链、Markdown 围栏、前缀等。

    本地小模型（Qwen3 等）即使关掉思考模式，也可能在正文前漏出一个
    `</think>` 标记；部分推理模型则会把整段思考链写在正文里，这里统一清掉。
    """
    t = (text or "").strip()
    t = re.sub(r"Thinking.*?</think\s*>", "", t, flags=re.S)   # 完整思考链
    t = re.sub(r"^\s*(?:</?think\s*>|Thinking)\s*", "", t)      # 开头残留的 think 标记
    t = re.sub(r"</?think\s*>", "", t)                        # 正文中零散标记
    t = t.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
    t = re.sub(r"\n?```$", "", t)
    t = re.sub(r"^(?:译文|翻译|中文翻译|Translation)\s*[:：]\s*", "", t.strip())
    t = t.strip().strip('"').strip("“”").strip()
    return t


def looks_untranslated(zh: str) -> bool:
    """判断译文是否压根没翻译（几乎不含中文）。"""
    if not zh:
        return True
    han = len(re.findall(r"[\u4e00-\u9fff]", zh))
    letters = len(re.findall(r"[A-Za-z]", zh))
    return han == 0 and letters > 24


def looks_like_thinking(text: str) -> bool:
    """输出开头仍是思考链（没有闭合标记时防不住，交给上游重试）。"""
    head = (text or "").lstrip()[:48]
    return head.startswith("Thinking") or "Thinking" in head


def looks_like_math(text: str) -> bool:
    """判断整段是否为公式/代码/表格残片，此类内容不做翻译。"""
    t = text.strip()
    if len(t) < 8:
        return False
    compact = re.sub(r"\s+", "", t)
    if not compact:
        return False
    letters = sum(c.isalpha() for c in compact)
    han = len(re.findall(r"[\u4e00-\u9fff]", compact))
    symbols = sum(1 for c in compact if c in MATH_SYMBOLS)
    ratio_symbol = symbols / len(compact)
    ratio_letter = letters / len(compact)

    if ratio_symbol > 0.16:
        return True
    if ratio_letter < 0.45 and han == 0:
        return True
    words = t.split()
    if len(words) >= 5:
        singles = sum(1 for w in words if len(w.strip("(),.;:[]{}=+-<>")) <= 1)
        if singles / len(words) > 0.45:
            return True
    return False


def select_glossary(text: str, mapping: dict[str, str], limit: int = 40,
                     mode: str = "match") -> list[tuple[str, str]]:
    """挑选注入提示词的术语。

    match：只挑当前段落里出现的术语，提示词短，适合按 token 计费的云端 API；
    all  ：注入全部术语且顺序固定，使 system 前缀在整篇论文里完全一致，
           本地模型可以命中前缀 KV 缓存，第二段起预填充几乎为 0（强烈推荐本地模型使用）。
    """
    if mode == "all":
        return sorted(((en, zh) for en, zh in mapping.items() if en and zh),
                      key=lambda kv: kv[0].lower())
    lowered = text.lower()
    hits: list[tuple[str, str]] = []
    for en, zh in mapping.items():
        if not en or not zh:
            continue
        if en.lower() in lowered:
            hits.append((en, zh))
        if len(hits) >= limit:
            break
    return hits


def glossary_fingerprint(mapping: dict[str, str]) -> str:
    payload = "|".join(f"{k}={v}" for k, v in sorted(mapping.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

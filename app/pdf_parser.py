"""PDF 版面解析与段落切片。

处理要点：
1. 双栏检测：用横向占用直方图判断页面是否为双栏，并做"全宽块分段 + 栏内排序"的阅读顺序还原；
2. 页眉页脚：位于上下边距、且跨页重复或形如页码的块直接丢弃；
3. 段落合并：跨行、跨块、跨栏、跨页的断段按间距/断句/字号三类信号合并为完整段落；
4. 内容识别：标题、章节标题、图注、公式/代码残片、参考文献分区分别打标；
5. 公式保护：整块视为数学/代码的段落标记为 keep，不送翻译。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

from .protect import looks_like_math

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
    "\u00ad": "", "\u200b": "", "\ufeff": "",
}

TOP_SECTION = re.compile(
    r"^(?:abstract|introduction|related\s+work|background|preliminaries|"
    r"method(?:s|ology)?|approach|model|experiments?|evaluation|results?|"
    r"analysis|discussion|ablation|conclusions?|future\s+work|references|"
    r"bibliography|acknowledge?ments?|appendix|supplementary)\b", re.I)

NUMBERED = re.compile(r"^(?:\d+(?:\.\d+){0,2}|[IVX]{1,4})[.)]?\s+\S")

CAPTION = re.compile(r"^(?:Figure|Fig\.|Table|Tab\.|Algorithm|Alg\.|Listing|Chart)\s*\d+", re.I)

REFS_HEAD = re.compile(r"^(?:references|bibliography|参考文献)\s*$", re.I)
APPENDIX_HEAD = re.compile(r"^(?:appendix|supplementary|附录)", re.I)

JUNK = re.compile(
    r"^(?:arxiv:\S+|doi:\s*\S+|https?://\S+|©|copyright\b|all rights reserved|"
    r"preprint\b|under review\b|published as\b|proceedings of\b|pages?\s+\d+[-–]\d+)", re.I)


@dataclass(eq=False)
class RawBlock:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    line_h: float
    bold: bool
    text: str
    page_w: float
    page_h: float

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class ParsedDoc:
    title: str
    num_pages: int
    blocks: list[dict] = field(default_factory=list)
    toc: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- 工具
def _clean(text: str) -> str:
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)
    text = text.replace("\u00a0", " ").replace("\t", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _join_lines(lines: list[str]) -> str:
    out = ""
    for i, raw in enumerate(lines):
        t = raw.strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if re.search(r"[A-Za-z]-$", out) and re.match(r"^[a-z]", t):
            out = out[:-1] + t
        else:
            out = out + " " + t
    return _clean(out)


def _is_junk(text: str) -> bool:
    if len(text) < 2:
        return True
    if re.fullmatch(r"(?:page\s*)?[\divxlc]{1,6}", text, re.I):
        return True
    if JUNK.match(text):
        return True
    return False


# --------------------------------------------------------------------- 抽取
def _horizontal(line: dict) -> bool:
    d = line.get("dir") or (1.0, 0.0)
    return abs(d[1]) < 0.15


def _extract_page(page: fitz.Page, page_no: int) -> list[RawBlock]:
    data = page.get_text("dict", sort=False)
    out: list[RawBlock] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[tuple[str, float, float]] = []  # (text, max_size, height)
        size_chars: dict[float, int] = {}
        bold_chars = 0
        total_chars = 0
        heights: list[float] = []
        for line in block.get("lines", []):
            if not _horizontal(line):
                continue
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans)
            max_size = max(float(s.get("size") or 0) for s in spans)
            for s in spans:
                n = len(s.get("text", "").strip())
                sz = round(float(s.get("size") or 0), 1)
                size_chars[sz] = size_chars.get(sz, 0) + n
                total_chars += n
                if int(s.get("flags") or 0) & 16:
                    bold_chars += n
            lh = float(line["bbox"][3] - line["bbox"][1]) if line.get("bbox") else 0.0
            if lh > 0:
                heights.append(lh)
            lines.append((text, max_size, lh))
        if not lines:
            continue
        text = _join_lines([t for t, _, _ in lines])
        if not text or _is_junk(text):
            continue
        dominant = max(size_chars.items(), key=lambda kv: kv[1])[0] if size_chars else 10.0
        heights.sort()
        line_h = heights[len(heights) // 2] if heights else dominant * 1.2
        x0, y0, x1, y1 = block["bbox"]
        out.append(RawBlock(
            page=page_no, x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1),
            size=dominant, line_h=line_h,
            bold=total_chars > 0 and bold_chars / total_chars > 0.6,
            text=text, page_w=float(page.rect.width), page_h=float(page.rect.height),
        ))
    return out


def _body_size(blocks: list[RawBlock]) -> float:
    weight: dict[float, int] = {}
    for b in blocks:
        if len(b.text) >= 25:
            key = round(b.size, 1)
            weight[key] = weight.get(key, 0) + len(b.text)
    if not weight:
        return 10.0
    return max(weight.items(), key=lambda kv: kv[1])[0]


def _strip_headers_footers(pages: list[list[RawBlock]]) -> tuple[list[list[RawBlock]], int]:
    total = len(pages)
    counter: dict[str, int] = {}
    for blocks in pages:
        for b in blocks:
            if b.y1 <= b.page_h * 0.075 or b.y0 >= b.page_h * 0.93:
                counter[b.text.lower()] = counter.get(b.text.lower(), 0) + 1
    repeated = {t for t, c in counter.items() if c >= max(2, round(total * 0.4))}

    kept: list[list[RawBlock]] = []
    dropped = 0
    for blocks in pages:
        page_kept = []
        for b in blocks:
            in_margin = b.y1 <= b.page_h * 0.075 or b.y0 >= b.page_h * 0.93
            if in_margin and (b.text.lower() in repeated
                              or re.fullmatch(r"(?:page\s*)?[\divxlc]{1,6}", b.text, re.I)):
                dropped += 1
                continue
            page_kept.append(b)
        kept.append(page_kept)
    return kept, dropped


# --------------------------------------------------------------------- 阅读顺序
def _column_split(blocks: list[RawBlock], width: float, height: float) -> float | None:
    """在页面中部寻找竖向"空白沟"(gutter)，返回分栏位置；单栏返回 None。

    只有"窄块"参与统计（整幅宽的标题/摘要/跨栏图表不参与），并跳过上下页边距区域，
    这样"首页整幅标题 + 摘要 + 下方双栏正文"这类常见版面也能正确判定为双栏。
    """
    narrow_all = [b for b in blocks if (b.x1 - b.x0) <= 0.62 * width]
    if len(narrow_all) < 5:
        return None

    bins = 200
    occ = [0] * bins
    for b in narrow_all:
        if b.y1 <= height * 0.08 or b.y0 >= height * 0.92:
            continue  # 页眉页脚区域不参与沟槽判定
        i0 = max(0, min(bins - 1, int(b.x0 / width * bins)))
        i1 = max(0, min(bins - 1, int(b.x1 / width * bins)))
        for i in range(i0, i1 + 1):
            occ[i] = 1

    best_len, best_start = 0, None
    run_start = None
    for i in range(50, 150):
        if occ[i] == 0:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            if i - run_start > best_len:
                best_len, best_start = i - run_start, run_start
            run_start = None
    if run_start is not None and 150 - run_start > best_len:
        best_len, best_start = 150 - run_start, run_start
    if best_start is None or best_len < 2:
        return None

    split = (best_start + best_len / 2) / bins * width
    left = [b for b in narrow_all if b.center_x < split]
    right = [b for b in narrow_all if b.center_x >= split]
    if len(left) < 2 or len(right) < 2:
        return None
    if sum(occ[20:best_start]) < 16 or sum(occ[best_start + best_len:180]) < 16:
        return None
    return split


def _reading_order(blocks: list[RawBlock], width: float, height: float) -> list[RawBlock]:
    if not blocks:
        return []
    mid = _column_split(blocks, width, height)
    if mid is None:  # 单栏：按纵坐标顺序即可
        return sorted(blocks, key=lambda b: (round(b.y0, 1), b.x0))

    spanning, narrow = [], []
    for b in blocks:
        if b.x0 < mid - 0.12 * width and b.x1 > mid + 0.12 * width:
            spanning.append(b)
        else:
            narrow.append(b)

    ordered: list[RawBlock] = []
    remaining = list(narrow)

    def side(b: RawBlock) -> int:
        return 0 if b.center_x < mid else 1

    for divider in sorted(spanning, key=lambda b: b.y0):
        band = [b for b in remaining if b.y1 <= divider.y0 + 2]
        if band:
            for b in band:
                remaining.remove(b)
            band.sort(key=lambda b: (side(b), round(b.y0, 1)))
            ordered.extend(band)
        ordered.append(divider)
    remaining.sort(key=lambda b: (side(b), round(b.y0, 1)))
    ordered.extend(remaining)
    return ordered


# --------------------------------------------------------------------- 语义标记
def _heading_level(text: str, size: float, body: float, bold: bool) -> int:
    t = text.strip()
    if not t or len(t) > 130:
        return 0
    stripped = t.rstrip(" .:：")
    if TOP_SECTION.match(stripped) and len(stripped) <= 48:
        return 1
    m = NUMBERED.match(stripped)
    if m:
        dots = stripped.split(" ", 1)[0].count(".")
        if len(stripped) <= 90:
            return min(3, dots + 1)
    if size >= body * 1.35 and len(stripped) <= 110:
        return 1
    if size >= body * 1.14 and len(stripped) <= 110:
        return 2
    if bold and size >= body * 1.02 and len(stripped) <= 70 and not re.search(r"[.!?]$", stripped):
        return 3
    return 0


def _merge_paragraphs(blocks: list[RawBlock], body: float) -> list[dict]:
    items: list[dict] = []
    cur: dict | None = None

    def flush() -> None:
        nonlocal cur
        if cur and cur["text"].strip():
            items.append(cur)
        cur = None

    for b in blocks:
        level = _heading_level(b.text, b.size, body, b.bold)
        kind = "heading" if level else ("caption" if CAPTION.match(b.text) else "para")
        if b.size >= body * 1.45 and len(b.text) <= 200 and not items:
            kind = "title"

        if kind != "para" or cur is None:
            flush()
            cur = {"page": b.page, "kind": kind, "level": level,
                   "text": b.text, "y0": b.y0, "y1": b.y1, "size": b.size,
                   "line_h": b.line_h}
            if kind != "para":
                flush()
            continue

        prev = cur
        lh = max(prev["line_h"], b.line_h, b.size * 1.15)
        prev_text = prev["text"].rstrip()
        soft_end = not re.search(r"[.!?:;”\"')\]]$", prev_text) or re.search(r"[a-z,]$", prev_text)
        same_page = prev["page"] == b.page
        if same_page:
            gap = b.y0 - prev["y1"]
            near = gap < 0.85 * lh or gap < 0
        else:
            near = (b.y0 < b.page_h * 0.32 and prev["y1"] > prev["line_h"] * 2
                    and b.page == prev["page"] + 1)
        similar = abs(b.size - prev["size"]) <= 0.22 * body
        if near and soft_end and similar and len(prev["text"]) < 4000:
            joiner = "" if (prev_text.endswith("-") and re.match(r"^[a-z]", b.text)) else " "
            prev["text"] = (prev_text[:-1] if joiner == "" else prev_text) + joiner + b.text
            prev["y1"] = b.y1
        else:
            flush()
            cur = {"page": b.page, "kind": "para", "level": 0, "text": b.text,
                   "y0": b.y0, "y1": b.y1, "size": b.size, "line_h": b.line_h}
    flush()
    return items


# --------------------------------------------------------------------- 后处理
def _mark_front_matter(items: list[dict]) -> None:
    """首页 Abstract 之前的作者/单位等前置信息不翻译。"""
    abstract_at = None
    for i, it in enumerate(items):
        if it["page"] == 0 and re.match(r"^abstract\b", it["text"], re.I):
            abstract_at = i
            break
    if abstract_at is None:
        for i, it in enumerate(items):
            if it["page"] == 0 and it["kind"] == "para" and len(it["text"]) > 420:
                abstract_at = i
                break
    if abstract_at is None:
        abstract_at = min(8, len(items))
    for it in items[:abstract_at]:
        if it["kind"] in {"para", "caption", "heading"}:
            it["kind"] = "meta"


def _split_long(text: str, max_chars: int) -> list[tuple[str, bool]]:
    if len(text) <= max_chars:
        return [(text, False)]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if buf and len(buf) + len(s) + 1 > max_chars:
            chunks.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        chunks.append(buf)
    out: list[tuple[str, bool]] = []
    for i, c in enumerate(chunks):
        out.append((c, i > 0))
    return out


def _finalize(items: list[dict], max_chars: int, translate_refs: bool) -> tuple[list[dict], list[dict]]:
    blocks: list[dict] = []
    toc: list[dict] = []
    idx = 0
    in_refs = False
    for it in items:
        text = it["text"].strip()
        if not text:
            continue
        kind = it["kind"]

        if kind == "heading":
            if REFS_HEAD.match(text.rstrip(":：.")):
                in_refs = True
                blocks.append({"idx": idx, "page": it["page"], "kind": "refhead",
                               "level": 1, "text": text, "cont": False})
                idx += 1
                continue
            if APPENDIX_HEAD.match(text):
                in_refs = False
            toc.append({"idx": idx, "text": text, "level": it["level"], "page": it["page"]})

        if in_refs and kind in {"para", "caption", "meta"}:
            kind = "ref"

        if kind == "ref" and not translate_refs:
            blocks.append({"idx": idx, "page": it["page"], "kind": "ref", "level": 0,
                           "text": text, "cont": False})
            idx += 1
            continue

        if kind in {"para", "ref"} and looks_like_math(text):
            kind = "keep"

        if kind in {"meta", "keep", "ref"}:
            blocks.append({"idx": idx, "page": it["page"], "kind": kind, "level": 0,
                           "text": text, "cont": False})
            idx += 1
            continue

        for chunk, cont in _split_long(text, max_chars):
            blocks.append({"idx": idx, "page": it["page"], "kind": kind,
                           "level": it["level"], "text": chunk, "cont": cont})
            idx += 1
    return blocks, toc


# --------------------------------------------------------------------- 入口
def parse_pdf(path: str | Path, max_chars: int = 1800, translate_refs: bool = False) -> ParsedDoc:
    doc = fitz.open(str(path))
    try:
        pages: list[list[RawBlock]] = []
        for i, page in enumerate(doc):
            pages.append(_extract_page(page, i))
        if not any(pages):
            raise ValueError("该 PDF 未提取到任何文本，可能是扫描件或加密文件，请先做 OCR。")

        body = _body_size([b for p in pages for b in p])
        pages, dropped = _strip_headers_footers(pages)

        ordered: list[RawBlock] = []
        for blocks in pages:
            if not blocks:
                continue
            ordered.extend(_reading_order(blocks, blocks[0].page_w, blocks[0].page_h))

        items = _merge_paragraphs(ordered, body)
        _mark_front_matter(items)

        title = ""
        for it in items:
            if it["kind"] == "title":
                title = it["text"].strip()
                break
        if not title:
            cands = [b for p in pages[:1] for b in p if b.y0 < b.page_h * 0.45]
            if cands:
                best = max(cands, key=lambda b: b.size)
                title = best.text.strip()[:200]

        blocks, toc = _finalize(items, max_chars, translate_refs)
        notes = []
        if dropped:
            notes.append(f"已过滤 {dropped} 处页眉/页脚/页码")
        notes.append(f"共 {len(blocks)} 个段落块")
        return ParsedDoc(title=title or Path(path).stem, num_pages=doc.page_count,
                         blocks=blocks, toc=toc, notes=notes)
    finally:
        doc.close()

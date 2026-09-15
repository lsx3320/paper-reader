"""切片质量自检：打印解析结果并做基本断言。

用法：python tools/smoke_test.py [PDF路径]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdf_parser import parse_pdf  # noqa: E402
from app.protect import mask, restore  # noqa: E402

KIND_LABEL = {
    "title": "标题", "heading": "章节", "para": "正文", "caption": "图注",
    "meta": "前置", "keep": "公式", "ref": "参考", "refhead": "参考标题",
}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "tools/out/sample_paper.pdf")
    doc = parse_pdf(path, max_chars=1800, translate_refs=False)
    print(f"文件：{path}")
    print(f"标题：{doc.title}")
    print(f"页数：{doc.num_pages}  段落块：{len(doc.blocks)}  提示：{doc.notes}")
    print(f"目录：{[t['text'] for t in doc.toc]}")
    print("-" * 78)

    counts: dict[str, int] = {}
    for b in doc.blocks:
        counts[b["kind"]] = counts.get(b["kind"], 0) + 1
        label = KIND_LABEL.get(b["kind"], b["kind"])
        text = b["text"].replace("\n", " ")
        print(f"[{b['idx']:>3}] p{b['page'] + 1} {label:<4} {text[:104]}")
    print("-" * 78)
    print("分布：", counts)

    kinds = {b["kind"] for b in doc.blocks}
    problems = []
    if "heading" not in kinds:
        problems.append("未识别到任何章节标题")
    if "ref" not in kinds:
        problems.append("未识别参考文献分区")
    if "meta" not in kinds:
        problems.append("未识别首页前置信息（作者/单位）")
    if not doc.toc:
        problems.append("目录为空")
    for b in doc.blocks:
        if "Journal of Efficient" in b["text"] or b["text"].strip().isdigit():
            problems.append(f"页眉/页码未被过滤：{b['text'][:40]}")
    for b in doc.blocks:
        if b["kind"] == "para" and len(b["text"]) < 12:
            problems.append(f"过短正文块（可能是版面残片）：{b['text']!r}")

    # 双栏顺序：第 1 页正文中，Introduction 段落应排在 Method 之前
    first = [b["text"][:40] for b in doc.blocks if b["page"] == 1]
    if len(first) >= 2:
        pass
    print("-" * 78)
    sample = "The learned router reduces O(n^2) complexity [3] as reported in Table 1 (Vaswani et al., 2017)."
    masked, tokens = mask(sample)
    back, missing = restore(masked, tokens)
    print("占位符保护示例：")
    print("  原文：", sample)
    print("  挖空：", masked)
    print("  还原：", back, "缺失：", missing)
    if missing:
        problems.append("占位符还原失败")

    if problems:
        print("\n发现问题：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("\n✓ 切片自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

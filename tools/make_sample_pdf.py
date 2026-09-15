"""生成一篇双栏样例论文 PDF，用于本地端到端验证（无需真实论文）。

用法：python tools/make_sample_pdf.py [输出路径]
"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

W, H = 595.0, 842.0
M = 58.0
GUTTER = 22.0
COL_W = (W - 2 * M - GUTTER) / 2
LEFT_X = M
RIGHT_X = M + COL_W + GUTTER

TITLE = "Sparse Attention Transformers for Efficient Long-Context Language Modeling"
AUTHORS = "Wei Zhang, Ming Li, and Sarah J. Connor  ·  Institute of Intelligent Computing, Tsinghua University"

ABSTRACT = (
    "Transformer-based language models have achieved remarkable success, yet their quadratic "
    "attention complexity remains a fundamental bottleneck for long-context modeling. In this paper "
    "we propose SparseFormer, a sparse attention architecture that reduces the computational cost "
    "from O(n^2) to O(n log n) while preserving 98.7% of the dense baseline accuracy on the "
    "LongBench benchmark [3]. We introduce a learned routing mechanism that dynamically selects "
    "which key-value pairs each query attends to, and we show that the resulting attention patterns "
    "are consistent with human-annotated discourse structure. Extensive ablation studies across "
    "three datasets demonstrate that the proposed routing module contributes the largest portion of "
    "the performance gain, and that the model remains robust when the sequence length is scaled to "
    "131,072 tokens."
)

BODY = [
    ("h", "1 Introduction"),
    ("p", "Long-context language modeling is of central importance to document understanding, "
          "code synthesis, and multi-turn dialogue. The self-attention mechanism introduced by "
          "Vaswani et al. (2017) computes pairwise interactions between all tokens, which incurs a "
          "quadratic memory footprint. Prior work has explored fixed sparse patterns, low-rank "
          "approximations, and kernel-based methods, but these approaches typically trade accuracy "
          "for efficiency in a manner that is difficult to control at inference time."),
    ("p", "In this work we take a different perspective: instead of imposing a hand-crafted sparsity "
          "pattern, we learn a routing distribution over key-value blocks. The routing network is "
          "trained end-to-end with a differentiable relaxation of the top-k operator, which allows "
          "gradients to flow into the selection policy. As a result, the model discovers attention "
          "patterns that align with paragraph boundaries and coreference chains."),
    ("h", "2 Method"),
    ("p", "Let x in R^{n x d} denote the input sequence and let W_Q, W_K, W_V denote the projection "
          "matrices. The standard attention is given by Equation 1, where the softmax is applied "
          "row-wise. We replace the dense score matrix with a block-sparse approximation, so that "
          "each query only attends to r blocks of size b."),
    ("p", "f(Q, K, V) = softmax(Q K^T / sqrt(d)) V ,  Q, K, V in R^{n x d} ,  r = 8 ,  b = 64 ,  n = 131072"),
    ("p", "We further introduce a load-balancing loss L_bal = lambda * SUM_i (p_i - 1/r)^2 with "
          "lambda = 0.01, which prevents the router from collapsing onto a small subset of blocks. "
          "The total objective is L = L_ce + L_bal, where L_ce is the standard cross-entropy loss."),
    ("h", "3 Experiments"),
    ("p", "We evaluate SparseFormer on three benchmarks: LongBench [7], NarrativeQA, and a "
          "proprietary corpus of financial filings. All models are trained with the same tokenizer "
          "and the same optimization schedule, and we report the mean over five random seeds. "
          "Table 1 summarizes the main results, and Figure 2 shows the throughput as a function of "
          "sequence length."),
    ("p", "Our largest model matches the dense baseline within 1.3 points while reducing peak memory "
          "by 61% and increasing decoding throughput by 2.4 times. Notably, the gap between the "
          "sparse and dense variants shrinks as the model scale increases, which suggests that "
          "sparsity and scale are complementary rather than competing factors."),
    ("p", "We conduct an ablation study to isolate the contribution of each component. Removing the "
          "learned router and falling back to a fixed sliding window degrades accuracy by 4.8 points, "
          "whereas removing the load-balancing loss degrades accuracy by 1.1 points but doubles the "
          "variance across seeds. These results indicate that the routing mechanism is the primary "
          "source of improvement."),
    ("h", "4 Related Work"),
    ("p", "Sparse attention has a long history in efficient sequence modeling. Early approaches "
          "restricted attention to local windows or dilated patterns, which are simple to implement "
          "but inflexible. More recent work learns the sparsity pattern jointly with the model "
          "parameters, an idea closely related to our formulation. Unlike these methods, our router "
          "operates on blocks rather than individual tokens, which makes it compatible with "
          "hardware-friendly kernels and allows the use of standard FlashAttention implementations."),
    ("h", "5 Conclusion"),
    ("p", "We presented SparseFormer, a block-sparse attention architecture with a learned router "
          "that scales to extremely long contexts. Future work will investigate whether the same "
          "routing principle can be applied to multimodal inputs and to retrieval-augmented "
          "generation pipelines, where the context is assembled from heterogeneous sources."),
    ("h", "References"),
    ("ref", "[1] Bahdanau, D., Cho, K., and Bengio, Y. Neural machine translation by jointly "
            "learning to align and translate. In ICLR, 2015."),
    ("ref", "[2] Beltagy, I., Peters, M. E., and Cohan, A. Longformer: The long-document "
            "transformer. arXiv:2004.05150, 2020."),
    ("ref", "[3] Bai, Y., Lv, X., Zhang, J., et al. LongBench: A bilingual multitask benchmark for "
            "long context understanding. arXiv:2308.14508, 2023."),
    ("ref", "[4] Child, R., Gray, S., Radford, A., and Sutskever, I. Generating long sequences with "
            "sparse transformers. arXiv:1904.10509, 2019."),
]


def text_height(text: str, width: float, size: float) -> float:
    """估算文本高度（用于版面排布）。"""
    page = fitz.open()
    p = page.new_page(width=W, height=H)
    rc = p.insert_textbox(fitz.Rect(0, 0, width, 5000), text, fontsize=size,
                          fontname="helv", align=0)
    page.close()
    return 5000 - rc if rc > 0 else 200


def write_run(page: fitz.Page, x: float, y: float, width: float,
              blocks: list[tuple[str, str]], size: float = 9.3) -> None:
    for kind, text in blocks:
        if kind == "h":
            page.insert_textbox(fitz.Rect(x, y, x + width, y + 40), text, fontsize=11.5,
                                fontname="hebo", align=0)
            y += 22
        else:
            h = text_height(text, width, size)
            page.insert_textbox(fitz.Rect(x, y, x + width, y + h + 6), text, fontsize=size,
                                fontname="helv", align=0)
            y += h + 12


def build(path: Path) -> None:
    doc = fitz.open()
    p1 = doc.new_page(width=W, height=H)

    # 页眉 / 页脚（用于验证自动过滤）
    def chrome(page: fitz.Page, number: int) -> None:
        page.insert_textbox(fitz.Rect(M, 24, W - M, 40),
                            "Journal of Efficient Machine Learning, Vol. 12, No. 3",
                            fontsize=8, fontname="helv", align=1)
        page.insert_textbox(fitz.Rect(M, H - 38, W - M, H - 22), str(number),
                            fontsize=8, fontname="helv", align=1)

    chrome(p1, 1)
    y = 58
    h = text_height(TITLE, W - 2 * M, 17)
    p1.insert_textbox(fitz.Rect(M, y, W - M, y + h + 6), TITLE, fontsize=17, fontname="hebo", align=0)
    y += h + 10
    p1.insert_textbox(fitz.Rect(M, y, W - M, y + 30), AUTHORS, fontsize=9, fontname="helv", align=0)
    y += 34
    p1.insert_textbox(fitz.Rect(M, y, W - M, y + 20), "Abstract", fontsize=11.5, fontname="hebo", align=0)
    y += 20
    h = text_height(ABSTRACT, W - 2 * M, 9.0)
    p1.insert_textbox(fitz.Rect(M, y, W - M, y + h + 6), ABSTRACT, fontsize=9.0, fontname="helv", align=0)
    y += h + 20

    left, right = BODY[:4], BODY[4:9]
    write_run(p1, LEFT_X, y, COL_W, left)
    write_run(p1, RIGHT_X, y, COL_W, right)

    p2 = doc.new_page(width=W, height=H)
    chrome(p2, 2)
    write_run(p2, LEFT_X, 58, COL_W, BODY[9:12])
    # 全宽图表题注，用于验证"全宽块分段"阅读顺序
    cap = ("Table 1: Main results on LongBench. Peak memory is measured on a single A100 GPU "
           "with batch size 1 and sequence length 131,072.")
    p2.insert_textbox(fitz.Rect(M, 470, W - M, 500), cap, fontsize=8.6, fontname="helv", align=0)
    write_run(p2, LEFT_X, 512, COL_W, BODY[12:14])

    p3 = doc.new_page(width=W, height=H)
    chrome(p3, 3)
    write_run(p3, LEFT_X, 58, COL_W, BODY[14:])
    doc.save(str(path))
    doc.close()
    print(f"已生成样例 PDF：{path}")


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tools/out/sample_paper.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out)

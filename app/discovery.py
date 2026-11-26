"""论文发现：让大模型推荐论文，写成中文概括，给出链接。

## 为什么是「推荐」而不是「检索」

这里没有论文数据库。模型凭自己的知识给出论文，代码只负责把它展示出来。
代价是明确且已知的（都是实测数据，不是猜测）：

  - **看不到新论文。** 连问四次共 24 篇，2024 年之后的一篇都没有——模型的知识
    停在训练截止那天。
  - **会重复。** 同一句话问四次，24 篇里只有 10 篇不重样，其中 4 篇每次都出现。
    所以下面专门做了 ANGLES：每次随机换一个细分角度，逼模型换话题。
  - **链接多是 DOI 落地页**，能不能下到 PDF 不归这里管。

## 为什么不做「下载 + 自动翻译」

卡片上因此只有「去原站阅读」，没有「翻译这篇」。判断一个链接能不能下到 PDF，
本身就要发起一次下载探测；模型给的又多是落地页，成功率低。下载导入交给用户
自己在浏览器里点链接，比在这里猜要可靠。

## 池子

模型推荐要好几秒，页面刷新不该等。所以后台提前攒几篇进 SQLite，刷新只是读一次
本地库（<50ms）。池子见底时后台自动补，不阻塞当前请求。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import uuid
from typing import Any

from .config import Settings
from .store import Store
from .translator import Translator

log = logging.getLogger("app.discovery")

CATEGORY_LABELS = {
    "ai": "AI",
    "tech": "科技",
    "religion": "宗教",
    "philosophy": "哲学",
    "fun": "有意思",
}

# 每个方向下的细分角度。每次随机挑一个塞进提示词。
#
# 这不是锦上添花——不做的话同一个泛泛的问题会得到同一批答案：实测连问四次，
# 24 篇里只有 10 篇不重样（58% 重复），「Attention Is All You Need」四次全中。
# 「刷新换一篇」刷三下就见底了。给出具体角度能显著拉开推荐范围。
ANGLES: dict[str, list[str]] = {
    "ai": [
        "可解释性与探针", "对齐与安全", "多模态", "推理与规划", "小样本与迁移",
        "模型压缩与蒸馏", "检索增强", "智能体与环境交互", "训练动力学与缩放定律",
        "评测基准与方法论反思", "注意力机制分析", "生成模型的采样策略",
    ],
    "tech": [
        "材料与失效分析", "电池与储能", "生物医学工程", "机器人控制",
        "芯片与体系结构", "气候与环境工程", "增材制造", "柔性传感器",
        "量子计算与通信", "工程中的可靠性设计", "能源系统优化", "极端环境材料",
    ],
    "religion": [
        "宗教认知科学", "仪式的演化与功能", "宗教与心理健康", "比较神话学",
        "宗教社会学与世俗化", "宗教文本的诠释方法", "宗教经验的神经科学",
        "朝圣与空间实践", "宗教与道德的关系", "古代宗教的物质文化",
        "宗教传播与网络", "神秘主义传统",
    ],
    "philosophy": [
        "心灵哲学与意识", "认识论与知识辩护", "伦理学与道德心理学", "语言哲学",
        "科学哲学与实在论", "形而上学与因果性", "政治哲学与正义", "美学与艺术哲学",
        "行动哲学与自由意志", "逻辑与悖论", "现象学与具身认知", "实验哲学",
    ],
    "fun": [
        "反直觉的心理学实验", "动物的古怪行为", "睡眠与梦的功能",
        "人体微生物与行为", "概率直觉的系统性偏差", "气味与记忆",
        "音乐与大脑", "体育运动中的物理", "失败与不可复现的实验",
        "随机性错觉", "幽默的认知机制", "拖延与时间感知",
    ],
}

RECOMMEND_SYSTEM = """你在给一位中文读者推荐论文。他兴趣很杂：AI、科技、宗教、哲学，以及各种反直觉、有意思的研究。

严格遵守以下几条：

1. 只推荐你确信真实存在的论文。标题、作者、年份、期刊/会议都要准确。拿不准就换一篇，宁可推荐普通的，也不要编造。
2. link 必须是能打开的地址：优先 arXiv（https://arxiv.org/abs/xxxx.xxxxx），其次 DOI（https://doi.org/10.xxxx/xxx）。不要拼凑你没把握的 URL。
3. 在满足前两条的前提下，尽量选近几年的、不那么烂大街的论文。不要反复推荐那些人尽皆知的名篇。
4. zh 是给读者看的中文概括：一句话说清这篇论文做了什么、发现了什么。不超过 60 字。
5. why 是推荐理由：为什么值得一个好奇的读者花时间。不超过 30 字。
6. 只输出 JSON 数组，不要任何解释文字，不要 markdown 代码块。"""


# --------------------------------------------------------------------- 纯函数
def normalize_title(title: str) -> str:
    """标题归一化，用于去重：小写、去标点、压缩空白。"""
    t = re.sub(r"[^\w\s]", " ", (title or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def title_key(title: str) -> str:
    """拿去重键顶替 OpenAlex 的 work id。

    picks 表里那一列叫 oa_id 且是 NOT NULL UNIQUE，现在没有 OpenAlex 了，
    就用标题哈希填进去——约束和去重逻辑都不用动，也不用做表迁移。
    """
    return "t:" + hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:20]


def _clean(value: Any, limit: int) -> str:
    """压平空白 + 截断。模型偶尔会吐多行或超长文本，直接放进卡片会撑破版式。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _to_year(value: Any) -> int:
    """年份容忍字符串（"2023" / "2023年" / 2023.0）。拿不到就记 0，卡片上不显示。"""
    m = re.search(r"(1[89]\d{2}|20\d{2})", str(value or ""))
    return int(m.group(1)) if m else 0


def build_user(category: str, angle: str, count: int, avoid: list[str]) -> str:
    """拼用户消息。avoid 是最近推荐过的标题——光靠代码过滤只能挡住重复入库，
    把清单给模型看才能真正让它换个话题。"""
    parts = [f"请推荐 {count} 篇{category}方向的论文，具体角度：{angle}。"]
    if avoid:
        listed = "\n".join(f"- {t}" for t in avoid)
        parts.append(f"\n下面这些已经推荐过了，不要再推荐：\n{listed}")
    parts.append(
        "\n只输出 JSON 数组，格式：\n"
        '[{"title":"...","authors":"...","year":2023,"venue":"...",'
        '"link":"https://...","zh":"...","why":"..."}]'
    )
    return "\n".join(parts)


def parse_recommendations(text: str, limit: int) -> list[dict[str, Any]]:
    """把模型输出解析成规范化的推荐列表。解析不出来就返回空列表，由上层当作失败。

    模型经常在 JSON 外面包一层解释文字或 ```json 围栏，所以取最外层方括号。
    """
    block = re.search(r"\[.*\]", text or "", re.S)
    if not block:
        return []
    try:
        raw = json.loads(block.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 200)
        if not title:
            continue
        link = _clean(item.get("link") or item.get("url"), 500)
        # 只放行 http(s)：模型偶尔会给出畸形或被污染的地址，塞进 href 是要出事的
        if not re.match(r"^https?://", link, re.I):
            link = ""
        out.append({
            "title": title,
            "authors": _clean(item.get("authors"), 120),
            "year": _to_year(item.get("year")),
            "venue": _clean(item.get("venue"), 100),
            "link": link,
            # 提示词里写的是 60/30，这里放宽一点当安全网——卡在字数中间截断更难看
            "zh": _clean(item.get("zh"), 80),
            "why": _clean(item.get("why"), 40),
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------- 调度
class Recommender:
    """维护一个预先攒好的推荐池，让页面刷新时无需等待模型。"""

    # 池子见底时的最短补充间隔，避免连点「换一篇」把模型打爆
    KICK_COOLDOWN = 30.0
    # 连续失败后的冷却：模型挂了或一直吐不出合格 JSON 时别死循环
    FAIL_COOLDOWN = 300.0

    def __init__(self, settings: Settings, store: Store, translator: Translator) -> None:
        self.s = settings
        self.db = store
        self.tr = translator
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None   # 持有引用，否则可能被 GC 回收
        self._loop_task: asyncio.Task | None = None
        # 必须用 -inf 而不是 0：macOS 上 time.monotonic() 返回的是「进程启动至今的秒数」，
        # 进程刚起来时只有零点几，用 0 初始化会把启动时的第一次补充挡掉，功能静默失效。
        self._last_kick = float("-inf")
        self._fail_until = 0.0
        # 启动或运行中出了无法恢复的问题就置 False，接口据此告诉前端「别等了」
        self._enabled = True

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        if not self.s.pick_enabled or not self.tr.s.llm_ready:
            log.info("论文发现未启用（pick_enabled=%s llm_ready=%s）",
                     self.s.pick_enabled, self.tr.s.llm_ready)
            return
        # 整个启动过程包在 try 里：推荐是锦上添花的功能，它出任何问题都不该让
        # 阅读器起不来。踩过一次——picks 表的 btree 损坏，这里的清库语句抛
        # DatabaseError，直接把整个 lifespan 带崩，连书库和翻译都进不去。
        try:
            if self.s.pick_purge_versions:
                n = self.db.pick_purge_versions(self.s.pick_prompt_ver)
                if n:
                    log.info("提示词版本变化，已清空 %d 条旧推荐", n)
            self._kick()
            self._loop_task = asyncio.create_task(self._run_forever())
        except Exception:  # noqa: BLE001
            log.exception("论文发现启动失败，本次运行将不提供推荐（不影响其余功能）")
            self._enabled = False

    async def aclose(self) -> None:
        for task in (self._task, self._loop_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("关闭推荐任务时出错")

    async def _run_forever(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.s.pick_refresh_sec)
                if self.db.pick_count_unused() < self.s.pick_pool_min:
                    self._kick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("论文发现后台巡检出错")

    def _kick(self) -> None:
        """确保同一时刻只有一个补充任务在跑。"""
        if self._task is not None and not self._task.done():
            return
        now = time.monotonic()
        if now < self._fail_until or now - self._last_kick < self.KICK_COOLDOWN:
            return
        self._task = asyncio.create_task(self._top_up_safe())

    async def _top_up_safe(self) -> None:
        try:
            added, attempted = await self.top_up()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("补充推荐池失败")
            added, attempted = 0, True
        # 只有「真的去补了、却一篇都没补上」才算失败。
        # 池子本来就够时 top_up 直接返回、一次模型都没调，那是正常空转——
        # 早先没区分这两种情况，导致每次带着满池子重启都会误判成模型挂了，
        # 白锁 5 分钟不自补，卡片还会显示「模型暂时不可用」。
        if attempted and not added:
            self._last_kick = time.monotonic()
            self._fail_until = time.monotonic() + self.FAIL_COOLDOWN

    # ---------------------------------------------------------------- 池子
    async def top_up(self) -> tuple[int, bool]:
        """把池子补到 pick_pool_min。

        返回 (新增条数, 是否真的调用过模型)。池子本来就够时不调用模型，
        由调用方据第二个值区分「无事可做」和「补了但没补上」。
        """
        if not self.s.pick_enabled or not self._enabled:
            return 0, False
        async with self._lock:
            added = 0
            attempted = False
            # 每轮最多两次调用，避免模型一直吐不出合格内容时死循环
            for _ in range(2):
                if self.db.pick_count_unused() >= self.s.pick_pool_min:
                    break
                attempted = True
                try:
                    added += await self._generate_batch()
                except Exception:  # noqa: BLE001
                    log.exception("生成推荐失败")
            if added:
                self.db.pick_prune()
                log.info("推荐池补充完成，新增 %d 篇，当前未用 %d 篇",
                         added, self.db.pick_count_unused())
            return added, attempted

    @property
    def blocked_for(self) -> float:
        """还有多少秒解除失败冷却；0 表示正常。"""
        return max(0.0, self._fail_until - time.monotonic())

    @property
    def available(self) -> bool:
        """功能是不是还活着。挂了的话接口要直说，别让前端一直转圈等。"""
        return self._enabled

    async def next_pick(self) -> dict[str, Any] | None:
        """取一篇推荐。池子见底时顺手在后台补充，不阻塞本次响应。

        读池子本身也可能因为库损坏而抛错——这里兜住，返回 None 让上层降级显示，
        绝不能把 500 抛给前端。
        """
        if not self._enabled:
            return None
        try:
            pick = self.db.pick_next()
            if self.db.pick_count_unused() < self.s.pick_pool_min:
                self._kick()
            return pick
        except Exception:  # noqa: BLE001
            log.exception("读取推荐池失败，本次不返回推荐")
            self._enabled = False
            return None

    # ---------------------------------------------------------------- 生成
    async def _generate_batch(self) -> int:
        """随机挑方向 + 角度，让模型推荐一批，去重后入库。返回新增条数。"""
        category = random.choice(list(CATEGORY_LABELS))
        angle = random.choice(ANGLES[category])
        avoid = self.db.pick_recent_titles(15)

        text = await self.tr.complete(
            RECOMMEND_SYSTEM,
            build_user(CATEGORY_LABELS[category], angle, self.s.pick_batch, avoid),
            temperature=self.s.pick_temperature,
        )
        items = parse_recommendations(text, self.s.pick_batch)
        if not items:
            log.warning("模型没给出可解析的推荐（方向=%s 角度=%s）：%r",
                        category, angle, (text or "")[:200])
            return 0

        added = 0
        for it in items:
            if self.db.pick_add(
                pick_id=uuid.uuid4().hex[:16],
                oa_id=title_key(it["title"]),
                category=category,
                title=it["title"],
                authors=it["authors"],
                year=it["year"],
                venue=it["venue"],
                doi="",
                landing_url=it["link"],
                pdf_url="",
                pdf_ok=False,
                zh=it["zh"],
                why=it["why"],
                prompt_ver=self.s.pick_prompt_ver,
            ):
                added += 1
                log.info("推荐【%s·%s】%s", CATEGORY_LABELS[category], angle, it["title"][:50])

        log.info("方向=%s 角度=%s：模型给 %d 篇，去重后入库 %d 篇",
                 category, angle, len(items), added)
        return added

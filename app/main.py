"""FastAPI 服务：PDF 导入 → 切片 → 逐段流式翻译 → 双语阅读。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .auth import AuthMiddleware, COOKIE_NAME, SESSION_TTL, login_page, make_token
from .config import Settings, get_settings
from .pdf_parser import parse_pdf
from .protect import glossary_fingerprint
from .store import Store, cache_key
from .translator import Translator, TranslationError
from .tts import VOICES, SpeechError, Synthesizer, clean_for_speech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
TRANSLATABLE = {"title", "heading", "para", "caption"}
VERBATIM = {"keep"}
SKIPPED = {"meta", "ref", "refhead"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    store = Store(settings.db_path, settings.default_glossary)
    translator = Translator(settings)
    tts = Synthesizer(api_key=settings.tts_api_key, base_url=settings.tts_base_url,
                      model=settings.tts_model, voice=settings.tts_voice,
                      audio_format=settings.tts_format, timeout=settings.tts_timeout,
                      cache_dir=settings.tts_dir,
                      max_chunk=settings.tts_chunk_chars,
                      concurrency=settings.tts_concurrency,
                      cache_max_mb=settings.tts_cache_max_mb)
    app.state.settings = settings
    app.state.store = store
    app.state.translator = translator
    app.state.tts = tts
    log.info("启动完成：模型=%s mock=%s 数据目录=%s",
             settings.model, settings.mock, settings.data_dir)
    try:
        yield
    finally:
        if settings.tts_cache_on_shutdown:
            freed = 0
            for f in list(settings.tts_dir.glob("*.wav")) + list(settings.tts_dir.glob("*.mp3")):
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except OSError:
                    pass
            if freed:
                log.info("服务退出，已清理语音缓存：释放 %.1f MB", freed / 1048576)
        await translator.aclose()
        await tts.aclose()
        store.close()


app = FastAPI(title="Paper Reader · 论文双语阅读器", version="1.0.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware, password=get_settings().app_password)


@app.get("/login")
async def login_form(request: Request) -> HTMLResponse:
    """登录页（未设置 APP_PASSWORD 时直接放行）。"""
    if not get_settings().app_password:
        return RedirectResponse("/", status_code=302)
    return login_page(next_url=request.query_params.get("next", "/"))


@app.post("/login")
async def login_submit(request: Request) -> Response:
    """校验口令并下发签名 Cookie。"""
    password = get_settings().app_password
    form = await request.form()
    nxt = str(form.get("next") or "/")
    if not nxt.startswith("/"):
        nxt = "/"
    if password and str(form.get("password") or "").strip() == password:
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE_NAME, make_token(password), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        log.info("登录成功：%s", request.client.host if request.client else "?")
        return resp
    return login_page(error=True, next_url=nxt)


def S() -> Settings:
    return app.state.settings


def DB() -> Store:
    return app.state.store


def TR() -> Translator:
    return app.state.translator


def TTS_ENGINE() -> Synthesizer:
    return app.state.tts


def variant_key() -> str:
    s = S()
    fp = glossary_fingerprint(DB().glossary_map())
    return f"{s.prompt_version}:{'mock' if s.mock else s.model}:{fp}"


def _doc_or_404(doc_id: str) -> dict[str, Any]:
    doc = DB().get_doc(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc


def _blocks_payload(doc_id: str, include_refs: bool = True) -> list[dict[str, Any]]:
    rows = DB().get_blocks(doc_id, include_refs=include_refs)
    for r in rows:
        r.pop("hash", None)
        r["cont"] = bool(r.get("cont"))
    return rows


def _section_map(blocks: list[dict[str, Any]]) -> dict[int, str]:
    out: dict[int, str] = {}
    cur = ""
    for b in blocks:
        if b["kind"] in {"heading", "refhead"}:
            cur = b["text"]
        out[b["idx"]] = cur
    return out


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------- 基础接口
@app.get("/api/health")
async def health() -> dict[str, Any]:
    s = S()
    return {"ok": True, "llm_ready": s.llm_ready, "config": s.public()}


@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    docs = DB().list_docs()
    return {
        "docs": len(docs),
        "blocks": sum(d["total"] for d in docs),
        "translated": sum(d["done"] for d in docs),
        "cache_entries": DB().cache_size(),
        "llm": dict(TR().stats),
        "tts": dict(TTS_ENGINE().stats),
        "config": S().public(),
    }


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return S().public()


# --------------------------------------------------------------------- 文档
@app.get("/api/docs")
async def list_docs() -> dict[str, Any]:
    return {"docs": DB().list_docs()}


@app.post("/api/docs")
async def upload_doc(file: UploadFile = File(...)) -> dict[str, Any]:
    s = S()
    name = file.filename or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")

    doc_id = uuid.uuid4().hex[:16]
    dest = s.upload_dir / f"{doc_id}.pdf"
    size = 0
    limit = s.max_upload_mb * 1024 * 1024
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"文件超过 {s.max_upload_mb} MB 限制")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        parsed = await run_in_threadpool(parse_pdf, dest, s.max_chars, s.translate_refs)
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        log.exception("解析失败")
        raise HTTPException(422, f"PDF 解析失败：{exc}") from exc

    if not parsed.blocks:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "未能从该 PDF 中切分出可用段落")

    doc = DB().create_doc(
        doc_id=doc_id, filename=name, title=parsed.title,
        num_pages=parsed.num_pages, blocks=parsed.blocks,
        meta={"notes": parsed.notes, "toc": parsed.toc,
              "kinds": _kind_counts(parsed.blocks)},
        variant=variant_key(),
    )
    assert doc is not None
    return {"doc": doc, "blocks": _blocks_payload(doc_id)}


def _kind_counts(blocks: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in blocks:
        out[b["kind"]] = out.get(b["kind"], 0) + 1
    return out


@app.get("/api/docs/{doc_id}")
async def get_doc(doc_id: str) -> dict[str, Any]:
    doc = _doc_or_404(doc_id)
    return {"doc": doc, "blocks": _blocks_payload(doc_id), "config": S().public()}


@app.delete("/api/docs/{doc_id}")
async def delete_doc(doc_id: str) -> dict[str, Any]:
    _doc_or_404(doc_id)
    freed = purge_speech_cache(doc_id)
    DB().delete_doc(doc_id)
    (S().upload_dir / f"{doc_id}.pdf").unlink(missing_ok=True)
    return {"ok": True, "speech_freed_mb": round(freed / 1048576, 1)}


@app.get("/api/docs/{doc_id}/file")
async def get_pdf(doc_id: str) -> FileResponse:
    doc = _doc_or_404(doc_id)
    path = S().upload_dir / f"{doc_id}.pdf"
    if not path.is_file():
        raise HTTPException(404, "原文件已丢失")
    return FileResponse(path, media_type="application/pdf",
                        filename=doc["filename"], content_disposition_type="inline")


@app.post("/api/docs/{doc_id}/reset")
async def reset_doc(doc_id: str, keep_done: bool = Query(False)) -> dict[str, Any]:
    _doc_or_404(doc_id)
    DB().reset_doc(doc_id, include_done=not keep_done)
    return {"ok": True, "doc": DB().get_doc(doc_id)}


# --------------------------------------------------------------------- 翻译
@app.get("/api/docs/{doc_id}/translate")
async def translate_stream(doc_id: str, request: Request,
                           idx: str | None = Query(None, description="逗号分隔的段落号，缺省=全部未翻译"),
                           force: bool = Query(False)) -> StreamingResponse:
    s = S()
    doc = _doc_or_404(doc_id)
    if not s.llm_ready:
        raise HTTPException(400, "尚未配置 LLM：请在 .env 中设置 LLM_API_KEY，或设置 LLM_MOCK=1 试用")

    indices = None
    if idx:
        try:
            indices = sorted({int(x) for x in idx.split(",") if x.strip()})
        except ValueError as exc:
            raise HTTPException(400, "idx 参数格式错误") from exc

    async def generate() -> AsyncIterator[str]:
        all_blocks = await run_in_threadpool(_blocks_payload, doc_id, s.translate_refs)
        sections = _section_map(all_blocks)
        title = doc.get("title") or ""

        targets = [b for b in all_blocks
                   if b["kind"] in TRANSLATABLE and (indices is None or b["idx"] in indices)]
        if force:
            pending = targets
        else:
            pending = [b for b in targets if b["status"] != "done"]
        verbatim = [b for b in all_blocks if b["kind"] in VERBATIM
                    and (indices is None or b["idx"] in indices)]
        skipped = [b for b in all_blocks if b["kind"] in SKIPPED
                   and (indices is None or b["idx"] in indices)]

        yield sse("meta", {"doc_id": doc_id, "model": "mock" if s.mock else s.model,
                           "total": len(targets), "pending": len(pending),
                           "force": force})

        for b in skipped:
            if b["status"] not in {"skip", "done"}:
                await run_in_threadpool(DB().set_block_translation, doc_id, b["idx"], None, "skip")
                yield sse("block", {"idx": b["idx"], "zh": None, "status": "skip"})
        for b in verbatim:
            if b["zh"] != b["text"]:
                await run_in_threadpool(DB().set_block_translation, doc_id, b["idx"], b["text"], "done")
                yield sse("block", {"idx": b["idx"], "zh": b["text"], "status": "done", "verbatim": True})

        glossary = DB().glossary_map()
        variant = variant_key()
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        # 散热节流：串行放行请求，且两段之间至少间隔 request_interval 秒，
        # 让无风扇机器的 CPU 有机会回落，避免长时间 100% 占空比。
        pace_lock = asyncio.Lock()
        next_slot = 0.0

        async def throttle() -> None:
            nonlocal next_slot
            if s.request_interval <= 0:
                return
            async with pace_lock:
                now = time.monotonic()
                if next_slot > now:
                    await asyncio.sleep(next_slot - now)
                next_slot = time.monotonic() + s.request_interval

        async def handle(block: dict[str, Any]) -> None:
            key = cache_key(block["text"], variant)
            if not force:
                hit = await run_in_threadpool(DB().cache_get_many, [key])
                if key in hit:
                    zh = hit[key]
                    await run_in_threadpool(DB().set_block_translation, doc_id, block["idx"], zh, "done")
                    await queue.put(("block", {"idx": block["idx"], "zh": zh,
                                               "status": "done", "cached": True}))
                    return
            await throttle()
            try:
                zh = await TR().translate(block["text"], title=title,
                                         section=sections.get(block["idx"], ""),
                                         glossary=glossary)
            except TranslationError as exc:
                log.warning("段落 %s 翻译失败：%s", block["idx"], exc)
                await run_in_threadpool(DB().set_block_translation, doc_id, block["idx"], None, "error")
                await queue.put(("block", {"idx": block["idx"], "zh": None,
                                           "status": "error", "error": str(exc)[:200]}))
                return
            await run_in_threadpool(DB().set_block_translation, doc_id, block["idx"], zh, "done")
            await run_in_threadpool(DB().cache_put, key, zh, "mock" if s.mock else s.model)
            await queue.put(("block", {"idx": block["idx"], "zh": zh, "status": "done"}))

        async def producer() -> None:
            tasks = [asyncio.create_task(handle(b)) for b in pending]
            try:
                for fut in asyncio.as_completed(tasks):
                    await fut
            finally:
                await queue.put(("__done__", {"translated": len(pending)}))

        prod = asyncio.create_task(producer())
        ok = failed = 0
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    if await request.is_disconnected():
                        break
                    continue
                if event == "__done__":
                    break
                if payload.get("status") == "error":
                    failed += 1
                elif payload.get("zh"):
                    ok += 1
                yield sse(event, payload)
                if await request.is_disconnected():
                    break
        finally:
            prod.cancel()
            try:
                await prod
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        yield sse("done", {"doc_id": doc_id, "translated": ok, "failed": failed,
                           "progress": DB().progress(doc_id)})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.post("/api/docs/{doc_id}/blocks/{idx}/translate")
async def translate_one(doc_id: str, idx: int, force: bool = Query(True)) -> dict[str, Any]:
    s = S()
    doc = _doc_or_404(doc_id)
    rows = DB().get_blocks(doc_id, indices=[idx])
    if not rows:
        raise HTTPException(404, "段落不存在")
    block = rows[0]
    if block["kind"] in SKIPPED:
        return {"idx": idx, "zh": None, "status": "skip"}
    if block["kind"] in VERBATIM:
        DB().set_block_translation(doc_id, idx, block["text"], "done")
        return {"idx": idx, "zh": block["text"], "status": "done"}
    if not s.llm_ready:
        raise HTTPException(400, "尚未配置 LLM")
    sections = _section_map(DB().get_blocks(doc_id))
    try:
        zh = await TR().translate(block["text"], title=doc.get("title") or "",
                                  section=sections.get(idx, ""), glossary=DB().glossary_map())
    except TranslationError as exc:
        DB().set_block_translation(doc_id, idx, None, "error")
        raise HTTPException(502, f"翻译失败：{exc}") from exc
    key = cache_key(block["text"], variant_key())
    DB().set_block_translation(doc_id, idx, zh, "done")
    DB().cache_put(key, zh, "mock" if s.mock else s.model)
    return {"idx": idx, "zh": zh, "status": "done", "doc": DB().get_doc(doc_id)}


# --------------------------------------------------------------------- 语音合成
@app.get("/api/tts/config")
async def tts_config() -> dict[str, Any]:
    s = S()
    return {"ready": s.tts_ready, "model": s.tts_model, "voice": s.tts_voice,
            "format": s.tts_format, "voices": VOICES}


@app.post("/api/tts")
async def tts_speak(payload: dict[str, Any]) -> Response:
    """把任意文本合成为音频（带磁盘缓存，重听不重复计费）。"""
    engine = TTS_ENGINE()
    if not engine.enabled:
        raise HTTPException(400, "未配置 TTS：请在 .env 中设置 TTS_API_KEY")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    if len(text) > 6000:
        raise HTTPException(400, "文本过长，请分段合成")
    try:
        data, cached = await engine.synthesize(text, payload.get("voice"))
    except SpeechError as exc:
        raise HTTPException(502, f"语音合成失败：{exc}") from exc
    media = "audio/wav" if engine.audio_format == "wav" else f"audio/{engine.audio_format}"
    return Response(content=data, media_type=media,
                    headers={"X-TTS-Cached": "1" if cached else "0",
                             "Cache-Control": "public, max-age=86400"})


@app.get("/api/docs/{doc_id}/blocks/{idx}/speech")
async def tts_block(doc_id: str, idx: int, voice: str | None = Query(None)) -> Response:
    """朗读某一段的中文译文（翻译未完成时返回 409，前端会跳过）。"""
    _doc_or_404(doc_id)
    engine = TTS_ENGINE()
    if not engine.enabled:
        raise HTTPException(400, "未配置 TTS：请在 .env 中设置 TTS_API_KEY")
    rows = DB().get_blocks(doc_id, indices=[idx])
    if not rows:
        raise HTTPException(404, "段落不存在")
    block = rows[0]
    text = block.get("zh") or ""
    if not text:
        raise HTTPException(409, "该段尚未翻译，无法朗读")
    try:
        data, cached = await engine.synthesize(text, voice)
    except SpeechError as exc:
        raise HTTPException(502, f"语音合成失败：{exc}") from exc
    media = "audio/wav" if engine.audio_format == "wav" else f"audio/{engine.audio_format}"
    return Response(content=data, media_type=media,
                    headers={"X-TTS-Cached": "1" if cached else "0"})


def doc_speech_files(doc_id: str) -> list[Path]:
    """按段落译文逐个推导出语音缓存文件（缓存键由"清洗后文本+音色"决定）。"""
    engine = TTS_ENGINE()
    seen: set[Path] = set()
    for b in DB().get_blocks(doc_id):
        zh = b.get("zh")
        if not zh:
            continue
        spoken = clean_for_speech(zh)
        if not spoken:
            continue
        for v in VOICES:
            p = engine.cache_path(spoken, v["id"])
            if p.is_file():
                seen.add(p)
    return sorted(seen)


def all_speech_files() -> list[Path]:
    d = S().tts_dir
    return sorted(d.glob("*.wav")) + sorted(d.glob("*.mp3")) if d.is_dir() else []


def purge_speech_cache(doc_id: str | None = None) -> int:
    """删除语音缓存，返回释放的字节数。doc_id 为空则清空全部。"""
    files = doc_speech_files(doc_id) if doc_id else all_speech_files()
    freed = 0
    for f in files:
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return freed


@app.post("/api/tts/cache/purge")
async def tts_cache_purge_beacon(doc_id: str | None = Query(None)) -> dict[str, Any]:
    """供浏览器关闭/离开页面时用 sendBeacon 调用（sendBeacon 只能发 POST）。"""
    files = doc_speech_files(doc_id) if doc_id else all_speech_files()
    freed = purge_speech_cache(doc_id)
    log.info("退出清理语音缓存：%d 个文件，释放 %.1f MB", len(files), freed / 1048576)
    return {"ok": True, "freed_mb": round(freed / 1048576, 1)}


@app.get("/api/tts/cache")
async def tts_cache_info(doc_id: str | None = Query(None)) -> dict[str, Any]:
    """语音缓存占用情况。带 doc_id 时只统计该篇论文。"""
    if doc_id:
        _doc_or_404(doc_id)
        files = doc_speech_files(doc_id)
    else:
        files = all_speech_files()
    total = sum(f.stat().st_size for f in files if f.is_file())
    return {"doc_id": doc_id, "files": len(files), "bytes": total,
            "mb": round(total / 1048576, 1),
            "total_mb": round(sum(f.stat().st_size for f in all_speech_files()) / 1048576, 1),
            "cap_mb": TTS_ENGINE().cache_max_mb}


@app.delete("/api/tts/cache")
async def tts_cache_purge(doc_id: str | None = Query(None)) -> dict[str, Any]:
    """清理语音缓存：带 doc_id 清该篇，不带则全部清空。"""
    freed = purge_speech_cache(doc_id)
    rest = sum(f.stat().st_size for f in all_speech_files())
    return {"ok": True, "freed_mb": round(freed / 1048576, 1),
            "remaining_mb": round(rest / 1048576, 1)}


@app.get("/api/cache")
async def translation_cache_info(doc_id: str | None = Query(None)) -> dict[str, Any]:
    """翻译缓存（避免同一段落重复调用大模型计费）。"""
    info = DB().cache_stats()
    info["doc_id"] = doc_id
    return info


@app.delete("/api/cache")
async def translation_cache_purge(doc_id: str | None = Query(None)) -> dict[str, Any]:
    """清理翻译缓存：带 doc_id 只清该篇（按段落内容哈希反查），否则清空全部。

    注意：清掉之后再导入同一篇论文会重新调用大模型，产生真实费用。
    """
    if doc_id:
        _doc_or_404(doc_id)
        variant = variant_key()
        keys = [cache_key(b["text"], variant) for b in DB().get_blocks(doc_id) if b.get("text")]
        removed = DB().cache_delete(keys)
    else:
        removed = DB().cache_clear()
    return {"ok": True, "removed": removed, **DB().cache_stats()}


# --------------------------------------------------------------------- 导出
@app.get("/api/docs/{doc_id}/export.md")
async def export_markdown(doc_id: str) -> PlainTextResponse:
    doc = _doc_or_404(doc_id)
    lines = [f"# {doc['title']}", ""]
    for b in _blocks_payload(doc_id):
        kind, text, zh = b["kind"], b["text"], b.get("zh")
        if kind == "title":
            lines += [f"> {zh}", ""] if zh else []
            continue
        if kind in {"heading", "refhead"}:
            lines += [f"## {text}", ""]
            if zh:
                lines += [f"### {zh}", ""]
            continue
        if kind in {"meta", "ref"}:
            lines += [f"> {text}", ""]
            continue
        lines.append(text)
        lines.append("")
        if zh:
            lines.append(zh)
            lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{doc_id}.md"'})


# --------------------------------------------------------------------- 术语表
@app.get("/api/glossary")
async def get_glossary() -> dict[str, Any]:
    return {"terms": DB().glossary_all()}


@app.post("/api/glossary")
async def put_glossary(payload: dict[str, Any]) -> dict[str, Any]:
    en = (payload.get("en") or "").strip()
    zh = (payload.get("zh") or "").strip()
    if not en or not zh:
        raise HTTPException(400, "en 与 zh 均不能为空")
    DB().glossary_upsert(en, zh, payload.get("note") or "")
    return {"ok": True, "terms": DB().glossary_all()}


@app.delete("/api/glossary/{en}")
async def del_glossary(en: str) -> dict[str, Any]:
    DB().glossary_delete(en)
    return {"ok": True, "terms": DB().glossary_all()}


# --------------------------------------------------------------------- 前端
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
@app.get("/read/{doc_id}", response_class=HTMLResponse)
async def index(doc_id: str | None = None) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "接口不存在"}, status_code=404)
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"), status_code=200)

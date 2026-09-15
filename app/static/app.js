'use strict';
/* Paper Reader 前端逻辑：书库 + 双语阅读器（无构建步骤，原生 ES2020） */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const PREF_KEY = 'paperreader.prefs.v1';
const prefs = Object.assign(
  { mode: 'bi', theme: 'light', fs: 17, scope: 'all', auto: true, voice: 'mimo_default', rate: 1, autoCleanTts: false },
  JSON.parse(localStorage.getItem(PREF_KEY) || '{}'),
);
const savePrefs = () => localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
// 一次性迁移：早期默认「退出即清理语音缓存」，现改为保留缓存（服务端按 TTS_CACHE_MAX_MB 上限 LRU 淘汰）
if (!prefs.capRetentionV1) {
  prefs.autoCleanTts = false;
  prefs.capRetentionV1 = true;
  savePrefs();
}

const state = {
  id: null, doc: null, blocks: [], nodes: new Map(),
  es: null, finished: false, cfg: {}, io: null, inflight: new Set(), tts: { ready: false, voices: [] },
};

/* 朗读播放器：逐段合成 → 播放 → 自动续下一段，音频按段落缓存 */
const player = { queue: [], pos: 0, audio: null, playing: false, loading: false, token: 0,
                  session: 0, prewarm: null, warm: 0,
                  urls: new Map(), inflight: new Map() };   // urls: 段落→blob；inflight: 去重；session: 一次朗读会话

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401 && !location.pathname.startsWith('/login')) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('未登录');
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

/* ------------------------------------------------------------------ 偏好 */
function applyPrefs() {
  document.documentElement.dataset.theme = prefs.theme;
  document.documentElement.style.setProperty('--fs', prefs.fs + 'px');
  document.body.dataset.mode = prefs.mode;
  $$('#modeSeg button').forEach((b) => b.classList.toggle('on', b.dataset.mode === prefs.mode));
  const chk = $('#scopeChk');
  if (chk) chk.checked = prefs.scope === 'view';
}

function setMode(mode) {
  prefs.mode = mode; savePrefs(); applyPrefs();
}

/* ------------------------------------------------------------------ 书库 */
async function loadConfig() {
  try {
    state.cfg = await api('/api/config');
  } catch (_) { state.cfg = {}; }
  try { state.tts = await api('/api/tts/config'); } catch (_) { state.tts = { ready: false, voices: [] }; }
  document.body.classList.toggle('no-tts', !state.tts.ready);
  const sel = $('#voiceSel');
  if (sel) {
    sel.innerHTML = (state.tts.voices || []).map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join('');
    sel.value = prefs.voice || state.tts.voice || 'mimo_default';
    if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
  }
  const chip = $('#llmChip');
  const warn = $('#llmWarn');
  if (state.cfg.mock) {
    chip.textContent = '模拟模式（不调用真实模型）'; chip.className = 'chip ok'; warn.classList.add('hidden');
  } else if (state.cfg.llm_ready) {
    chip.textContent = `模型 ${state.cfg.model}`; chip.className = 'chip ok'; warn.classList.add('hidden');
  } else {
    chip.textContent = '未配置模型'; chip.className = 'chip bad';
    warn.innerHTML = '尚未配置翻译模型：请在项目根目录的 <code>.env</code> 中填写 '
      + '<code>LLM_API_KEY</code>（可同时设置 <code>LLM_BASE_URL</code> / <code>LLM_MODEL</code>），'
      + '然后重启容器；或设置 <code>LLM_MOCK=1</code> 先体验流程。';
    warn.classList.remove('hidden');
  }
}

async function loadLibrary() {
  const list = $('#docList');
  let docs = [];
  try { docs = (await api('/api/docs')).docs; } catch (_) { docs = []; }
  list.innerHTML = '';
  $('#libEmpty').classList.toggle('hidden', docs.length > 0);
  for (const d of docs) {
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="d-main">
        <p class="d-title">${esc(d.title || d.filename)}</p>
        <div class="d-meta">
          <span>${esc(d.filename)}</span>
          <span>${d.num_pages} 页</span>
          <span>${d.total} 段</span>
          <span>${new Date(d.created_at * 1000).toLocaleString()}</span>
        </div>
      </div>
      <div class="d-ring">${d.percent}%</div>
      <button class="btn" data-open="${d.id}">阅读</button>
      <button class="icon" data-del="${d.id}" title="删除">✕</button>`;
    list.appendChild(li);
  }
  list.onclick = async (e) => {
    const open = e.target.closest('[data-open]');
    const del = e.target.closest('[data-del]');
    if (open) location.hash = '#/doc/' + open.dataset.open;
    if (del) {
      if (!confirm('删除这篇论文及其译文？')) return;
      await api('/api/docs/' + del.dataset.del, { method: 'DELETE' });
      loadLibrary();
    }
  };
}

async function upload(file) {
  const box = $('#uploadState');
  if (!file) return;
  if (!/\.pdf$/i.test(file.name)) { box.textContent = '请选择 PDF 文件'; return; }
  const fd = new FormData();
  fd.append('file', file);
  box.textContent = `正在解析 ${file.name} …`;
  try {
    const out = await api('/api/docs', { method: 'POST', body: fd });
    box.textContent = `解析完成：${out.doc.total} 个段落块`;
    location.hash = '#/doc/' + out.doc.id;
    setTimeout(() => { box.textContent = ''; }, 1200);
  } catch (err) {
    box.textContent = '解析失败：' + err.message;
  }
}

/* ------------------------------------------------------------------ 阅读器 */
/* 与服务端 TRANSLATABLE 保持一致：refhead/meta/ref/keep 不参与翻译进度 */
const TRANSLATABLE = new Set(['title', 'heading', 'para', 'caption']);

function isTranslatable(b) {
  return TRANSLATABLE.has(b.kind);
}

function buildBlock(b) {
  const el = document.createElement('article');
  el.className = `blk k-${b.kind}` + (b.level ? ` lv${b.level}` : '') + (b.cont ? ' cont' : '');
  el.id = 'b' + b.idx;
  el.dataset.idx = String(b.idx);

  const headTag = (b.kind === 'heading' || b.kind === 'title') ? 'h2' : 'p';
  const en = document.createElement(headTag);
  en.className = 'en';
  en.textContent = b.text;
  el.appendChild(en);

  let zh = null;
  if (b.kind !== 'meta' && b.kind !== 'ref') {
    zh = document.createElement(headTag);
    zh.className = 'zh';
    el.appendChild(zh);
  }

  if (isTranslatable(b)) {
    const tools = document.createElement('div');
    tools.className = 'tools';
    tools.innerHTML = `
      <button data-act="speak" title="朗读本段译文">🔈</button>
      <button data-act="retrans" title="重译本段">⟳</button>
      <button data-act="copy-en" title="复制原文">EN</button>`;
    el.appendChild(tools);
  }
  const node = { el, en, zh, kind: b.kind };
  state.nodes.set(b.idx, node);
  paint(node, b);
  return el;
}

function paint(node, b) {
  if (!node.zh) return;
  const zh = node.zh;
  zh.classList.remove('pending', 'error');
  if (b.kind === 'keep') {
    zh.textContent = '';
    zh.className = 'zh hidden';
    return;
  }
  if (b.status === 'done' && b.zh) {
    zh.textContent = b.zh;
    zh.className = 'zh';
  } else if (b.status === 'error') {
    zh.innerHTML = '本段翻译失败。';
    zh.className = 'zh error';
    const btn = document.createElement('button');
    btn.className = 'retry';
    btn.textContent = '重试';
    btn.onclick = () => translateOne(b.idx);
    zh.appendChild(btn);
  } else if (b.status === 'skip') {
    zh.textContent = '';
    zh.className = 'zh hidden';
  } else {
    zh.textContent = '';
    zh.className = 'zh pending';
  }
}

function renderReader() {
  const doc = state.doc;
  $('#docTitle').textContent = doc.title || doc.filename;
  $('#docSub').textContent = `${doc.filename} · ${doc.num_pages} 页 · ${doc.total} 段 · ${doc.percent}% 已译`;
  const content = $('#content');
  content.innerHTML = '';
  state.nodes.clear();

  let refsBox = null;
  for (const b of state.blocks) {
    if (b.kind === 'refhead') {
      refsBox = document.createElement('details');
      refsBox.className = 'refs';
      const sum = document.createElement('summary');
      sum.textContent = b.text + (b.zh ? ` · ${b.zh}` : '');
      refsBox.appendChild(sum);
      content.appendChild(refsBox);
      state.nodes.set(b.idx, { el: refsBox, en: null, zh: null, kind: 'refhead' });
      continue;
    }
    const el = buildBlock(b);
    if (refsBox && b.kind === 'ref') refsBox.appendChild(el);
    else { content.appendChild(el); refsBox = null; }
  }
  content.onclick = (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const idx = Number(btn.closest('.blk').dataset.idx);
    if (btn.dataset.act === 'retrans') translateOne(idx);
    if (btn.dataset.act === 'copy-en') copy(state.blocks.find((x) => x.idx === idx)?.text || '');
    if (btn.dataset.act === 'speak') speakOne(idx);
  };
  updateProgress();
}

function copy(text) {
  navigator.clipboard?.writeText(text).then(
    () => setStatus('已复制到剪贴板', false, 1400),
    () => setStatus('复制失败', true, 2000),
  );
}

let statusTimer = null;
function setStatus(msg, isErr = false, autoHide = 0) {
  const el = $('#status');
  if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
  if (!msg) { el.classList.add('hidden'); return; }
  el.textContent = msg;
  el.classList.toggle('err', !!isErr);
  el.classList.remove('hidden');
  if (autoHide) statusTimer = setTimeout(() => el.classList.add('hidden'), autoHide);
}

function updateProgress() {
  const tr = state.blocks.filter(isTranslatable);
  const done = tr.filter((b) => b.status === 'done').length;
  const pct = tr.length ? Math.round((done / tr.length) * 100) : 0;
  $('#bar').style.width = pct + '%';
  if (state.doc && state.blocks.length) {
    $('#docSub').textContent =
      `${state.doc.filename} · ${state.doc.num_pages} 页 · ${done}/${tr.length} 段已译 · ${pct}%`;
  }
}

/* ------------------------------------------------------------ 流式翻译 */
async function openDoc(id) {
  stopStream();
  stopSpeaking(true);
  player.urls.clear();
  player.inflight.clear();
  state.id = id;
  const data = await api('/api/docs/' + id);
  state.doc = data.doc;
  state.blocks = data.blocks;
  state.cfg = data.config || state.cfg;
  $('#library').classList.add('hidden');
  $('#reader').classList.remove('hidden');
  renderReader();

  refreshTtsCacheInfo();
  const pending = state.blocks.filter((b) => isTranslatable(b) && b.status !== 'done');
  if (!pending.length) { setStatus('本文已全部翻译完成（命中本地缓存，未消耗 token）', false, 4000); return; }
  if (prefs.scope === 'view') { setupViewObserver(); setStatus('省流量模式：滚动到哪就翻译哪'); }
  else if (prefs.auto) startStream(false);
  else setStatus('存在未翻译段落，点击 ⋯ → 继续翻译');
}

function stopStream() {
  if (state.es) { state.es.close(); state.es = null; }
  if (state.io) { state.io.disconnect(); state.io = null; }
}

function startStream(force) {
  stopStream();
  state.finished = false;
  setStatus(force ? '正在重译全文…' : '正在翻译，段落会逐条出现…');
  const es = new EventSource(`/api/docs/${state.id}/translate${force ? '?force=1' : ''}`);
  state.es = es;

  es.addEventListener('meta', (e) => {
    const d = JSON.parse(e.data);
    if (!d.pending) setStatus('没有待翻译的段落', false, 2500);
  });
  es.addEventListener('block', (e) => applyBlock(JSON.parse(e.data)));
  es.addEventListener('done', (e) => {
    const d = JSON.parse(e.data);
    state.finished = true;
    es.close(); state.es = null;
    if (!d.translated && !d.failed) {
      setStatus('本文已全部翻译完成（命中本地缓存，未消耗 token）', false, 4000);
    } else {
      setStatus(`翻译完成：成功 ${d.translated} 段${d.failed ? `，失败 ${d.failed} 段（可点 ⟳ 重试）` : ''}`, !!d.failed, 6000);
    }
  });
  es.onerror = () => {
    if (state.finished) return;
    es.close(); state.es = null;
    setStatus('翻译连接中断，可点击 ⋯ → 继续翻译 恢复', true);
  };
}

function applyBlock(d) {
  const b = state.blocks.find((x) => x.idx === d.idx);
  const node = state.nodes.get(d.idx);
  if (b) { b.zh = d.zh; b.status = d.status; }
  if (node) paint(node, b || d);
  if (b && b.kind === 'refhead' && node && d.zh) {
    node.el.querySelector('summary').textContent = `${b.text} · ${d.zh}`;
  }
  updateProgress();
}

/* ------------------------------------------------------ 省流量：按可见翻译 */
function setupViewObserver() {
  stopStream();
  state.io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      const idx = Number(en.target.dataset.idx);
      const b = state.blocks.find((x) => x.idx === idx);
      if (!b || !isTranslatable(b) || b.status === 'done' || state.inflight.has(idx)) continue;
      translateOne(idx);
    }
  }, { rootMargin: '300px 0px' });
  $$('#content .blk').forEach((el) => {
    const idx = Number(el.dataset.idx);
    const b = state.blocks.find((x) => x.idx === idx);
    if (b && isTranslatable(b) && b.status !== 'done') state.io.observe(el);
  });
}

async function translateOne(idx) {
  if (state.inflight.has(idx)) return;
  state.inflight.add(idx);
  const b = state.blocks.find((x) => x.idx === idx);
  const node = state.nodes.get(idx);
  if (node && b && b.status !== 'done') { b.status = 'pending'; paint(node, b); }
  try {
    const out = await api(`/api/docs/${state.id}/blocks/${idx}/translate?force=1`, { method: 'POST' });
    applyBlock({ idx, zh: out.zh, status: out.status });
    if (out.doc) { state.doc = out.doc; }
  } catch (err) {
    applyBlock({ idx, zh: null, status: 'error' });
    setStatus('翻译失败：' + err.message, true, 5000);
  } finally {
    state.inflight.delete(idx);
  }
}

/* --------------------------------------------------------- 朗读与语音播放器 */
/* 播放器只有一个 <audio> 实例：换段时改 src，绝不新建第二个，
   从根上杜绝"两路音频叠着放"的重播/回声现象。
   token 是播放代次：每次发起播放 +1，异步合成回来时代次变了就丢弃，
   避免连点、自动续播、预取等路径重复启动同一段。 */
function audioEl() {
  if (!player.audio) {
    const el = document.createElement('audio');
    el.preload = 'auto';
    document.body.appendChild(el);
    player.audio = el;
    el.onplay = updatePlayerUI;
    el.onpause = updatePlayerUI;
    el.ontimeupdate = updatePlayerUI;
    el.onloadedmetadata = updatePlayerUI;
    el.ondurationchange = updatePlayerUI;
  }
  return player.audio;
}

function speakableIdx(from = 0) {
  return state.blocks
    .filter((b) => isTranslatable(b) && b.status === 'done' && b.zh && b.idx >= from)
    .map((b) => b.idx);
}

async function speechUrl(idx) {
  const key = `${state.id}:${idx}:${prefs.voice}`;
  if (player.urls.has(key)) return player.urls.get(key);
  if (player.inflight.has(key)) return player.inflight.get(key);
  const task = (async () => {
    const res = await fetch(`/api/docs/${state.id}/blocks/${idx}/speech?voice=${encodeURIComponent(prefs.voice)}`);
    if (!res.ok) {
      let msg = `合成失败 (${res.status})`;
      try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    const url = URL.createObjectURL(await res.blob());
    player.urls.set(key, url);
    return url;
  })();
  player.inflight.set(key, task);
  try { return await task; } finally { player.inflight.delete(key); }
}

let loadingTicker = null;
function startLoadingTicker() {
  if (loadingTicker) return;
  loadingTicker = setInterval(() => { if (player.loading) updatePlayerUI(); else stopLoadingTicker(); }, 500);
}
function stopLoadingTicker() {
  if (loadingTicker) { clearInterval(loadingTicker); loadingTicker = null; }
}

function fmtTime(t) {
  if (!isFinite(t) || t < 0) t = 0;
  return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
}

function markPlaying(idx) {
  $$('.blk.playing').forEach((el) => el.classList.remove('playing'));
  if (idx == null) return;
  const el = $('#b' + idx);
  if (!el) return;
  el.classList.add('playing');
  const r = el.getBoundingClientRect();
  const barH = $('#player').classList.contains('hidden') ? 0 : ($('#player').offsetHeight || 60);
  const fullyVisible = r.top >= 64 && r.bottom <= window.innerHeight - barH;
  if (!fullyVisible) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function showPlayer() {
  $('#player').classList.remove('hidden');
  document.body.classList.add('has-player');
  $('#plRate').value = String(prefs.rate);
}
function hidePlayer() {
  $('#player').classList.add('hidden');
  document.body.classList.remove('has-player');
}

function updatePlayerUI() {
  const a = player.audio;
  const dur = a && isFinite(a.duration) ? a.duration : 0;
  const cur = a && a.src ? a.currentTime : 0;
  $('#plBar').style.width = dur ? `${(cur / dur) * 100}%` : '0%';
  $('#plTime').textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
  const segBase = player.queue.length
    ? `第 ${player.pos + 1}/${player.queue.length} 段（全文第 ${player.queue[player.pos] + 1} 块）`
    : '—';
  const warming = state.blocks.length && player.queue.length
    ? player.queue.slice(player.pos + 1).filter((i) => hasAudio(i)).length : 0;
  const waiting = player.loading && player.loadingSince
    ? ` · 正在合成 ${((Date.now() - player.loadingSince) / 1000).toFixed(0)}s…`
    : (warming ? ` · 已备好后续 ${warming} 段` : '');
  $('#plSeg').textContent = segBase + waiting;
  $('#plToggle').textContent = player.loading ? '⋯' : (a && a.src && !a.paused && !a.ended ? '❚❚' : '▶');
  $('#plPrev').disabled = player.pos <= 0;
  $('#plNext').disabled = player.pos >= player.queue.length - 1;
}

function hardStopAudio() {
  const a = player.audio;
  if (!a) return;
  a.onended = null;
  a.onerror = null;
  a.pause();
  a.removeAttribute('src');       // 断开旧音源，防止残留的后台播放
  try { a.load(); } catch (_) { /* ignore */ }
}

function skip(sec) {
  const a = player.audio;
  if (!a || !a.src) return;
  if (!isFinite(a.duration)) { setStatus('音频还在加载，稍后再试', true, 1500); return; }
  if (a.ended && sec > 0) { gotoParagraph(1); return; }
  a.currentTime = Math.min(Math.max(0, a.currentTime + sec), Math.max(0, a.duration - 0.05));
  updatePlayerUI();
  setStatus(sec > 0 ? '快进 3 秒' : '后退 3 秒', false, 900);
}

function togglePlay() {
  if (player.loading) {                    // 合成途中点按 = 取消
    player.playing = false;
    player.token += 1;
    player.loading = false;
    updatePlayerUI();
    setStatus('已取消', false, 1200);
    return;
  }
  const a = player.audio;
  if (!a || !a.src || a.ended) {
    if (player.queue.length) { player.playing = true; playCurrent(); return; }
    speakFrom(0);
    return;
  }
  if (a.paused) { a.play().then(() => pumpSynth()).catch(() => setStatus('恢复播放失败', true, 2000)); }
  else a.pause();
  updatePlayerUI();
}

function gotoParagraph(delta) {
  const np = player.pos + delta;
  if (np < 0 || np >= player.queue.length) return;
  player.pos = np;
  player.playing = true;
  hardStopAudio();                          // 用户明确要换段：立刻闭嘴，别继续念旧的
  playCurrent();
}

function stopSpeaking(silent = false) {
  player.playing = false;
  player.loading = false;
  player.token += 1;                        // 让所有在飞的合成结果作废
  player.session += 1;                      // 终止后台预合成
  hardStopAudio();
  player.queue = [];
  player.pos = 0;
  markPlaying(null);
  hidePlayer();
  updatePlayerUI();
  if (!silent) setStatus('');
}

function pausedStop() {
  player.playing = false;
  const a = player.audio;
  if (a) a.pause();
  updatePlayerUI();
}

/* 语音调度器：3 路并行、最近优先，边听边把后面几段备好。
   实测接口无并发限制（6 路并发同样 6.7~8.8s 全部返回），串行预合成才是
   "上段读完等下段"的根因：章节标题只有 1.4s 音频，而后一段要 6~20s 才合成完。 */
const synth = { workers: 0, max: 3 };

const audioKey = (idx) => `${state.id}:${idx}:${prefs.voice}`;
const hasAudio = (idx) => player.urls.has(audioKey(idx));
const synthPending = (idx) => player.inflight.has(audioKey(idx));

function nextNeedingAudio() {
  for (let i = player.pos + 1; i < player.queue.length; i += 1) {
    const idx = player.queue[i];
    if (!hasAudio(idx) && !synthPending(idx)) return idx;
  }
  return undefined;
}

function pumpSynth() {
  if (!player.playing) return;            // 暂停期间不排新任务；恢复播放时会自动续上
  while (synth.workers < synth.max) {
    const idx = nextNeedingAudio();
    if (idx === undefined) return;
    synth.workers += 1;
    speechUrl(idx).catch(() => {}).finally(() => { synth.workers -= 1; pumpSynth(); });
  }
}

function speakOne(idx) {
  hardStopAudio();
  player.session += 1;
  player.warm = 0;
  player.queue = [idx];
  player.pos = 0;
  player.playing = true;
  showPlayer();
  pumpSynth();
  playCurrent();
}

function speakFrom(startIdx = 0) {
  const list = speakableIdx(startIdx);
  if (!list.length) { setStatus('还没有已翻译的段落可朗读', true, 3000); return; }
  hardStopAudio();
  player.session += 1;
  player.warm = 0;
  player.queue = list;
  player.pos = 0;
  player.playing = true;
  showPlayer();
  pumpSynth();             // 立即并行备好后几段
  playCurrent();
}

async function playCurrent() {
  if (!player.playing) return;
  if (player.pos >= player.queue.length) {
    // 队列放完了，但可能期间又译好了新段落 → 自动续上，不要莫名停下
    const lastIdx = player.queue.length ? player.queue[player.queue.length - 1] : -1;
    const more = speakableIdx(lastIdx + 1);
    if (more.length) {
      player.queue.push(...more);
      pumpSynth();
    } else {
      stopSpeaking();
      setStatus(`朗读结束（共 ${player.queue.length} 段）`, false, 3000);
      return;
    }
  }
  const token = ++player.token;             // 本次播放的代次
  const idx = player.queue[player.pos];
  markPlaying(idx);
  updatePlayerUI();

  const node = state.nodes.get(idx);
  let url;
  if (!hasAudio(idx)) {
    player.loading = true;
    player.loadingSince = Date.now();
    startLoadingTicker();
    updatePlayerUI();
    if (node && node.zh) node.zh.classList.add('synthesizing');
    setStatus(`正在合成第 ${player.pos + 1}/${player.queue.length} 段…`);
  }
  try {
    url = await speechUrl(idx);
  } catch (err) {
    if (token !== player.token) return;
    player.loading = false;
    player.loadingSince = 0;
    stopLoadingTicker();
    if (node && node.zh) node.zh.classList.remove('synthesizing');
    toast(`第 ${idx + 1} 段合成失败，已跳过`, `${err.message}（可点该段 🔈 单独重试）`, '⚠️', 6000);
    player.pos += 1;
    playCurrent();
    return;
  } finally {
    if (node && node.zh) node.zh.classList.remove('synthesizing');
  }
  if (token !== player.token || !player.playing) return;   // 已被更新的播放请求取代
  player.loading = false;
  player.loadingSince = 0;
  stopLoadingTicker();

  const a = audioEl();
  a.pause();                                // 同一实例：先停掉当前音源再换，永不并发播放
  a.src = url;
  a.playbackRate = prefs.rate;
  a.onended = () => {
    if (token !== player.token) return;     // 过期的回调不许推进进度
    player.pos += 1;
    playCurrent();
  };
  a.onerror = () => {
    if (token !== player.token) return;
    setStatus(`第 ${idx + 1} 段播放失败`, true, 3000);
    player.pos += 1;
    playCurrent();
  };
  try {
    await a.play();
  } catch (_) {
    setStatus('浏览器阻止了自动播放，请点一下播放按钮', true, 4000);
    pausedStop();
    return;
  }
  setStatus('');
  updatePlayerUI();

  pumpSynth();                                 // 播放中持续把后面几段并行备好
}

function seekTo(ratio) {
  const a = player.audio;
  if (!a || !a.src || !isFinite(a.duration)) return;
  a.currentTime = Math.min(Math.max(0, ratio), 1) * a.duration;
  updatePlayerUI();
}

/* ------------------------------------------------------------ 浮层通知 */
let toastTimer = null;
function toast(title, message, icon = '🧹', ms = 6000) {
  const el = $('#toast');
  if (!el) return;
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  $('#toastIcon').textContent = icon;
  $('#toastTitle').textContent = title;
  $('#toastMsg').textContent = message || '';
  el.classList.remove('hidden', 'hide');
  if (ms > 0) {
    toastTimer = setTimeout(() => {
      el.classList.add('hide');
      setTimeout(() => el.classList.add('hidden'), 220);
    }, ms);
  }
}
function hideToast() {
  const el = $('#toast');
  if (!el) return;
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  el.classList.add('hide');
  setTimeout(() => el.classList.add('hidden'), 220);
}

/* ------------------------------------------------------------ 语音缓存管理 */
function fmtSize(bytes) {
  if (!bytes) return '0 MB';
  const mb = bytes / 1048576;
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

async function refreshTtsCacheInfo() {
  const el = $('#ttsSize');
  if (!el) return;
  if (!state.id || !state.tts.ready) { el.textContent = ''; return; }
  try {
    const d = await api(`/api/tts/cache?doc_id=${state.id}`);
    const cap = d.cap_mb ? ` / 上限 ${d.cap_mb} MB` : '';
    el.textContent = `（本篇 ${fmtSize(d.bytes)} / 全部 ${fmtSize(d.total_mb * 1048576)}${cap}）`;
  } catch (_) { el.textContent = ''; }
}

async function purgeTtsCache(scope = 'doc') {
  try {
    const q = scope === 'doc' && state.id ? `?doc_id=${state.id}` : '';
    const d = await api('/api/tts/cache' + q, { method: 'DELETE' });
    if (d.freed_mb > 0) {
      toast(scope === 'doc' ? '已清理本篇语音缓存' : '已清空全部语音缓存',
            `释放 ${d.freed_mb} MB，剩余 ${d.remaining_mb} MB`, '🧹', 6000);
    } else {
      toast('没有需要清理的语音缓存', '当前没有已合成的音频文件', '✅', 3500);
    }
    refreshTtsCacheInfo();
  } catch (err) {
    toast('清理失败', err.message, '⚠️', 5000);
  }
}

/* ------------------------------------------------------------------ 弹层 */
function openModal(title, html) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
const closeModal = () => $('#modal').classList.add('hidden');

async function showGlossary() {
  const { terms } = await api('/api/glossary');
  const rows = terms.map((t) => `
    <div class="g-row" data-en="${esc(t.en)}">
      <input class="g-en" value="${esc(t.en)}" ${t.builtin ? 'readonly' : ''}>
      <input class="g-zh" value="${esc(t.zh)}">
      <span class="builtin">${t.builtin ? '内置' : ''}</span>
      <button class="icon" data-act="save" title="保存">✓</button>
      <button class="icon" data-act="del" title="删除">✕</button>
    </div>`).join('');
  openModal('术语表（命中即强制统一译法）', `
    <div class="g-row">
      <input class="g-en" id="newEn" placeholder="英文术语，如 Latent Space">
      <input class="g-zh" id="newZh" placeholder="中文译法，如 潜在空间">
      <button class="btn primary" id="addTerm">添加</button>
    </div>
    <p class="sub" style="margin:10px 2px">修改术语后，新翻译会自动采用；已译段落可用“重译全文（忽略缓存）”刷新。</p>
    ${rows}`);
  const body = $('#modalBody');
  body.onclick = async (e) => {
    const btn = e.target.closest('[data-act]');
    const row = e.target.closest('.g-row');
    if (btn && row && btn.dataset.act === 'save') {
      await api('/api/glossary', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ en: $('.g-en', row).value, zh: $('.g-zh', row).value }),
      });
      setStatus('术语已保存', false, 1500);
    }
    if (btn && row && btn.dataset.act === 'del') {
      await api('/api/glossary/' + encodeURIComponent(row.dataset.en), { method: 'DELETE' });
      showGlossary();
    }
  };
  $('#addTerm').onclick = async () => {
    const en = $('#newEn').value.trim(); const zh = $('#newZh').value.trim();
    if (!en || !zh) return;
    await api('/api/glossary', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ en, zh }),
    });
    showGlossary();
  };
}

async function showStats() {
  const s = await api('/api/stats');
  openModal('用量与状态', `
    <div class="stats-grid">
      <div><b>${s.docs}</b><span>文档</span></div>
      <div><b>${s.translated}/${s.blocks}</b><span>已译段落 / 总段落</span></div>
      <div><b>${s.cache_entries}</b><span>缓存条目</span></div>
      <div><b>${s.llm.requests}</b><span>本次进程请求数</span></div>
      <div><b>${s.llm.prompt_tokens}</b><span>输入 token</span></div>
      <div><b>${s.llm.completion_tokens}</b><span>输出 token</span></div>
      <div><b>${s.llm.errors}</b><span>失败次数</span></div>
      <div><b>${esc(s.config.model)}</b><span>当前模型</span></div>
    </div>
    <p class="sub" style="margin-top:14px">token 统计仅针对当前进程；缓存命中不会重复计费。</p>`);
}

/* ------------------------------------------------------------------ 路由 */
async function route() {
  const m = location.hash.match(/#\/doc\/([0-9a-zA-Z]+)/);
  closeModal();
  if (m && m[1]) {
    try { await openDoc(m[1]); } catch (err) { setStatus('打开失败：' + err.message, true); location.hash = ''; }
    return;
  }
  stopStream();
  stopSpeaking(true);
  player.urls.clear();
  player.inflight.clear();
  if (state.id && prefs.autoCleanTts && state.tts.ready) {
    // 返回书库即清理本篇语音缓存：不等响应，避免拖慢返回动作；清理完弹出提示
    const leaving = state.id;
    const leavingTitle = (state.doc && state.doc.title) || '';
    api(`/api/tts/cache?doc_id=${leaving}`, { method: 'DELETE' })
      .then((d) => {
        if (d.freed_mb > 0) {
          toast('已清理本篇语音缓存',
                `释放 ${d.freed_mb} MB 磁盘空间（剩余 ${d.remaining_mb} MB）`
                + `${leavingTitle ? `\n《${leavingTitle.slice(0, 22)}${leavingTitle.length > 22 ? '…' : ''}》` : ''}`
                + '下次朗读会重新合成，可在 ⋯ 菜单关闭此行为',
                '🧹', 7000);
        }
      })
      .catch(() => {});
  }
  state.id = null; state.doc = null; state.blocks = []; state.nodes.clear();
  $('#reader').classList.add('hidden');
  $('#library').classList.remove('hidden');
  loadLibrary();
}

/* ------------------------------------------------------------------ 事件 */
function bind() {
  const drop = $('#drop');
  $('#fileInput').onchange = (e) => upload(e.target.files[0]);
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', (e) => upload(e.dataTransfer.files[0]));

  $('#glossaryBtn').onclick = showGlossary;
  $('#statsBtn').onclick = showStats;
  $('#modalClose').onclick = closeModal;
  $('#modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(); };

  $('#backBtn').onclick = () => { location.hash = ''; };
  $('#modeSeg').onclick = (e) => { const b = e.target.closest('button'); if (b) setMode(b.dataset.mode); };
  $('#fontPlus').onclick = () => { prefs.fs = Math.min(24, prefs.fs + 1); savePrefs(); applyPrefs(); };
  $('#fontMinus').onclick = () => { prefs.fs = Math.max(13, prefs.fs - 1); savePrefs(); applyPrefs(); };
  $('#themeBtn').onclick = () => {
    prefs.theme = prefs.theme === 'dark' ? 'light' : 'dark'; savePrefs(); applyPrefs();
  };
  $('#pdfBtn').onclick = () => { if (state.id) window.open(`/api/docs/${state.id}/file`, '_blank'); };
  $('#exportBtn').onclick = () => { if (state.id) location.href = `/api/docs/${state.id}/export.md`; };
  $('#resumeBtn').onclick = () => { $('#moreMenu').classList.add('hidden'); startStream(false); };
  $('#retransAllBtn').onclick = () => {
    $('#moreMenu').classList.add('hidden');
    if (confirm('忽略缓存，重新翻译全部段落？这会重新消耗 token。')) startStream(true);
  };
  $('#moreBtn').onclick = (e) => {
    e.stopPropagation();
    const m = $('#moreMenu');
    m.classList.toggle('hidden');
    if (!m.classList.contains('hidden')) refreshTtsCacheInfo();
  };
  $('#toastClose').onclick = hideToast;
  $('#ttsCleanBtn').onclick = () => { $('#moreMenu').classList.add('hidden'); purgeTtsCache('doc'); };
  $('#ttsCleanAllBtn').onclick = () => {
    $('#moreMenu').classList.add('hidden');
    purgeTtsCache('all');
  };
  $('#ttsAutoChk').checked = !!prefs.autoCleanTts;
  $('#ttsAutoChk').onchange = (e) => {
    prefs.autoCleanTts = e.target.checked; savePrefs();
    if (prefs.autoCleanTts) {
      toast('已开启「退出即清理」', '返回书库、关闭页面或服务停止时，自动清掉已合成的语音缓存', '🧹', 4500);
    } else {
      toast('已改为保留语音缓存', '重听、二次打开都不再重新合成（不重复计费），代价是占磁盘', '💾', 4500);
    }
  };
  $('#speakBtn').onclick = () => {
    if (player.playing || (player.audio && !player.audio.paused)) { pausedStop(); return; }
    if (player.queue.length && player.audio) { togglePlay(); return; }
    const first = state.blocks.find((b) => {
      const el = $('#b' + b.idx);
      return el && el.getBoundingClientRect().bottom > 80;
    });
    speakFrom(first ? first.idx : 0);
  };
  $('#plToggle').onclick = togglePlay;
  $('#plFwd').onclick = () => skip(3);
  $('#plBack').onclick = () => skip(-3);
  $('#plPrev').onclick = () => gotoParagraph(-1);
  $('#plNext').onclick = () => gotoParagraph(1);
  $('#plClose').onclick = () => { stopSpeaking(); setStatus('已关闭播放器', false, 1500); };
  $('#plRate').onchange = (e) => {
    prefs.rate = parseFloat(e.target.value) || 1; savePrefs();
    if (player.audio) player.audio.playbackRate = prefs.rate;
    setStatus(`语速 ${prefs.rate}×`, false, 1200);
  };
  const prog = $('#plProgress');
  const posToRatio = (ev) => {
    const r = prog.getBoundingClientRect();
    return (ev.clientX - r.left) / Math.max(r.width, 1);
  };
  prog.addEventListener('pointerdown', (ev) => {
    prog.setPointerCapture(ev.pointerId);
    seekTo(posToRatio(ev));
    const move = (e2) => seekTo(posToRatio(e2));
    const up = () => { prog.removeEventListener('pointermove', move); prog.removeEventListener('pointerup', up); };
    prog.addEventListener('pointermove', move);
    prog.addEventListener('pointerup', up);
  });
  $('#voiceSel').onchange = (e) => {
    prefs.voice = e.target.value; savePrefs();
    player.urls.clear();
    if (player.playing) { stopSpeaking(true); speakFrom(player.queue[player.pos] ?? 0); }
    setStatus('已切换音色：' + e.target.value, false, 2000);
  };
  $('#scopeChk').onchange = (e) => {
    prefs.scope = e.target.checked ? 'view' : 'all';
    savePrefs();
    if (prefs.scope === 'view') {
      stopStream(); setupViewObserver();
      setStatus('省流量模式：滚动到哪就翻译哪');
    } else {
      stopStream(); startStream(false);
    }
  };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#moreMenu') && !e.target.closest('#moreBtn')) $('#moreMenu').classList.add('hidden');
  });

  document.addEventListener('keydown', (e) => {
    const tgt = e.target;
    if (tgt && typeof tgt.matches === 'function' && tgt.matches('input, textarea, select')) return;
    const playerOpen = !$('#player').classList.contains('hidden');
    if (playerOpen && e.code === 'Space') { e.preventDefault(); togglePlay(); return; }
    if (playerOpen && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      e.preventDefault(); skip(e.key === 'ArrowRight' ? 3 : -3); return;
    }
    if (e.key === 'Escape') { if (!$('#modal').classList.contains('hidden')) closeModal(); else if (state.id) location.hash = ''; }
    if (e.key === 'm') setMode(prefs.mode === 'bi' ? 'zh' : prefs.mode === 'zh' ? 'en' : 'bi');
    if (e.key === 't') $('#themeBtn').click();
    if (e.key === 'p') $('#speakBtn').click();
    if (e.key === 'j' || e.key === 'k') {
      window.scrollBy({ top: (e.key === 'j' ? 1 : -1) * window.innerHeight * 0.42, behavior: 'smooth' });
    }
  });
}

/* 退出即清理：关闭标签页 / 离开页面 / 刷新时，用 sendBeacon 通知服务端清掉本篇语音缓存。
   （返回书库那条路径在 route() 里处理；服务停止时后端还会清一次全部。） */
function purgeOnPageExit() {
  if (!state.id || !prefs.autoCleanTts || !state.tts.ready) return;
  try {
    const url = `/api/tts/cache/purge?doc_id=${encodeURIComponent(state.id)}`;
    const ok = navigator.sendBeacon(url, new Blob(['{}'], { type: 'application/json' }));
    if (!ok) fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
  } catch (_) { /* 退出路径失败不打扰用户 */ }
}
window.addEventListener('pagehide', purgeOnPageExit);   // 关闭标签页 / 关闭浏览器 / 离开页面
// 注意：刻意不监听 visibilitychange —— 切到别的标签页不算退出，
// 而且"边听边干别的"是常见用法，那时清理会导致切回来重新合成、白花钱。

(async function main() {
  applyPrefs();
  bind();
  await loadConfig();
  await route();
  window.addEventListener('hashchange', route);
})();

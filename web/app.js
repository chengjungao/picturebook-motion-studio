/* 让绘本动起来 —— 界面逻辑。原生 JS，没有构建步骤。 */
'use strict';

let ST = null;          // 最近一次 /api/state
let SEQ = 0;            // 日志游标
let POLL = null;        // 轮询定时器

const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body || {})
  });
  try { return await r.json(); }
  catch (e) { return { ok: false, error: '服务端返回了看不懂的东西（HTTP ' + r.status + '）' }; }
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ---------------- 渲染 ---------------- */

async function load(keepScroll) {
  const y = window.scrollY;
  ST = await api('/api/state');
  if (!ST || !ST.ok) { return; }
  $('folder').textContent = ST.folder;
  $('head-sub').textContent = ST.title
    ? ST.title + (ST.subtitle ? '　·　' + ST.subtitle : '')
    : '把一本做好的绘本，变成一集能看的动画片';
  $('f-title').value = ST.title || '';
  $('f-subtitle').value = ST.subtitle || '';
  renderPages();
  renderTimeline();
  renderFilm();
  if (ST.kokoro) $('btn-kokoro').hidden = false;
  $('voice-hint').textContent = ST.voice_hint || '';
  const nv = ST.pages.filter(p => p.has_voice).length;
  $('voice-stat').textContent = nv + '/' + ST.pages.length + ' 页有旁白';
  if (keepScroll) window.scrollTo(0, y);
}

function renderPages() {
  const box = $('pages');
  box.innerHTML = ST.pages.map((p, i) => {
    const img = p.has_image
      ? '<img class="thumb" src="/img/' + encodeURIComponent(p.id) + '?t=' + Date.now() + '" alt="">'
      : '<div class="thumb">还没图</div>';
    const clip = p.has_clip
      ? '<span class="badge on">片段 ' + p.clip_kb + ' KB</span>'
      : '<span class="badge off">没片段</span>';
    const voice = p.has_voice
      ? '<span class="badge on">旁白 ' + esc(p.voice_name) + '</span>'
      : '<span class="badge off">没旁白</span>';
    const kind = p.kind === 'cover' ? '<span class="badge">封面</span>'
      : (p.kind === 'back' ? '<span class="badge">封底</span>' : '');
    const audio = p.has_voice
      ? '<audio controls preload="none" src="/voice/' + encodeURIComponent(p.id) + '"></audio>'
      : '';
    return '<div class="pagerow" data-i="' + i + '" data-id="' + esc(p.id) + '">'
      + '<div class="row-head">' + img
      + '<div class="meta"><div class="pid">' + esc(p.id) + '</div>'
      + '<div class="badges">' + kind + clip + voice + '</div></div></div>'
      + '<label><span>正文（会当字幕）</span>'
      + '<textarea rows="2" data-f="text">' + esc(p.text) + '</textarea></label>'
      + '<label><span>怎么动' + (p.need_motion ? '　← 还没填，先套兜底描述' : '') + '</span>'
      + '<textarea rows="2" data-f="motion" placeholder="例：一片叶子慢慢落在它鼻尖上，它眨了一下眼睛，尾巴慢慢摆动">'
      + esc(p.motion) + '</textarea></label>'
      + '<div class="voicebar">' + audio
      + '<span class="filebtn"><button class="ghost small">选个音频当旁白</button>'
      + '<input type="file" accept="audio/*" data-act="voice"></span>'
      + (p.has_voice ? '<button class="ghost small" data-act="clr">清掉旁白</button>' : '')
      + '</div></div>';
  }).join('');
}

function renderTimeline() {
  const rows = ST.timeline || [];
  $('tl-total').textContent = ST.total.toFixed(2) + ' 秒（'
    + (ST.total / 60).toFixed(2) + ' 分钟）· ' + rows.length + ' 个镜头';
  $('tl').innerHTML = '<tr><th>镜头</th><th class="num">起点</th><th class="num">时长</th>'
    + '<th class="num">帧数</th><th class="num">旁白</th><th>片段</th></tr>'
    + rows.map(r => '<tr><td>' + esc(r.id) + '</td>'
      + '<td class="num">' + r.start.toFixed(2) + '</td>'
      + '<td class="num">' + r.shot.toFixed(2) + '</td>'
      + '<td class="num">' + r.frames + '</td>'
      + '<td class="num">' + (r.voiced ? r.nar_dur.toFixed(2) + '@' + r.nar_start.toFixed(2) : '—') + '</td>'
      + '<td>' + esc(r.fit) + '</td></tr>').join('');
}

function renderFilm() {
  const has = ST.films && ST.films.length;
  $('panel-film').hidden = !has;
  if (!has) return;
  const f = ST.films[0];
  $('dl').href = '/film';
  $('dl').setAttribute('download', f.name);
  const v = $('player');
  if (v.getAttribute('data-src') !== f.name) {
    v.setAttribute('data-src', f.name);
    v.src = '/film?t=' + Date.now();
  }
  const fr = (ST.verify && ST.verify.frames) || [];
  $('frames').innerHTML = fr.map(n =>
    '<a href="/镜头校验/' + encodeURIComponent(n) + '" target="_blank">'
    + '<img src="/镜头校验/' + encodeURIComponent(n) + '" alt="' + esc(n) + '"></a>').join('');
}

/* ---------------- 日志 ---------------- */

function pushLog(lines) {
  const box = $('log');
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const l of lines) {
    const div = document.createElement('div');
    div.className = l.level || 'info';
    div.textContent = '[' + l.t + '] ' + l.text;
    box.appendChild(div);
  }
  while (box.childElementCount > 600) box.removeChild(box.firstChild);
  if (near) box.scrollTop = box.scrollHeight;
}

function startPoll() {
  if (POLL) return;
  POLL = setInterval(async () => {
    const r = await api('/api/log?since=' + SEQ);
    if (!r || !r.ok) return;
    SEQ = r.seq;
    pushLog(r.lines);
    const busy = r.busy;
    $('busy').textContent = busy ? '正在跑：' + r.job : '';
    $('btn-cancel').hidden = !busy;
    document.querySelectorAll('button').forEach(b => {
      if (b.id !== 'btn-cancel') b.disabled = busy;
    });
    if (!busy && wasBusy) { wasBusy = false; await load(true); }
    wasBusy = busy;
  }, 600);
}
let wasBusy = false;

/* ---------------- 动作 ---------------- */

function collectPages() {
  return Array.from(document.querySelectorAll('.pagerow')).map(row => ({
    id: row.getAttribute('data-id'),
    text: row.querySelector('[data-f=text]').value,
    motion: row.querySelector('[data-f=motion]').value
  }));
}

async function save() {
  const r = await api('/api/save', {
    title: $('f-title').value, subtitle: $('f-subtitle').value, pages: collectPages()
  });
  log1(r, '已保存');
  await load(true);
}

function log1(r, okMsg) {
  const box = $('log');
  const d = document.createElement('div');
  const ok = r && r.ok;
  d.className = ok ? 'good' : 'bad';
  d.textContent = '[' + new Date().toTimeString().slice(0, 8) + '] '
    + (ok ? (okMsg || '完成') : ('✗ ' + ((r && r.error) || '失败')));
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function bind() {
  $('btn-save').onclick = save;
  $('btn-check').onclick = async () => { SEQ = 0; $('log').innerHTML = ''; await api('/api/check', {}); };
  $('btn-plan').onclick = async () => { SEQ = 0; $('log').innerHTML = ''; await api('/api/plan', {}); $('panel-tl').hidden = false; await load(true); };
  $('btn-reveal').onclick = () => api('/api/reveal', {});
  $('btn-fill').onclick = async () => { log1(await api('/api/fill_motion', {}), '兜底描述已填上'); await load(true); };
  $('btn-cancel').onclick = () => api('/api/cancel', {});

  $('btn-clips').onclick = async () => {
    await save();
    SEQ = 0; $('log').innerHTML = '';
    const limit = parseInt($('c-limit').value || '0', 10) || 0;
    log1(await api('/api/clips', { resume: $('c-resume').checked, limit: limit }),
      '已开跑，日志在下面');
  };
  $('btn-kokoro').onclick = async () => {
    SEQ = 0; $('log').innerHTML = '';
    log1(await api('/api/voice/kokoro', {}), '开始生成旁白');
  };
  $('btn-assemble').onclick = async () => {
    await save();
    SEQ = 0; $('log').innerHTML = '';
    log1(await api('/api/assemble', {
      music: $('c-music').value.trim(), subtitles: $('c-subs').checked
    }), '开始合成');
  };
  $('btn-verify').onclick = async () => {
    SEQ = 0; $('log').innerHTML = '';
    const r = await api('/api/verify', {});
    $('panel-film').hidden = false;
    await load(true);
    log1(r, r.ok ? '校验通过' : '校验有发现，看上面的逐条说明');
  };

  // 分镜列表里的交互用事件委托，重渲染后不用重新绑
  $('pages').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act=clr]');
    if (!btn) return;
    const row = btn.closest('.pagerow');
    const r = await api('/api/voice/clear', { id: row.getAttribute('data-id') });
    log1(r, '已清掉旁白');
    await load(true);
  });
  $('pages').addEventListener('change', async (e) => {
    const inp = e.target.closest('input[type=file][data-act=voice]');
    if (!inp || !inp.files || !inp.files[0]) return;
    const row = inp.closest('.pagerow');
    const f = inp.files[0];
    if (f.size > 40 * 1024 * 1024) { alert('这个文件太大了（超过 40MB），旁白一般是几百 KB。'); return; }
    const b64 = await new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = rej;
      fr.readAsDataURL(f);
    });
    SEQ = 0; $('log').innerHTML = '';
    log1(await api('/api/voice/upload', { id: row.getAttribute('data-id'), name: f.name, data: b64 }),
      '旁白已收下');
    await load(true);
  });
}

/* ---------------- 启动 ---------------- */

(async function main() {
  bind();
  await load();
  startPoll();
})();

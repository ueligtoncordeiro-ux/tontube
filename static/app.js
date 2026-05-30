// ── STATE ──
const state = {
  url: '',
  videoInfo: null,
  selectedFormat: null,
  fileId: null,
};

// ── DOM ──
const $ = id => document.getElementById(id);

const sections = {
  hero:     $('hero-section'),
  loading:  $('loading-section'),
  result:   $('result-section'),
  progress: $('progress-section'),
  done:     $('done-section'),
};

function show(name) {
  Object.entries(sections).forEach(([k, el]) => {
    if (!el) return;
    if (k === name) {
      el.classList.remove('hidden');
      el.classList.add('fade-in');
    } else {
      el.classList.add('hidden');
      el.classList.remove('fade-in');
    }
  });
}

function showError(msg) {
  $('error-message').textContent = msg;
  $('error-card').classList.remove('hidden');
}

function hideError() {
  $('error-card').classList.add('hidden');
}

// ── ANALYZE PROGRESS (fake animation) ──
const ANALYZE_MESSAGES = [
  'Conectando ao YouTube...',
  'Buscando formatos disponíveis...',
  'Verificando qualidade do vídeo...',
  'Obtendo informações do canal...',
  'Quase pronto...',
];

let _analyzeTimer   = null;
let _analyzeMsgTimer = null;
let _analyzeTarget  = 0;
let _analyzeCurrent = 0;

function startAnalyzeProgress() {
  _analyzeCurrent = 0;
  _analyzeTarget  = 0;
  setAnalyzeBar(0);
  setAnalyzeMsg(0);

  // slowly advance the bar to 88% over ~10 s
  const steps = [
    { target: 18, delay: 400 },
    { target: 35, delay: 1200 },
    { target: 52, delay: 2200 },
    { target: 68, delay: 3800 },
    { target: 80, delay: 5800 },
    { target: 88, delay: 8500 },
  ];

  steps.forEach(({ target, delay }) => {
    setTimeout(() => {
      if (_analyzeTimer !== null) setAnalyzeBar(target);
    }, delay);
  });

  // cycle status messages
  let msgIdx = 0;
  _analyzeMsgTimer = setInterval(() => {
    msgIdx = Math.min(msgIdx + 1, ANALYZE_MESSAGES.length - 1);
    setAnalyzeMsg(msgIdx);
  }, 2200);

  _analyzeTimer = true; // flag "running"
}

function setAnalyzeBar(pct) {
  const fill = $('analyze-bar-fill');
  const pctEl = $('analyze-pct');
  if (fill) fill.style.width = pct + '%';
  if (pctEl) pctEl.textContent = pct + '%';
}

function setAnalyzeMsg(idx) {
  const el = $('loading-status');
  if (!el) return;
  el.style.opacity = '0';
  setTimeout(() => {
    el.textContent = ANALYZE_MESSAGES[idx];
    el.style.opacity = '1';
  }, 150);
}

function stopAnalyzeProgress(success) {
  _analyzeTimer = null;
  clearInterval(_analyzeMsgTimer);
  _analyzeMsgTimer = null;
  if (success) {
    setAnalyzeBar(100);
    const el = $('analyze-pct');
    if (el) el.textContent = '100%';
  }
}

// ── ANALYZE ──
async function analyze() {
  const url = $('url-input').value.trim();
  if (!url) { showError('Cole um link do YouTube válido.'); return; }

  state.url = url;
  hideError();
  show('loading');
  startAnalyzeProgress();

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Não foi possível analisar este vídeo.');
    }

    const data = await res.json();
    stopAnalyzeProgress(true);
    state.videoInfo = data;
    renderResult(data);
    show('result');
  } catch (e) {
    stopAnalyzeProgress(false);
    show('hero');
    showError(e.message);
  }
}

// ── RENDER RESULT ──
function fmtViews(n) {
  if (!n) return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M views`;
  if (n >= 1_000)     return `${Math.round(n / 1_000)}K views`;
  return `${n} views`;
}

function renderResult(data) {
  const thumb = $('video-thumb');
  if (data.thumbnail) { thumb.src = data.thumbnail; thumb.style.display = 'block'; }

  $('video-duration').textContent = data.duration || '';
  $('video-channel').textContent  = data.channel  || '';
  $('video-title').textContent    = data.title    || '';

  const stats = $('video-stats');
  stats.innerHTML = '';
  if (data.duration)   stats.innerHTML += `<span class="stat-chip">⏱ ${data.duration}</span>`;
  if (data.view_count) stats.innerHTML += `<span class="stat-chip">👁 ${fmtViews(data.view_count)}</span>`;

  // build format cards
  const grid = $('formats-grid');
  grid.innerHTML = '';
  state.selectedFormat = null;
  $('download-btn').disabled = true;

  let autoSelect = null;

  data.formats.forEach(fmt => {
    const card = document.createElement('div');
    card.className = 'fmt-card';

    const isAudio = fmt.type === 'audio';
    const icon    = isAudio ? '🎵' : '🎬';

    let badgeHtml = '';
    if (fmt.badge === 'HD')  badgeHtml = '<span class="fmt-badge hd">HD</span>';
    if (fmt.badge === 'Pop') badgeHtml = '<span class="fmt-badge pop">MP3</span>';

    card.innerHTML = `
      ${badgeHtml}
      <span class="fmt-check">✓</span>
      <span class="fmt-icon">${icon}</span>
      <span class="fmt-label">${fmt.label}</span>
      ${fmt.sublabel ? `<span class="fmt-sub">${fmt.sublabel}</span>` : ''}
    `;

    card.addEventListener('click', () => selectFormat(fmt, card));
    grid.appendChild(card);

    if (fmt.popular && !autoSelect) autoSelect = { fmt, card };
    if (!autoSelect && !state.selectedFormat) autoSelect = { fmt, card };
  });

  if (autoSelect) setTimeout(() => selectFormat(autoSelect.fmt, autoSelect.card), 80);
}

function selectFormat(fmt, card) {
  document.querySelectorAll('.fmt-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  state.selectedFormat = fmt;
  $('download-btn').disabled = false;
}

// ── DOWNLOAD ──
function startDownload() {
  const fmt = state.selectedFormat;
  if (!fmt) return;

  show('progress');
  setStep('dl');
  updateProgress(0, 0);

  const params = new URLSearchParams({
    url:           state.url,
    format_string: fmt.format_string,
    ext:           fmt.ext,
    media_type:    fmt.type,
  });

  const es = new EventSource(`/api/download?${params}`);

  es.onmessage = e => {
    const d = JSON.parse(e.data);

    if (d.error) {
      es.close();
      show('result');
      showError(d.error);
      return;
    }

    if (typeof d.progress === 'number') {
      updateProgress(d.progress, d.speed || 0, d.phase || '');
      if (d.phase === 'convertendo' || d.progress >= 90) setStep('proc');
    }

    if (d.done) {
      es.close();
      state.fileId = d.file_id;
      updateProgress(100, 0);
      setStep('done-step');
      setTimeout(() => show('done'), 700);
    }
  };

  es.onerror = () => {
    es.close();
    show('result');
    showError('Erro de conexão. Tente novamente.');
  };
}

function updateProgress(pct, speed, phase) {
  $('progress-fill').style.width = `${pct}%`;
  $('progress-pct-badge').textContent = `${pct}%`;

  if (phase === 'convertendo') {
    $('progress-speed').textContent = 'Convertendo e mesclando faixas...';
  } else if (speed > 0) {
    const mb = (speed / 1024 / 1024).toFixed(1);
    $('progress-speed').textContent = `Velocidade: ${mb} MB/s`;
  } else if (pct >= 90) {
    $('progress-speed').textContent = 'Finalizando processamento...';
  }
}

function setStep(step) {
  $('step-dl').className   = 'pstep';
  $('step-proc').className = 'pstep';
  $('step-done').className = 'pstep';

  if (step === 'dl') {
    $('step-dl').classList.add('active');
  } else if (step === 'proc') {
    $('step-dl').classList.add('done');
    $('step-proc').classList.add('active');
  } else if (step === 'done-step') {
    $('step-dl').classList.add('done');
    $('step-proc').classList.add('done');
    $('step-done').classList.add('active');
  }
}

// ── SAVE FILE ──
function saveFile() {
  if (state.fileId) window.location.href = `/api/file/${state.fileId}`;
}

// ── RESET ──
function reset() {
  state.url = '';
  state.videoInfo = null;
  state.selectedFormat = null;
  state.fileId = null;
  $('url-input').value = '';
  hideError();
  show('hero');
}

// ── PASTE FROM CLIPBOARD ──
async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    $('url-input').value = text;
    if (isYouTubeUrl(text)) analyze();
  } catch {
    $('url-input').focus();
  }
}

function isYouTubeUrl(url) {
  return /youtube\.com|youtu\.be/i.test(url);
}

// ── EVENTS ──
document.addEventListener('DOMContentLoaded', () => {
  $('analyze-btn').addEventListener('click', analyze);

  $('url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') analyze();
  });

  $('url-input').addEventListener('paste', () => {
    setTimeout(() => {
      if (isYouTubeUrl($('url-input').value)) analyze();
    }, 120);
  });

  $('paste-btn').addEventListener('click', pasteFromClipboard);

  $('back-btn').addEventListener('click', () => { show('hero'); hideError(); });

  $('download-btn').addEventListener('click', startDownload);

  $('save-btn').addEventListener('click', saveFile);

  $('new-btn').addEventListener('click', reset);
});

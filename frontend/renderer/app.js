import { toast, wireTheme, flashDone } from './vendor/fx.js';

const TOKEN = new URLSearchParams(location.search).get('token') || '';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let state = null;
let lastLogSeq = 0;
let logBuffer = [];
let levelFilter = 'ALL';
let fastTimer = null;
let currentView = 'manual';
let formTouched = false;

/* ------------------------------------------------------------------ 网络 */
async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ------------------------------------------------------------------ 工具 */
const KIND_LABEL = { ethernet: '有线', wifi: 'WiFi', hotspot: '手机热点' };

function statusText(status) {
  const s = (status || '').toLowerCase();
  if (s === 'up') return '已连接';
  if (s === 'disabled') return '已禁用';
  if (s === 'disconnected') return '未连接';
  return status || '—';
}

function setText(sel, value) {
  const el = $(sel);
  if (el && el.textContent !== String(value)) el.textContent = String(value);
}

function setValue(sel, value) {
  const el = $(sel);
  if (!el || el === document.activeElement) return;
  if (el.value !== String(value)) el.value = value;
}

/* ------------------------------------------------------------------ 导航 */
function wireNav() {
  $$('.ef-cmdbar nav a').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      switchView(a.dataset.view);
    });
  });
}

function switchView(view) {
  currentView = view;
  $$('.ef-cmdbar nav a').forEach((a) => {
    if (a.dataset.view === view) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $$('.view').forEach((section) => {
    section.hidden = section.id !== `view-${view}`;
  });
  if (view === 'logs') renderLogs();
  if (view === 'library') renderLibrary();
  window.scrollTo({ top: 0 });
}

/* ------------------------------------------------------------------ 渲染：切换页 */
function renderManual(s) {
  const wired = (s.adapters || []).filter((a) => a.kind === 'ethernet');
  const wlan = (s.adapters || []).find((a) => a.kind === 'wifi') || null;
  const egress = s.egress || {};
  const wifi = s.wifi || {};
  const hotspots = (s.profiles || []).filter((p) => p.type === 'hotspot');

  const wiredActive = wired.some((a) => a.up) && egress.kind === 'ethernet';
  // 热点和校园 WiFi 走的是同一块无线网卡，必须靠 SSID 区分，
  // 否则两张卡会同时挂「当前出口」，看起来像 UI 没刷新。
  const hotspotSsids = new Set(hotspots.map((p) => p.ssid));
  const onHotspot = !!(wifi.connected && hotspotSsids.has(wifi.ssid));
  const wifiActive = !!(wlan && wlan.up && egress.kind === 'wifi');
  const campusWifiActive = wifiActive && !onHotspot;

  // 有线
  const primary = wired.find((a) => a.up) || wired[0] || null;
  setText('#status-ethernet', primary ? statusText(primary.status) : '无网卡');
  setText('#speed-ethernet', primary ? primary.speed.replace(' bps', '') : '—');
  setText('#nic-ethernet', primary ? primary.name : '未检测到有线网卡');
  setText('#ip-ethernet', primary && primary.ip ? primary.ip : '—');
  setText('#tag-ethernet', wiredActive ? '当前出口' : primary && primary.up ? '可用' : '未启用');
  toggleCurrent('#card-ethernet', wiredActive);
  toggleDisabled('[data-switch="ethernet"]', !wired.length);

  // 无线
  setText('#status-wifi', wlan ? statusText(wlan.status) : '无网卡');
  setText('#signal-wifi', wifi.signal != null ? `${wifi.signal}%` : '—');
  setText('#ssid-wifi', wifi.connected ? wifi.ssid : '未连接');
  setText('#ip-wifi', wlan && wlan.ip ? wlan.ip : '—');
  setText(
    '#tag-wifi',
    campusWifiActive
      ? '当前出口'
      : onHotspot
        ? '热点在用'
        : wlan && wlan.up
          ? '待连接'
          : '未启用'
  );
  toggleCurrent('#card-wifi', !!campusWifiActive);

  // 热点
  const target = (s.auto || {}).target || '';
  setText('#status-hotspot', onHotspot ? '已连接' : hotspots.length ? '已保存' : '未保存');
  setText('#count-hotspot', hotspots.length);
  setText('#auto-target-hint', target || '未设置');
  setText('#tag-hotspot', onHotspot ? '当前出口' : '备用');
  toggleCurrent('#card-hotspot', onHotspot);
  toggleDisabled('#hotspot-connect-btn', !hotspots.length);

  // 出口账本
  setText('#egress-kind', egress.kind ? KIND_LABEL[egress.kind] || egress.kind : '无出口');
  const ledger = $('#egress-ledger');
  if (ledger) {
    const rows = ledger.querySelectorAll('li .ef-field');
    const values = [
      egress.name || '—',
      egress.ip || '—',
      egress.gateway || '—',
      (egress.dns || []).join(' / ') || '—',
      egress.metric != null ? String(egress.metric) : '—',
    ];
    rows.forEach((cell, i) => {
      if (cell.textContent !== values[i]) cell.textContent = values[i];
    });
  }

  // 系统代理
  const proxy = s.proxy || {};
  setText('#proxy-state', proxy.active ? '开' : '关');
  setText('#proxy-port', proxy.server || proxy.pac || '—');
  setText('#proc-count', (s.processes || []).length);
  const autoMode = (s.auto || {}).mode || 'system_proxy';
  setText(
    '#proxy-note',
    proxy.active
      ? `系统代理已开启，出口是 ${proxy.server || '自动配置脚本'}。${
          autoMode === 'system_proxy' ? '自动模式现在会触发。' : '当前用「代理软件」触发，模式一不会响应。'
        }`
      : autoMode === 'system_proxy'
        ? '系统代理关闭，模式一不会触发。'
        : '系统代理关闭；模式二看的是进程，不看这里。'
  );

  // 徽标
  const badge = $('#net-badge');
  badge.classList.toggle('is-on', !!s.online);
  badge.classList.toggle('is-off', !s.online);
  setText('#net-text', s.online ? `在线 · ${egress.kind ? KIND_LABEL[egress.kind] : '未知'}` : '离线');

  const admin = $('#admin-badge');
  admin.textContent = s.admin ? '管理员' : '普通权限';
  admin.classList.toggle('is-on', !!s.admin);
  $('#admin-notice').hidden = !!s.admin;
  $('#journal-notice').hidden = !s.pending_journal;
  setText('#about-admin', s.admin ? '管理员' : '普通用户');
  setText('#about-version', s.version || '—');

  // 开关状态
  toggleDisabled('.target .ef-btn', (s.job || {}).running);

  // 进度
  const job = s.job || {};
  renderProgress(job);
}

function toggleCurrent(sel, on) {
  const el = $(sel);
  if (el) el.classList.toggle('is-current', !!on);
}

function toggleDisabled(sel, disabled) {
  $$(sel).forEach((el) => {
    el.disabled = !!disabled;
  });
}

let progressClosedFor = null;
let autoHideTimer = null;
let autoHideJobId = null;
const PROGRESS_AUTO_HIDE_MS = 15000;

function scheduleAutoHide(job) {
  if (autoHideJobId === job.id) return; // 同一次任务只排一次
  clearTimeout(autoHideTimer);
  autoHideJobId = job.id;
  autoHideTimer = setTimeout(() => {
    progressClosedFor = job.id;
    const panel = $('#progress-panel');
    if (panel) panel.hidden = true;
  }, PROGRESS_AUTO_HIDE_MS);
}

function renderProgress(job) {
  const panel = $('#progress-panel');
  const steps = job.steps || [];
  if (!steps.length) {
    panel.hidden = true;
    return;
  }
  if (!job.running && progressClosedFor === job.id) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  setText('#progress-elapsed', `${(job.elapsed || 0).toFixed(1)}s`);
  setText('#progress-title', job.message || KIND_LABEL[job.kind] || '—');

  const phase = $('#progress-phase');
  // is-unknown 必须挂在 .ef-progress 上（套件的选择器是 `.ef-progress.is-unknown > i`）。
  // 现在改成「每完成一项就涨一格」，所以不再用跑马灯，始终按比例填充。
  const wrap = $('#progress-wrap');
  const okCount = steps.filter((x) => x.state === 'ok').length;
  const runCount = steps.filter((x) => x.state === 'run').length;
  wrap.classList.remove('is-unknown');
  const ratio = job.running
    ? Math.min(0.98, (okCount + runCount * 0.45) / steps.length)
    : job.ok
      ? 1
      : okCount / steps.length;
  wrap.style.setProperty('--p', ratio.toFixed(3));
  wrap.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
  if (job.running) {
    phase.textContent = 'Switching / 切换中';
    // 新任务开始：把上一次的自动收起倒计时取消掉
    clearTimeout(autoHideTimer);
    autoHideTimer = null;
    autoHideJobId = null;
  } else {
    phase.textContent = job.ok ? 'Done / 已完成' : 'Failed / 失败';
    scheduleAutoHide(job);
  }

  const list = $('#step-list');
  list.classList.add('steps');
  list.innerHTML = (job.steps || [])
    .map((step) => {
      const cls = step.state === 'ok' ? 'is-ok' : step.state === 'fail' ? 'is-fail' : step.state === 'run' ? 'is-run' : '';
      const label = { pending: '待执行', run: '进行中', ok: '完成', fail: '失败' }[step.state] || step.state;
      return `<li><div><div class="msg">${escapeHtml(step.label)}</div>
        <div class="meta"><span>${escapeHtml(step.detail || '')}</span></div></div>
        <span class="st ${cls}">${label}</span></li>`;
    })
    .join('');
}

/* ------------------------------------------------------------------ 渲染：自动页 */
function nextStepText(auto) {
  if (!auto.enabled) return '打开上面的开关，才会开始监听触发。';
  if (auto.cooldown_left > 0) return `连续失败后的冷却中，还剩 ${auto.cooldown_left} 秒。`;
  if (auto.manual_grace_left > 0)
    return `手动优先还剩 ${auto.manual_grace_left} 秒；想立刻切换就去下面点「立即接管」。`;
  if (auto.state === 'switching' || auto.state === 'reverting') return '正在执行切换，等本次完成。';
  if (auto.state === 'on_target')
    return `已在 ${auto.target || '目标热点'}，触发消失 ${auto.debounce_off ?? 10} 秒后回切。`;
  if (auto.state === 'wait_off')
    return `触发已消失，${auto.debounce_off ?? 10} 秒后回切到原网络。`;
  if (auto.state === 'wait_on') return `已命中触发，正在等 ${auto.debounce_on ?? 1} 秒确认。`;
  if ((auto.triggers || []).length)
    return `已命中触发，${auto.debounce_on ?? 1} 秒内切到 ${auto.target || '目标热点'}。`;
  return `等待触发：${auto.mode === 'app_process' ? '代理软件一运行' : '系统代理一打开'}就会切到 ${auto.target || '目标热点'}。`;
}

function renderAuto(s) {
  const auto = s.auto || {};
  setText('#auto-state', auto.state_label || '空闲');
  setText('#auto-state-label', auto.state_label || '空闲');
  setText('#auto-since', `${auto.since || 0}s`);
  setText('#auto-fails', auto.fail_count || 0);
  setText('#auto-reason', auto.reason || '—');
  setText('#auto-grace', auto.manual_grace_left > 0 ? `还有 ${auto.manual_grace_left}s` : '无');
  setText('#auto-cooldown', auto.cooldown_left > 0 ? `${auto.cooldown_left}s` : '无');
  setText('#auto-lasterr', auto.last_error || '无');
  setText('#auto-mode-name', auto.mode === 'app_process' ? '代理软件' : '系统代理');

  const matched = s.processes || [];
  setText(
    '#auto-matched',
    matched.length ? matched.join('、') : auto.mode === 'app_process' ? '未匹配任何进程' : '仅模式二需要'
  );

  if (!formTouched) {
    setValue('#debounce-on', auto.debounce_on);
    setValue('#debounce-off', auto.debounce_off);
  }
  const enabledEl = $('#auto-enabled');
  if (enabledEl && document.activeElement !== enabledEl) enabledEl.checked = !!auto.enabled;

  // 顶部的总开关格
  const hero = $('#auto-hero');
  if (hero) {
    hero.classList.toggle('is-on', !!auto.enabled);
    setText('#auto-hero-state', auto.enabled ? '开启' : '关闭');
    setText(
      '#auto-hero-sub',
      auto.enabled
        ? `正在监听${auto.mode === 'app_process' ? '代理软件进程' : '系统代理开关'}，命中后 ${auto.debounce_on ?? 1} 秒内切到目标热点。`
        : '关闭时不监听任何触发，网络不会自动切换。打开后才会响应代理状态。'
    );
    setText(
      '#auto-hero-badge',
      auto.enabled ? (auto.state === 'idle' ? '监听中' : auto.state_label || '监听中') : '已停用'
    );
    setText('#auto-hero-trigger', auto.mode === 'app_process' ? '代理软件运行' : '系统代理开启');
    setText('#auto-hero-target', auto.target || '未设置');
    setText('#auto-hero-mode', auto.state_label || '空闲');

    // 左半边的实时读数
    const hit = (auto.triggers || []).length > 0;
    const triggerEl = $('#hero-trigger');
    if (triggerEl) {
      triggerEl.textContent = hit ? '命中' : '未命中';
      triggerEl.classList.toggle('is-hit', hit && !!auto.enabled);
    }
    const job = s.job || {};
    const hasJob = Array.isArray(job.steps) && job.steps.length > 0;
    setText('#hero-last', hasJob ? (job.ok ? `${(job.elapsed || 0).toFixed(1)}s` : '失败') : '—');
    const eg = s.egress || {};
    setText('#hero-egress', eg.kind ? KIND_LABEL[eg.kind] || eg.kind : '无出口');
    setText('#hero-next', nextStepText(auto));
  }

  const radio = auto.mode === 'app_process' ? $('#mode-process') : $('#mode-proxy');
  if (radio && document.activeElement !== radio) radio.checked = true;

  setText(
    '#mode-hint',
    auto.mode === 'app_process'
      ? '监控代理软件的进程。代理一启动就切，进程全部退出后切回。'
      : '监听 Windows 的「使用代理服务器」开关。开的时候切热点，关的时候切回来。'
  );

  // 目标热点下拉
  const select = $('#auto-target');
  if (select) {
    const options = (s.profiles || []).filter((p) => p.type === 'hotspot');
    const wanted = auto.target_id || '';
    const signature = options.map((o) => o.id).join(',');
    if (select.dataset.sig !== signature) {
      select.dataset.sig = signature;
      select.innerHTML = options.length
        ? options
            .map((o) => `<option value="${o.id}">${escapeHtml(o.ssid)}</option>`)
            .join('')
        : '<option value="">网络库里还没有手机热点</option>';
    }
    if (document.activeElement !== select) select.value = wanted;
  }

  renderTimeline();
}

function renderTimeline() {
  const list = $('#auto-timeline');
  if (!list) return;
  const items = logBuffer.filter((l) => l.module === 'auto' || l.module === 'switch').slice(-20).reverse();
  if (!items.length) {
    list.innerHTML = '<li><div><div class="msg">还没有自动模式的动作。</div><div class="meta"><span>打开开关后，这里会记录每一次触发和回切</span></div></div></li>';
    return;
  }
  list.innerHTML = items
    .map(
      (l) => `<li><div><div class="msg">${escapeHtml(l.message)}</div>
      <div class="meta"><span>${l.ts}</span><span>${l.module}</span></div></div>
      <span class="st ${l.level === 'ERROR' ? 'is-fail' : ''}">${l.level}</span></li>`
    )
    .join('');
}

/* ------------------------------------------------------------------ 渲染：网络库 */
function renderLibrary() {
  if (!state) return;
  const rows = $('#profile-rows');
  const profiles = state.profiles || [];
  setText('#lib-count', `${profiles.length} 条`);
  setText('#nav-count', profiles.length);
  $('#profile-empty').hidden = profiles.length > 0;

  rows.innerHTML = profiles
    .map((p) => {
      const badge = p.type === 'hotspot' ? '手机热点' : 'WiFi';
      const target = p.is_auto_target ? '<span class="ef-state is-alert">自动目标</span>' : '';
      return `<li data-id="${p.id}">
        <div>
          <div>${escapeHtml(p.ssid)} <span class="ef-tag">${badge}</span> ${target}</div>
          <div class="meta">
            <span>${p.has_password ? '已保存密码' : '无密码'}</span>
            <span>优先级 ${p.priority || 0}</span>
            ${p.note ? `<span>${escapeHtml(p.note)}</span>` : ''}
          </div>
        </div>
        <div class="acts">
          <button class="ef-btn small" data-act="connect" data-ssid="${escapeHtml(p.ssid)}" data-type="${p.type}">连接</button>
          <button class="ef-btn small" data-act="pwd" data-ssid="${escapeHtml(p.ssid)}" data-type="${p.type}">改密码</button>
          <button class="ef-btn small" data-act="target" data-id="${p.id}">设为自动目标</button>
          <button class="ef-btn small danger" data-act="del" data-id="${p.id}">删除</button>
        </div>
      </li>`;
    })
    .join('');

  rows.onclick = async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === 'connect') {
      await runSwitch(btn.dataset.type === 'hotspot' ? 'hotspot' : 'wifi', btn.dataset.ssid, '', true);
    } else if (act === 'pwd') {
      const ssid = btn.dataset.ssid;
      const pwd = await askPassword(ssid, {
        title: '改密码',
        ok: '保存密码',
        hint: '会同时更新系统里的 WiFi 配置；已经连着的话下次连接生效。',
      });
      if (pwd === null || pwd === '') return toast('没有输入密码，已取消');
      const res = await api('/api/profiles', {
        method: 'POST',
        body: { ssid, type: btn.dataset.type, password: pwd },
      });
      const synced = res.system;
      toast(
        synced && synced.ok
          ? '密码已保存，并同步到系统配置'
          : synced
            ? `已保存到网络库，但系统配置未同步：${synced.message}`
            : '密码已保存到网络库（系统里还没这个网络的配置）'
      );
      refresh();
    } else if (act === 'target') {
      await api('/api/auto', { method: 'POST', body: { target_id: btn.dataset.id } });
      toast('已设为自动目标');
      refresh();
    } else if (act === 'del') {
      if (!confirm('从网络库删除这条记录？系统里的 WiFi 配置不受影响。')) return;
      await api(`/api/profiles/${btn.dataset.id}`, { method: 'DELETE' });
      toast('已删除');
      refresh();
    }
  };
}

/* ------------------------------------------------------------------ 渲染：WiFi 列表 */
let lastScan = 0;

function renderWifi(networks, live = false) {
  const body = $('#wifi-rows');
  const empty = $('#wifi-empty');
  if (!body) return;
  const current = state && state.wifi && state.wifi.connected ? state.wifi.ssid : '';
  empty.hidden = networks.length > 0;
  body.innerHTML = networks
    .map((n) => {
      const isCurrent = current && n.ssid === current;
      const stateTag = isCurrent
        ? '<span class="ef-state is-alert">当前</span>'
        : n.saved
        ? '<span class="ef-state">已保存</span>'
        : '<span class="ef-state is-draft">未保存</span>';
      const enc = n.open ? '开放' : n.encryption || n.auth || '—';
      return `<tr>
        <td class="num">${n.signal}%</td>
        <td>${escapeHtml(n.ssid)}</td>
        <td>${escapeHtml(enc)}</td>
        <td>${escapeHtml(n.band || '—')}</td>
        <td>${stateTag}</td>
        <td class="num"><button class="ef-btn small" data-ssid="${escapeHtml(n.ssid)}" data-open="${n.open ? 1 : 0}">连接</button></td>
      </tr>`;
    })
    .join('');

  body.onclick = async (e) => {
    const btn = e.target.closest('button[data-ssid]');
    if (!btn) return;
    const open = btn.dataset.open === '1';
    await runSwitch('wifi', btn.dataset.ssid, '', open);
  };

  window.__networks = networks;
  setText('#scan-time', networks.length ? `可见 ${networks.length} 个网络` : '未发现网络');
  // live=true 表示这是跟着状态刷新的重绘，不能把 lastScan 顶掉，否则永远触发不了自动重扫
  if (!live) lastScan = Date.now();
}

async function scanNetworks(force = false) {
  const btn = $('#scan-btn');
  btn.disabled = true;
  btn.textContent = '扫描中…';
  try {
    const data = await api(`/api/scan${force ? '?force=1' : ''}`);
    renderWifi(data.networks || []);
  } catch (err) {
    toast(`扫描失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = '重新扫描';
  }
}

/* ------------------------------------------------------------------ 动作 */
async function runSwitch(kind, ssid = '', password = '', allowPrompt = false) {
  if (allowPrompt && ssid && !password) {
    const profile = (state.profiles || []).find((p) => p.ssid === ssid);
    const hasPassword = !!(profile && profile.has_password);
    if (!hasPassword && !isOpenNetwork(ssid)) {
      password = await askPassword(ssid);
      if (password === null) return;
    }
  }
  startFastPoll();
  try {
    const res = await api('/api/switch', { method: 'POST', body: { kind, ssid, password } });
    toast(res.message || (res.ok ? '已切换' : '切换失败'));
    if (res.ok) await savePasswordIfNew(ssid, password);
  } catch (err) {
    toast(`切换失败：${err.message}`);
  } finally {
    stopFastPoll();
    refresh();
  }
}

function isOpenNetwork(ssid) {
  const cached = (window.__networks || []).find((n) => n.ssid === ssid);
  return cached ? !!cached.open : false;
}

async function savePasswordIfNew(ssid, password) {
  if (!ssid || !password) return;
  const profile = (state.profiles || []).find((p) => p.ssid === ssid);
  if (profile && profile.has_password) return;
  await api('/api/profiles', {
    method: 'POST',
    body: { ssid, type: (state.auto && state.auto.target === ssid) || /热点|hotspot/i.test(ssid) ? 'hotspot' : 'wifi', password },
  });
}

function askPassword(ssid, opts = {}) {
  return new Promise((resolve) => {
    const dialog = $('#pwd-dialog');
    $('#pwd-ssid').textContent = ssid;
    setText('#pwd-title', opts.title || '连接');
    setText('#pwd-ok', opts.ok || '连接');
    setText(
      '#pwd-hint',
      opts.hint || '这个网络还没有保存过密码，输入一次就会记住。'
    );
    $('#pwd-input').value = '';
    dialog.showModal();
    $('#pwd-input').focus();
    const done = (value) => {
      dialog.close();
      $('#pwd-ok').onclick = null;
      dialog.onclose = null;
      resolve(value);
    };
    $('#pwd-ok').onclick = () => done($('#pwd-input').value);
    dialog.onclose = () => resolve(null);
    $('#pwd-input').onkeydown = (e) => {
      if (e.key === 'Enter') done($('#pwd-input').value);
    };
    $$('[data-close="cancel"]', dialog).forEach((b) => (b.onclick = () => done(null)));
  });
}

/* ------------------------------------------------------------------ 轮询 */
async function refresh() {
  try {
    const s = await api('/api/state');
    state = s;
    renderManual(s);
    renderAuto(s);
    // WiFi 列表里的「当前」要跟着实时状态走，否则切完网络还标着上一条
    if (window.__networks && window.__networks.length) renderWifi(window.__networks, true);
    if (currentView === 'library') renderLibrary();
    await pullLogs();
  } catch (err) {
    setText('#net-text', '内核未响应');
  }
}

async function pullLogs() {
  try {
    const data = await api(`/api/logs?since=${lastLogSeq}&limit=200`);
    const items = data.logs || [];
    if (items.length) {
      lastLogSeq = items[items.length - 1].seq;
      logBuffer = logBuffer.concat(items).slice(-400);
      if (currentView === 'logs') renderLogs();
      if (currentView === 'auto') renderTimeline();
    }
  } catch (err) {
    /* 静默 */
  }
}

function renderLogs() {
  const list = $('#log-rows');
  const items = logBuffer.filter((l) => levelFilter === 'ALL' || l.level === levelFilter);
  const counts = { ALL: logBuffer.length, INFO: 0, WARN: 0, ERROR: 0 };
  logBuffer.forEach((l) => {
    if (counts[l.level] != null) counts[l.level] += 1;
  });
  $('#n-all').textContent = counts.ALL;
  $('#n-info').textContent = counts.INFO;
  $('#n-warn').textContent = counts.WARN;
  $('#n-error').textContent = counts.ERROR;
  setText('#log-count', `${items.length} 条`);
  $('#log-empty').hidden = items.length > 0;

  list.innerHTML = items
    .slice()
    .reverse()
    .map(
      (l) => `<li><div>
        <div class="msg"><span class="lv ${l.level === 'ERROR' ? 'is-error' : ''}">${l.level}</span> ${escapeHtml(l.message)}</div>
        <div class="meta"><span>${l.ts}</span><span>${l.module}</span></div>
      </div></li>`
    )
    .join('');
}

function startFastPoll() {
  if (fastTimer) return;
  fastTimer = setInterval(refresh, 350);
}

function stopFastPoll() {
  if (!fastTimer) return;
  clearInterval(fastTimer);
  fastTimer = null;
}

/* ------------------------------------------------------------------ 事件绑定 */
function wireActions() {
  $('[data-switch="ethernet"]').addEventListener('click', () => runSwitch('ethernet'));

  $('#wifi-pick-btn').addEventListener('click', () => {
    $('#wifi-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    scanNetworks(true);
  });

  $('#hotspot-connect-btn').addEventListener('click', () => {
    const target = state && state.auto && state.auto.target;
    if (!target) return toast('先去网络库选一个自动目标热点');
    runSwitch('hotspot', target, '', true);
  });

  $('#scan-btn').addEventListener('click', () => scanNetworks(true));

  $('#progress-close').addEventListener('click', () => {
    progressClosedFor = state && state.job ? state.job.id : null;
    $('#progress-panel').hidden = true;
  });

  $('#journal-dismiss').addEventListener('click', async () => {
    await api('/api/journal/clear', { method: 'POST', body: {} });
    $('#journal-notice').hidden = true;
    toast('已忽略上次未完成的记录');
  });

  $('#proc-add').addEventListener('click', async () => {
    const name = $('#proc-picker').value;
    if (!name) return toast('先选一个进程');
    const lines = $('#set-patterns')
      .value.split('\n')
      .map((x) => x.trim())
      .filter(Boolean);
    if (lines.some((x) => x.toLowerCase() === name.toLowerCase())) return toast('名单里已经有了');
    lines.push(name);
    $('#set-patterns').value = lines.join('\n');
    await api('/api/auto', { method: 'POST', body: { process_patterns: lines } });
    toast(`已加入 ${name}`);
    refresh();
  });
  $('#restore-btn').addEventListener('click', async () => {
    await runSwitch('ethernet');
  });

  // 自动模式
  const autoSave = async () => {
    const mode = $('#mode-process').checked ? 'app_process' : 'system_proxy';
    const body = {
      mode,
      enabled: $('#auto-enabled').checked,
      debounce_on: Number($('#debounce-on').value || 1),
      debounce_off: Number($('#debounce-off').value || 10),
      target_id: $('#auto-target').value || '',
    };
    await api('/api/auto', { method: 'POST', body });
    formTouched = false;
    flashDone($('#auto-save'), '已保存');
    toast('自动模式已更新');
    refresh();
  };
  $('#auto-save').addEventListener('click', autoSave);
  $('#auto-enabled').addEventListener('change', autoSave);
  $$('input[name="automode"]').forEach((r) => r.addEventListener('change', autoSave));
  $('#auto-target').addEventListener('change', autoSave);
  ['#debounce-on', '#debounce-off'].forEach((sel) =>
    $(sel).addEventListener('input', () => {
      formTouched = true;
    })
  );
  $('#cooldown-reset').addEventListener('click', async () => {
    await api('/api/cooldown/reset', { method: 'POST', body: {} });
    toast('已解除冷却');
    refresh();
  });
  $('#takeover-btn').addEventListener('click', async () => {
    await api('/api/auto/takeover', { method: 'POST', body: {} });
    toast('已取消手动优先，触发命中后会立刻切换');
    refresh();
  });

  // 网络库
  $('#profile-save').addEventListener('click', async () => {
    const ssid = $('#f-ssid').value.trim();
    if (!ssid) return toast('SSID 不能为空');
    const type = $('input[name="ptype"]:checked').value;
    const body = { ssid, type, password: $('#f-pwd').value, note: $('#f-note').value.trim() };
    try {
      await api('/api/profiles', { method: 'POST', body });
      const msg = $('#lib-msg');
      msg.hidden = false;
      msg.textContent = `已保存「${ssid}」。在自动模式里把它设为目标热点就能用。`;
      $('#f-ssid').value = '';
      $('#f-pwd').value = '';
      $('#f-note').value = '';
      toast('已保存到网络库');
      refresh();
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
  });

  // 日志
  $$('.filters .ef-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      levelFilter = chip.dataset.level;
      $$('.filters .ef-chip').forEach((c) => c.classList.toggle('is-active', c === chip));
      renderLogs();
    });
  });
  $('#log-clear').addEventListener('click', () => {
    logBuffer = [];
    renderLogs();
    toast('已清空显示');
  });

  // 设置
  $('#set-save').addEventListener('click', async () => {
    const body = {
      switch: {
        connect_timeout: Number($('#set-connect').value || 18),
        online_timeout: Number($('#set-online').value || 20),
      },
      auto: {
        verify_online: $('#set-verify').checked,
        manual_grace_seconds: Number($('#set-grace').value || 600),
        cooldown_seconds: Number($('#set-cooldown').value || 300),
      },
    };
    await api('/api/config', { method: 'PATCH', body });
    toast('设置已保存');
    refresh();
  });

  $('#patterns-save').addEventListener('click', async () => {
    const list = $('#set-patterns')
      .value.split('\n')
      .map((s) => s.trim())
      .filter(Boolean);
    await api('/api/auto', { method: 'POST', body: { process_patterns: list } });
    toast('名单已保存');
  });

}

async function loadProcessPicker() {
  const select = $('#proc-picker');
  if (!select) return;
  try {
    const data = await api('/api/processes');
    const list = data.processes || [];
    select.innerHTML =
      '<option value="">从正在运行的进程里挑…</option>' +
      list.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
  } catch (err) {
    select.innerHTML = '<option value="">读取失败</option>';
  }
}

/* ------------------------------------------------------------------ 启动 */
function escapeHtml(text) {
  return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function boot() {
  wireTheme();
  wireNav();
  wireActions();

  // 关窗口/刷新时通知内核。窗口模式下后端据此判断「界面没了」并退出进程，
  // 不能靠 Edge 子进程是否还活着来判断 —— Edge 会把窗口交接给已有实例然后自己退出。
  window.addEventListener('pagehide', () => {
    try {
      navigator.sendBeacon(`/api/bye?token=${encodeURIComponent(TOKEN)}`);
    } catch (e) {
      /* 关窗口途中失败无所谓 */
    }
  });

  const config = await api('/api/config');
  $('#set-connect').value = config.switch.connect_timeout;
  $('#set-online').value = config.switch.online_timeout;
  $('#set-grace').value = config.auto.manual_grace_seconds;
  $('#set-cooldown').value = config.auto.cooldown_seconds;
  $('#set-verify').checked = !!config.auto.verify_online;
  $('#set-patterns').value = (config.auto.process_patterns || []).join('\n');
  $('#about-dir').textContent = '%APPDATA%\\NetSwitcher';

  await refresh();
  scanNetworks(false);
  loadProcessPicker();

  setInterval(refresh, 1500);
  setInterval(() => {
    if (currentView === 'manual' && Date.now() - lastScan > 30000 && !$('#scan-btn').disabled) {
      scanNetworks(true);
    }
  }, 5000);
}

boot().catch((err) => {
  document.body.insertAdjacentHTML(
    'afterbegin',
    `<p class="ef-notice" style="margin:1rem">无法连接本地内核：${escapeHtml(err.message)}。请确认程序是以 python -m netswitcher serve 启动的。</p>`
  );
});

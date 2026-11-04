/* Emby 媒体信息预热面板 —— 前端逻辑（无框架、无依赖） */

const $ = (sel) => document.querySelector(sel);

const el = {
  pillConn: $('#pillConn'),
  connText: $('#connText'),
  stTotal: $('#stTotal'),
  stTotalHint: $('#stTotalHint'),
  stComplete: $('#stComplete'),
  stPending: $('#stPending'),
  stFailed: $('#stFailed'),
  stFailedHint: $('#stFailedHint'),
  navFailBadge: $('#navFailBadge'),
  lastRunText: $('#lastRunText'),
  btnRun: $('#btnRun'),
  btnRetryFailed: $('#btnRetryFailed'),
  btnExport: $('#btnExport'),
  btnScan: $('#btnScan'),
  btnStop: $('#btnStop'),
  phaseText: $('#phaseText'),
  counterText: $('#counterText'),
  progressBar: $('#progressBar'),
  elapsedText: $('#elapsedText'),
  activeText: $('#activeText'),
  log: $('#log'),
  autoScroll: $('#autoScroll'),
  syncLog: $('#syncLog'),
  autoScrollSync: $('#autoScrollSync'),
  failBadge: $('#failBadge'),
  failList: $('#failList'),
  kvSchedEnabled: $('#kvSchedEnabled'),
  kvNextRun: $('#kvNextRun'),
  kvWatch: $('#kvWatch'),
  kvHook: $('#kvHook'),
  kvRetryEnabled: $('#kvRetryEnabled'),
  kvNextRetry: $('#kvNextRetry'),
  syncBadge: $('#syncBadge'),
  btnSyncNow: $('#btnSyncNow'),
  btnSyncSettings: $('#btnSyncSettings'),
  syncLastText: $('#syncLastText'),
  syncReasonText: $('#syncReasonText'),
  syncCopied: $('#syncCopied'),
  syncSkipped: $('#syncSkipped'),
  syncFailedN: $('#syncFailedN'),
  kvSyncNext: $('#kvSyncNext'),
  kvSyncAfter: $('#kvSyncAfter'),
  kvSyncScope: $('#kvSyncScope'),
  kvSyncPath: $('#kvSyncPath'),
  kvTrigAfter: $('#kvTrigAfter'),
  kvTrigSched: $('#kvTrigSched'),
  histBody: $('#histBody'),
  histTotalText: $('#histTotalText'),
  btnHistRefresh: $('#btnHistRefresh'),
  drawer: $('#drawer'),
  drawerMask: $('#drawerMask'),
  toast: $('#toast'),
};

let state = {
  cfg: null,
  logSeq: { prewarm: 0, sync: 0 },
  logClearedAt: { prewarm: 0, sync: 0 },
  busy: false,
  failuresFetched: -1,
  dailyTimes: [],
  page: 'prewarm',
  historyLoadedAt: 0,
  plan: null,
};

/* ------------------------------------------------------------------ 页面路由 */
const PAGES = ['prewarm', 'sync', 'history'];

function switchPage(name, push = true) {
  if (!PAGES.includes(name)) name = 'prewarm';
  state.page = name;
  document.querySelectorAll('.page').forEach((p) => {
    p.classList.toggle('active', p.id === 'page-' + name);
  });
  document.querySelectorAll('.nav-tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.page === name);
  });
  if (push && location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
  if (name === 'history') fetchHistory();
}

document.querySelectorAll('.nav-tab').forEach((tab) => {
  tab.addEventListener('click', (e) => {
    e.preventDefault();
    switchPage(tab.dataset.page);
  });
});
window.addEventListener('hashchange', () => switchPage(location.hash.slice(1), false));

/* ------------------------------------------------------------------ 工具 */
// 统一网关下页面地址是 /app/embyprewarm/，接口要拼上前缀；直连端口时前缀为空
const BASE = (() => {
  const p = new URL('./', window.location.href).pathname.replace(/\/+$/, '');
  return p;
})();

async function api(path, method = 'GET', body) {
  const options = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const res = await fetch(BASE + '/' + String(path).replace(/^\/+/, ''), options);
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).error || ''; } catch (e) { /* ignore */ }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

let toastTimer = null;
function toast(message, bad = false) {
  el.toast.textContent = message;
  el.toast.classList.toggle('bad', bad);
  el.toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.remove('show'), 2600);
}

const fmtNum = (n) => (typeof n === 'number' ? n.toLocaleString('zh-CN') : '—');

function fmtDuration(seconds) {
  if (!seconds || seconds < 0) return '';
  const s = Math.round(seconds);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
}

/* ------------------------------------------------------------------ 日志 */
// 预热与备份同步日志走两个独立通道，分别展示在各自页面
function appendLogs(box, lines, channel) {
  if (!lines || !lines.length) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    if (line.seq <= state.logClearedAt[channel]) {
      state.logSeq[channel] = Math.max(state.logSeq[channel], line.seq);
      continue;
    }
    const row = document.createElement('div');
    row.className = `row ${line.level}`;
    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = line.ts.slice(11);
    const msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = line.msg;
    row.append(ts, msg);
    frag.appendChild(row);
    state.logSeq[channel] = Math.max(state.logSeq[channel], line.seq);
  }
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  box.appendChild(frag);
  while (box.childElementCount > 1500) box.removeChild(box.firstChild);
  const auto = channel === 'sync' ? el.autoScrollSync : el.autoScroll;
  if (auto.checked || atBottom) box.scrollTop = box.scrollHeight;
}

async function pollLogs() {
  for (const channel of ['prewarm', 'sync']) {
    try {
      const data = await api(`/api/logs?since=${state.logSeq[channel]}&channel=${channel}`);
      if (data.lines && data.lines.length) {
        appendLogs(channel === 'sync' ? el.syncLog : el.log, data.lines, channel);
      }
    } catch (e) { /* 忽略瞬时错误 */ }
  }
}

/* ------------------------------------------------------------------ 状态 */
function renderStatus(s) {
  state.busy = !!s.busy;

  // 顶部状态
  el.pillConn.className = 'pill';
  if (s.busy) {
    el.pillConn.classList.add('is-busy');
    el.connText.textContent = `运行中 · ${s.mode_text}`;
  } else if (s.last_error) {
    el.pillConn.classList.add('is-bad');
    el.connText.textContent = '连接异常';
    el.pillConn.title = s.last_error;
  } else if (s.summary && s.summary.ts) {
    el.pillConn.classList.add('is-ok');
    el.connText.textContent = '已连接';
    el.pillConn.title = 'Emby 连接正常';
  } else {
    el.connText.textContent = '待扫描';
    el.pillConn.title = '';
  }

  // 统计
  const sum = s.summary || {};
  el.stTotal.textContent = sum.total != null ? fmtNum(sum.total) : '—';
  el.stComplete.textContent = sum.complete != null ? fmtNum(sum.complete) : '—';
  el.stPending.textContent = sum.pending != null ? fmtNum(sum.pending) : '—';
  el.stTotalHint.textContent = sum.ts ? '上次扫描 ' + new Date(sum.ts * 1000).toLocaleTimeString('zh-CN') : '等待扫描';
  el.stFailed.textContent = fmtNum(s.failures || 0);
  el.stFailedHint.textContent = s.failures ? '自动循环重试中' : '无失败项目';

  // 进度
  if (s.busy) {
    el.phaseText.className = 'phase running';
    el.phaseText.textContent = s.phase || '运行中';
    const total = s.total || 0;
    const done = s.done || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    el.progressBar.style.width = pct + '%';
    const exportPart = s.exported ? ` · 备份 ${s.exported}` : '';
    el.counterText.textContent = total
      ? `进度 ${done}/${total} · 成功 ${s.success} · 失败 ${s.failed}${exportPart}`
      : `成功 ${s.success} · 失败 ${s.failed}${exportPart}`;
    el.elapsedText.textContent = `已运行 ${fmtDuration(s.elapsed)}`;
    el.activeText.textContent = (s.active && s.active.length) ? '处理中：' + s.active.join(' · ') : '';
  } else {
    el.phaseText.className = 'phase';
    el.phaseText.textContent = s.last_error ? '上次运行出错' : '空闲';
    el.progressBar.style.width = s.total ? '100%' : '0%';
    const last = s.last_summary || {};
    el.counterText.textContent = last.ts
      ? `上次：成功 ${last.success} · 失败 ${last.failed}` +
        (last.exported ? ` · 备份 ${last.exported}` : '') +
        ` · 用时 ${last.duration}s`
      : '—';
    el.elapsedText.textContent = last.ts || '';
    el.activeText.textContent = s.last_error || '';
  }

  // 按钮状态
  el.btnRun.disabled = s.busy;
  el.btnScan.disabled = s.busy;
  el.btnExport.disabled = s.busy;
  el.btnRetryFailed.disabled = s.busy || !s.failures;
  el.btnStop.disabled = !s.busy;

  // 定时区
  el.kvSchedEnabled.textContent = s.schedule_enabled
    ? (s.schedule_mode === 'daily' ? '每天定时' : '固定间隔')
    : '已关闭';
  el.kvSchedEnabled.className = s.schedule_enabled ? 'on' : 'off';
  el.kvNextRun.textContent = s.next_run || '—';
  el.kvWatch.textContent = s.watch_enabled
    ? `每 ${s.watch_poll_seconds} 秒 · ${s.next_watch || '—'}`
    : '已关闭';
  el.kvWatch.className = s.watch_enabled ? 'on' : 'off';
  el.kvHook.textContent = s.hook_enabled
    ? (s.hook_token_set ? '已开启（带令牌）' : '已开启')
    : '已关闭';
  el.kvHook.className = s.hook_enabled ? 'on' : 'off';
  el.kvRetryEnabled.textContent = s.retry_enabled ? '已开启' : '已关闭';
  el.kvRetryEnabled.className = s.retry_enabled ? 'on' : 'off';
  el.kvNextRetry.textContent = s.next_retry || '—';

  // 备份同步（同步页 + 导航徽标）
  const sync = s.sync || {};
  const last = sync.last || {};
  if (sync.running) {
    el.syncBadge.textContent = '同步中';
    el.syncBadge.className = 'badge ok';
    el.btnSyncNow.disabled = true;
  } else {
    el.syncBadge.textContent = last.ts ? (last.ok ? '正常' : '异常') : '未运行';
    el.syncBadge.className = 'badge' + (last.ts ? (last.ok ? ' ok' : ' has') : '');
    el.btnSyncNow.disabled = false;
  }
  el.syncLastText.textContent = last.ts ? last.ts_text : '—';
  el.syncReasonText.textContent = last.ts
    ? `触发方式：${last.reason || '—'}` + (last.error ? ` · ${last.error}` : '')
    : (last.error ? '上次同步出错：' + last.error : '尚未执行过同步');
  el.syncCopied.textContent = last.ts ? fmtNum(last.copied || 0) : '—';
  el.syncSkipped.textContent = last.ts ? fmtNum(last.skipped || 0) : '—';
  el.syncFailedN.textContent = last.ts ? fmtNum(last.failed || 0) : '—';
  el.kvSyncNext.textContent = s.next_sync || '—';
  el.kvSyncNext.className = s.next_sync === '未开启' ? 'off' : 'on';
  el.kvSyncNext.title = last.note || '';
  el.kvSyncAfter.textContent = (state.cfg && state.cfg.sync && state.cfg.sync.after_prewarm) ? '已开启' : '已关闭';
  el.kvSyncAfter.className = (state.cfg && state.cfg.sync && state.cfg.sync.after_prewarm) ? 'on' : 'off';
  el.kvTrigAfter.textContent = el.kvSyncAfter.textContent;
  el.kvTrigAfter.className = el.kvSyncAfter.className;
  el.kvTrigSched.textContent = (state.cfg && state.cfg.sync && state.cfg.sync.schedule_enabled)
    ? `每天 ${state.cfg.sync.schedule_time || '—'}`
    : '已关闭';
  el.kvTrigSched.className = (state.cfg && state.cfg.sync && state.cfg.sync.schedule_enabled) ? 'on' : 'off';
  if (state.cfg && state.cfg.sync) {
    const parts = [];
    const scope = ['mediainfo.json'];
    if (state.cfg.sync.images) scope.push('图片');
    if (state.cfg.sync.subtitles) scope.push('字幕');
    el.kvSyncScope.textContent = scope.join(' + ');
    for (const src of state.cfg.sync.source || []) parts.push(src + ' → ' + (state.cfg.sync.target || '(未设置)'));
    if (!parts.length) {
      const mappings = (state.cfg.scan && state.cfg.scan.path_mappings) || {};
      const mapped = Object.values(mappings).map((p) => p + ' → ' + (state.cfg.sync.target || '(未设置)'));
      parts.push(...(mapped.length ? mapped : ['（自动：媒体库本机路径） → ' + (state.cfg.sync.target || '(未设置)')]));
    }
    el.kvSyncPath.textContent = parts.join('；');
    el.kvSyncPath.title = el.kvSyncPath.textContent;
  }

  // 上次运行时间（预热页）
  const lastSum = s.last_summary || {};
  el.lastRunText.textContent = lastSum.ts ? '上次运行 ' + String(lastSum.ts).slice(5, 16) : '';
}

function escapeHtml(text) {
  return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ------------------------------------------------------------- 失败队列 */
function renderFailures(list) {
  el.failBadge.textContent = list.length;
  el.failBadge.classList.toggle('has', list.length > 0);
  el.navFailBadge.textContent = list.length > 99 ? '99+' : list.length;
  el.navFailBadge.hidden = list.length === 0;
  if (!list.length) {
    el.failList.innerHTML = '<li class="empty">暂无失败项目</li>';
    return;
  }
  el.failList.innerHTML = list
    .map((item) => {
      const dead = item.status === 'dead';
      return `<li class="fail-item ${dead ? 'dead' : ''}">
        <div class="fail-title">
          <span class="name" title="${escapeHtml(item.path)}">${escapeHtml(item.name || item.path)}</span>
          <span class="badge ${dead ? 'has' : ''}">${item.attempts}次</span>
        </div>
        <div class="fail-meta">
          <span>${dead ? '已停止自动重试' : '下次 ' + (item.next_retry_text || '—')}</span>
        </div>
        <div class="fail-err" title="${escapeHtml(item.error)}">${escapeHtml(shorten(item.error, 110))}</div>
        <div class="fail-ops">
          <button class="tiny accent" data-act="prewarm" data-id="${item.id}">立即预热</button>
          <button class="tiny" data-act="requeue" data-id="${item.id}">重新排队</button>
          <button class="tiny" data-act="drop" data-id="${item.id}">移除</button>
        </div>
      </li>`;
    })
    .join('');
}

function shorten(text, max) {
  const s = String(text || '');
  return s.length > max ? s.slice(0, max) + '…' : s;
}

async function pollFailures(force = false) {
  try {
    const data = await api('/api/failures');
    const list = data.failures || [];
    if (force || list.length !== state.failuresFetched) {
      state.failuresFetched = list.length;
      renderFailures(list);
    }
  } catch (e) { /* ignore */ }
}

el.failList.addEventListener('click', async (event) => {
  const btn = event.target.closest('button[data-act]');
  if (!btn) return;
  const labels = { prewarm: '已开始手动预热', requeue: '已重新排队', drop: '已移除' };
  try {
    const res = await api('/api/failures/action', 'POST', {
      action: btn.dataset.act,
      ids: [btn.dataset.id],
    });
    if (btn.dataset.act === 'prewarm' && res.ok === false) {
      toast(res.error || '启动失败', true);
      return;
    }
    toast(labels[btn.dataset.act] || '操作成功');
    pollFailures(true);
    refresh();
  } catch (e) {
    toast(e.message, true);
  }
});

/* ------------------------------------------------------------------ 轮询 */
async function refresh() {
  try {
    const s = await api('/api/status');
    renderStatus(s);
  } catch (e) {
    el.pillConn.className = 'pill is-bad';
    el.connText.textContent = '面板失联';
  }
}

/* ------------------------------------------------------------------ 任务记录 */
function renderHistoryTable(history) {
  el.histTotalText.textContent = history.length ? `共 ${history.length} 条` : '';
  if (!history.length) {
    el.histBody.innerHTML = '<tr class="empty-row"><td colspan="8">暂无记录</td></tr>';
    return;
  }
  el.histBody.innerHTML = history.map((h) => {
    const failCls = h.failed > 0 ? 'td-fail' : 'td-ok';
    return `<tr>
      <td>${escapeHtml(h.ts.slice(0, 16))}</td>
      <td class="td-mode">${escapeHtml(h.mode)}</td>
      <td class="num">${fmtNum(h.total)}</td>
      <td class="num td-ok">✓ ${fmtNum(h.success)}</td>
      <td class="num ${failCls}">${h.failed ? '✗ ' + fmtNum(h.failed) : '0'}</td>
      <td class="num">${h.exported ? '⇩ ' + fmtNum(h.exported) : '—'}</td>
      <td class="num">${fmtDuration(h.duration) || h.duration + 's'}</td>
      <td class="td-note" title="${escapeHtml(h.note || '')}">${escapeHtml(shorten(h.note || '—', 60))}</td>
    </tr>`;
  }).join('');
}

async function fetchHistory() {
  try {
    const data = await api('/api/history');
    renderHistoryTable(data.history || []);
    state.historyLoadedAt = Date.now();
  } catch (e) { /* 忽略瞬时错误 */ }
}

el.btnHistRefresh.addEventListener('click', fetchHistory);

async function loop() {
  const wasBusy = state.busy;
  await refresh();
  await pollLogs();
  await pollFailures();
  // 任务结束后或记录页停留时刷新任务记录
  if (state.page === 'history') {
    if (wasBusy && !state.busy) await fetchHistory();
    else if (Date.now() - state.historyLoadedAt > 8000) await fetchHistory();
  }
  setTimeout(loop, state.busy ? 1200 : 3000);
}

/* ------------------------------------------------------------------ 操作 */
async function startJob(mode) {
  try {
    const res = await api('/api/run', 'POST', { mode });
    if (res.ok) {
      const labels = { scan: '开始扫描', export: '开始导出备份', failed: '开始重试失败项目', full: '开始预热' };
      toast(labels[mode] || '任务已启动');
      await pollLogs();
      await refresh();
    } else {
      toast(res.error || '启动失败', true);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

el.btnRun.addEventListener('click', () => startJob('full'));
el.btnScan.addEventListener('click', () => startJob('scan'));
el.btnRetryFailed.addEventListener('click', () => startJob('failed'));
el.btnExport.addEventListener('click', () => startJob('export'));
el.btnStop.addEventListener('click', async () => {
  try {
    await api('/api/stop', 'POST', {});
    toast('已发送停止指令');
  } catch (e) {
    toast(e.message, true);
  }
});
$('#btnClearLog').addEventListener('click', () => {
  state.logClearedAt.prewarm = state.logSeq.prewarm;
  el.log.innerHTML = '<div class="empty">日志已清空（仅清空显示）</div>';
});
$('#btnClearSyncLog').addEventListener('click', () => {
  state.logClearedAt.sync = state.logSeq.sync;
  el.syncLog.innerHTML = '<div class="empty">日志已清空（仅清空显示）</div>';
});
$('#btnRequeueAll').addEventListener('click', async () => {
  try {
    const res = await api('/api/failures/action', 'POST', { action: 'requeue', ids: [] });
    toast(`已重新排队 ${res.count} 个项目`);
    pollFailures(true);
  } catch (e) { toast(e.message, true); }
});
$('#btnClearFailures').addEventListener('click', async () => {
  if (!confirm('确定清空失败队列？不影响媒体库本身。')) return;
  try {
    const res = await api('/api/failures/action', 'POST', { action: 'clear', ids: [] });
    toast(`已清空 ${res.count} 条记录`);
    pollFailures(true);
  } catch (e) { toast(e.message, true); }
});

/* ------------------------------------------------------- 同步预览与执行 */
function fmtSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(2) + ' MB';
}

function renderPlanList() {
  const plan = state.plan;
  const list = $('#syncPlanList');
  if (!plan || !plan.ok) {
    list.innerHTML = `<div class="empty">${escapeHtml((plan && plan.error) || '读取失败')}</div>`;
    $('#btnConfirmSync').disabled = true;
    return;
  }
  const onlyCopy = $('#planOnlyCopy').checked;
  const items = (plan.items || []).filter((it) => !onlyCopy || it.action === 'copy');
  if (!items.length) {
    list.innerHTML = '<div class="empty">没有可显示的文件</div>';
  } else {
    list.innerHTML = items.map((it) => {
      const cls = it.action === 'copy' ? 'plan-copy' : 'plan-skip';
      const tag = it.action === 'copy' ? '复制' : '跳过';
      return `<div class="plan-item">
        <span class="plan-tag ${cls}">${tag}</span>
        <span class="plan-path" title="${escapeHtml(it.rel)}">${escapeHtml(it.rel)}</span>
        <span class="plan-size">${fmtSize(it.size)}</span>
      </div>`;
    }).join('');
  }
  $('#btnConfirmSync').disabled = !plan.copied;
}

function openSyncModal() {
  const mask = $('#syncModalMask');
  mask.hidden = false;
  requestAnimationFrame(() => mask.classList.add('show'));
  $('#syncPlanSummary').textContent = '正在对比两边目录…';
  $('#syncPlanList').innerHTML = '<div class="empty">正在读取…</div>';
  $('#btnConfirmSync').disabled = true;
  $('#planOnlyCopy').checked = false;
  api('/api/sync/plan').then((plan) => {
    state.plan = plan;
    if (!plan.ok) {
      $('#syncPlanSummary').textContent = '❌ ' + (plan.error || '读取失败');
      renderPlanList();
      return;
    }
    if (plan.empty) {
      $('#syncPlanSummary').textContent = '两边内容一致，没有需要同步的文件';
      $('#syncPlanList').innerHTML = '<div class="empty">没有需要同步的文件</div>';
      return;
    }
    const miss = plan.missing_sources ? `（${plan.missing_sources} 个来源目录不可读）` : '';
    $('#syncPlanSummary').innerHTML =
      `将 <b class="plan-copy">复制 ${plan.copied}</b> 个文件，跳过已存在的 ${plan.skipped} 个${miss}` +
      (plan.truncated ? '，明细仅显示前 1000 条' : '');
    renderPlanList();
  }).catch((e) => {
    $('#syncPlanSummary').textContent = '❌ ' + e.message;
  });
}

function closeSyncModal() {
  const mask = $('#syncModalMask');
  mask.classList.remove('show');
  setTimeout(() => { mask.hidden = true; }, 180);
  state.plan = null;
}

$('#btnSyncNow').addEventListener('click', () => {
  if (!(state.cfg && state.cfg.sync && state.cfg.sync.enabled)) {
    toast('请先在下方「同步设置」中启用备份同步并保存', true);
    openSyncSettings();
    return;
  }
  openSyncModal();
});
$('#btnCloseSyncModal').addEventListener('click', closeSyncModal);
$('#btnCancelSync').addEventListener('click', closeSyncModal);
$('#syncModalMask').addEventListener('click', (e) => {
  if (e.target === $('#syncModalMask')) closeSyncModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#syncModalMask').hidden) closeSyncModal();
});
$('#planOnlyCopy').addEventListener('change', renderPlanList);

$('#btnConfirmSync').addEventListener('click', async () => {
  const btn = $('#btnConfirmSync');
  btn.disabled = true;
  try {
    const res = await api('/api/sync', 'POST', {});
    if (res.ok) {
      closeSyncModal();
      toast('备份同步已开始，可在同步日志查看进度');
      el.syncBadge.textContent = '同步中';
      el.syncBadge.className = 'badge ok';
      await pollLogs();
    } else {
      toast(res.error || '启动失败', true);
      btn.disabled = false;
    }
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
});

function openSyncSettings() {
  const block = $('#syncSettingsBlock');
  block.open = true;
  block.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

el.btnSyncSettings.addEventListener('click', openSyncSettings);

/* ------------------------------------------------------------------ 设置 */
function openDrawer() {
  el.drawer.classList.add('show');
  el.drawerMask.classList.add('show');
}
function closeDrawer() {
  el.drawer.classList.remove('show');
  el.drawerMask.classList.remove('show');
}

$('#btnOpenSettings').addEventListener('click', async () => {
  await loadConfig();
  openDrawer();
});
$('#btnCloseSettings').addEventListener('click', closeDrawer);
$('#btnCancelSettings').addEventListener('click', closeDrawer);
el.drawerMask.addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDrawer();
});

function fillConfig(cfg) {
  state.cfg = cfg;
  $('#cfgUrl').value = cfg.emby.url || '';
  $('#cfgToken').value = '';
  $('#cfgToken').placeholder = cfg.emby.token_set ? '已配置，留空则不修改' : '请填写 API Token';
  $('#cfgTimeout').value = cfg.emby.timeout;
  $('#cfgRoots').value = (cfg.scan.roots || []).join('\n');
  $('#cfgMappings').value = Object.entries(cfg.scan.path_mappings || {})
    .map(([a, b]) => `${a} = ${b}`)
    .join('\n');
  $('#cfgConcurrency').value = cfg.prewarm.concurrency;
  $('#cfgDelay').value = cfg.prewarm.delay_seconds;
  $('#cfgRetries').value = cfg.prewarm.max_retries;
  $('#cfgRetryDelay').value = cfg.prewarm.retry_delay_seconds;
  $('#cfgMaxItems').value = cfg.prewarm.max_items_per_run;
  $('#cfgVerify').checked = !!cfg.prewarm.verify;
  $('#cfgExportEnabled').checked = !!(cfg.export && cfg.export.enabled);
  $('#cfgExportOverwrite').checked = !!(cfg.export && cfg.export.overwrite);
  $('#cfgSchedEnabled').checked = !!cfg.schedule.enabled;
  $('#cfgSchedMode').value = cfg.schedule.mode;
  $('#cfgInterval').value = cfg.schedule.interval_minutes;
  state.dailyTimes = (cfg.schedule.daily_times && cfg.schedule.daily_times.length
    ? cfg.schedule.daily_times
    : [cfg.schedule.daily_time || '03:30']).slice();
  renderDailyChips();
  $('#cfgWatchEnabled').checked = !!(cfg.watch && cfg.watch.enabled);
  $('#cfgWatchPoll').value = (cfg.watch && cfg.watch.poll_seconds) || 300;
  $('#cfgHookEnabled').checked = !!(cfg.hook && cfg.hook.enabled);
  $('#cfgHookToken').value = (cfg.hook && cfg.hook.token) || '';
  $('#cfgLoopEnabled').checked = !!cfg.failure_loop.enabled;
  $('#cfgRounds').value = cfg.failure_loop.rounds;
  $('#cfgRoundDelay').value = cfg.failure_loop.round_delay_seconds;
  $('#cfgAutoRetry').checked = !!cfg.failure_loop.auto_retry;
  $('#cfgRetryInterval').value = cfg.failure_loop.auto_retry_interval_seconds;
  $('#cfgMaxAttempts').value = cfg.failure_loop.max_attempts;
  $('#cfgPort').value = cfg.server.port;
  $('#cfgPassword').value = '';
  $('#cfgPassword').placeholder = cfg.server.password_set ? '已设置，留空则不修改' : '不设置则免密码';
  $('#cfgWebhook').value = cfg.notify.webhook_url || '';
  $('#cfgPushplus').value = '';
  $('#cfgPushplus').placeholder = cfg.notify.pushplus_token_set
    ? '已配置，留空则不修改'
    : '在 pushplus.plus 获取 Token，留空不启用';
  $('#cfgSyncEnabled').checked = !!(cfg.sync && cfg.sync.enabled);
  $('#cfgSyncSource').value = ((cfg.sync && cfg.sync.source) || []).join('\n');
  $('#cfgSyncTarget').value = (cfg.sync && cfg.sync.target) || '';
  $('#cfgSyncImages').checked = !!(cfg.sync && cfg.sync.images);
  $('#cfgSyncSubs').checked = !!(cfg.sync && cfg.sync.subtitles);
  $('#cfgSyncAfter').checked = !!(cfg.sync && cfg.sync.after_prewarm);
  $('#cfgSyncSched').checked = !!(cfg.sync && cfg.sync.schedule_enabled);
  $('#cfgSyncTime').value = (cfg.sync && cfg.sync.schedule_time) || '04:30';
  syncScheduleFields();
}

function renderDailyChips() {
  const box = $('#dailyTimeChips');
  if (!state.dailyTimes.length) {
    box.innerHTML = '<span class="muted">尚未添加时刻</span>';
    return;
  }
  box.innerHTML = state.dailyTimes
    .map((t) => `<span class="chip">${escapeHtml(t)}<button type="button" data-time="${escapeHtml(t)}" title="移除">×</button></span>`)
    .join('');
}

function addDailyTime() {
  const value = ($('#cfgDailyTime').value || '').trim();
  if (!/^\d{2}:\d{2}$/.test(value)) {
    toast('请先选择一个时刻', true);
    return;
  }
  if (state.dailyTimes.includes(value)) {
    toast('该时刻已存在');
    return;
  }
  state.dailyTimes.push(value);
  state.dailyTimes.sort();
  renderDailyChips();
  $('#cfgDailyTime').value = '';
}

$('#btnAddDailyTime').addEventListener('click', addDailyTime);
$('#cfgDailyTime').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); addDailyTime(); }
});
$('#dailyTimeChips').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-time]');
  if (!btn) return;
  state.dailyTimes = state.dailyTimes.filter((t) => t !== btn.dataset.time);
  renderDailyChips();
});

function syncScheduleFields() {
  const daily = $('#cfgSchedMode').value === 'daily';
  $('#wrapInterval').classList.toggle('hidden', daily);
  $('#wrapDaily').classList.toggle('hidden', !daily);
}

$('#cfgSchedMode').addEventListener('change', syncScheduleFields);

function parseMappings(text) {
  const out = {};
  for (const line of text.split('\n')) {
    const raw = line.trim();
    if (!raw) continue;
    const idx = raw.indexOf('=');
    if (idx <= 0) continue;
    const left = raw.slice(0, idx).trim();
    const right = raw.slice(idx + 1).trim();
    if (left && right) out[left] = right;
  }
  return out;
}

function collectConfig() {
  const roots = $('#cfgRoots').value.split('\n').map((s) => s.trim()).filter(Boolean);
  return {
    emby: {
      url: $('#cfgUrl').value.trim(),
      token: $('#cfgToken').value.trim(),
      user_id: (state.cfg && state.cfg.emby.user_id) || '',
      timeout: Number($('#cfgTimeout').value) || 180,
    },
    scan: {
      roots,
      path_mappings: parseMappings($('#cfgMappings').value),
      only_strm: true,
      include_types: 'Movie,Episode,Video',
    },
    prewarm: {
      concurrency: Number($('#cfgConcurrency').value) || 2,
      delay_seconds: Number($('#cfgDelay').value) || 0,
      retry_delay_seconds: Number($('#cfgRetryDelay').value) || 0,
      max_retries: Number($('#cfgRetries').value) || 0,
      max_items_per_run: Number($('#cfgMaxItems').value) || 0,
      verify: $('#cfgVerify').checked,
    },
    export: {
      enabled: $('#cfgExportEnabled').checked,
      overwrite: $('#cfgExportOverwrite').checked,
    },
    schedule: {
      enabled: $('#cfgSchedEnabled').checked,
      mode: $('#cfgSchedMode').value,
      interval_minutes: Number($('#cfgInterval').value) || 180,
      daily_times: state.dailyTimes.length ? state.dailyTimes.slice() : ['03:30'],
      daily_time: state.dailyTimes[0] || '03:30',
    },
    watch: {
      enabled: $('#cfgWatchEnabled').checked,
      poll_seconds: Number($('#cfgWatchPoll').value) || 300,
    },
    hook: {
      enabled: $('#cfgHookEnabled').checked,
      token: $('#cfgHookToken').value.trim(),
    },
    failure_loop: {
      enabled: $('#cfgLoopEnabled').checked,
      rounds: Number($('#cfgRounds').value) || 0,
      round_delay_seconds: Number($('#cfgRoundDelay').value) || 0,
      auto_retry: $('#cfgAutoRetry').checked,
      auto_retry_interval_seconds: Number($('#cfgRetryInterval').value) || 600,
      max_attempts: Number($('#cfgMaxAttempts').value) || 0,
    },
    server: {
      host: (state.cfg && state.cfg.server.host) || '0.0.0.0',
      port: Number($('#cfgPort').value) || 8787,
      password: $('#cfgPassword').value,
    },
    notify: {
      webhook_url: $('#cfgWebhook').value.trim(),
      pushplus_token: $('#cfgPushplus').value.trim(),
    },
    sync: {
      enabled: $('#cfgSyncEnabled').checked,
      source: $('#cfgSyncSource').value.split('\n').map((s) => s.trim()).filter(Boolean),
      target: $('#cfgSyncTarget').value.trim(),
      images: $('#cfgSyncImages').checked,
      subtitles: $('#cfgSyncSubs').checked,
      after_prewarm: $('#cfgSyncAfter').checked,
      schedule_enabled: $('#cfgSyncSched').checked,
      schedule_time: $('#cfgSyncTime').value || '04:30',
    },
  };
}

async function loadConfig() {
  try {
    const data = await api('/api/config');
    fillConfig(data.config);
  } catch (e) {
    toast('读取配置失败：' + e.message, true);
  }
}

async function saveSettings() {
  const cfg = collectConfig();
  if (!cfg.emby.url) { toast('请先在「公共设置」中填写 Emby 地址', true); openDrawer(); return; }
  if (cfg.sync.enabled && !cfg.sync.target) { toast('启用备份同步需要填写目标目录 B', true); return; }
  try {
    await api('/api/config', 'POST', { config: cfg });
    toast('配置已保存');
    await loadConfig();
    await refresh();
    closeDrawer();
  } catch (e) {
    toast('保存失败：' + e.message, true);
  }
}

$('#btnSaveSettings').addEventListener('click', saveSettings);
document.querySelectorAll('.js-save-settings').forEach((btn) => {
  btn.addEventListener('click', saveSettings);
});

$('#btnTest').addEventListener('click', async () => {
  const btn = $('#btnTest');
  const out = $('#testResult');
  btn.disabled = true;
  out.className = 'test-result';
  out.textContent = '正在连接…';
  try {
    const cfg = collectConfig();
    const res = await api('/api/test', 'POST', { config: cfg });
    if (res.ok) {
      out.className = 'test-result ok';
      out.innerHTML =
        `✅ ${escapeHtml(res.product)} ${escapeHtml(res.version)}「${escapeHtml(res.server_name)}」<br>` +
        `API 前缀 ${escapeHtml(res.base_url)} · 用户 ${res.users} 个 · STRM ${res.strm_count} 个` +
        (res.truncated ? '（已截断统计）' : '');
    } else {
      out.className = 'test-result bad';
      out.textContent = '❌ ' + (res.error || '连接失败');
    }
  } catch (e) {
    out.className = 'test-result bad';
    out.textContent = '❌ ' + e.message;
  } finally {
    btn.disabled = false;
  }
});

$('#btnTestNotify').addEventListener('click', () => runNotifyTest('pushplus', $('#btnTestNotify'), $('#notifyTestResult')));

$('#btnTestWebhook').addEventListener('click', () => runNotifyTest('webhook', $('#btnTestWebhook'), $('#webhookTestResult')));

async function runNotifyTest(channel, btn, out) {
  const url = $('#cfgWebhook').value.trim();
  const token = $('#cfgPushplus').value.trim();
  if (channel === 'pushplus' && !token && !(state.cfg && state.cfg.notify.pushplus_token_set)) {
    out.className = 'test-result bad';
    out.textContent = '❌ 请先填写 PushPlus Token';
    return;
  }
  if (channel === 'webhook' && !url && !(state.cfg && state.cfg.notify.webhook_url)) {
    out.className = 'test-result bad';
    out.textContent = '❌ 请先填写 Webhook 地址';
    return;
  }
  btn.disabled = true;
  out.className = 'test-result';
  out.textContent = '正在发送…';
  try {
    const res = await api('/api/notify/test', 'POST', {
      channel,
      config: { notify: { pushplus_token: token, webhook_url: url } },
    });
    if (res.ok) {
      out.className = 'test-result ok';
      out.textContent = '✅ ' + (res.message || '测试通知已发送');
    } else {
      out.className = 'test-result bad';
      out.textContent = '❌ ' + (res.error || '发送失败');
    }
  } catch (e) {
    out.className = 'test-result bad';
    out.textContent = '❌ ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ 启动 */
(async function boot() {
  el.log.innerHTML = '<div class="empty">等待预热任务…</div>';
  el.syncLog.innerHTML = '<div class="empty">等待同步任务…</div>';
  switchPage(location.hash.slice(1) || 'prewarm', false);
  await loadConfig();
  await pollLogs();
  await pollFailures(true);
  loop();
  if (location.hash === '#settings') openDrawer();
})();

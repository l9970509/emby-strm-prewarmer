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
  strmBadge: $('#strmBadge'),
  btnStrmNow: $('#btnStrmNow'),
  btnStrmSettings: $('#btnStrmSettings'),
  btnStrmStop: $('#btnStrmStop'),
  strmLastText: $('#strmLastText'),
  strmReasonText: $('#strmReasonText'),
  strmCreated: $('#strmCreated'),
  strmUpdated: $('#strmUpdated'),
  strmSkipped: $('#strmSkipped'),
  strmDeleted: $('#strmDeleted'),
  kvStrmSource: $('#kvStrmSource'),
  kvStrmPrefix: $('#kvStrmPrefix'),
  kvStrmNext: $('#kvStrmNext'),
  kvStrmTasks: $('#kvStrmTasks'),
  kvTaskTotal: $('#kvTaskTotal'),
  kvTaskEnabled: $('#kvTaskEnabled'),
  kvTaskSched: $('#kvTaskSched'),
  kvTaskQueued: $('#kvTaskQueued'),
  strmTaskList: $('#strmTaskList'),
  strmTaskBadge: $('#strmTaskBadge'),
  strmLog: $('#strmLog'),
  autoScrollStrm: $('#autoScrollStrm'),
  drawer: $('#drawer'),
  drawerMask: $('#drawerMask'),
  toast: $('#toast'),
};

let state = {
  cfg: null,
  logSeq: { prewarm: 0, sync: 0, strm: 0 },
  logClearedAt: { prewarm: 0, sync: 0, strm: 0 },
  busy: false,
  failuresFetched: -1,
  dailyTimes: [],
  page: 'prewarm',
  historyLoadedAt: 0,
  plan: null,
  strmPlan: null,
  // STRM：任务列表在本地编辑，保存时整体提交
  strmTasks: [],
  strmStatus: {},
  taskTimes: [],
  editingTaskId: null,
  planTask: null,
  planMode: 'incremental',
  browsePath: '/',
};

/* ------------------------------------------------------------------ 页面路由 */
const PAGES = ['prewarm', 'strm', 'sync', 'history'];

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
  const boxes = { prewarm: el.log, sync: el.syncLog, strm: el.strmLog };
  for (const channel of ['prewarm', 'sync', 'strm']) {
    try {
      const data = await api(`/api/logs?since=${state.logSeq[channel]}&channel=${channel}`);
      if (data.lines && data.lines.length) {
        appendLogs(boxes[channel], data.lines, channel);
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

  // STRM 生成（生成页）
  const strm = s.strm || {};
  const lastStrm = strm.last || {};
  state.strmStatus = strm;
  if (strm.running) {
    el.strmBadge.textContent = strm.queued ? `生成中 · 排队 ${strm.queued}` : '生成中';
    el.strmBadge.className = 'badge ok';
  } else {
    el.strmBadge.textContent = lastStrm.ts ? (lastStrm.ok ? '正常' : '异常') : '未运行';
    el.strmBadge.className = 'badge' + (lastStrm.ts ? (lastStrm.ok ? ' ok' : ' has') : '');
  }
  el.btnStrmNow.disabled = false;
  el.btnStrmStop.disabled = !strm.running;
  el.strmLastText.textContent = lastStrm.ts_text || '—';
  el.strmReasonText.textContent = lastStrm.ts
    ? `触发方式：${lastStrm.reason || '—'}` +
      (lastStrm.error ? ` · ${lastStrm.error}` : ` · 用时 ${fmtDuration(lastStrm.duration) || '—'}`)
    : (lastStrm.error ? '上次生成出错：' + lastStrm.error : '尚未执行过生成');
  el.strmCreated.textContent = lastStrm.ts ? fmtNum(lastStrm.created || 0) : '—';
  el.strmUpdated.textContent = lastStrm.ts ? fmtNum(lastStrm.updated || 0) : '—';
  el.strmSkipped.textContent = lastStrm.ts ? fmtNum(lastStrm.skipped || 0) : '—';
  el.strmDeleted.textContent = lastStrm.ts ? fmtNum(lastStrm.deleted || 0) : '—';
  el.kvStrmNext.textContent = s.next_strm || '—';
  el.kvStrmNext.className = s.next_strm === '未开启' ? 'off' : 'on';
  if (state.cfg && state.cfg.strm) {
    el.kvStrmSource.textContent = state.cfg.strm.openlist_url || '—';
    el.kvStrmSource.title = state.cfg.strm.openlist_url || '';
    el.kvStrmPrefix.textContent = state.cfg.strm.prefix || '(未设置)';
    el.kvStrmPrefix.title = state.cfg.strm.prefix || '';
  }
  updateStrmTaskStatus();

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
$('#btnClearStrmLog').addEventListener('click', () => {
  state.logClearedAt.strm = state.logSeq.strm;
  el.strmLog.innerHTML = '<div class="empty">日志已清空（仅清空显示）</div>';
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
  if (e.key !== 'Escape') return;
  if (!$('#browseModalMask').hidden) { closeBrowse(); return; }
  if (!$('#taskModalMask').hidden) { closeTaskModal(); return; }
  if (!$('#syncModalMask').hidden) closeSyncModal();
  if (!$('#strmModalMask').hidden) closeStrmModal();
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

/* ------------------------------------------------------- STRM 生成页 */
const STRM_ACTION_LABEL = {
  create: '新增',
  update: '更新',
  skip: '跳过',
  delete: '删除',
  extra: '附件',
};
const STRM_MODE_LABEL = { incremental: '增量', full: '全量' };

function normalizeStrmTask(t) {
  const remote = String((t && t.remote) || '').trim();
  const fallbackName = remote.replace(/\/+$/, '').split('/').pop() || '任务';
  return {
    id: String((t && t.id) || ''),
    name: String((t && t.name) || fallbackName),
    remote,
    local: String((t && t.local) || '').trim().replace(/\/+$/, ''),
    enabled: !(t && t.enabled === false),
    mode: t && t.mode === 'full' ? 'full' : 'incremental',
    rebuild: !!(t && t.rebuild),
    schedule_enabled: !!(t && t.schedule_enabled),
    schedule_times: Array.isArray(t && t.schedule_times) ? t.schedule_times.slice().sort() : [],
  };
}

function showMask(selector) {
  const mask = $(selector);
  if (!mask) return;
  mask.hidden = false;
  requestAnimationFrame(() => mask.classList.add('show'));
}

function hideMask(selector) {
  const mask = $(selector);
  if (!mask) return;
  mask.classList.remove('show');
  setTimeout(() => { mask.hidden = true; }, 180);
}

function setSeg(id, value) {
  const box = document.getElementById(id);
  if (!box) return;
  box.querySelectorAll('button[data-mode]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.mode === value);
  });
}

function segValue(id) {
  const box = document.getElementById(id);
  const active = box && box.querySelector('button[data-mode].active');
  return active ? active.dataset.mode : 'incremental';
}

function taskLastText(st) {
  if (!st || !st.ts) return '尚未生成过';
  let text = `${st.ts_text || ''} · ${STRM_MODE_LABEL[st.mode] || '增量'} · ` +
    `新增 ${st.created || 0} / 更新 ${st.updated || 0} / 跳过 ${st.skipped || 0}`;
  if (st.deleted) text += ` / 删除 ${st.deleted}`;
  if (st.failed) text += ` / 失败 ${st.failed}`;
  return text;
}

function renderStrmTasks() {
  const tasks = state.strmTasks || [];
  const enabled = tasks.filter((t) => t.enabled);
  const scheduled = enabled.filter((t) => t.schedule_enabled && (t.schedule_times || []).length);
  el.strmTaskBadge.textContent = tasks.length;
  el.kvTaskTotal.textContent = tasks.length;
  el.kvTaskEnabled.textContent = enabled.length;
  el.kvTaskSched.textContent = scheduled.length;
  el.kvStrmTasks.textContent = `${enabled.length} / ${tasks.length}`;

  if (!tasks.length) {
    el.strmTaskList.innerHTML = '<div class="empty">尚未配置任务，点右上角「添加目录」</div>';
    return;
  }
  const st = state.strmStatus || {};
  const states = st.task_state || {};
  const nextMap = st.next_map || {};
  el.strmTaskList.innerHTML = tasks.map((t) => {
    const info = states[t.id] || {};
    const chips = [
      `<span class="pill ${t.mode === 'full' ? 'pill-full' : 'pill-inc'}">默认${STRM_MODE_LABEL[t.mode]}</span>`,
    ];
    if (t.schedule_enabled && (t.schedule_times || []).length) {
      chips.push(`<span class="pill pill-sched">每天 ${escapeHtml(t.schedule_times.join(' / '))}</span>`);
    } else {
      chips.push('<span class="pill pill-off">未定时</span>');
    }
    if (t.rebuild) chips.push('<span class="pill pill-warn">清理残留</span>');
    if (!t.enabled) chips.push('<span class="pill pill-off">已停用</span>');
    const next = nextMap[t.id] ? `下次 ${nextMap[t.id]}` : '';
    return `<article class="strm-task-card${t.enabled ? '' : ' is-off'}" data-task="${escapeHtml(t.id)}">
      <div class="stc-head">
        <div class="stc-title">
          <b title="${escapeHtml(t.name)}">${escapeHtml(t.name || '未命名')}</b>
          ${chips.join('')}
        </div>
        <label class="switch" title="启用 / 停用此任务"><input type="checkbox" class="js-task-enabled"${t.enabled ? ' checked' : ''}><i></i></label>
      </div>
      <div class="stc-paths">
        <div><span>网盘</span><code title="${escapeHtml(t.remote)}">${escapeHtml(t.remote)}</code></div>
        <div><span>输出</span><code title="根目录 ${escapeHtml(t.local)}">${escapeHtml(taskOutputDir(t))}</code></div>
      </div>
      <div class="stc-meta" data-task-meta="${escapeHtml(t.id)}">
        <span class="stc-last">${escapeHtml(taskLastText(info))}</span>
        <span class="stc-next">${escapeHtml(next)}</span>
      </div>
      <div class="stc-ops">
        <button class="btn sm primary js-task-run" data-mode="incremental" type="button" title="对比本地目标目录：已有 strm 就跳过，缺的才生成">增量生成</button>
        <button class="btn sm js-task-run" data-mode="full" type="button" title="不看是否已存在，整目录重写一遍（改前缀后用）">全量生成</button>
        <button class="btn sm ghost js-task-edit" type="button">编辑</button>
        <button class="btn sm ghost danger js-task-del" type="button">删除</button>
      </div>
    </article>`;
  }).join('');
}

/* 本地根目录 + 网盘完整路径 = 实际落盘目录 */
function taskOutputDir(task) {
  const root = String((task && task.local) || '').replace(/\/+$/, '');
  const remote = String((task && task.remote) || '').replace(/^\/+/, '');
  return remote ? `${root}/${remote}` : root;
}

function updateLocalPreview() {
  const root = ($('#taskLocal').value || '').trim().replace(/\/+$/, '');
  const remote = ($('#taskRemote').value || '').trim().replace(/^\/+/, '');
  const box = $('#taskLocalPreview');
  if (!box) return;
  if (!root) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $('#taskLocalPreviewPath').textContent = remote ? `${root}/${remote}` : root;
}

function updateStrmTaskStatus() {
  const st = state.strmStatus || {};
  const states = st.task_state || {};
  const nextMap = st.next_map || {};
  document.querySelectorAll('#strmTaskList [data-task-meta]').forEach((node) => {
    const id = node.dataset.taskMeta;
    const last = node.querySelector('.stc-last');
    const next = node.querySelector('.stc-next');
    if (last) last.textContent = taskLastText(states[id] || {});
    if (next) next.textContent = nextMap[id] ? `下次 ${nextMap[id]}` : '';
  });
  el.kvTaskQueued.textContent = st.queued || 0;
}

/* ---------------------------------------------------- 任务增删改（立即生效） */
async function persistTasks() {
  if (!state.cfg) throw new Error('配置尚未加载');
  // 只提交任务列表：整份配置里带着打码后的密钥，回传会覆盖真令牌
  const tasks = state.strmTasks.map((t) => ({ ...t }));
  const res = await api('/api/strm/tasks', 'POST', { tasks });
  if (!res.ok) throw new Error(res.error || '保存失败');
  state.cfg = res.config;
  state.strmTasks = (res.config.strm.tasks || []).map(normalizeStrmTask);
  renderStrmTasks();
}

function openTaskModal(taskId) {
  state.editingTaskId = taskId || null;
  const task = taskId ? state.strmTasks.find((t) => t.id === taskId) : null;
  $('#taskModalTitle').textContent = task ? '编辑生成目录' : '添加生成目录';
  $('#taskName').value = task ? task.name : '';
  $('#taskRemote').value = task ? task.remote : '';
  $('#taskLocal').value = task ? task.local : '';
  $('#taskEnabled').checked = task ? !!task.enabled : true;
  $('#taskRebuild').checked = task ? !!task.rebuild : false;
  $('#taskSched').checked = task ? !!task.schedule_enabled : false;
  state.taskTimes = task ? (task.schedule_times || []).slice() : [];
  setSeg('taskMode', task ? task.mode : 'incremental');
  $('#taskTime').value = '';
  renderTaskTimes();
  updateLocalPreview();
  showMask('#taskModalMask');
}

function closeTaskModal() {
  hideMask('#taskModalMask');
  state.editingTaskId = null;
}

function renderTaskTimes() {
  const box = $('#taskTimeChips');
  if (!state.taskTimes.length) {
    box.innerHTML = '<span class="muted">尚未添加时刻</span>';
    return;
  }
  box.innerHTML = state.taskTimes
    .map((t) => `<span class="chip">${escapeHtml(t)}<button type="button" data-time="${escapeHtml(t)}" title="移除">×</button></span>`)
    .join('');
}

function addTaskTime() {
  const value = ($('#taskTime').value || '').trim();
  if (!/^\d{2}:\d{2}$/.test(value)) {
    toast('请先选择一个时刻', true);
    return;
  }
  if (state.taskTimes.includes(value)) {
    toast('该时刻已存在');
    return;
  }
  state.taskTimes.push(value);
  state.taskTimes.sort();
  renderTaskTimes();
  $('#taskTime').value = '';
}

async function saveTaskModal() {
  const remote = $('#taskRemote').value.trim();
  const local = $('#taskLocal').value.trim();
  if (!remote || !remote.startsWith('/')) {
    toast('网盘目录要以 / 开头，可点「浏览…」选择', true);
    return;
  }
  if (!local) {
    toast('请填写本地目录', true);
    return;
  }
  const schedOn = $('#taskSched').checked;
  if (schedOn && !state.taskTimes.length) {
    toast('已开启定时，请至少添加一个时刻', true);
    return;
  }
  const normalizedRemote = remote.replace(/\/+$/, '') || '/';
  const duplicate = state.strmTasks.find(
    (t) => t.id !== state.editingTaskId && t.remote === normalizedRemote && t.local === local,
  );
  if (duplicate) {
    toast('已存在相同的网盘目录与本地目录任务', true);
    return;
  }
  const task = {
    id: state.editingTaskId || '',
    name: $('#taskName').value.trim() || normalizedRemote.split('/').pop() || '任务',
    remote: normalizedRemote,
    local,
    enabled: $('#taskEnabled').checked,
    mode: segValue('taskMode'),
    rebuild: $('#taskRebuild').checked,
    schedule_enabled: schedOn,
    schedule_times: state.taskTimes.slice(),
  };
  if (state.editingTaskId) {
    const idx = state.strmTasks.findIndex((t) => t.id === state.editingTaskId);
    if (idx >= 0) state.strmTasks[idx] = task;
    else state.strmTasks.push(task);
  } else {
    state.strmTasks.push(task);
  }
  const btn = $('#btnSaveTask');
  btn.disabled = true;
  try {
    await persistTasks();
    closeTaskModal();
    toast('任务已保存');
    refresh();
  } catch (e) {
    toast('保存失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function deleteTask(taskId) {
  const task = state.strmTasks.find((t) => t.id === taskId);
  if (!task) return;
  if (!window.confirm(`删除任务「${task.name}」？\n只删除任务配置，不会删除本地已生成的 strm 文件。`)) return;
  state.strmTasks = state.strmTasks.filter((t) => t.id !== taskId);
  try {
    await persistTasks();
    toast('任务已删除');
    refresh();
  } catch (e) {
    toast('删除失败：' + e.message, true);
    await loadConfig();
  }
}

el.strmTaskList.addEventListener('click', (e) => {
  const card = e.target.closest('[data-task]');
  if (!card) return;
  const taskId = card.dataset.task;
  const runBtn = e.target.closest('.js-task-run');
  if (runBtn) {
    openStrmModal(taskId, runBtn.dataset.mode);
    return;
  }
  if (e.target.closest('.js-task-edit')) {
    openTaskModal(taskId);
    return;
  }
  if (e.target.closest('.js-task-del')) {
    deleteTask(taskId);
  }
});

el.strmTaskList.addEventListener('change', async (e) => {
  if (!e.target.classList.contains('js-task-enabled')) return;
  const card = e.target.closest('[data-task]');
  const task = state.strmTasks.find((t) => t.id === card.dataset.task);
  if (!task) return;
  const before = task.enabled;
  task.enabled = e.target.checked;
  try {
    await persistTasks();
    toast(task.enabled ? '任务已启用' : '任务已停用');
  } catch (err) {
    task.enabled = before;
    renderStrmTasks();
    toast('保存失败：' + err.message, true);
  }
});

$('#btnAddStrmTask').addEventListener('click', () => openTaskModal(null));
$('#btnCloseTaskModal').addEventListener('click', closeTaskModal);
$('#btnCancelTask').addEventListener('click', closeTaskModal);
$('#taskModalMask').addEventListener('click', (e) => {
  if (e.target === $('#taskModalMask')) closeTaskModal();
});
$('#taskRemote').addEventListener('input', updateLocalPreview);
$('#taskLocal').addEventListener('input', updateLocalPreview);
$('#btnSaveTask').addEventListener('click', saveTaskModal);
$('#btnAddTaskTime').addEventListener('click', addTaskTime);
$('#taskTime').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); addTaskTime(); }
});
$('#taskTimeChips').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-time]');
  if (!btn) return;
  state.taskTimes = state.taskTimes.filter((t) => t !== btn.dataset.time);
  renderTaskTimes();
});
$('#taskMode').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-mode]');
  if (!btn) return;
  setSeg('taskMode', btn.dataset.mode);
});

/* ---------------------------------------------------------- 目录浏览 */
async function loadBrowse(path) {
  const list = $('#browseList');
  list.innerHTML = '<div class="empty">正在读取…</div>';
  $('#browseHint').textContent = '';
  try {
    const data = await api('/api/strm/browse?path=' + encodeURIComponent(path || '/'));
    if (!data.ok) {
      list.innerHTML = `<div class="empty">${escapeHtml(data.error || '读取失败')}</div>`;
      return;
    }
    state.browsePath = data.path;
    $('#browsePath').textContent = data.path;
    const rows = [];
    if (data.path !== '/') {
      rows.push(`<button class="browse-item" type="button" data-path="${escapeHtml(data.parent)}">
        <span class="bi-ic">↰</span><span class="bi-name">返回上级</span></button>`);
    }
    for (const dir of data.dirs || []) {
      rows.push(`<button class="browse-item" type="button" data-path="${escapeHtml(dir.path)}">
        <span class="bi-ic">📁</span><span class="bi-name">${escapeHtml(dir.name)}</span><span class="bi-go">›</span></button>`);
    }
    list.innerHTML = rows.length ? rows.join('') : '<div class="empty">该目录下没有子目录</div>';
    $('#browseHint').textContent = `当前目录还有 ${data.file_count || 0} 个文件`;
  } catch (e) {
    list.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

function openBrowse() {
  state.browsePath = ($('#taskRemote').value || '').trim() || '/';
  $('#browsePath').textContent = state.browsePath;
  showMask('#browseModalMask');
  loadBrowse(state.browsePath);
}

function closeBrowse() {
  hideMask('#browseModalMask');
}

$('#btnBrowseRemote').addEventListener('click', openBrowse);
$('#btnCloseBrowse').addEventListener('click', closeBrowse);
$('#btnBrowseCancel').addEventListener('click', closeBrowse);
$('#browseModalMask').addEventListener('click', (e) => {
  if (e.target === $('#browseModalMask')) closeBrowse();
});
$('#browseList').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-path]');
  if (!btn) return;
  loadBrowse(btn.dataset.path);
});
$('#btnBrowsePick').addEventListener('click', () => {
  $('#taskRemote').value = state.browsePath;
  closeBrowse();
  const nameInput = $('#taskName');
  if (!nameInput.value.trim()) {
    nameInput.value = state.browsePath.replace(/\/+$/, '').split('/').pop() || '';
  }
});

/* ---------------------------------------------------------- 生成预览 */
function renderStrmPlanList() {
  const plan = state.strmPlan;
  const list = $('#strmPlanList');
  if (!plan || !plan.ok) {
    list.innerHTML = `<div class="empty">${escapeHtml((plan && plan.error) || '读取失败')}</div>`;
    $('#btnConfirmStrm').disabled = true;
    return;
  }
  const onlyChanged = $('#strmOnlyChanged').checked;
  const items = (plan.items || []).filter((it) => !onlyChanged || it.action !== 'skip');
  if (!items.length) {
    list.innerHTML = '<div class="empty">没有可显示的文件</div>';
  } else {
    list.innerHTML = items.map((it) => {
      const tag = STRM_ACTION_LABEL[it.action] || it.action;
      const cls = it.action === 'create' ? 'plan-copy' : it.action === 'delete' ? 'plan-del' : 'plan-skip';
      const diff = it.action === 'update'
        ? `<div class="plan-diff">
             <span>本地现有</span><code title="${escapeHtml(it.prev || '(空文件)')}">${escapeHtml(it.prev || '(空文件)')}</code>
             <span>本次写入</span><code title="${escapeHtml(it.next || '')}">${escapeHtml(it.next || '')}</code>
           </div>`
        : '';
      return `<div class="plan-item ${diff ? 'has-diff' : ''}">
        <span class="plan-tag ${cls}">${tag}</span>
        <span class="plan-path" title="${escapeHtml(it.remote || it.rel)}">${escapeHtml(it.rel)}</span>
        <span class="plan-size">${fmtSize(it.size)}</span>
        ${diff}
      </div>`;
    }).join('');
  }
  const changed = (plan.create || 0) + (plan.update || 0) + (plan.delete || 0) + (plan.extra || 0);
  $('#btnConfirmStrm').disabled = changed === 0;
}

async function loadStrmPlan() {
  $('#strmPlanSummary').textContent = '正在读取网盘目录，大库可能需要一会儿…';
  $('#strmPlanList').innerHTML = '<div class="empty">正在读取…</div>';
  $('#btnConfirmStrm').disabled = true;
  const params = new URLSearchParams();
  if (state.planTask) params.set('task', state.planTask);
  params.set('mode', state.planMode);
  try {
    const plan = await api('/api/strm/plan?' + params.toString());
    state.strmPlan = plan;
    if (!plan.ok) {
      $('#strmPlanSummary').textContent = '❌ ' + (plan.error || '读取失败');
      renderStrmPlanList();
      return;
    }
    if (plan.empty) {
      $('#strmPlanSummary').textContent = `${STRM_MODE_LABEL[state.planMode]}模式下网盘与本地一致，没有需要生成的文件`;
      $('#strmPlanList').innerHTML = '<div class="empty">没有需要生成的文件</div>';
      return;
    }
    const parts = [];
    if (plan.create) parts.push(`<b class="plan-copy">新增 ${plan.create}</b>`);
    if (plan.update) parts.push(`更新 ${plan.update}`);
    if (plan.extra) parts.push(`附件 ${plan.extra}`);
    if (plan.delete) parts.push(`<b class="plan-del">删除 ${plan.delete}</b>`);
    if (plan.skipped) parts.push(`跳过 ${plan.skipped}`);
    let text = parts.join(' · ');
    if (plan.truncated) text += '，明细仅显示前 1000 条';
    if (plan.groups && plan.groups.length > 1) {
      text += '<div class="plan-groups">' + plan.groups.map((g) => {
        const c = g.counts || {};
        let line = `${escapeHtml(g.name)}：新增 ${c.create || 0} / 更新 ${c.update || 0} / 跳过 ${c.skip || 0}`;
        if (c.migrate) line += ` / 迁移 ${c.migrate}`;
        if (c.delete) line += ` / 删除 ${c.delete}`;
        return line;
      }).join('<br>') + '</div>';
    }
    if (plan.errors && plan.errors.length) {
      text += `<br><span class="muted">${escapeHtml(plan.errors[0])}</span>`;
    }
    $('#strmPlanSummary').innerHTML = text;
    renderStrmPlanList();
  } catch (e) {
    $('#strmPlanSummary').textContent = '❌ ' + e.message;
  }
}

function openStrmModal(taskId, mode) {
  state.planTask = taskId || null;
  state.planMode = mode === 'full' ? 'full' : 'incremental';
  setSeg('strmPlanMode', state.planMode);
  const task = taskId ? state.strmTasks.find((t) => t.id === taskId) : null;
  $('#strmModalTitle').textContent = task ? `生成预览 · ${task.name}` : '生成预览 · 全部启用任务';
  $('#strmPlanScope').textContent = task
    ? task.remote
    : `${state.strmTasks.filter((t) => t.enabled).length} 个启用任务`;
  $('#strmOnlyChanged').checked = true;
  showMask('#strmModalMask');
  loadStrmPlan();
}

function closeStrmModal() {
  hideMask('#strmModalMask');
  state.strmPlan = null;
}

function openStrmSettings() {
  if (state.page !== 'strm') switchPage('strm');
  const block = $('#strmSettingsBlock');
  block.open = true;
  block.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

el.btnStrmSettings.addEventListener('click', openStrmSettings);
$('#btnCloseStrmModal').addEventListener('click', closeStrmModal);
$('#btnCancelStrm').addEventListener('click', closeStrmModal);
$('#strmModalMask').addEventListener('click', (e) => {
  if (e.target === $('#strmModalMask')) closeStrmModal();
});
$('#strmOnlyChanged').addEventListener('change', renderStrmPlanList);
$('#strmPlanMode').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-mode]');
  if (!btn) return;
  state.planMode = btn.dataset.mode;
  setSeg('strmPlanMode', state.planMode);
  loadStrmPlan();
});

$('#btnStrmNow').addEventListener('click', () => {
  if (!state.strmTasks.some((t) => t.enabled)) {
    toast('请先添加并启用至少一个生成目录', true);
    openStrmSettings();
    return;
  }
  openStrmModal(null, 'incremental');
});

$('#btnConfirmStrm').addEventListener('click', async () => {
  const btn = $('#btnConfirmStrm');
  btn.disabled = true;
  try {
    const res = await api('/api/strm', 'POST', {
      task_id: state.planTask || '',
      mode: state.planMode,
    });
    if (res.ok) {
      closeStrmModal();
      toast(res.queued ? '已有生成任务在跑，本次已排队' : 'STRM 生成已开始，可在生成日志查看进度');
      el.strmBadge.textContent = res.queued ? '排队中' : '生成中';
      el.strmBadge.className = 'badge ok';
      await pollLogs();
      refresh();
    } else {
      toast(res.error || '启动失败', true);
      btn.disabled = false;
    }
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
});

$('#btnStrmStop').addEventListener('click', async () => {
  try {
    await api('/api/strm/stop', 'POST', {});
    toast('已发送停止指令');
  } catch (e) {
    toast(e.message, true);
  }
});

$('#btnTestOpenlist').addEventListener('click', async () => {
  const btn = $('#btnTestOpenlist');
  const out = $('#openlistTestResult');
  btn.disabled = true;
  out.className = 'test-result';
  out.textContent = '正在连接…';
  try {
    const res = await api('/api/strm/test', 'POST', {
      config: {
        strm: {
          openlist_url: $('#cfgOlUrl').value.trim(),
          openlist_token: $('#cfgOlToken').value.trim(),
          openlist_username: $('#cfgOlUser').value.trim(),
          openlist_password: $('#cfgOlPass').value,
          tasks: state.strmTasks.map((t) => ({ remote: t.remote })),
        },
      },
    });
    if (res.ok) {
      out.className = 'test-result ok';
      out.innerHTML =
        `✅ 已连接「${escapeHtml(res.user)}」<br>` +
        `${escapeHtml(res.path)} · 子目录 ${res.dirs} 个 · 文件 ${res.files} 个`;
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

/* 密钥被误存成打码值（********abcd）时给出明确提示 */
function setFieldWarn(id, text) {
  const node = document.getElementById(id);
  if (!node) return;
  node.hidden = !text;
  node.textContent = text ? '⚠️ ' + text : '';
}

function fillConfig(cfg) {
  state.cfg = cfg;
  $('#cfgUrl').value = cfg.emby.url || '';
  $('#cfgToken').value = '';
  $('#cfgToken').placeholder = cfg.emby.token_set ? '已配置，留空则不修改' : '请填写 API Token';
  setFieldWarn('embyTokenWarn', cfg.emby.token_corrupt
    ? '已保存的 API Token 是打码值，无法用于连接。请重新填写 Emby API 密钥并保存。'
    : '');
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
  setFieldWarn('pushplusWarn', cfg.notify.pushplus_token_corrupt
    ? '已保存的 PushPlus Token 是打码值，通知会发送失败。请重新填写并保存。'
    : '');
  $('#cfgSyncEnabled').checked = !!(cfg.sync && cfg.sync.enabled);
  $('#cfgSyncSource').value = ((cfg.sync && cfg.sync.source) || []).join('\n');
  $('#cfgSyncTarget').value = (cfg.sync && cfg.sync.target) || '';
  $('#cfgSyncImages').checked = !!(cfg.sync && cfg.sync.images);
  $('#cfgSyncSubs').checked = !!(cfg.sync && cfg.sync.subtitles);
  $('#cfgSyncAfter').checked = !!(cfg.sync && cfg.sync.after_prewarm);
  $('#cfgSyncSched').checked = !!(cfg.sync && cfg.sync.schedule_enabled);
  $('#cfgSyncTime').value = (cfg.sync && cfg.sync.schedule_time) || '04:30';
  syncScheduleFields();

  // STRM 生成
  const strm = cfg.strm || {};
  $('#cfgStrmEnabled').checked = !!strm.enabled;
  $('#cfgOlUrl').value = strm.openlist_url || '';
  $('#cfgOlToken').value = '';
  $('#cfgOlToken').placeholder = strm.openlist_token_set ? '已配置，留空则不修改' : '留空则用账号密码登录';
  $('#cfgOlUser').value = strm.openlist_username || '';
  $('#cfgOlPass').value = '';
  $('#cfgOlPass').placeholder = strm.openlist_password_set ? '已配置，留空则不修改' : '留空则不修改';
  setFieldWarn('olTokenWarn', (strm.openlist_token_corrupt || strm.openlist_password_corrupt)
    ? '已保存的 OpenList 令牌/密码是打码值，登录会报 token is invalidated。请在下方重新填写令牌（或账号密码）后保存。'
    : '');
  $('#cfgStrmPrefix').value = strm.prefix || '';
  $('#cfgStrmExts').value = strm.exts || '';
  $('#cfgStrmExtras').value = strm.extras || '';
  $('#cfgStrmMaxMb').value = strm.max_file_mb != null ? strm.max_file_mb : 200;
  state.strmTasks = (strm.tasks || []).map(normalizeStrmTask);
  renderStrmTasks();
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
    strm: {
      enabled: $('#cfgStrmEnabled').checked,
      openlist_url: $('#cfgOlUrl').value.trim(),
      openlist_token: $('#cfgOlToken').value.trim(),
      openlist_username: $('#cfgOlUser').value.trim(),
      openlist_password: $('#cfgOlPass').value,
      prefix: $('#cfgStrmPrefix').value.trim(),
      tasks: state.strmTasks.map((t) => ({ ...t })),
      exts: $('#cfgStrmExts').value.trim(),
      extras: $('#cfgStrmExtras').value.trim(),
      max_file_mb: Number($('#cfgStrmMaxMb').value) || 0,
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
  if (cfg.strm.enabled) {
    if (!cfg.strm.openlist_url) { toast('启用 STRM 生成需要填写 OpenList 地址', true); openStrmSettings(); return; }
    if (!cfg.strm.tasks.some((t) => t.enabled)) { toast('请至少添加并启用一个生成目录', true); openStrmSettings(); return; }
    if (!cfg.strm.prefix) { toast('请填写 STRM 前缀（OpenList /d 地址或 rclone 挂载根）', true); openStrmSettings(); return; }
  }
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
  el.strmLog.innerHTML = '<div class="empty">等待生成任务…</div>';
  switchPage(location.hash.slice(1) || 'prewarm', false);
  await loadConfig();
  await pollLogs();
  await pollFailures(true);
  loop();
  if (location.hash === '#settings') openDrawer();
})();

"use strict";
const $ = id => document.getElementById(id);
const base = window.PADLET_CONFIG.serverUrl;
let token = "", connected = false, state = null, logs = [], lastLog = 0;
let generation = 0, pollTimer = null, preparing = false, needsConnect = false;
let reconnectTimer = null;
let fieldsSignature = '';
let frameBusy = false, frameUrl = '', actionBusy = false;
try { token = sessionStorage.getItem('padlet-session') || ''; } catch (_) { /* Memory-only fallback. */ }
const phases = {waiting:"시작 대기",opening:"브라우저 준비 중",capturing:"브라우저에서 1건 게시 후 캡처",ready:"게시 요청 캡처 완료",sending:"전송 중",backoff:"요청 제한 · 대기 중",stopping:"중지 중 · 진행 중인 요청 완료 대기",stopped:"사용자 중지",finished:"전송 완료",error:"오류 확인 필요"};
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
async function api(path, data, blob = false) {
  const res = await fetch(base + path, {method:data === undefined ? 'GET':'POST',
    headers:{Authorization:`Bearer ${token}`, ...(data === undefined ? {}:{'Content-Type':'application/json'})},
    body:data === undefined ? undefined:JSON.stringify(data), signal:AbortSignal.timeout(path.endsWith('/open') || path.endsWith('/connect') ? 55000:20000), cache:'no-store'});
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    const error = new Error(typeof detail.detail === 'string' ? detail.detail : `요청 실패 (HTTP ${res.status})`);
    error.status = res.status;
    throw error;
  }
  return blob ? res.blob() : res.json();
}
function networkMessage(error) {
  return error.name === 'TimeoutError' || error instanceof TypeError
    ? '서버 연결이 지연되거나 끊겼습니다. 잠시 후 다시 연결해 주세요. 실행 중인 작업은 서버에서 계속될 수 있습니다.'
    : error.message;
}
function renderBoardFields(board) {
  const fields = board?.fields || {};
  const signature = JSON.stringify([board?.url, board?.capture_id, fields]);
  if (signature === fieldsSignature) return;
  fieldsSignature = signature;
  const required = fields.required || {};
  for (const id of ['subject', 'body', 'attachment']) {
    const enabled = Boolean(board) && fields[`${id}_enabled`] !== false;
    $(id).closest('label').hidden = id === 'attachment' ? !enabled : Boolean(board) && !enabled;
    $(id).required = enabled && Boolean(required[id]);
    $(id).closest('label').firstChild.textContent = ({subject:'제목',body:'본문',attachment:'첨부 링크'}[id])
      + ($(id).required ? ' (필수)' : '');
    if (id === 'attachment' || board && !enabled) $(id).value = '';
    if (board?.draft && enabled) $(id).value = board.draft[id] || '';
  }
  $('boardFields').replaceChildren();
  for (const field of fields.custom || []) {
    if (!field.supported) {
      if (field.required) {
        const hint = document.createElement('p'); hint.className = 'hint';
        hint.textContent = `필수 항목 ‘${field.name}’은 브라우저에서 게시한 값을 그대로 사용합니다.`;
        $('boardFields').append(hint);
      }
      continue;
    }
    const label = document.createElement('label');
    label.textContent = field.name + (field.required ? ' (필수)' : '');
    const input = document.createElement(field.type === 'single_select' ? 'select' : 'input');
    input.dataset.propertyId = field.id;
    input.dataset.propertyType = field.type;
    input.required = field.required;
    if (field.type === 'single_select') {
      input.add(new Option('선택하세요', ''));
      for (const option of field.options) input.add(new Option(option.name, option.id, option.default, option.default));
    } else {
      input.type = field.type === 'date' && field.show_time ? 'datetime-local'
        : ({number:'number',date:'date',email:'email',url:'url',phone_number:'tel'}[field.type] || 'text');
      if (field.type === 'number') input.step = 'any';
      input.maxLength = 20000;
    }
    const captured = board?.draft?.custom_properties?.[field.id];
    if (captured !== undefined && captured !== null) {
      input.value = input.type === 'date' ? String(captured).slice(0, 10)
        : input.type === 'datetime-local' ? localDateTime(captured) : String(captured);
    }
    label.append(input); $('boardFields').append(label);
  }
}
function localDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0,16);
}
function customProperties() {
  return Object.fromEntries([...$('boardFields').querySelectorAll('[data-property-id]')].map(input => {
    const value = input.value.trim();
    return [input.dataset.propertyId, !value ? null : input.dataset.propertyType === 'number' ? Number(value)
      : input.type === 'datetime-local' ? new Date(value).toISOString() : value];
  }));
}
function render(next) {
  state = next;
  renderBoardFields(next.board);
  $('sentCount').textContent = next.sent.toLocaleString();
  $('errorCount').textContent = next.errors.toLocaleString();
  $('phase').textContent = phases[next.phase] || next.phase;
  $('captureBadge').textContent = next.ready ? '● 성공 요청 캡처됨':'성공 요청 캡처 대기';
  $('captureBadge').classList.toggle('ready', next.ready);
  $('liveDot').classList.toggle('live', next.running);
  $('openBtn').disabled = !connected || next.running || next.opening || preparing;
  $('openBtn').textContent = next.opening || preparing ? '브라우저 준비 중…':'게시판 브라우저 열기 ↗';
  $('startBtn').disabled = !connected || !next.ready || next.running || next.opening || preparing || needsConnect;
  $('stopBtn').disabled = !connected || !next.running || next.phase === 'stopping';
  $('clearSession').disabled = !connected || next.opening || preparing;
  $('targetUrl').disabled = next.running || next.opening || preparing;
  for (const id of ['subject','body','attachment','limit','delay']) $(id).disabled = next.running;
  for (const input of $('boardFields').querySelectorAll('input,select')) input.disabled = next.running;
  $('boardInfo').hidden = !next.board;
  $('boardTitle').textContent = next.board?.title || '';
  const sections = next.board?.sections || [];
  const scope = sections.length ? `모든 ${sections.length}개 섹션에 각각 전송` : '섹션 없는 게시판 · 매 주기 1건 전송';
  $('boardDetail').textContent = `${scope}${next.board?.approval_required ? ' · 관리자 승인 필요' : ''}`;
  $('sectionNames').textContent = sections.map(section => section.title).join(' · ');
  if (next.board && !needsConnect && !$('targetUrl').value) $('targetUrl').value = next.board.url;
  $('actionHint').textContent = next.running ? '중지를 누르면 현재 요청이 끝난 후 멈춥니다.' : needsConnect ? '주소가 변경되었습니다. 게시판을 다시 여세요.' : next.ready ? `준비 완료. 매 주기 ${sections.length || 1}건을 게시합니다. 섹션 변경 후에는 다시 캡처하세요.` : next.browser ? '아래 브라우저에서 글을 1건 게시하면 성공한 요청을 캡처합니다.' : '게시판 주소를 입력하고 브라우저를 여세요.';
  $('browserPanel').hidden = !next.browser || next.ready || next.running;
  for (const control of $('browserPanel').querySelectorAll('button,input,select'))
    control.disabled = actionBusy || next.opening || next.ready || next.running || preparing;
  if ($('browserPanel').hidden && frameUrl) {
    URL.revokeObjectURL(frameUrl); frameUrl = ''; $('browserFrame').removeAttribute('src');
  }
  if (next.limit > 0) {
    $('progress').value = Math.min((next.rounds || 0) / next.limit * 100, 100);
    $('progressText').textContent = `${next.rounds || 0} / ${next.limit}회 · ${next.sent} / ${next.limit * (next.target_count || 1)}건`;
  } else {
    $('progress').value = 0;
    $('progressText').textContent = next.running ? '∞ 무제한':'—';
  }
  const incoming = next.logs || [];
  if (incoming.length && incoming[incoming.length - 1].id < lastLog) { lastLog = 0; $('log').replaceChildren(); }
  logs = incoming;
  const fresh = incoming.filter(row => row.id > lastLog);
  if (fresh.length) {
    if ($('log').querySelector('.empty-log')) $('log').replaceChildren();
    const scroll = $('log').scrollHeight - $('log').scrollTop - $('log').clientHeight < 70;
    for (const row of fresh) {
      const el = document.createElement('div'); el.className = `log-row ${row.level}`;
      const time = document.createElement('time'); time.textContent = new Date(row.time).toLocaleTimeString('ko-KR',{hour12:false});
      const message = document.createElement('span'); message.textContent = row.message;
      el.append(time, message); $('log').append(el); lastLog = row.id;
    }
    while ($('log').children.length > 300) $('log').firstChild.remove();
    if (scroll) $('log').scrollTop = $('log').scrollHeight;
  }
  $('logCount').textContent = `${incoming.length} EVENTS`;
}
async function poll(current) {
  if (!connected || current !== generation) return;
  try {
    const next = await api('/api/state');
    if (current !== generation) return;
    render(next); $('connectionBadge').textContent = '● 서버 연결됨'; $('connectionBadge').classList.add('connected');
    refreshFrame(current);
  } catch (error) {
    if (current !== generation) return;
    $('connectionBadge').textContent = '● 연결 재시도 중'; $('connectionBadge').classList.remove('connected');
    notice(networkMessage(error));
    $('openBtn').disabled = $('startBtn').disabled = true;
    if (error.status === 401) { forgetSession(); connect(); return; }
  }
  if (current === generation) pollTimer = setTimeout(() => poll(current), document.hidden ? 4000:1200);
}
function forgetSession() {
  token = '';
  try { sessionStorage.removeItem('padlet-session'); } catch (_) {}
}
async function connect() {
  clearTimeout(reconnectTimer);
  clearTimeout(pollTimer); generation++; connected = false;
  const current = generation;
  $('connectionBadge').textContent = '● 자동 연결 중';
  $('connectionBadge').classList.remove('connected');
  $('openBtn').disabled = $('startBtn').disabled = true;
  try {
    if (!token) {
      const session = await api('/api/session', {});
      token = session.token;
      try { sessionStorage.setItem('padlet-session', token); } catch (_) {}
      lastLog = 0; logs = []; $('log').replaceChildren();
    }
    const next = await api('/api/state');
    if (current !== generation) return;
    connected = true; notice();
    render(next); poll(generation);
  } catch (error) {
    if (current !== generation) return;
    if (error.status === 401) forgetSession();
    $('connectionBadge').textContent = '● 자동 재연결 중';
    $('actionHint').textContent = '서버가 준비되면 자동으로 연결됩니다.';
    notice(networkMessage(error));
    reconnectTimer = setTimeout(connect, error.status === 401 ? 100:5000);
  }
}
$('captureForm').addEventListener('submit', async event => {
  event.preventDefault(); notice(); preparing = true;
  if (state) render(state);
  try {
    const next = await api('/api/browser/open', {url:$('targetUrl').value.trim()});
    needsConnect = false;
    if (next.board) $('targetUrl').value = next.board.url;
    render(next);
    refreshFrame(generation);
    $('browserPanel').scrollIntoView({behavior:'smooth', block:'start'});
  } catch (error) { notice(networkMessage(error)); }
  finally {
    preparing = false;
    try { render(await api('/api/state')); } catch (_) { if (state) render(state); }
  }
});
$('targetUrl').addEventListener('input', () => { needsConnect = true; if (state) render(state); });
$('sendForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (needsConnect || preparing || !state?.ready || state.running) return;
  notice(); $('startBtn').disabled = true;
  try { render(await api('/api/start', {subject:$('subject').value,body:$('body').value,attachment:$('attachment').value,
    custom_properties:customProperties(),limit:Number($('limit').value),delay:Number($('delay').value)})); }
  catch (error) { notice(networkMessage(error)); if (state) render(state); }
});
$('stopBtn').onclick = async () => {
  $('stopBtn').disabled = true;
  try { render(await api('/api/stop', {})); } catch (error) { notice(networkMessage(error)); $('stopBtn').disabled = false; }
};
$('clearSession').onclick = async () => {
  try { render(await api('/api/reset', {})); notice(); }
  catch (error) { notice(networkMessage(error)); }
};
$('downloadLog').onclick = () => {
  const blob = new Blob([logs.map(row => `${row.time} [${row.level}] ${row.message}`).join('\n')],{type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href=url;anchor.download='padlet-session.log';anchor.click();setTimeout(() => URL.revokeObjectURL(url),1000);
};
window.addEventListener('pagehide', () => { generation++; clearTimeout(pollTimer); clearTimeout(reconnectTimer); });
window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
async function refreshFrame(current) {
  if (frameBusy || document.hidden || $('browserPanel').hidden || state?.opening || preparing) return;
  frameBusy = true;
  try {
    const blob = await api('/api/browser/frame', undefined, true);
    if (current !== generation || $('browserPanel').hidden || preparing) return;
    const nextUrl = URL.createObjectURL(blob);
    $('browserFrame').src = nextUrl;
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    frameUrl = nextUrl;
    $('frameStatus').textContent = '● 화면 연결됨';
    const tabs = await api('/api/browser/tabs');
    if (current !== generation) return;
    $('browserTabs').replaceChildren(...tabs.map(tab => new Option(`${tab.index + 1}. ${tab.host}`, tab.index, tab.active, tab.active)));
  } catch (_) { $('frameStatus').textContent = '화면 다시 불러오는 중'; }
  finally { frameBusy = false; }
}
async function browserAction(action) {
  if (actionBusy || !connected || !state?.browser || state.ready || state.running || state.opening || preparing) return;
  actionBusy = true;
  if (state) render(state);
  try {
    render(await api('/api/browser/action', action));
    await refreshFrame(generation);
  } catch (error) { notice(networkMessage(error)); }
  finally { actionBusy = false; if (state) render(state); }
}
$('browserFrame').addEventListener('click', event => {
  if (!frameUrl) return;
  const rect = event.currentTarget.getBoundingClientRect();
  browserAction({kind:'click', x:Math.max(0, Math.min(1100, (event.clientX - rect.left) * 1100 / rect.width)),
    y:Math.max(0, Math.min(720, (event.clientY - rect.top) * 720 / rect.height))});
});
$('browserFrame').addEventListener('wheel', event => {
  event.preventDefault();
  browserAction({kind:'scroll', delta:Math.round(Math.max(-1500, Math.min(1500, event.deltaY)))});
}, {passive:false});
$('browserFrame').addEventListener('keydown', event => {
  const keys = ['Enter','Tab','Backspace','Delete','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'];
  if (keys.includes(event.key) || event.key === ' ') {
    event.preventDefault();
    browserAction({kind:'key', key:event.key === ' ' ? 'Space' : event.key === 'Tab' && event.shiftKey ? 'Shift+Tab' : event.key});
  }
});
$('browserTextForm').addEventListener('submit', event => {
  event.preventDefault();
  if (actionBusy || !$('browserText').value) return;
  const value = $('browserText').value; $('browserText').value = '';
  browserAction({kind:'text', text:value});
});
document.querySelectorAll('[data-browser-key]').forEach(button =>
  button.addEventListener('click', () => browserAction({kind:'key', key:button.dataset.browserKey})));
$('browserReload').onclick = () => browserAction({kind:'reload'});
$('browserTabs').onchange = () => browserAction({kind:'tab', index:Number($('browserTabs').value)});
connect();

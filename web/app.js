"use strict";
const $ = id => document.getElementById(id);
const base = window.PADLET_CONFIG.serverUrl;
let token = '', connected = false, state = null, logs = [], lastLog = 0;
let generation = 0, pollTimer = null, reconnectTimer = null;
try { token = sessionStorage.getItem('padlet-session') || ''; } catch (_) {}
const phases = {waiting:'시작 대기',opening:'내 PC 브라우저 실행 중',capturing:'PC에서 로그인·게시 요청 대기',ready:'캡처 완료',sending:'전송 중',backoff:'요청 제한 · 대기 중',stopping:'중지 중 · 진행 중인 요청 완료 대기',stopped:'사용자 중지',finished:'전송 완료',error:'오류 확인 필요'};
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
async function api(path, data) {
  const res = await fetch(base + path, {method:data === undefined ? 'GET':'POST',
    headers:{Authorization:`Bearer ${token}`, ...(data === undefined ? {}:{'Content-Type':'application/json'})},
    body:data === undefined ? undefined:JSON.stringify(data), signal:AbortSignal.timeout(20000), cache:'no-store'});
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    const error = new Error(typeof detail.detail === 'string' ? detail.detail : `요청 실패 (HTTP ${res.status})`);
    error.status = res.status; throw error;
  }
  return res.json();
}
function networkMessage(error) {
  return error.name === 'TimeoutError' || error instanceof TypeError
    ? '서버에 다시 연결하고 있습니다. 실행 중인 전송은 서버에서 계속될 수 있습니다.' : error.message;
}
function render(next) {
  state = next;
  $('sentCount').textContent = next.sent.toLocaleString();
  $('errorCount').textContent = next.errors.toLocaleString();
  $('phase').textContent = phases[next.phase] || next.phase;
  $('captureBadge').textContent = next.ready ? '● 캡처 완료':'캡처 대기';
  $('captureBadge').classList.toggle('ready', next.ready);
  $('liveDot').classList.toggle('live', next.running);
  $('pcStatus').textContent = next.pc_connected ? '● PC 연결됨':'도우미 미연결';
  $('pcStatus').classList.toggle('ready', next.pc_connected);
  $('pcInstructions').hidden = Boolean(next.pc_connected);
  $('pcHint').hidden = !next.pc_connected;
  $('pcHint').textContent = next.pc_active ? 'PC에 열린 브라우저에서 로그인하고 글을 한 번 게시하세요.' : '도우미가 연결되었습니다. 아래 버튼으로 브라우저를 여세요.';
  if (next.pc_connected) { $('pairCode').textContent = ''; $('pairBox').hidden = true; }
  $('pairBtn').disabled = !connected;
  $('openBtn').disabled = !connected || !next.pc_connected || next.running || next.opening;
  $('openBtn').textContent = next.opening ? '내 PC 브라우저 실행 중…':'▣ 내 PC 브라우저 열기 ↗';
  $('startBtn').disabled = !connected || !next.ready || next.running || next.opening;
  $('stopBtn').disabled = !connected || !next.running || next.phase === 'stopping';
  $('clearSession').disabled = !connected;
  $('targetUrl').disabled = next.running || next.opening;
  for (const id of ['subject','body','limit','delay']) $(id).disabled = next.running;
  $('actionHint').textContent = next.running ? '전송은 Render에서 실행됩니다. PC 도우미를 닫아도 계속됩니다.' : next.ready ? '캡처 완료. 제목과 본문을 입력하고 전송을 시작하세요.' : next.pc_active ? 'PC 브라우저에서 글을 한 번 게시하면 자동으로 캡처됩니다.' : 'PC 도우미를 연결한 뒤 게시판을 여세요.';
  $('progress').value = next.limit > 0 ? Math.min(next.sent / next.limit * 100, 100):0;
  $('progressText').textContent = next.limit > 0 ? `${next.sent} / ${next.limit}`:next.running ? '∞ 무제한':'—';
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
function forgetSession() {
  token = ''; $('pairCode').textContent = ''; $('pairBox').hidden = true;
  try { sessionStorage.removeItem('padlet-session'); } catch (_) {}
}
async function poll(current) {
  if (!connected || current !== generation) return;
  try {
    const next = await api('/api/state');
    if (current !== generation) return;
    render(next); $('connectionBadge').textContent = '● 서버 연결됨'; $('connectionBadge').classList.add('connected');
  } catch (error) {
    if (current !== generation) return;
    $('connectionBadge').textContent = '● 연결 재시도 중'; $('connectionBadge').classList.remove('connected');
    notice(networkMessage(error)); $('openBtn').disabled = $('startBtn').disabled = true;
    if (error.status === 401) { forgetSession(); connect(); return; }
  }
  if (current === generation) pollTimer = setTimeout(() => poll(current), document.hidden ? 4000:1200);
}
async function connect() {
  clearTimeout(reconnectTimer); clearTimeout(pollTimer); generation++; connected = false;
  const current = generation;
  $('connectionBadge').textContent = '● 자동 연결 중'; $('connectionBadge').classList.remove('connected');
  $('openBtn').disabled = $('startBtn').disabled = $('pairBtn').disabled = true;
  try {
    if (!token) {
      const session = await api('/api/session', {}); token = session.token;
      try { sessionStorage.setItem('padlet-session', token); } catch (_) {}
      lastLog = 0; logs = []; $('log').replaceChildren();
    }
    const next = await api('/api/state');
    if (current !== generation) return;
    connected = true; notice(); render(next); poll(generation);
  } catch (error) {
    if (current !== generation) return;
    if (error.status === 401) forgetSession();
    $('connectionBadge').textContent = '● 자동 재연결 중';
    notice(networkMessage(error)); reconnectTimer = setTimeout(connect, error.status === 401 ? 100:5000);
  }
}
$('pairBtn').onclick = async () => {
  $('pairBtn').disabled = true;
  try { const pairing = await api('/api/pc/pair', {}); $('pairCode').textContent = pairing.code; $('pairBox').hidden = false; $('copyCode').textContent = '복사'; notice(); }
  catch (error) { notice(networkMessage(error)); }
  finally { $('pairBtn').disabled = !connected; }
};
$('copyCode').onclick = async () => {
  try { await navigator.clipboard.writeText($('pairCode').textContent); $('copyCode').textContent = '복사됨'; }
  catch (_) { notice('표시된 연결 코드를 선택해서 복사하세요.'); }
};
$('captureForm').addEventListener('submit', async event => {
  event.preventDefault(); notice(); $('openBtn').disabled = true;
  try { render(await api('/api/pc/open', {url:$('targetUrl').value.trim()})); }
  catch (error) { notice(networkMessage(error)); }
  finally { if (state) render(state); }
});
$('sendForm').addEventListener('submit', async event => {
  event.preventDefault(); notice(); $('startBtn').disabled = true;
  try { render(await api('/api/start', {subject:$('subject').value,body:$('body').value,limit:Number($('limit').value),delay:Number($('delay').value)})); }
  catch (error) { notice(networkMessage(error)); if (state) render(state); }
});
$('stopBtn').onclick = async () => {
  $('stopBtn').disabled = true;
  try { render(await api('/api/stop', {})); } catch (error) { notice(networkMessage(error)); $('stopBtn').disabled = false; }
};
$('clearSession').onclick = async () => {
  try { render(await api('/api/reset', {})); notice(); } catch (error) { notice(networkMessage(error)); }
};
$('downloadLog').onclick = () => {
  const blob = new Blob([logs.map(row => `${row.time} [${row.level}] ${row.message}`).join('\n')], {type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href=url; anchor.download='padlet-session.log'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
};
window.addEventListener('pagehide', () => { generation++; clearTimeout(pollTimer); clearTimeout(reconnectTimer); });
window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
connect();

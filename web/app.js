"use strict";
const $ = id => document.getElementById(id);
let base = "", token = "", connected = false, state = null, logs = [], lastLog = 0;
let generation = 0, imageUrl = null, pollTimer = null, frameBusy = false, actionQueue = Promise.resolve();
const phases = {waiting:"시작 대기",opening:"브라우저 실행 중",capturing:"로그인·게시 요청 대기",ready:"캡처 완료",sending:"전송 중",backoff:"요청 제한 · 대기 중",stopping:"중지 중 · 진행 중인 요청 완료 대기",stopped:"사용자 중지",finished:"전송 완료",error:"오류 확인 필요"};
$('serverUrl').value = window.PADLET_CONFIG?.serverUrl || localStorage.getItem('padlet-server') || '';
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function serverAddress(raw) {
  const url = new URL(raw);
  const local = ['localhost','127.0.0.1'].includes(url.hostname);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
      !(url.protocol === 'https:' && url.hostname.endsWith('.onrender.com') || local && url.protocol === 'http:')) {
    throw new Error('Render의 https://…onrender.com 서버 주소를 입력하세요.');
  }
  return url.origin;
}
async function api(path, data, asBlob = false) {
  const res = await fetch(base + path, {method:data === undefined ? 'GET':'POST',
    headers:{Authorization:`Bearer ${token}`, ...(data === undefined ? {}:{'Content-Type':'application/json'})},
    body:data === undefined ? undefined:JSON.stringify(data), signal:AbortSignal.timeout(path.endsWith('/open') ? 90000:20000), cache:'no-store'});
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(typeof detail.detail === 'string' ? detail.detail : `요청 실패 (HTTP ${res.status})`);
  }
  return asBlob ? res.blob() : res.json();
}
function networkMessage(error) {
  return error.name === 'TimeoutError' || error instanceof TypeError
    ? '서버 연결이 지연되거나 끊겼습니다. Render 서버 상태·주소를 확인하세요. 실행 중인 작업은 서버에서 계속될 수 있습니다.'
    : error.message;
}
function render(next) {
  state = next;
  $('sentCount').textContent = next.sent.toLocaleString();
  $('errorCount').textContent = next.errors.toLocaleString();
  $('phase').textContent = phases[next.phase] || next.phase;
  $('captureBadge').textContent = next.ready ? '● 캡처 완료':'캡처 대기';
  $('captureBadge').classList.toggle('ready', next.ready);
  $('liveDot').classList.toggle('live', next.running);
  $('openBtn').disabled = !connected || next.running || next.opening;
  $('openBtn').textContent = next.opening ? '브라우저 실행 중…':'▣ 브라우저 열기 & 로그인 ↗';
  $('startBtn').disabled = !connected || !next.ready || next.running || next.opening;
  $('stopBtn').disabled = !connected || !next.running || next.phase === 'stopping';
  $('clearSession').disabled = !connected || next.opening;
  $('resetBtn').disabled = next.opening;
  $('serverUrl').disabled = next.running || next.opening;
  $('connectBtn').disabled = next.running || next.opening;
  $('targetUrl').disabled = next.running || next.opening;
  for (const id of ['subject','body','limit','delay']) $(id).disabled = next.running;
  $('browserSection').hidden = !next.browser && !next.opening;
  $('actionHint').textContent = next.running ? '중지를 누르면 현재 요청이 끝난 후 멈춥니다.' : next.ready ? '캡처 완료. 제목과 본문을 입력하고 전송을 시작하세요.' : next.browser ? '아래 브라우저에서 로그인하고 글을 한 번 게시하세요.' : '브라우저를 열고 게시 요청을 캡처하세요.';
  if (next.limit > 0) {
    $('progress').value = Math.min(next.sent / next.limit * 100, 100);
    $('progressText').textContent = `${next.sent} / ${next.limit}`;
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
async function frame() {
  if (frameBusy || !state?.browser) return;
  frameBusy = true;
  const current = generation;
  try {
    const blob = await api('/api/browser/frame', undefined, true);
    if (current !== generation || !state?.browser) return;
    const old = imageUrl; imageUrl = URL.createObjectURL(blob); $('screen').src = imageUrl;
    $('screenPlaceholder').hidden = true; if (old) URL.revokeObjectURL(old);
    const tabs = await api('/api/browser/tabs');
    $('tabs').replaceChildren(...tabs.map(tab => {
      const button = document.createElement('button'); button.textContent = `${tab.index + 1} · ${tab.host}`;
      button.className = tab.active ? 'active':'';
      button.onclick = () => control({kind:'tab',index:tab.index}); return button;
    }));
  } catch (_) { /* Capture closes the browser while the final frame may be in flight. */ }
  finally { frameBusy = false; }
}
async function poll(current) {
  if (!connected || current !== generation) return;
  try {
    const next = await api('/api/state');
    if (current !== generation) return;
    render(next); $('connectionBadge').textContent = '● 서버 연결됨'; $('connectionBadge').classList.add('connected');
    await frame();
  } catch (error) {
    if (current !== generation) return;
    $('connectionBadge').textContent = '● 연결 재시도 중'; $('connectionBadge').classList.remove('connected');
    notice(networkMessage(error));
    $('openBtn').disabled = $('startBtn').disabled = true;
    $('connectBtn').disabled = false; $('serverUrl').disabled = false;
  }
  if (current === generation) pollTimer = setTimeout(() => poll(current), document.hidden ? 4000:1200);
}
$('connectForm').addEventListener('submit', async event => {
  event.preventDefault(); notice();
  if (state?.running) return;
  clearTimeout(pollTimer); generation++; connected = false;
  try {
    base = serverAddress($('serverUrl').value.trim()); token = $('token').value;
    $('connectBtn').disabled = true; $('connectBtn').textContent = '연결 중…';
    const next = await api('/api/state');
    connected = true; localStorage.setItem('padlet-server', base); lastLog = 0; $('log').replaceChildren();
    render(next); poll(generation);
  } catch (error) { notice(networkMessage(error)); $('connectionBadge').textContent = '● 연결 실패'; $('connectionBadge').classList.remove('connected'); }
  finally { $('connectBtn').disabled = false; $('connectBtn').textContent = '서버 연결 ↗'; }
});
$('captureForm').addEventListener('submit', async event => {
  event.preventDefault(); notice(); $('openBtn').disabled = true;
  try {
    const url = new URL($('targetUrl').value.trim());
    if (url.protocol !== 'https:' || !['padlet.com','www.padlet.com'].includes(url.hostname)) throw new Error('https://padlet.com/ 게시판 주소를 입력하세요.');
    $('browserSection').hidden = false; $('screen').removeAttribute('src'); $('screenPlaceholder').hidden = false;
    render(await api('/api/browser/open', {url:url.href}));
    await frame(); $('browserSection').scrollIntoView({behavior:'smooth', block:'start'});
  } catch (error) { notice(networkMessage(error)); }
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
async function reset() {
  try { render(await api('/api/reset', {})); $('remoteText').value = ''; $('screen').removeAttribute('src'); if (imageUrl) URL.revokeObjectURL(imageUrl); imageUrl = null; notice(); }
  catch (error) { notice(networkMessage(error)); }
}
$('clearSession').onclick = $('resetBtn').onclick = reset;
function control(action) {
  // Preserve ordering between clicks, text, and keys over a slow connection.
  actionQueue = actionQueue.then(async () => {
    await api('/api/browser/action', action);
  }).catch(error => notice(networkMessage(error)));
  return actionQueue;
}
$('screen').addEventListener('click', event => {
  const box = $('screen').getBoundingClientRect();
  control({kind:'click',x:Math.max(0,Math.min(1100,(event.clientX-box.left)*1100/box.width)),y:Math.max(0,Math.min(720,(event.clientY-box.top)*720/box.height))});
  $('screen').focus({preventScroll:true});
});
$('screen').addEventListener('keydown', event => {
  if (event.isComposing) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {event.preventDefault(); control({kind:'key',key:'Control+a'});return;}
  const keys = ['Enter','Tab','Backspace','Delete','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'];
  if (keys.includes(event.key)) {event.preventDefault(); control({kind:'key',key:event.shiftKey && event.key === 'Tab' ? 'Shift+Tab':event.key});}
  else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {event.preventDefault(); control({kind:'text',text:event.key});}
});
$('screen').addEventListener('wheel', event => {event.preventDefault();control({kind:'scroll',delta:Math.max(-1500,Math.min(1500,Math.round(event.deltaY)))});},{passive:false});
$('typeForm').addEventListener('submit', async event => {event.preventDefault(); const text = $('remoteText').value; $('remoteText').value = ''; if (text) await control({kind:'text',text});});
document.querySelectorAll('[data-key]').forEach(button => button.onclick = () => control({kind:'key',key:button.dataset.key}));
document.querySelectorAll('[data-scroll]').forEach(button => button.onclick = () => control({kind:'scroll',delta:Number(button.dataset.scroll)}));
$('downloadLog').onclick = () => {
  const blob = new Blob([logs.map(row => `${row.time} [${row.level}] ${row.message}`).join('\n')],{type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href=url;anchor.download='padlet-session.log';anchor.click();setTimeout(() => URL.revokeObjectURL(url),1000);
};
window.addEventListener('pagehide', () => { token=''; $('token').value=''; $('remoteText').value=''; clearTimeout(pollTimer); if (imageUrl) URL.revokeObjectURL(imageUrl); });

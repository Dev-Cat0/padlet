/* Runs entirely in the user's Padlet tab. No remote server, storage, or cookie export. */
(() => {
  'use strict';
  const hostId = 'padlet-anonymous-sender';
  if (document.getElementById(hostId)) {
    document.getElementById(hostId).dispatchEvent(new Event('sender-show'));
    return;
  }
  if (location.protocol !== 'https:' || !['padlet.com', 'www.padlet.com'].includes(location.hostname)
      || !/^\/[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_-]+)?\/?$/.test(location.pathname)
      || /^(api|dashboard|auth|login)$/.test(location.pathname.split('/')[1])) {
    alert('로그인하지 않은 시크릿 창에서 Padlet 게시판을 연 뒤 실행하세요.');
    return;
  }
  const origin = location.origin, pageKey = origin + location.pathname.replace(/\/$/, '');
  const originalFetch = window.fetch, fetchPage = window.fetch.bind(window);
  const originalOpen = XMLHttpRequest.prototype.open, originalSend = XMLHttpRequest.prototype.send;
  const xhrInfo = new WeakMap();
  let guest = null, captured = null, sections = [], busy = false, running = false, stopped = false;
  let disposed = false, sent = 0, errors = 0, wake = null, capturing = false;
  const host = document.createElement('div');
  host.id = hostId;
  host.style.cssText = 'position:fixed;bottom:18px;left:18px;z-index:2147483647;width:min(360px,calc(100vw - 36px));';
  const root = host.attachShadow({mode:'open'});
  const element = (tag, text, parent, attrs = {}) => {
    const el = document.createElement(tag);
    if (text) el.textContent = text;
    Object.assign(el, attrs);
    if (parent) parent.append(el);
    return el;
  };
  element('style', `
    :host{all:initial;font:13px/1.5 system-ui,sans-serif;color:#edf4ef}
    *{box-sizing:border-box}button,input,textarea{font:inherit}button{cursor:pointer}
    button{border:1px solid #46534b;background:#263c2d;color:#d3f8b8;border-radius:7px;padding:9px 12px}
    button:disabled{opacity:.4;cursor:not-allowed}input,textarea{width:100%;background:#111b15;color:#fff;
      border:1px solid #445349;border-radius:6px;padding:8px}textarea{resize:vertical}
    label{display:block;margin:10px 0}small{display:block;color:#b5c7ba}h2{font-size:15px;margin:0}
    .panel{background:#17221b;border:1px solid #52674c;border-radius:12px;padding:16px;
      box-shadow:0 12px 40px #0005;max-height:80vh;overflow:auto}
    .row{display:flex;gap:8px;align-items:center;justify-content:space-between}.row>label{flex:1;min-width:0}
    .log{white-space:pre-wrap;overflow:auto;max-height:140px;font-size:11px;margin-top:12px}
    .status{color:#d2f2ad;margin:12px 0;overflow-wrap:anywhere}
    .toggle{margin-top:8px;box-shadow:0 4px 16px #0003}
    [hidden]{display:none!important}
  `, root);
  const panel = element('section', '', root, {className:'panel'});
  const heading = element('div', '', panel, {className:'row'});
  element('h2', 'Padlet 익명 전송', heading);
  const close = element('button', '종료', heading, {type:'button', id:'close'});
  const status = element('p', '익명 세션 확인 중…', panel, {className:'status', id:'status'});
  element('small', '계정 로그인 없이만 작동합니다. 이 탭을 닫으면 전송도 종료됩니다.', panel);
  const captureButton = element('button', '익명 상태 확인 / 다시 캡처', panel, {type:'button', id:'capture'});
  captureButton.style.marginTop = '12px';
  const form = element('form', '', panel);
  const field = (name, id, tag = 'input') => {
    const label = element('label', name, form);
    return element(tag, '', label, {id});
  };
  const subject = field('제목', 'subject'); subject.maxLength = 1000;
  const body = field('본문', 'body', 'textarea'); body.rows = 3; body.maxLength = 20000;
  const details = element('small', '먼저 익명 확인 후 Padlet 화면에서 글을 1건 게시하세요.', form, {id:'details'});
  const row = element('div', '', form, {className:'row'});
  const numeric = (name, id, value, step, max) => {
    const label = element('label', name, row);
    return element('input', '', label, {id, type:'number', value, min:'0', max, step, required:true});
  };
  const limit = numeric('섹션당 횟수 (0=무제한)', 'limit', '50', '1', '1000000');
  const delay = numeric('반복 주기 (초)', 'delay', '0.3', 'any', '86400');
  const actions = element('div', '', form, {className:'row'});
  const start = element('button', '전송 시작', actions, {type:'submit', id:'start', disabled:true});
  const stop = element('button', '중지', actions, {type:'button', id:'stop', disabled:true});
  const logBox = element('div', '', panel, {className:'log', id:'log', role:'log'});
  const toggle = element('button', '익명 전송 ▾', root, {className:'toggle', type:'button', id:'toggle'});
  document.documentElement.append(host);
  const show = () => { panel.hidden = false; };
  host.addEventListener('sender-show', show);
  toggle.onclick = () => { panel.hidden = !panel.hidden; };
  const log = message => {
    const line = element('div', `${new Date().toLocaleTimeString()} ${message}`, logBox);
    while (logBox.children.length > 100) logBox.firstChild.remove();
    line.scrollIntoView({block:'nearest'});
  };
  function render(message) {
    if (message) status.textContent = message;
    start.disabled = !captured || running || busy || disposed;
    stop.disabled = !running || stopped;
    captureButton.disabled = running || busy;
    for (const input of [subject, body, limit, delay]) input.disabled = running;
    toggle.textContent = running ? `익명 전송 · 성공 ${sent}건` : '익명 전송 ▾';
  }
  function checkPage() {
    if (disposed || location.origin + location.pathname.replace(/\/$/, '') !== pageKey)
      throw new Error('페이지가 변경됐습니다. 종료 후 이 게시판에서 다시 실행하세요.');
  }
  function headers() {
    const values = {'Accept':'application/json, application/vnd.api+json', 'Content-Type':'application/json; charset=utf-8'};
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    if (csrf) values['X-CSRF-Token'] = csrf;
    if (window.ww?.uid) values['X-UID'] = String(window.ww.uid);
    return values;
  }
  async function request(url, method = 'GET', payload) {
    checkPage();
    const target = new URL(url, origin);
    if (target.origin !== origin || method === 'POST' && target.href !== captured?.endpoint)
      throw new Error('이 게시판 밖으로는 전송할 수 없습니다.');
    const response = await fetchPage(target.href, {method, headers:headers(), credentials:'same-origin',
      redirect:'error', cache:'no-store', signal:AbortSignal.timeout(15000),
      body:method === 'POST' ? JSON.stringify(payload) : undefined});
    const challenge = response.headers.get('cf-mitigated') === 'challenge';
    if (challenge || [401, 403].includes(response.status))
      throw new Error(`HTTP ${response.status}: 접근 또는 보안 확인이 필요합니다. 중지했습니다.`);
    return response;
  }
  async function jsonGet(path) {
    const response = await request(path);
    if (!response.ok) throw new Error(`게시판 조회 실패 (HTTP ${response.status})`);
    return response.json();
  }
  async function anonymousState(expected = null) {
    const state = await jsonGet('/api/11/padlet-starting-state' + location.pathname.replace(/\/$/, ''));
    const user = state?.user, wall = state?.wall;
    if (!user || !Number.isInteger(user.id) || user.id <= 0 || !user.hashid
        || user.registered_at !== null || user.username || user.email)
      throw new Error('익명 세션이 아닙니다. Padlet에 로그인하지 않은 시크릿 창에서 실행하세요.');
    if (!wall || !Number.isInteger(wall.id) || state.canIWrite !== true || state.isReadOnly
        || wall.frozen || wall.is_archived || wall.is_trashed)
      throw new Error('현재 익명 글쓰기가 허용되지 않은 게시판입니다.');
    if (expected && (expected.user.id !== user.id || expected.wall.id !== wall.id))
      throw new Error('익명 사용자 또는 게시판이 바뀌었습니다. 다시 캡처하세요.');
    return {user, wall};
  }
  const isPost = url => {
    try { const target = new URL(url, origin); return target.origin === origin && target.pathname === '/api/9/wishes'; }
    catch (_) { return false; }
  };
  const isCreated = data => Boolean(data && !data.errors && !data.error && data.data
    && (data.data.id || data.data.attributes?.id));
  const plain = html => {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    doc.querySelectorAll('br').forEach(el => el.replaceWith('\n'));
    doc.querySelectorAll('p,div,li').forEach(el => el.append('\n'));
    return (doc.body.textContent || '').replace(/\n+$/, '');
  };
  async function capture(endpoint, raw, result) {
    if (!guest || captured || busy || running || capturing || disposed || !isCreated(result)) return;
    capturing = true; busy = true; render('성공한 익명 게시 요청 확인 중…');
    try {
      const payload = typeof raw === 'string' ? JSON.parse(raw) : null;
      if (!payload || !Number.isInteger(payload.wall_id)) throw new Error('게시 요청 형식을 확인할 수 없습니다.');
      const current = await anonymousState(guest);
      if (payload.wall_id !== current.wall.id || payload.author_id !== current.user.id)
        throw new Error('익명 사용자와 실제 게시 요청의 작성자가 다릅니다. 캡처하지 않았습니다.');
      const data = await jsonGet('/api/5/wall_sections?wall_id=' + current.wall.id);
      if (!Array.isArray(data.data)) throw new Error('섹션 목록을 확인하지 못했습니다.');
      sections = data.data.map(row => row.attributes);
      if (sections.some(section => !Number.isInteger(section?.id) || section.id <= 0
          || section.wall_id != null && section.wall_id !== current.wall.id))
        throw new Error('게시판 섹션 정보가 일치하지 않습니다.');
      sections = [...new Map(sections.map(section => [section.id, section])).values()];
      captured = {endpoint, payload:structuredClone(payload), plain:plain(payload.body)};
      subject.value = payload.subject || '';
      body.value = captured.plain;
      details.textContent = `${sections.length || 1}곳에 매 주기 1건씩 전송합니다. 첨부·추가 항목은 방금 게시한 값을 유지합니다.`;
      render('익명 게시 요청 캡처 완료');
      log('수동 게시 1건은 반복 횟수에 포함되지 않습니다.'); show();
    } catch (error) {
      guest = null; captured = null;
      render(error.message); log(error.message); show();
    } finally { capturing = false; busy = false; render(); }
  }
  function observedFetch(input, init) {
    const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
    const method = (init?.method || input?.method || 'GET').toUpperCase();
    let raw = null;
    if (guest && !captured && !running && method === 'POST' && isPost(url)) {
      if (typeof init?.body === 'string') raw = Promise.resolve(init.body);
      else if (input instanceof Request) raw = input.clone().text().catch(() => null);
    }
    const result = originalFetch.apply(this, arguments);
    if (raw) result.then(async response => {
      if (![200, 201].includes(response.status) || response.headers.get('cf-mitigated') === 'challenge') return;
      const data = await response.clone().json();
      await capture(new URL(url, origin).href, await raw, data);
    }).catch(() => {});
    return result;
  }
  function observedOpen(method, url) {
    xhrInfo.set(this, {method:String(method).toUpperCase(), url:new URL(url, origin).href});
    return originalOpen.apply(this, arguments);
  }
  function observedSend(raw) {
    const info = xhrInfo.get(this);
    if (guest && !captured && info?.method === 'POST' && isPost(info.url)) {
      this.addEventListener('load', () => {
        if (![200, 201].includes(this.status) || this.getResponseHeader('cf-mitigated') === 'challenge') return;
        try {
          const data = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
          capture(info.url, raw, data).catch(() => {});
        } catch (_) { /* Non-JSON responses never count as a successful post. */ }
      }, {once:true});
    }
    return originalSend.apply(this, arguments);
  }
  window.fetch = observedFetch;
  XMLHttpRequest.prototype.open = observedOpen;
  XMLHttpRequest.prototype.send = observedSend;
  async function arm() {
    if (busy || running) return;
    guest = captured = null; busy = true; render('로그인하지 않은 익명 세션인지 확인 중…');
    try {
      guest = await anonymousState();
      render('익명 확인 완료 · Padlet 화면에서 글을 1건 게시하세요.');
      log('이 패널을 접으면 Padlet 화면을 편하게 조작할 수 있습니다.');
    } catch (error) { render(error.message); log(error.message); }
    finally { busy = false; render(); }
  }
  const sleep = seconds => new Promise(resolve => {
    const timer = setTimeout(() => { wake = null; resolve(); }, seconds * 1000);
    wake = () => { clearTimeout(timer); wake = null; resolve(); };
  });
  function halt() { stopped = true; if (wake) wake(); render('중지 중 · 진행 중인 요청 결과 확인'); }
  stop.onclick = halt;
  captureButton.onclick = arm;
  function payloadFor(section, title, text) {
    const payload = structuredClone(captured.payload);
    payload.subject = title;
    const escape = value => value.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    payload.body = text === captured.plain ? captured.payload.body : text ? '<p>' + escape(text).replace(/\n/g, '<br>') + '</p>' : '';
    payload.cid = 'c_new' + crypto.randomUUID().replaceAll('-', '');
    payload.created_at = payload.updated_at = new Date().toISOString();
    if (section) {
      payload.wall_section_id = section.id; delete payload.wall_section_hashid;
      if (section.hashid) payload.wall_section_hashid = section.hashid;
    }
    return payload;
  }
  async function sendOne(section, title, text) {
    if (stopped) return {pending:true};
    // Recheck the current identity before every POST; never trust a display name.
    await anonymousState(guest);
    if (stopped) return {pending:true};
    const response = await request(captured.endpoint, 'POST', payloadFor(section, title, text));
    if (response.status === 429) {
      errors++;
      const retry = Number(response.headers.get('retry-after'));
      return {pending:true, retry:Number.isFinite(retry) ? Math.max(3, retry) : 3};
    }
    let data;
    try { data = await response.json(); } catch (_) { /* Fail closed below. */ }
    if (![200, 201].includes(response.status) || !isCreated(data))
      throw new Error(`HTTP ${response.status}: 게시 성공을 확인하지 못해 중지했습니다. 마지막 게시 여부를 확인하세요.`);
    sent++; render(`익명 전송 중 · 성공 ${sent}건 · 오류 ${errors}건`);
    log(`${section?.title || '게시판'} · 성공 ${sent}건`);
    return {pending:false};
  }
  form.onsubmit = async event => {
    event.preventDefault();
    if (!captured || busy || running) return;
    const count = Number(limit.value), seconds = Number(delay.value), title = subject.value, text = body.value;
    if (!Number.isInteger(count) || count < 0 || count > 1000000 || !Number.isFinite(seconds) || seconds < 0 || seconds > 86400) return;
    running = true; stopped = false; sent = errors = 0; render('익명 상태 재확인 후 전송합니다.');
    const targets = sections.length ? [...sections] : [null];
    let rounds = 0, failure = null;
    try {
      while (!stopped && (count === 0 || rounds < count)) {
        await sleep(seconds);
        if (stopped) break;
        let pending = targets;
        while (pending.length && !stopped) {
          const remaining = []; let retry = 0;
          for (let i = 0; i < pending.length && !stopped; i += 8) {
            const group = pending.slice(i, i + 8);
            const results = await Promise.all(group.map(async section => {
              try { return await sendOne(section, title, text); }
              catch (error) { stopped = true; errors++; failure = error; return {pending:true}; }
            }));
            results.forEach((result, index) => {
              if (result.pending) remaining.push(group[index]);
              retry = Math.max(retry, result.retry || 0);
            });
            if (retry) { remaining.push(...pending.slice(i + 8)); break; }
          }
          pending = remaining;
          if (retry && !stopped) { render(`429 요청 제한 · ${retry}초 대기`); await sleep(retry); }
        }
        if (!pending.length) rounds++;
      }
    } catch (error) { failure = error; errors++; }
    finally {
      running = false;
      if (failure) { captured = guest = null; render(failure.message); log(failure.message); }
      else render(`${stopped ? '중지' : '완료'} · 성공 ${sent}건 · 오류 ${errors}건`);
      render();
    }
  };
  function cleanup() {
    disposed = true; stopped = true; if (wake) wake();
    if (window.fetch === observedFetch) window.fetch = originalFetch;
    if (XMLHttpRequest.prototype.open === observedOpen) XMLHttpRequest.prototype.open = originalOpen;
    if (XMLHttpRequest.prototype.send === observedSend) XMLHttpRequest.prototype.send = originalSend;
    window.removeEventListener('pagehide', onPageHide);
    host.remove();
  }
  async function onClose() {
    if (running) { halt(); render('전송을 중지한 뒤 종료 버튼을 다시 누르세요.'); return; }
    if (busy) return;
    guest = captured = null; cleanup();
  }
  function onPageHide() { cleanup(); }
  close.onclick = onClose;
  window.addEventListener('pagehide', onPageHide, {once:true});
  arm();
})();

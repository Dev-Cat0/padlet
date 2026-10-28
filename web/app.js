"use strict";
const $ = id => document.getElementById(id);
const base = 'https://padlet-sender-panel.onrender.com';
let token = '', connected = false, state = null, submitting = false, timer = null, lastLog = 0, generation = 0;
let authGeneration = 0, authenticating = false;
try { token = sessionStorage.getItem('padlet-session') || ''; } catch (_) {}
const phases = {waiting:'시작 대기',preparing:'봇 익명 세션 준비 중',opening:'준비 중',sending:'게시글 전송 중',backoff:'요청 제한 · 대기 중',stopping:'중지 중',stopped:'중지됨',finished:'전송 완료',error:'오류로 중단',ready:'준비 완료',capturing:'이전 작업이 남아 있습니다. 새로 실행하면 교체됩니다.'};
Object.assign(phases,{deleting:'긴급 삭제 중 · 전송 중지 및 게시물 삭제',deleted:'긴급 삭제 완료',delete_incomplete:'긴급 삭제 미완료 · 남은 항목 확인'});
function notice(text='') { $('notice').textContent=text; $('notice').hidden=!text; }
async function api(path, data, extraHeaders={}, authToken=token) {
  const response = await fetch(base + path, {method:data===undefined?'GET':'POST',
    headers:{Authorization:'Bearer '+authToken,...(data===undefined?{}:{'Content-Type':'application/json'}),...extraHeaders},
    body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',signal:AbortSignal.timeout(20000)});
  const result=await response.json();
  if(!response.ok) {const error=new Error(typeof result.detail==='string'?result.detail:'요청 실패 (HTTP '+response.status+')');error.status=response.status;throw error;}
  return result;
}
function authMessage(text) { $('authStatus').textContent=text; }
function beginAuth(){
  clearTimeout(timer);generation++;authGeneration++;
  connected=false;token='';
  try{sessionStorage.removeItem('padlet-session');}catch(_){}
  $('authGate').hidden=false;$('panel').inert=true;$('start').disabled=true;
  $('connection').textContent='키 인증 필요';$('accessKey').value='';
  authMessage('디스코드 DM으로 받은 5자리 키를 입력하세요.');
  $('accessKey').focus();
}
$('keyForm').onsubmit=async event=>{
  event.preventDefault();if(authenticating)return;
  authenticating=true;const current=++authGeneration;
  $('keySubmit').disabled=true;authMessage('키를 확인하고 있습니다…');
  const body={key:$('accessKey').value.trim().toUpperCase()};
  $('accessKey').value='';
  try{
    const request=api('/api/auth/login',body,{},'');
    body.key='';
    const result=await request;
    if(current!==authGeneration)return;
    token=result.token;
    try{sessionStorage.setItem('padlet-session',token);}catch(_){}
    $('authGate').hidden=true;$('panel').inert=false;lastLog=0;$('log').replaceChildren();
    connect();
  }catch(error){if(current===authGeneration){authMessage(message(error));$('accessKey').focus();}}
  finally{body.key='';authenticating=false;$('keySubmit').disabled=false;}
};
$('reauth').onclick=beginAuth;
function accessBlocked(access) {
  return !access?.known || access.can_run===false || access.remaining===0 ||
    (access.expires!==null && access.expires!==undefined && Date.now()>=access.expires*1000);
}
function renderAccess(next) {
  const access=next.access, active=next.running||next.opening||next.deleting||next.phase==='stopping';
  const expired=access?.expired || (access?.expires!=null && Date.now()>=access.expires*1000);
  let text='이용권 정보를 확인할 수 없습니다. 새로고침 후 다시 인증하세요.';
  if(access?.known) {
    if(expired) text='시간제 키 만료 · 새 실행이 차단되었습니다.';
    else if(access.remaining===0) text='남은 실행 0회 · 모두 사용했습니다. 새 키로 다시 인증하세요.';
    else if(access.blocked) text='사용할 수 없는 키입니다. 새 키로 다시 인증하세요.';
    else if(access.remaining!==null) text='남은 실행 '+access.remaining+'회 · 실행 접수 1번당 1회 차감';
    else if(access.expires!==null) text='이용 기한: '+new Date(access.expires*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' (UTC+9)';
    else text='영구 이용권';
  } else if(access?.blocked) text='키가 만료·폐기되었거나 사용 횟수가 소진되었습니다.';
  $('accessStatus').textContent=text;
  const countLimit=access?.post_limit??5000;
  $('limit').max=String(countLimit);
  $('limitHint').textContent=countLimit===5000
    ?'모든 섹션에 각각 최대 5,000개 · 0은 무제한'
    :'모든 섹션에 각각 이 개수만큼 · 0은 무제한';
  $('start').disabled=!connected||active||submitting||accessBlocked(access);
  $('start').textContent=submitting?'접수 중…':active?'진행 중…':expired?'이용 기한 만료':access?.remaining===0?'실행 횟수 소진':'▶ 실행';
}
function render(next) {
  state=next;
  const active=next.running||next.opening||next.deleting||next.phase==='stopping';
  renderAccess(next);
  $('reauth').disabled=active||submitting;
  $('stop').disabled=!connected||!active||next.deleting||next.phase==='stopping';
  $('emergencyDelete').disabled=!connected||submitting||next.deleting||next.delete_retry_after>0||(!active&&!next.delete_pending);
  $('emergencyDelete').textContent=next.deleting?'삭제 중…':'긴급 삭제';
  $('deleteStatus').textContent='삭제 대상 '+(next.delete_pending||0)+'건 · 삭제 확인 '+(next.deleted||0)+'건'+
    (next.delete_untracked?' · 추적 불가 '+next.delete_untracked+'건':'')+
    (next.delete_errors?' · 삭제 오류 '+next.delete_errors+'건':'')+
    (next.delete_retry_after?' · '+next.delete_retry_after+'초 후 재시도':'');
  for(const id of ['url','subject','body','limit','delay','passwordEnabled']) $(id).disabled=active||submitting;
  renderPassword(active||submitting);
  $('sent').textContent=next.sent;
  $('errors').textContent=next.errors;
  $('phase').textContent=phases[next.phase]||next.phase;
  const total=next.limit*(next.target_count||1);
  $('progress').value=total>0?Math.min(100,next.sent/total*100):0;
  $('board').textContent=next.board ? next.board.title+(next.board.sections?.length?' · 전체 '+next.board.sections.length+'개 섹션':'')+(total>0?' · 총 '+total+'건 예정':''):'';
  const logs=next.logs||[];
  if(logs.length&&logs.at(-1).id<lastLog){lastLog=0;$('log').replaceChildren();}
  for(const row of logs.filter(row=>row.id>lastLog)) {
    const line=document.createElement('div');line.className=row.level;
    line.textContent=new Date(row.time).toLocaleTimeString('ko-KR')+' '+row.message;
    $('log').append(line);lastLog=row.id;
  }
  while($('log').children.length>300)$('log').firstChild.remove();
  $('log').scrollTop=$('log').scrollHeight;
}
function message(error){return error.name==='TimeoutError'||error instanceof TypeError?'서버 응답이 지연됩니다. 실행 상태를 다시 확인하고 있습니다.':error.message;}
async function poll(current) {
  if(current!==generation)return;
  try {const next=await api('/api/state');if(current!==generation)return;connected=true;$('connection').textContent='● 서버 연결됨';render(next);}
  catch(error){if(current!==generation)return;connected=false;$('connection').textContent='연결 재시도 중';if(state)render(state);notice(message(error));if(error.status===401){beginAuth();return;}}
  if(current===generation)timer=setTimeout(()=>poll(current),document.hidden?4000:1500);
}
async function connect(){
  clearTimeout(timer);const current=++generation;connected=false;$('start').disabled=true;
  if(!token){beginAuth();return;}
  try {
    const next=await api('/api/state');if(current!==generation)return;connected=true;
    $('authGate').hidden=true;$('panel').inert=false;notice();render(next);poll(current);
  }catch(error){notice(message(error));if(error.status===401){beginAuth();return;}authMessage(message(error));timer=setTimeout(connect,3000);}
}
function renderPassword(busy=false) {
  const enabled=$('passwordEnabled').checked;
  $('passwordSettings').hidden=!enabled;
  $('boardPassword').disabled=busy||!enabled;
  $('boardPassword').required=enabled;
}
$('passwordEnabled').onchange=()=>{
  if(!$('passwordEnabled').checked) $('boardPassword').value='';
  renderPassword();
  if($('passwordEnabled').checked) $('boardPassword').focus();
};
renderPassword();
$('sendForm').onsubmit=async event=>{
  event.preventDefault();if(!connected||submitting||state?.running||state?.opening||state?.deleting||accessBlocked(state?.access))return;
  submitting=true;notice();if(state)render(state);
  try{
    const data={url:$('url').value.trim(),subject:$('subject').value,body:$('body').value,limit:Number($('limit').value),delay:Number($('delay').value)};
    if($('passwordEnabled').checked) data.password=$('boardPassword').value;
    render(await api('/api/run',data, {'X-Request-ID':crypto.randomUUID()}));
  }
  catch(error){notice(message(error));try{render(await api('/api/state'));}catch(_){connected=false;}}
  finally{$('boardPassword').value='';submitting=false;if(state)render(state);}
};
$('stop').onclick=async()=>{ $('stop').disabled=true;try{render(await api('/api/stop',{}));}catch(error){notice(message(error));if(state)render(state);}};
$('emergencyDelete').onclick=async()=>{
  if(!connected||submitting||state?.deleting)return;
  submitting=true;notice();if(state)render(state);
  try{render(await api('/api/emergency-delete',{}));}
  catch(error){notice(message(error));try{render(await api('/api/state'));}catch(_){connected=false;}}
  finally{submitting=false;if(state)render(state);}
};
window.addEventListener('pagehide',()=>{generation++;authGeneration++;clearTimeout(timer);});
window.addEventListener('pageshow',event=>{if(event.persisted)connect();});
setInterval(()=>{if(state&&token)renderAccess(state);},500);
connect();

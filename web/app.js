"use strict";
const $ = id => document.getElementById(id);
const base = 'https://padlet-sender-panel.onrender.com';
let token = '', connected = false, state = null, submitting = false, timer = null, lastLog = 0, generation = 0;
try { token = sessionStorage.getItem('padlet-session') || ''; } catch (_) {}
const phases = {waiting:'시작 대기',preparing:'봇 익명 세션 준비 중',opening:'준비 중',sending:'전송 중',backoff:'요청 제한 · 대기 중',stopping:'중지 중',stopped:'중지됨',finished:'전송 완료',error:'오류로 중단',ready:'준비 완료',capturing:'이전 작업이 남아 있습니다. 새로 실행하면 교체됩니다.'};
function notice(text='') { $('notice').textContent=text; $('notice').hidden=!text; }
async function api(path, data) {
  const response = await fetch(base + path, {method:data===undefined?'GET':'POST',
    headers:{Authorization:'Bearer '+token,...(data===undefined?{}:{'Content-Type':'application/json'})},
    body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',signal:AbortSignal.timeout(20000)});
  const result=await response.json();
  if(!response.ok) {const error=new Error(typeof result.detail==='string'?result.detail:'요청 실패 (HTTP '+response.status+')');error.status=response.status;throw error;}
  return result;
}
function render(next) {
  state=next;
  const active=next.running||next.opening||next.phase==='stopping';
  $('start').disabled=!connected||active||submitting;
  $('start').textContent=submitting?'접수 중…':active?'진행 중…':'▶ 실행';
  $('stop').disabled=!connected||!active||next.phase==='stopping';
  for(const id of ['url','subject','body','limit','delay']) $(id).disabled=active||submitting;
  $('sent').textContent=next.sent;
  $('errors').textContent=next.errors;
  $('phase').textContent=phases[next.phase]||next.phase;
  $('progress').value=next.limit>0?Math.min(100,next.sent/next.limit*100):0;
  $('board').textContent=next.board ? next.board.title+(next.board.destination?' · '+next.board.destination:''):'';
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
  catch(error){if(current!==generation)return;connected=false;$('connection').textContent='연결 재시도 중';if(state)render(state);notice(message(error));if(error.status===401){token='';try{sessionStorage.removeItem('padlet-session');}catch(_){}connect();return;}}
  if(current===generation)timer=setTimeout(()=>poll(current),document.hidden?4000:1500);
}
async function connect(){
  clearTimeout(timer);const current=++generation;connected=false;$('start').disabled=true;
  try {
    if(!token){token=(await api('/api/session',{})).token;try{sessionStorage.setItem('padlet-session',token);}catch(_){}lastLog=0;$('log').replaceChildren();}
    const next=await api('/api/state');if(current!==generation)return;connected=true;notice();render(next);poll(current);
  }catch(error){notice(message(error));if(error.status===401)token='';timer=setTimeout(connect,2000);}
}
$('sendForm').onsubmit=async event=>{
  event.preventDefault();if(!connected||submitting||state?.running||state?.opening)return;
  submitting=true;notice();if(state)render(state);
  try{render(await api('/api/run',{url:$('url').value.trim(),subject:$('subject').value,body:$('body').value,limit:Number($('limit').value),delay:Number($('delay').value)}));}
  catch(error){notice(message(error));try{render(await api('/api/state'));}catch(_){connected=false;}}
  finally{submitting=false;if(state)render(state);}
};
$('stop').onclick=async()=>{ $('stop').disabled=true;try{render(await api('/api/stop',{}));}catch(error){notice(message(error));if(state)render(state);}};
window.addEventListener('pagehide',()=>{generation++;clearTimeout(timer);});
window.addEventListener('pageshow',event=>{if(event.persisted)connect();});
connect();

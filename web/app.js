"use strict";
const bookmark = document.getElementById('bookmark');
const notice = document.getElementById('notice');
const code = document.getElementById('code');
const copy = document.getElementById('copy');
bookmark.addEventListener('click', event => {
  event.preventDefault();
  notice.textContent = '이 버튼을 즐겨찾기 바에 드래그한 뒤, 시크릿 창의 Padlet 게시판에서 실행하세요.';
});
fetch('./anonymous.js?v=anonymous-local-1', {cache:'no-store'}).then(async response => {
  if (!response.ok) throw new Error('실행 코드를 불러오지 못했습니다. 새로고침하세요.');
  const source = await response.text();
  const url = 'javascript:' + encodeURIComponent('void ' + source.trim().replace(/;$/, ''));
  bookmark.href = url;
  bookmark.removeAttribute('aria-disabled');
  code.value = url;
  copy.disabled = false;
  notice.textContent = '준비 완료 · 버튼을 즐겨찾기 바에 드래그하세요.';
}).catch(error => { notice.textContent = error.message; });
copy.onclick = async () => {
  try { await navigator.clipboard.writeText(code.value); notice.textContent = '복사했습니다. 즐겨찾기의 주소에 붙여넣으세요.'; }
  catch (_) { code.focus(); code.select(); notice.textContent = '코드를 선택했습니다. 직접 복사해 즐겨찾기 주소에 넣으세요.'; }
};

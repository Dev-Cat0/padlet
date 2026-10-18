# Padlet Sender 웹 패널

제공된 `padlet.py`의 **브라우저 로그인 → 게시 요청 캡처 → 반복 전송** 흐름을 GitHub Pages + Render로 옮긴 프로젝트입니다. 공식 Padlet API 키는 사용하지 않습니다.

- 웹 패널: https://dev-cat0.github.io/padlet/
- 서버: https://padlet-sender-panel.onrender.com
- 서버 관리: https://dashboard.render.com/web/srv-dbae7t0m7kps738gj5c0

- `web/`: 빌드 없이 GitHub Pages에서 실행하는 한국어 웹 패널
- `server/`: FastAPI 세션·PC 연결·curl_cffi 전송 서버
- `pc-helper/`: PC에서 실제 Chromium을 여는 Windows 도우미
- `desktop/padlet.py`: 전달받은 데스크톱 원본
- `Dockerfile`: Chromium 포함 Render 실행 환경
- `render.yaml`: 수동 Blueprint 배포를 선택할 때 사용할 구성

## 사용 순서

1. GitHub Pages의 웹 패널을 엽니다.
2. 페이지가 고정된 Render 서버에 자동 연결됩니다. 서버 주소·접속 키 입력이나 연결 버튼은 없습니다.
3. **Windows 도우미 다운로드**로 ZIP을 받고 압축을 푼 뒤 `start.cmd`를 실행합니다. Python 3.12 이상이 필요하며 첫 실행은 의존성과 Chromium을 설치합니다.
4. 패널의 **PC 연결 코드 발급**을 눌러 10자리 코드를 도우미에 입력합니다. 연결 코드는 5분간 유효하고 1회만 사용할 수 있습니다. 대상 `https://padlet.com/...` 주소를 입력하고 **내 PC 브라우저 열기**를 누릅니다. 로그인과 입력은 PC에 열린 실제 브라우저에서 직접 합니다.
5. 해당 게시판에서 게시물을 한 번 작성합니다. 이 글은 실제로 게시됩니다. 성공한 `/api/9/wishes` POST 요청의 쿠키·인증 헤더·본문을 고정된 본인 Render 서버로 HTTPS 업로드합니다. 서버가 캡처를 확인하면 PC 브라우저가 닫히고 시작 버튼이 활성화됩니다. 로그인 비밀번호는 도우미가 수집하거나 Render에 업로드하지 않습니다.
6. 제목, 본문, 전송 횟수, 간격을 설정하고 시작합니다. 기본값은 원본과 같은 50회 / 0.3초이며 **0회는 무제한**입니다.
7. 중지를 누르면 진행 중인 요청이 끝난 뒤 멈춥니다(요청 제한시간 10초). 로그를 파일로 내려받을 수 있습니다.
8. **로그인·캡처 정보 삭제**로 서버의 로그인 세션과 캡처 데이터를 지웁니다.

새로고침하면 같은 탭의 작업 상태를 복구합니다. 전송 중에는 창을 닫지 말고 먼저 중지하세요. 창을 닫는 것만으로 서버 작업은 멈추지 않으며, 탭을 닫아 세션 식별자가 사라지면 해당 작업에 다시 접근하지 못할 수 있습니다. 각 브라우저 세션의 로그인·캡처·로그는 분리됩니다.

## 배포

### GitHub Pages

저장소 Settings → Pages → Source를 **GitHub Actions**로 설정합니다. `main`의 `web/` 또는 `pc-helper/` 변경 시 `.github/workflows/pages.yml`이 배포합니다. 예상 URL은 `https://dev-cat0.github.io/padlet/`입니다.

### Render

기존 서비스와 별도로 `padlet-sender-panel` Docker 웹 서비스를 사용합니다. 직접 생성한 서비스가 있다면 `render.yaml`로 중복 생성하지 마세요. Blueprint로 새로 배포할 경우:

[Render에서 Blueprint 열기](https://dashboard.render.com/blueprint/new?repo=https://github.com/Dev-Cat0/padlet)

- Runtime: Docker
- Branch: main
- Root directory: 비워 둠
- Dockerfile: `./Dockerfile`
- Health check: `/healthz`
- Region: Singapore
- Instance: Free (단일 인스턴스)
- `ALLOWED_ORIGINS`: `https://dev-cat0.github.io` (경로 `/padlet/`는 포함하지 않음)

서버 주소는 `web/config.js`의 `serverUrl`로 고정되며 패널에서 변경할 수 없습니다. 임의 세션 식별자는 서버가 자동 발급하며 저장소에 포함되지 않습니다. 이전 배포에 남아 있는 `PANEL_TOKEN` 환경변수는 사용하지 않습니다. 직접 생성한 Render 서비스 설정은 Dashboard가 관리하며 `render.yaml`은 대체 배포용입니다.

Chromium은 메모리를 사용하므로 무료 인스턴스에서 메모리 부족이 발생하면 Render 로그를 확인하세요. 유료 전환은 사용자가 선택해야 합니다. 서버가 재시작되거나 재배포되면 로그인, 캡처, 로그, 진행 중인 전송은 복구되지 않습니다. 인메모리 상태를 사용하므로 Uvicorn worker와 Render 인스턴스는 각각 하나로 유지합니다.

## 로컬 실행

Python 3.12와 Node.js(선택적 JS 문법 확인)를 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m playwright install chromium
$env:ALLOWED_ORIGINS = 'http://127.0.0.1:4173'
.\.venv\Scripts\python -m uvicorn server.app:app --host 127.0.0.1 --port 4174 --no-access-log
```

다른 터미널:

```powershell
.\.venv\Scripts\python -m http.server 4173 --bind 127.0.0.1 --directory web
```

로컬 검증 시에만 `web/config.js`의 `serverUrl`을 `http://127.0.0.1:4174`로 바꾸고 `http://127.0.0.1:4173`을 엽니다. 배포 전에는 Render 주소로 되돌립니다.

```powershell
.\.venv\Scripts\python -m pytest -q
node --check web/app.js
```

## 원본과의 차이 및 제한

- 브라우저는 PC에서 실행하므로 클릭·타이핑·화면 렌더링이 Render의 CPU나 네트워크 왕복을 거치지 않습니다. 무료 서버는 요청 캡처 이후 전송만 수행합니다.
- 도우미는 일반 브라우저 프로필과 분리된 임시 Chromium을 사용합니다. 도우미를 종료하면 PC 브라우저도 닫히지만, 이미 시작한 Render 전송은 계속됩니다.
- 도우미는 서버의 Padlet 열기/닫기 명령만 수행하며, 임의 명령이나 파일 접근 기능은 없습니다. 연결 코드와 도우미 토큰은 서버 메모리에 보관하며 패널 토큰과 권한을 분리합니다. 서버 재배포 후에는 새 연결 코드가 필요합니다.
- 헤더는 Playwright `all_headers()`로 읽어 쿠키를 포함합니다. 캡처한 헤더·본문은 서버 메모리에만 두며 상태 응답이나 로그로 반환하지 않습니다. 자동 발급한 세션 식별자는 탭의 `sessionStorage`에 보관하고 새로고침 때 재사용합니다.
- 429에는 원본의 지수 아닌 3초씩 증가하는 대기(최대 30초)를 유지하며 숫자형 `Retry-After`가 더 길면 이를 따릅니다. 인증 오류나 다른 HTTP 오류에는 중지합니다. 타임아웃은 게시 성공 여부가 불명확하므로 자동 재전송하지 않습니다.
- 본문은 일반 텍스트로 HTML 이스케이프하며 줄바꿈을 보존합니다.
- 30분 동안 전송이나 브라우저 조작이 없으면 로그인·캡처 세션을 삭제합니다.
- 패널에는 공개 자동 연결을 사용합니다. 로그인된 Padlet 계정이나 캡처 데이터가 공유되지 않도록 모든 제어 API가 해당 세션 식별자를 확인합니다. 세션 생성은 분당 20회, 보관 세션은 32개, 구형 원격 Chromium 경로는 1개로 제한합니다. 현재 패널은 해당 경로를 사용하지 않습니다. 이 제한은 서버 자원을 보호하기 위한 것으로 사용자 계정 인증을 대신하지는 않습니다.
- Padlet 내부 요청 형식은 변경될 수 있습니다. 캡처 또는 전송이 실패하면 다시 로그인·캡처해야 하며, 내부 API가 바뀌면 코드 수정이 필요할 수 있습니다.

자동 테스트는 모의 응답으로 요청 캡처, PC 페어링·세션 격리·만료·취소·업로드 재시도, 인증, CORS, 전송 횟수, 429 대기, 중지, 오류 처리를 확인합니다. 실제 계정 로그인과 게시 성공은 사용자의 Padlet 계정·게시판으로 확인해야 합니다.

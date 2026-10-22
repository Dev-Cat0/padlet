$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv-keybot\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    if (Test-Path -LiteralPath '.venv\Scripts\python.exe') {
        & '.\.venv\Scripts\python.exe' -m venv .venv-keybot
    } elseif (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')) {
        & (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe') -m venv .venv-keybot
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv-keybot
    } else {
        python -m venv .venv-keybot
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12를 설치하세요: https://www.python.org/downloads/' }
}
& $taskPython -c "import importlib.metadata,importlib.util,sys; sys.exit(0 if importlib.util.find_spec('discord') and importlib.metadata.version('discord.py') == '2.7.1' else 1)"
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r keybot/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '봇 패키지를 설치하지 못했습니다.' }
}
& $taskPython -c "import json,pathlib,sys; p=pathlib.Path('.local/keybot/config.json'); c=json.loads(p.read_text()) if p.exists() else {}; sys.exit(0 if c.get('DISCORD_BOT_TOKEN') and c.get('DISCORD_GUILD_ID') else 1)"
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m keybot.setup
    if ($LASTEXITCODE -ne 0) { throw '봇 설정을 완료하세요.' }
}
& $taskPython -m keybot

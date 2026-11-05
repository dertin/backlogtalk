$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot "..")
$pythonOverride = $env:BACKLOGTALK_PYTHON
if ($pythonOverride) {
    & $pythonOverride -c 'import sys; assert sys.version_info >= (3, 12)'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonOverride -m venv .venv
} else {
    py -3.12 -c 'import sys; assert sys.version_info >= (3, 12)'
    if ($LASTEXITCODE -ne 0) { throw 'Instalá Python 3.12 o indicá BACKLOGTALK_PYTHON.' }
    py -3.12 -m venv .venv
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -I scripts/bootstrap_pip.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Instalación terminada. Ejecutá .\scripts\run.ps1'

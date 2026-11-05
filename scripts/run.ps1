$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot "..")
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Falta instalar dependencias: .\scripts\install.ps1' }
& $python -m backlogtalk.qt_app @args
exit $LASTEXITCODE

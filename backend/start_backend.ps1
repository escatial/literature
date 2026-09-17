param(
  [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = $null
if ($env:CONDA_PREFIX) { $python = Join-Path $env:CONDA_PREFIX 'python.exe' }
if (-not $python -or -not (Test-Path $python)) { $python = (Get-Command python).Source }
if (-not $python -or -not (Test-Path $python)) { $python = 'D:\Anaconda\Anaconda\python.exe' }

$log = Join-Path $root 'uvicorn.log'
$err = Join-Path $root 'uvicorn.err.log'
$running = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($running) {
  Write-Host "Backend already listening on port $Port."
  exit 0
}

Start-Process -FilePath $python `
  -ArgumentList @('-m','uvicorn','main:app','--host','127.0.0.1','--port', "$Port") `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $log `
  -RedirectStandardError $err | Out-Null

for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Backend started: http://127.0.0.1:$Port"
    exit 0
  }
}

Write-Error "Backend failed to start. See $err"
exit 1

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WorkDir = Join-Path $ProjectRoot "backend"
$env:PYTHONPATH = Join-Path $WorkDir "src"
$PythonExe = (Get-Command python -ErrorAction Stop).Source

Start-Process -FilePath $PythonExe `
    -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8090", "--log-level", "info" `
    -WorkingDirectory $WorkDir `
    -RedirectStandardOutput (Join-Path $ProjectRoot "uvicorn.out") `
    -RedirectStandardError (Join-Path $ProjectRoot "uvicorn.err") `
    -WindowStyle Hidden `
    -ErrorAction Stop
Write-Host "uvicorn launched"

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# 杀旧 8090 监听者
$conns = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($p) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Seconds 2

$repo = "d:\code\个人开发项目\202608\文献综述agent"
$backendSrc = $repo + "\backend\src"

# 用 Start-Job 后台跑 uvicorn,避免 Start-Process 的 PS5 中文路径 / WorkingDirectory 解析问题
$job = Start-Job -ScriptBlock {
    param($repo, $backendSrc)
    $env:PYTHONPATH = $backendSrc
    $env:CONDA_NO_PLUGINS = "true"
    Set-Location -LiteralPath ($repo + "\backend")
    & "D:\Anaconda\Anaconda\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8090 --log-level info
} -ArgumentList $repo, $backendSrc

Start-Sleep -Seconds 6
$listen = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    foreach ($c in $listen) {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        Write-Host ("up pid=" + $c.OwningProcess + " name=" + $p.ProcessName)
    }
} else {
    Write-Host "NOT LISTENING, job output tail:"
    $out = Receive-Job -Job $job -Keep
    if ($out) {
        $out | Select-Object -Last 40 | ForEach-Object { Write-Host $_ }
    } else {
        Write-Host "(no job output yet)"
    }
}
# 文献综述 Agent 端到端冒烟测试(PowerShell 版)
# 用法:
#   1. 启动后端: $env:PYTHONPATH="backend\src" ; py -m uvicorn main:app --port 8765
#   2. 跑冒烟: pwsh scripts/smoke_test.ps1 -Base "http://127.0.0.1:8765/api"

param(
    [string]$Base = "http://127.0.0.1:8765/api"
)

$Errors = 0

function Check($Label, $Cond) {
    if ($Cond) {
        Write-Host "  ✓ $Label" -ForegroundColor Green
    } else {
        Write-Host "  ✗ $Label" -ForegroundColor Red
        $script:Errors++
    }
}

Write-Host "[1/3] healthz" -ForegroundColor Cyan
try {
    $h = Invoke-RestMethod "$Base/healthz"
    Check "status=ok" ($h.status -eq "ok")
} catch {
    Check "healthz 可访问" $false
}

Write-Host "[2/3] 中文批量导入" -ForegroundColor Cyan
$cn_body = @{
    raw_text = @"
刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229.
bad line
"@
} | ConvertTo-Json
try {
    $cn = Invoke-RestMethod "$Base/import/cn" -Method POST -Body $cn_body -ContentType 'application/json'
    Check "total=2" ($cn.total -eq 2)
    Check "parsed_ok=1" ($cn.parsed_ok -eq 1)
    Check "parsed_fail=1" ($cn.parsed_fail -eq 1)
    Check "第一条 year=2025" ($cn.citations[0].year -eq 2025)
} catch {
    Check "中文导入可调用" $false
}

Write-Host "[3/3] 英文检索(无 LLM 模式,只要 OpenAlex 命中)" -ForegroundColor Cyan
$en_body = @{
    topic = "deep learning medical imaging"
    year_start = 2022; year_end = 2025
    min_citations = 0; limit = 5; use_rerank = $false
} | ConvertTo-Json
try {
    $en = Invoke-RestMethod "$Base/retrieval/search" -Method POST -Body $en_body -ContentType 'application/json'
    Check "检索式非空" ($en.query_used -ne "")
    Write-Host "    query_used: $($en.query_used)"
    Write-Host "    total_before_filter: $($en.total_before_filter)"
    Write-Host "    total_after_filter: $($en.total_after_filter)"
} catch {
    Check "英文检索可调用" $false
}

Write-Host ""
if ($Errors -eq 0) {
    Write-Host "✓ 冒烟测试全部通过" -ForegroundColor Green
} else {
    Write-Host "✗ 冒烟测试失败: $Errors 处错误" -ForegroundColor Red
    exit 1
}

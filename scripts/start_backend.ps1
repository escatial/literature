# 后端启动脚本(后台运行,UTF-8 编码)
$env:PYTHONPATH = "d:\code\个人开发项目\202608\文献综述agent\backend\src"
$WorkDir = "d:\code\个人开发项目\202608\文献综述agent\backend"
Start-Process -FilePath "D:\Anaconda\Anaconda\python.exe" `
    -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8765", "--log-level", "info" `
    -WorkingDirectory $WorkDir `
    -RedirectStandardOutput "d:\code\个人开发项目\202608\文献综述agent\uvicorn.out" `
    -RedirectStandardError "d:\code\个人开发项目\202608\文献综述agent\uvicorn.err" `
    -WindowStyle Hidden
Write-Host "uvicorn launched"

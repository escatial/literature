"""重启后端并验证 plan_intent,完全用 Python 走,绕开 PowerShell 中文路径 bug。"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(r"d:\code\个人开发项目\202608\文献综述agent")
BACKEND = REPO / "backend"
SRC = BACKEND / "src"
PORT = 8090
PY = r"D:\Anaconda\Anaconda\python.exe"
LOG = REPO / "_check.out"


def log(msg: str):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()


def kill_old():
    # 用 python 自己启的 netstat 不靠谱;走 PowerShell 但禁用 profile / plugins
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        f"Get-NetTCPConnection -LocalPort {PORT} -State Listen -EA SilentlyContinue "
        f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }}"
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    time.sleep(2)


def spawn_uvicorn():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["CONDA_NO_PLUGINS"] = "true"
    out_f = open(REPO / "uvicorn.out", "wb")
    err_f = open(REPO / "uvicorn.err", "wb")
    # 直接启 python,不通过 cmd / PowerShell
    p = subprocess.Popen(
        [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "info"],
        cwd=str(BACKEND),
        env=env,
        stdout=out_f, stderr=err_f,
        stdin=subprocess.DEVNULL,
        close_fds=False,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    log(f"spawned pid={p.pid}")
    return p


def wait_listen(timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", PORT))
                return True
            except OSError:
                time.sleep(0.5)
    return False


def http_get(path):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return r.status, r.read()


def main():
    log(f"=== run @ {time.strftime('%H:%M:%S')}")
    kill_old()
    spawn_uvicorn()
    if not wait_listen():
        log("NOT LISTENING")
        try:
            log((REPO / "uvicorn.err").read_bytes()[-2000:].decode("utf-8", "replace"))
        except Exception as e:
            log(f"err read failed: {e}")
        sys.exit(1)
    log("LISTENING")
    # smoke
    try:
        st, body = http_get("/")
        log(f"GET / -> {st} {body[:80]!r}")
    except Exception as e:
        log(f"GET / failed: {e}")

    # 端到端测 plan_intent:导入 backend.src 模块跑一次
    sys.path.insert(0, str(SRC))
    try:
        from retrieval.query_planner import plan_intent
        intent = plan_intent("无人机协同配送应急物资")
        log("plan_intent OK")
        log(f"  topic_summary = {intent.topic_summary!r}")
        log(f"  concepts      = {[c.id + ':' + c.label_en for c in intent.concepts]}")
        log(f"  template      = {intent.boolean_template!r}")
    except Exception as e:
        log(f"plan_intent FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
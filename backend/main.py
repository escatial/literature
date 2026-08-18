"""FastAPI 入口。

启动:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO)


def _configure_uvicorn_logging() -> None:
    """给 uvicorn 控制台日志统一加上本地时间前缀。"""
    default_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    access_formatter = logging.Formatter(
        '%(asctime)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            if logger_name == "uvicorn.access":
                handler.setFormatter(access_formatter)
            else:
                handler.setFormatter(default_formatter)


_configure_uvicorn_logging()

from api.health import router as health_router  # noqa: E402
# 需求2:全站移除粘贴引文手动导入,不再注册 /api/import/cn
from api.papers import router as papers_router  # noqa: E402
from api.prompts import router as prompts_router  # noqa: E402
from api.query_plan import router as query_plan_router  # noqa: E402
from api.retrieval_history import router as retrieval_history_router  # noqa: E402
from api.retrieval_tasks import router as retrieval_tasks_router  # noqa: E402
from api.retrieval_v2 import router as retrieval_v2_router  # noqa: E402
from api.reviews import router as reviews_router  # noqa: E402
from api.screening import router as screening_router  # noqa: E402
from api.stop import router as stop_router  # noqa: E402
from api.writing import router as writing_router  # noqa: E402
from api.cnki import router as cnki_router
from api.review import router as review_router  # noqa: E402
from api.contacts import router as contacts_router  # noqa: E402
from db.session import close_db, connect_db, init_db  # noqa: E402


@asynccontextmanager
async def _lifespan(_: FastAPI):
    """应用生命周期:启动数据库,关闭数据库连接。"""
    init_db()
    connect_db()
    try:
        yield
    finally:
        close_db()


app = FastAPI(title="文献综述 Agent", version="0.2.0", lifespan=_lifespan)


@app.exception_handler(Exception)
async def _unhandled_exc(request, exc):
    import logging, traceback
    logging.getLogger(__name__).error(
        "Unhandled exception on %s %s:\n%s",
        request.method, request.url.path, traceback.format_exc(),
    )
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc().splitlines()[-12:]},
    )

# CORS:allow_origins=["*"] 与 allow_credentials=True 同时开启会被 Starlette 拒绝
# 开发环境放宽,带凭证的请求必须显式列出来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(retrieval_tasks_router, prefix="/api")
app.include_router(retrieval_v2_router, prefix="/api")
app.include_router(retrieval_history_router, prefix="/api")
app.include_router(query_plan_router, prefix="/api")
app.include_router(stop_router, prefix="/api")
app.include_router(screening_router, prefix="/api")
app.include_router(writing_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")
app.include_router(cnki_router, prefix="/api")
app.include_router(contacts_router, prefix="/api")
app.include_router(review_router, prefix="/api")


@app.get("/")
def root():
    return {"message": "文献综述 Agent API", "docs": "/docs"}


if __name__ == "__main__":
    # 支持 python main.py 直接启动
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

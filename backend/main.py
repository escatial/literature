"""FastAPI 入口。

启动:
    uvicorn main:app --reload --host 0.0.0.0 --port 8080
"""
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO)

from api.health import router as health_router  # noqa: E402
from api.import_cn import router as import_cn_router  # noqa: E402
from api.papers import router as papers_router  # noqa: E402
from api.query_plan import router as query_plan_router  # noqa: E402
from api.retrieval import router as retrieval_router  # noqa: E402
from api.reviews import router as reviews_router  # noqa: E402
from api.screening import router as screening_router  # noqa: E402
from api.writing import router as writing_router  # noqa: E402
from db.session import init_db  # noqa: E402

app = FastAPI(title="文献综述 Agent", version="0.2.0")

# 启动时建表
@app.on_event("startup")
def _startup():
    init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(retrieval_router, prefix="/api")
app.include_router(import_cn_router, prefix="/api")
app.include_router(query_plan_router, prefix="/api")
app.include_router(screening_router, prefix="/api")
app.include_router(writing_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")


@app.get("/")
def root():
    return {"message": "文献综述 Agent API", "docs": "/docs"}
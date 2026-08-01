"""FastAPI 入口。

- 挂载所有 API 路由
- 配置 CORS(开发环境开 *,生产收紧)
- 优雅启动(无 LLM 时也可启动,仅靠 LLM 调用的接口会失败)
"""
from __future__ import annotations

import logging

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 先加载 .env,再 import 路由(路由可能依赖环境变量)
load_dotenv()

from api.health import router as health_router  # noqa: E402
from api.import_cn import router as import_cn_router  # noqa: E402
from api.query_plan import router as query_plan_router  # noqa: E402
from api.retrieval import router as retrieval_router  # noqa: E402
from api.screening import router as screening_router  # noqa: E402
from api.writing import router as writing_router  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lit_review")

app = FastAPI(title="文献综述 Agent Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 开发用;生产收紧
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(retrieval_router, prefix="/api")
app.include_router(import_cn_router, prefix="/api")
app.include_router(query_plan_router, prefix="/api")
app.include_router(screening_router, prefix="/api")
app.include_router(writing_router, prefix="/api")


@app.get("/")
async def root():
    return {"app": "lit-review-agent", "version": "0.1.0"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

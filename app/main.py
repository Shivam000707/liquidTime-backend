from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

from app.routers import schedule


def create_app() -> FastAPI:
    app = FastAPI(
        title="LiquidTime API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    app.include_router(schedule.router, prefix="/api/v1")

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok", "service": "liquidtime-api"}

    return app


app = create_app()

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .database import Base, engine
from .routers import data_router, agent_router
from . import seed

app = FastAPI(title="AI Purchasing Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(data_router.router)
app.include_router(agent_router.router)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # Auto-seed on first run only (don't clobber data across restarts)
    from sqlalchemy.orm import Session
    from . import models
    with Session(engine) as db:
        if db.query(models.Product).count() == 0:
            seed.reset_and_seed()


@app.get("/api/health")
def health():
    return {"status": "ok", "reasoning_mode": "llm" if os.getenv("ANTHROPIC_API_KEY") else "rule_based"}


# Serve the static frontend (single-page dashboard) if present
frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

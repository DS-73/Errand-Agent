"""FastAPI application for the OrangeInvest errand agent.

Run in development:
    uvicorn webapp.backend.api:create_app --factory --reload --port 8000

Run the built frontend from this same server:
    python -m webapp
"""
from __future__ import annotations

import argparse
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import WebAgent, WebConfig

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


# --------------------------------------------------------------------- request bodies
class MessageIn(BaseModel):
    user_id: str = "u1"
    text: str = Field(min_length=1, max_length=4000)


class ApproveIn(BaseModel):
    user_id: str = "u1"
    decision: Literal["yes", "no"]


class EventsIn(BaseModel):
    user_id: str = "u1"


class ForgetIn(BaseModel):
    user_id: str = "u1"
    key: str = Field(min_length=1, max_length=200)


class CancelIn(BaseModel):
    user_id: str = "u1"
    provider_id: str | None = None


def _default_config() -> WebConfig:
    return WebConfig(
        user=os.environ.get("ERRAND_USER", "u1"),
        db=os.environ.get("ERRAND_DB", "data/web.db"),
        trace=os.environ.get("ERRAND_TRACE", "traces/web.jsonl"),
        seed=int(os.environ.get("ERRAND_SEED", "42")),
    )


def create_app(config: WebConfig | None = None) -> FastAPI:
    config = config or _default_config()
    agent = WebAgent(config)
    token = secrets.token_urlsafe(24)

    def require_token(x_web_token: str | None = Header(default=None)) -> None:
        """Reject requests that did not come from the served page.

        This is a localhost demo, but a per-process token still stops another
        local process from driving the agent through the API.
        """
        if not x_web_token or not secrets.compare_digest(x_web_token, token):
            raise HTTPException(status_code=403, detail="Invalid or missing X-Web-Token")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        agent.close()

    app = FastAPI(
        title="OrangeInvest Errand Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.agent = agent
    app.state.token = token

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session() -> dict:
        return {"token": token, **agent.session_info()}

    # ------------------------------------------------------------------ turns
    @app.post("/api/message", dependencies=[Depends(require_token)])
    def message(body: MessageIn) -> dict:
        return agent.handle_message(body.user_id, body.text)

    @app.post("/api/approve", dependencies=[Depends(require_token)])
    def approve(body: ApproveIn) -> dict:
        return agent.approve(body.user_id, body.decision)

    @app.post("/api/events", dependencies=[Depends(require_token)])
    def events(body: EventsIn) -> dict:
        return agent.poll_events(body.user_id)

    # ---------------------------------------------------------------- queries
    @app.get("/api/trace", dependencies=[Depends(require_token)])
    def trace(user_id: str = "u1",
              limit: int = Query(default=300, ge=1, le=1000)) -> dict:
        return {"events": agent.trace_events(user_id, limit)}

    @app.get("/api/memories", dependencies=[Depends(require_token)])
    def memories(user_id: str = "u1") -> dict:
        return {"memories": agent.memories(user_id)}

    @app.get("/api/bookings", dependencies=[Depends(require_token)])
    def bookings(user_id: str = "u1") -> dict:
        return {"bookings": agent.bookings(user_id)}

    @app.get("/api/state", dependencies=[Depends(require_token)])
    def state(user_id: str = "u1") -> dict:
        return agent.state(user_id)

    # ---------------------------------------------------------------- actions
    @app.post("/api/forget", dependencies=[Depends(require_token)])
    def forget(body: ForgetIn) -> dict:
        return agent.forget(body.user_id, body.key)

    @app.post("/api/simulate-cancel", dependencies=[Depends(require_token)])
    def simulate_cancel(body: CancelIn) -> dict:
        return agent.simulate_cancel(body.user_id, body.provider_id)

    # ------------------------------------------------------------ static frontend
    if FRONTEND_DIST.exists():
        assets = FRONTEND_DIST / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Unknown API route")
            candidate = (FRONTEND_DIST / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(FRONTEND_DIST.resolve()):
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/", include_in_schema=False)
    def root():
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({
            "message": "Frontend not built. Run `npm install && npm run build` "
                       "in webapp/frontend, or use the Vite dev server.",
            "docs": "/docs",
        })

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="OrangeInvest errand agent web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/web.db")
    parser.add_argument("--trace", default="traces/web.jsonl")
    parser.add_argument("--user", default="u1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.reload:
        os.environ["ERRAND_DB"] = args.db
        os.environ["ERRAND_TRACE"] = args.trace
        os.environ["ERRAND_USER"] = args.user
        os.environ["ERRAND_SEED"] = str(args.seed)
        uvicorn.run("webapp.backend.api:create_app", factory=True,
                    host=args.host, port=args.port, reload=True)
    else:
        config = WebConfig(user=args.user, db=args.db, trace=args.trace,
                           seed=args.seed)
        uvicorn.run(create_app(config), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

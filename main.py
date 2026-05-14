"""
FastAPI service for the KSA rideshare aggregator with multi-user OTP auth.

Endpoints:
    GET   /                           → static index.html (the comparison UI)
    GET   /login                      → /static/login.html
    GET   /api/health                 → app + db readiness
    GET   /api/auth/me                → current session info (per cookie)
    POST  /api/auth/start             → trigger OTP for { provider, phone }
    POST  /api/auth/verify            → submit OTP, on success set session cookie
    POST  /api/auth/logout            → clear server-side session + cookies
    POST  /api/quote                  → fetch fares using the caller's per-provider cookies

Cookies:
    sid_uber, sid_careem — each holds a server-issued session_id pointing to a
    DB row that stores the corresponding provider's storage_state.
"""

from __future__ import annotations

import asyncio
import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth_flow
import db
from providers.base import Location
from providers.careem import CareemProvider
from providers.uber import UberProvider

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
SESSIONS = ROOT / "sessions"
SESSIONS.mkdir(exist_ok=True)

# Bootstrap legacy file-based sessions from env if present (for the original
# shared-account demo mode; multi-user auth supersedes this).
for prov, env in [("uber", "AUTH_STATE_UBER"), ("careem", "AUTH_STATE_CAREEM")]:
    blob = os.environ.get(env)
    target = SESSIONS / f"auth_state_{prov}.json"
    if blob and not target.exists():
        try:
            target.write_bytes(base64.b64decode(blob))
            print(f"[bootstrap] wrote {target} from {env}")
        except Exception as e:
            print(f"[bootstrap] failed to decode {env}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    gc_task = asyncio.create_task(auth_flow.gc_pending())
    try:
        yield
    finally:
        gc_task.cancel()


app = FastAPI(title="KSA Rideshare Aggregator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

PROVIDERS = {p.name: p for p in [UberProvider(), CareemProvider()]}

COOKIE_SECURE = os.environ.get("RAILWAY_ENVIRONMENT") is not None
COOKIE_KW = {"httponly": True, "secure": COOKIE_SECURE, "samesite": "lax", "max_age": 60 * 60 * 24 * 30}


# ----------------------- request/response models ----------------------------


class Coord(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    label: str | None = None


class QuoteReq(BaseModel):
    pickup: Coord
    dropoff: Coord


class AuthStartReq(BaseModel):
    provider: str
    phone: str


class AuthVerifyReq(BaseModel):
    login_id: str
    otp: str


# ----------------------- helpers --------------------------------------------


def cookie_name(provider: str) -> str:
    return f"sid_{provider}"


async def session_for(request: Request, provider: str) -> dict | None:
    sid = request.cookies.get(cookie_name(provider))
    if not sid:
        return None
    rec = await db.load_session(sid)
    if not rec or rec["provider"] != provider:
        return None
    return rec["storage_state"]


# ----------------------- routes ---------------------------------------------


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/api/health")
async def health(request: Request):
    return {
        "status": "ok",
        "providers": [
            {
                "name": p.name,
                "session": bool(request.cookies.get(cookie_name(p.name))),
                "local_demo_session": p.has_local_session(),
            }
            for p in PROVIDERS.values()
        ],
    }


@app.get("/api/auth/me")
async def auth_me(request: Request):
    out = {}
    for name in PROVIDERS:
        sid = request.cookies.get(cookie_name(name))
        out[name] = {"signed_in": bool(sid)}
        if sid:
            rec = await db.load_session(sid)
            out[name]["valid"] = rec is not None
    return out


@app.post("/api/auth/start")
async def auth_start(req: AuthStartReq):
    if req.provider not in PROVIDERS:
        raise HTTPException(400, f"unknown provider {req.provider}")
    if not req.phone.strip():
        raise HTTPException(400, "phone required")
    result = await auth_flow.start(req.provider, req.phone.strip())
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "start failed"))
    return result


@app.post("/api/auth/verify")
async def auth_verify(req: AuthVerifyReq, response: Response):
    if not req.otp.strip():
        raise HTTPException(400, "otp required")
    result = await auth_flow.verify(req.login_id, req.otp.strip())
    if not result.get("ok"):
        # Strip oversized fields from error responses to keep them under 1MB
        result.pop("storage_state", None)
        return JSONResponse(result, status_code=400)

    provider = result["provider"]
    storage = result["storage_state"]
    sid = await db.save_session(provider, storage)
    response.set_cookie(cookie_name(provider), sid, **COOKIE_KW)
    return {"ok": True, "provider": provider}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    for name in PROVIDERS:
        sid = request.cookies.get(cookie_name(name))
        if sid:
            await db.delete_session(sid)
            response.delete_cookie(cookie_name(name))
    return {"ok": True}


@app.post("/api/quote")
async def quote(req: QuoteReq, request: Request):
    pickup = Location(lat=req.pickup.lat, lng=req.pickup.lng, label=req.pickup.label)
    dropoff = Location(lat=req.dropoff.lat, lng=req.dropoff.lng, label=req.dropoff.label)

    async def safe_fetch(name: str, provider):
        storage = await session_for(request, name)
        if storage is None and not provider.has_local_session():
            return name, {"ready": False, "error": "not signed in", "quotes": []}
        try:
            quotes = await provider.fetch(pickup, dropoff, storage_state=storage)
            return name, {"ready": True, "quotes": [q.to_dict() for q in quotes]}
        except Exception as e:
            return name, {"ready": True, "error": str(e), "quotes": []}

    results = await asyncio.gather(*(safe_fetch(name, p) for name, p in PROVIDERS.items()))
    return {
        "pickup": req.pickup.model_dump(),
        "dropoff": req.dropoff.model_dump(),
        "providers": {name: value for name, value in results},
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

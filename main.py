"""
FastAPI service that aggregates rideshare fare quotes.

Endpoints:
    GET  /                  → static index.html
    GET  /api/health        → { status, providers: [{name, ready}] }
    POST /api/quote         → { pickup, dropoff } → list of FareQuotes
    POST /api/geocode       → { query, lat?, lng? } → list of place suggestions

Session bootstrapping for Railway:
    Auth state files (sessions/auth_state_*.json) are gitignored. To deploy,
    base64-encode each file and set as env var, e.g.:
        AUTH_STATE_UBER=$(base64 < sessions/auth_state_uber.json)
        AUTH_STATE_CAREEM=$(base64 < sessions/auth_state_careem.json)
    On startup main.py writes them back to sessions/ if present.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from providers.base import Location
from providers.careem import CareemProvider
from providers.uber import UberProvider

ROOT = Path(__file__).parent
SESSIONS = ROOT / "sessions"
SESSIONS.mkdir(exist_ok=True)
STATIC = ROOT / "static"

# Bootstrap session files from env vars if present (used on Railway).
for prov, env in [("uber", "AUTH_STATE_UBER"), ("careem", "AUTH_STATE_CAREEM")]:
    blob = os.environ.get(env)
    target = SESSIONS / f"auth_state_{prov}.json"
    if blob and not target.exists():
        try:
            target.write_bytes(base64.b64decode(blob))
            print(f"[bootstrap] wrote {target} from {env}")
        except Exception as e:
            print(f"[bootstrap] failed to decode {env}: {e}")

app = FastAPI(title="KSA Rideshare Aggregator")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

PROVIDERS = [UberProvider(), CareemProvider()]


class Coord(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    label: str | None = None


class QuoteReq(BaseModel):
    pickup: Coord
    dropoff: Coord


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "providers": [{"name": p.name, "ready": p.has_session()} for p in PROVIDERS],
    }


@app.post("/api/quote")
async def quote(req: QuoteReq):
    pickup = Location(lat=req.pickup.lat, lng=req.pickup.lng, label=req.pickup.label)
    dropoff = Location(lat=req.dropoff.lat, lng=req.dropoff.lng, label=req.dropoff.label)

    async def safe_fetch(provider):
        if not provider.has_session():
            return provider.name, {"error": "not authenticated — run auth.py", "ready": False}
        try:
            return provider.name, await provider.fetch(pickup, dropoff)
        except Exception as e:
            return provider.name, {"error": str(e), "ready": True}

    results = await asyncio.gather(*(safe_fetch(p) for p in PROVIDERS))
    out = {}
    for name, value in results:
        if isinstance(value, dict) and value.get("error"):
            out[name] = {"ready": value.get("ready", False), "error": value["error"], "quotes": []}
        else:
            out[name] = {"ready": True, "quotes": [q.to_dict() for q in value]}
    return {"pickup": req.pickup.model_dump(), "dropoff": req.dropoff.model_dump(), "providers": out}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

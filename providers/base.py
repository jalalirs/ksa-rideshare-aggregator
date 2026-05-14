"""Provider base class + shared models."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SESSIONS = Path(__file__).parent.parent / "sessions"


@dataclass
class Location:
    lat: float
    lng: float
    label: str | None = None


@dataclass
class FareQuote:
    provider: str
    product: str
    fare_low: float | None
    fare_high: float | None
    currency: str
    eta_seconds: int | None
    raw: dict[str, Any] = field(default_factory=dict)

    def display_price(self) -> str:
        if self.fare_low is None:
            return "—"
        if self.fare_high and self.fare_high != self.fare_low:
            return f"{self.fare_low:.0f}–{self.fare_high:.0f} {self.currency}"
        return f"{self.fare_low:.0f} {self.currency}"

    def display_eta(self) -> str:
        if not self.eta_seconds:
            return "—"
        return f"{self.eta_seconds // 60} min"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["display_price"] = self.display_price()
        d["display_eta"] = self.display_eta()
        return d


class Provider:
    name: str
    session_file: str

    async def fetch(self, pickup: Location, dropoff: Location, storage_state: dict | None = None) -> list[FareQuote]:
        """Fetch fare quotes. If storage_state is None, falls back to local file (dev mode)."""
        raise NotImplementedError

    def session_path(self) -> Path:
        return SESSIONS / self.session_file

    def has_local_session(self) -> bool:
        return self.session_path().exists()

    def resolve_storage(self, storage_state: dict | None) -> dict | str | None:
        """Return either an inline dict for Playwright or a file path; None if nothing."""
        if storage_state:
            return storage_state
        if self.has_local_session():
            return str(self.session_path())
        return None


def walk_products(node: Any):
    """Yield dict nodes that smell like ride products."""
    if isinstance(node, dict):
        keys = set(node.keys())
        looks_like_product = (
            keys & {"displayName", "productName", "name", "carType", "serviceAreaName", "vehicleType", "productGroup"}
            and keys & {"fareLow", "fareHigh", "lowEstimate", "highEstimate", "fare", "estimate", "minFare", "maxFare", "price", "totalFare", "fareString"}
        )
        if looks_like_product:
            yield node
        for v in node.values():
            yield from walk_products(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_products(v)


def coerce_money(d: dict, keys: list[str]) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            for kk in ("amount", "value", "lowEstimate", "highEstimate", "low", "high"):
                if isinstance(v.get(kk), (int, float)):
                    return float(v[kk])
        if isinstance(v, str):
            m = re.search(r"(\d[\d.,]*)", v)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
    return None


def coerce_int(d: dict, keys: list[str]) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None

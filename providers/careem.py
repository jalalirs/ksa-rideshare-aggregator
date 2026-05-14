"""Careem provider — same shape as Uber, different selectors."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import async_playwright

from .base import FareQuote, Location, Provider, coerce_int, coerce_money, walk_products


class CareemProvider(Provider):
    name = "careem"
    session_file = "auth_state_careem.json"

    QUOTE_RE = re.compile(
        r"(estimate|fare|quote|product|servicearea|car-?type|ride.?type)",
        re.I,
    )

    async def fetch(self, pickup: Location, dropoff: Location) -> list[FareQuote]:
        if not self.has_session():
            return []

        payloads: list[dict[str, Any]] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                storage_state=str(self.session_path()),
                locale="en-SA",
                timezone_id="Asia/Riyadh",
                geolocation={"latitude": pickup.lat, "longitude": pickup.lng},
                permissions=["geolocation"],
                viewport={"width": 412, "height": 915},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
                ),
            )
            page = await ctx.new_page()

            async def on_response(resp):
                if "careem" not in resp.url:
                    return
                if not self.QUOTE_RE.search(resp.url):
                    return
                try:
                    body = await resp.json()
                    payloads.append({"url": resp.url, "body": body})
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                # Careem's web booking flow does not deep-link cleanly via URL params
                # for unauth'd guest quotes. Once authed we try to land directly on
                # the booking page; if needed, future iterations can drive the UI.
                await page.goto("https://app.careem.com/", wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(8000)
                # Once we know the exact URL structure from discover.py, replace this
                # with a direct booking deep-link.
            except Exception:
                pass

            await ctx.close()
            await browser.close()

        return list(self._parse_all(payloads))

    def _parse_all(self, payloads: list[dict[str, Any]]):
        for payload in payloads:
            for product in walk_products(payload["body"]):
                yield FareQuote(
                    provider=self.name,
                    product=str(product.get("name") or product.get("carType") or product.get("serviceAreaName") or "Ride"),
                    fare_low=coerce_money(product, ["minFare", "fareLow", "lowEstimate", "estimate", "price", "totalFare"]),
                    fare_high=coerce_money(product, ["maxFare", "fareHigh", "highEstimate", "estimate", "price", "totalFare"]),
                    currency=str(product.get("currency") or product.get("currencyCode") or "SAR"),
                    eta_seconds=coerce_int(product, ["eta", "pickupEta", "etaSeconds"]),
                    raw=product,
                )

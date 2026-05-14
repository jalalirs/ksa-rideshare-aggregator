"""Uber provider — drives the authenticated web booking flow with a saved session."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import async_playwright

from .base import FareQuote, Location, Provider, coerce_int, coerce_money, walk_products


class UberProvider(Provider):
    name = "uber"
    session_file = "auth_state_uber.json"

    # URLs that are known/likely to carry fare data once we're authenticated.
    # We match broadly; the parser is what does the heavy lifting.
    QUOTE_RE = re.compile(
        r"(fareEstimate|getFareEstimate|loadFEEstimate|getRideEstimates|trip/estimate|getProducts|/rt/riders/products|/rt/riders/fare)",
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
                if not self.QUOTE_RE.search(resp.url):
                    return
                try:
                    body = await resp.json()
                    payloads.append({"url": resp.url, "body": body})
                except Exception:
                    pass

            page.on("response", on_response)

            deep_link = (
                "https://m.uber.com/go/product-selection?"
                f'pickup={{"latitude":{pickup.lat},"longitude":{pickup.lng}}}&'
                f'drop[0]={{"latitude":{dropoff.lat},"longitude":{dropoff.lng}}}'
            )

            try:
                await page.goto(deep_link, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(8000)
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
                    product=str(product.get("displayName") or product.get("productName") or product.get("name") or "Ride"),
                    fare_low=coerce_money(product, ["fareLow", "lowEstimate", "minimumFare", "fare", "minFare"]),
                    fare_high=coerce_money(product, ["fareHigh", "highEstimate", "maximumFare", "fare", "maxFare"]),
                    currency=str(product.get("currencyCode") or product.get("currency") or "SAR"),
                    eta_seconds=coerce_int(product, ["pickupEta", "etaSeconds", "etaInSeconds", "pickupTimeSeconds"]),
                    raw=product,
                )

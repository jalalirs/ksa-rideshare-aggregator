"""
Discover the authenticated fare-quote endpoints.

Run AFTER auth.py — loads each saved session, drives a Riyadh booking flow,
logs every XHR, and writes the result to discover_out/<provider>.json.

Usage:
    python discover.py uber
    python discover.py careem
    python discover.py both
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SESSIONS = Path(__file__).parent / "sessions"
OUT = Path(__file__).parent / "discover_out"
OUT.mkdir(exist_ok=True)

PICKUP_LAT, PICKUP_LNG = 24.7607, 46.6420  # KAFD
DROP_LAT, DROP_LNG = 24.7117, 46.6745  # Kingdom Centre


async def run_uber():
    state = SESSIONS / "auth_state_uber.json"
    if not state.exists():
        print(f"  ! {state} missing — run `python auth.py uber` first.")
        return

    log = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            storage_state=str(state),
            locale="en-SA",
            timezone_id="Asia/Riyadh",
            geolocation={"latitude": PICKUP_LAT, "longitude": PICKUP_LNG},
            permissions=["geolocation"],
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if not (("uber.com" in url) and ("/api/" in url or "/rt/" in url or "/graphql" in url or "estimate" in url.lower() or "fare" in url.lower())):
                return
            entry = {"url": url, "status": resp.status, "method": resp.request.method}
            try:
                if resp.request.method in ("POST", "PUT"):
                    entry["req"] = resp.request.post_data
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" in ct or len(entry.get("req") or "") > 0:
                    entry["res"] = (await resp.text())[:5000]
            except Exception:
                pass
            log.append(entry)

        page.on("response", on_response)

        deep_link = (
            "https://m.uber.com/go/product-selection?"
            f'pickup={{"latitude":{PICKUP_LAT},"longitude":{PICKUP_LNG}}}&'
            f'drop[0]={{"latitude":{DROP_LAT},"longitude":{DROP_LNG}}}'
        )
        print(f"  → opening: {deep_link}")
        await page.goto(deep_link, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(15_000)

        url = page.url
        print(f"  → final URL: {url}")
        if "auth." in url:
            print("  ⚠️  Got bounced to auth — session is stale or invalid. Re-run auth.py.")

        try:
            (OUT / "uber.html").write_text((await page.content())[:400_000])
        except Exception:
            pass

        input("  >> inspect, then press Enter to close browser: ")

        await ctx.close()
        await browser.close()

    (OUT / "uber.json").write_text(json.dumps(log, indent=2, default=str))
    print(f"  ✓ {len(log)} XHRs → {OUT / 'uber.json'}")

    # Surface anything that smells like a fare endpoint
    print("\n  ── Interesting URLs (fare/estimate/product/price) ──")
    seen = set()
    for e in log:
        if any(k in e["url"].lower() for k in ("fare", "estimate", "product", "price")):
            base = e["url"].split("?")[0]
            if base in seen:
                continue
            seen.add(base)
            print(f"    {e['method']:5} {e['status']} {base}")
            if e.get("req"):
                print(f"      REQ: {str(e['req'])[:300]}")
            if e.get("res"):
                print(f"      RES: {str(e['res'])[:400]}")


async def run_careem():
    state = SESSIONS / "auth_state_careem.json"
    if not state.exists():
        print(f"  ! {state} missing — run `python auth.py careem` first.")
        return

    log = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            storage_state=str(state),
            locale="en-SA",
            timezone_id="Asia/Riyadh",
            geolocation={"latitude": PICKUP_LAT, "longitude": PICKUP_LNG},
            permissions=["geolocation"],
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if "careem" not in url:
                return
            if not ("/api/" in url or "/v1/" in url or "/v2/" in url or "graphql" in url or "estimate" in url.lower() or "fare" in url.lower() or "quote" in url.lower()):
                return
            entry = {"url": url, "status": resp.status, "method": resp.request.method}
            try:
                if resp.request.method in ("POST", "PUT"):
                    entry["req"] = resp.request.post_data
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" in ct or len(entry.get("req") or "") > 0:
                    entry["res"] = (await resp.text())[:5000]
            except Exception:
                pass
            log.append(entry)

        page.on("response", on_response)

        await page.goto("https://app.careem.com/", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(5000)

        print(f"  → page now at: {page.url}")
        print(f"  ⚠️  Manually drive a booking in Riyadh:")
        print(f"     pickup: KAFD or any Riyadh address")
        print(f"     dropoff: Kingdom Centre")
        print(f"     Don't confirm the ride — just get to the product/fare screen.")
        input("  >> when you see fares on screen, press Enter to capture: ")

        await page.wait_for_timeout(2000)
        try:
            (OUT / "careem.html").write_text((await page.content())[:400_000])
        except Exception:
            pass

        await ctx.close()
        await browser.close()

    (OUT / "careem.json").write_text(json.dumps(log, indent=2, default=str))
    print(f"  ✓ {len(log)} XHRs → {OUT / 'careem.json'}")

    print("\n  ── Interesting Careem URLs ──")
    seen = set()
    for e in log:
        if any(k in e["url"].lower() for k in ("fare", "estimate", "product", "price", "quote", "ride")):
            base = e["url"].split("?")[0]
            if base in seen:
                continue
            seen.add(base)
            print(f"    {e['method']:5} {e['status']} {base}")
            if e.get("req"):
                print(f"      REQ: {str(e['req'])[:300]}")
            if e.get("res"):
                print(f"      RES: {str(e['res'])[:400]}")


async def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"uber", "careem", "both"}:
        print("Usage: python discover.py [uber|careem|both]")
        sys.exit(1)
    target = sys.argv[1]
    if target in ("uber", "both"):
        await run_uber()
    if target in ("careem", "both"):
        await run_careem()


if __name__ == "__main__":
    asyncio.run(main())

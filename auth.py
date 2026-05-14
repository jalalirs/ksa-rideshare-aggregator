"""
Interactive OTP login. Run this LOCALLY on your machine.

It opens a real Chromium window, lands you on the provider's login page, and
waits for you to enter phone + OTP yourself. When you press Enter in the
terminal it saves the storage state (cookies + localStorage) to disk.

Usage:
    python auth.py uber
    python auth.py careem
    python auth.py both        # do both in one run

The saved state files (auth_state_uber.json, auth_state_careem.json) are
git-ignored. To deploy: base64-encode them into Railway env vars (see deploy.sh).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SESSIONS = Path(__file__).parent / "sessions"
SESSIONS.mkdir(exist_ok=True)


PROVIDERS = {
    "uber": {
        "login_url": "https://auth.uber.com/v2/",
        "post_login_check_url": "https://m.uber.com/go/home",
        "ready_signal": "You should now be signed in on m.uber.com",
    },
    "careem": {
        "login_url": "https://app.careem.com/",
        "post_login_check_url": "https://app.careem.com/",
        "ready_signal": "You should now be signed in on app.careem.com",
    },
}


async def login(provider: str) -> None:
    cfg = PROVIDERS[provider]
    state_path = SESSIONS / f"auth_state_{provider}.json"

    print(f"\n{'=' * 60}")
    print(f"  {provider.upper()} OTP LOGIN")
    print(f"{'=' * 60}")
    print(f"  → Opening {cfg['login_url']}")
    print(f"  → 1. Enter your phone number in the browser window")
    print(f"  → 2. Receive the OTP on your phone")
    print(f"  → 3. Enter the OTP in the browser")
    print(f"  → 4. Complete any captcha / extra steps shown")
    print(f"  → 5. {cfg['ready_signal']}")
    print(f"  → 6. Come back HERE and press Enter to save the session")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            locale="en-SA",
            timezone_id="Asia/Riyadh",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        try:
            await page.goto(cfg["login_url"], wait_until="domcontentloaded")
        except Exception as e:
            print(f"  ! initial navigation hiccup: {e}")

        # Wait for the user to finish login interactively.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "   >> press Enter once you're signed in: ")

        # Optional: navigate to confirm signed-in state on the post-login page.
        try:
            await page.goto(cfg["post_login_check_url"], wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            url = page.url
            print(f"   final URL: {url}")
            if "auth." in url or "/login" in url:
                print("   ⚠️  Looks like we landed back on a login page. Session may not be valid.")
        except Exception as e:
            print(f"   ! check failed: {e}")

        await ctx.storage_state(path=str(state_path))
        print(f"   ✓ saved → {state_path}")

        await ctx.close()
        await browser.close()


async def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"uber", "careem", "both"}:
        print("Usage: python auth.py [uber|careem|both]")
        sys.exit(1)

    target = sys.argv[1]
    providers = ["uber", "careem"] if target == "both" else [target]
    for p in providers:
        await login(p)


if __name__ == "__main__":
    asyncio.run(main())

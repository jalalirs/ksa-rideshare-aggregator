"""
In-process pending-login store + provider-specific OTP flows.

A `start()` call launches a Playwright BrowserContext, submits the user's phone,
and keeps the context alive in memory keyed by login_id. A subsequent `verify()`
call retrieves that context, submits the OTP, and on success extracts the
storage_state for persistence.

Design notes:
  - One BrowserContext per pending login, ~150-200MB memory each.
  - Pending logins time out after PENDING_TTL_SECONDS (default 8 min).
  - OTP field selectors are discovered adaptively rather than hardcoded.
  - Captcha or extra challenge screens cause verify() to return needs_more=True
    with a screenshot — the caller can surface this back to the user.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

PENDING_TTL_SECONDS = 480  # 8 min


@dataclass
class PendingLogin:
    id: str
    provider: str
    phone: str
    created_at: float
    # Playwright handles are held for the lifetime of the pending login.
    pw_ctx: Any = None  # async_playwright() context manager value
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    note: str = ""

    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_TTL_SECONDS


# In-memory store. Lost on process restart, which is fine — user just retries.
PENDING: dict[str, PendingLogin] = {}
_LOCK = asyncio.Lock()


def _new_id() -> str:
    return uuid.uuid4().hex


async def _screenshot_b64(page: Page) -> str:
    try:
        png = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(png).decode()
    except Exception:
        return ""


async def _cleanup(pl: PendingLogin):
    try:
        if pl.context:
            await pl.context.close()
        if pl.browser:
            await pl.browser.close()
        if pl.pw_ctx:
            await pl.pw_ctx.__aexit__(None, None, None)
    except Exception:
        pass


async def gc_pending():
    """Background sweeper — drops expired pending logins."""
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            expired = [k for k, v in PENDING.items() if now - v.created_at > PENDING_TTL_SECONDS]
            for k in expired:
                pl = PENDING.pop(k, None)
                if pl:
                    print(f"[auth_flow] gc expired login {k} ({pl.provider})")
                    await _cleanup(pl)
        except Exception as e:
            print(f"[auth_flow] gc error: {e}")


# --------------------------------------------------------------------------
# Provider-specific flows
# --------------------------------------------------------------------------


async def _start_uber(pl: PendingLogin, phone: str) -> dict:
    pw_ctx = async_playwright()
    pl.pw_ctx = await pw_ctx.__aenter__()
    pl.browser = await pl.pw_ctx.chromium.launch(headless=True)
    pl.context = await pl.browser.new_context(
        locale="en-SA",
        timezone_id="Asia/Riyadh",
        viewport={"width": 412, "height": 915},
        user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    )
    pl.page = await pl.context.new_page()
    await pl.page.goto("https://auth.uber.com/v2/", wait_until="domcontentloaded", timeout=45_000)
    await pl.page.wait_for_timeout(2500)

    await pl.page.fill("#PHONE_NUMBER_or_EMAIL_ADDRESS", phone)
    await pl.page.wait_for_timeout(400)

    # Find the visible "Continue" button (the first submit is the password-less continue)
    btn = pl.page.get_by_role("button", name="Continue", exact=False).first
    await btn.click()
    await pl.page.wait_for_timeout(4000)

    # Detect what came next: OTP field, captcha, or error
    state = await _detect_next_step(pl.page)
    return state


async def _start_careem(pl: PendingLogin, phone: str) -> dict:
    pw_ctx = async_playwright()
    pl.pw_ctx = await pw_ctx.__aenter__()
    pl.browser = await pl.pw_ctx.chromium.launch(headless=True)
    pl.context = await pl.browser.new_context(
        locale="en-SA",
        timezone_id="Asia/Riyadh",
        viewport={"width": 412, "height": 915},
        user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    )
    pl.page = await pl.context.new_page()
    await pl.page.goto("https://app.careem.com/", wait_until="domcontentloaded", timeout=45_000)
    await pl.page.wait_for_timeout(4000)

    # Careem expects the local mobile number (placeholder "50 123 4567").
    # We strip any country code (e.g. "+9665..." → "5...").
    local = phone.lstrip("+").lstrip("0")
    if local.startswith("966"):
        local = local[3:]
    await pl.page.fill('input[type="tel"]', local)
    await pl.page.wait_for_timeout(400)

    btn = pl.page.get_by_role("button", name="Continue", exact=False).first
    await btn.click()
    await pl.page.wait_for_timeout(4000)

    return await _detect_next_step(pl.page)


async def _detect_next_step(page: Page) -> dict:
    """Look at the page and decide whether we need OTP, captcha, or are done.

    We always include a screenshot so the client can verify what the server is
    actually seeing — the heuristics here are necessarily fragile.
    """
    info = await page.evaluate("""
        () => {
            const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
                type: i.type, name: i.name||'', id: i.id||'',
                placeholder: i.placeholder||'', aria: i.getAttribute('aria-label')||'',
                autocomplete: i.autocomplete||'',
                visible: i.offsetParent !== null,
            })).filter(i => i.visible);
            const heading = (document.querySelector('h1, h2, [role="heading"]')||{}).innerText || '';
            return {
                url: location.href,
                heading: heading.trim().slice(0, 200),
                text_sample: document.body.innerText.slice(0, 1200),
                has_captcha: /captcha|i'm not a robot|recaptcha|hcaptcha|challenge[- ]?press/i.test(document.body.innerText) || !!document.querySelector('iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i]'),
                has_error: /try again|incorrect|invalid|too many|something went wrong|couldn't find|isn't recognized|not a valid|invalid phone/i.test(document.body.innerText),
                inputs,
            };
        }
    """)
    screenshot = await _screenshot_b64(page)

    # Strong signal: a visible input that explicitly looks like an OTP field.
    otp_input = None
    for i in info["inputs"]:
        sig = " ".join([i["name"], i["id"], i["placeholder"], i["aria"], i["autocomplete"]]).lower()
        if i["type"] in ("number", "tel", "text", "password") and any(
            kw in sig for kw in ("one-time", "otp", "verif", "pin")
        ):
            otp_input = i
            break

    if info["has_captcha"]:
        return {"next": "captcha", "screenshot": screenshot, "info": info}
    if otp_input:
        return {"next": "otp", "info": info, "otp_input": otp_input, "screenshot": screenshot}
    if info["has_error"]:
        return {"next": "error", "info": info, "screenshot": screenshot}
    # Default to "unknown" with the full picture — the UI shows screenshot + text
    # so the user can tell us what actually appeared.
    return {"next": "unknown", "info": info, "screenshot": screenshot}


async def _verify_uber(pl: PendingLogin, otp: str) -> dict:
    return await _submit_otp_generic(pl, otp)


async def _verify_careem(pl: PendingLogin, otp: str) -> dict:
    return await _submit_otp_generic(pl, otp)


async def _submit_otp_generic(pl: PendingLogin, otp: str) -> dict:
    page = pl.page
    # Locate OTP input adaptively.
    sel = await page.evaluate("""
        () => {
            const inputs = Array.from(document.querySelectorAll('input')).filter(i => i.offsetParent !== null);
            // Try strongest signals first: autocomplete="one-time-code"
            let pick = inputs.find(i => (i.autocomplete||'').toLowerCase().includes('one-time'));
            if (pick) return pick.id || pick.name || null;
            // Match name/id/placeholder/aria for OTP-ish wording
            const re = /(otp|code|verif|pin|one.time)/i;
            pick = inputs.find(i => re.test(i.name || '') || re.test(i.id || '') || re.test(i.placeholder || '') || re.test(i.getAttribute('aria-label')||''));
            if (pick) return pick.id ? '#' + pick.id : (pick.name ? `input[name="${pick.name}"]` : 'input[type="tel"], input[type="text"], input[type="number"]');
            // Fallback: any visible numeric input
            pick = inputs.find(i => ['tel','number','text','password'].includes(i.type));
            if (pick) return pick.id ? '#' + pick.id : (pick.name ? `input[name="${pick.name}"]` : null);
            return null;
        }
    """)
    if not sel:
        return {"ok": False, "reason": "no OTP input found", "screenshot": await _screenshot_b64(page)}

    try:
        if sel.startswith("#") or sel.startswith("input"):
            await page.fill(sel, otp)
        else:
            await page.locator(f"#{sel}").fill(otp)
    except Exception as e:
        return {"ok": False, "reason": f"fill failed: {e}", "screenshot": await _screenshot_b64(page)}

    await page.wait_for_timeout(600)

    # Try to find and click a "Verify" / "Continue" / "Next" button
    for name in ["Verify", "Continue", "Next", "Submit", "Confirm"]:
        try:
            btn = page.get_by_role("button", name=name, exact=False).first
            if await btn.count() > 0:
                await btn.click()
                break
        except Exception:
            continue
    # Some flows auto-submit on 4th/6th digit; either way wait a bit.
    await page.wait_for_timeout(6000)

    final_url = page.url
    text = (await page.content()).lower()
    looks_authenticated = (
        "auth.uber.com" not in final_url and "auth.careem.com" not in final_url
        and "/login" not in final_url
    )
    if not looks_authenticated:
        return {"ok": False, "reason": "still on auth page", "url": final_url, "screenshot": await _screenshot_b64(page)}

    storage = await pl.context.storage_state()
    return {"ok": True, "storage_state": storage, "url": final_url}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


STARTERS = {"uber": _start_uber, "careem": _start_careem}
VERIFIERS = {"uber": _verify_uber, "careem": _verify_careem}


async def start(provider: str, phone: str) -> dict:
    if provider not in STARTERS:
        return {"ok": False, "reason": f"unknown provider {provider}"}
    pl = PendingLogin(id=_new_id(), provider=provider, phone=phone, created_at=time.time())
    try:
        state = await STARTERS[provider](pl, phone)
    except Exception as e:
        await _cleanup(pl)
        return {"ok": False, "reason": f"start failed: {e}"}
    PENDING[pl.id] = pl
    return {"ok": True, "login_id": pl.id, "state": state}


async def verify(login_id: str, otp: str) -> dict:
    async with _LOCK:
        pl = PENDING.get(login_id)
    if not pl:
        return {"ok": False, "reason": "login_id not found or expired"}
    if pl.expired():
        await _cleanup(pl)
        PENDING.pop(login_id, None)
        return {"ok": False, "reason": "login expired — start again"}
    try:
        result = await VERIFIERS[pl.provider](pl, otp)
    except Exception as e:
        return {"ok": False, "reason": f"verify failed: {e}"}
    if result.get("ok"):
        PENDING.pop(login_id, None)
        await _cleanup(pl)
        result["provider"] = pl.provider
    return result

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

# Realistic desktop Chrome UA — datacenter IP + mobile UA is a suspicious combo
# that automated-bot detectors flag immediately.
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Extra fingerprint spoofs applied via init script. Stealth handles most of
# this but we layer some additional defenses for the Arkose / "Protecting your
# account" puzzle that Uber shows from datacenter IPs.
STEALTH_INIT_JS = """
// Remove webdriver flag entirely
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Mimic plausible plugin set
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
    ],
});

Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'ar'] });

// Spoof a non-empty connection.rtt to look like a real network
try {
    const conn = navigator.connection || {};
    Object.defineProperty(navigator, 'connection', {
        get: () => Object.assign({}, conn, { rtt: 100, downlink: 10, effectiveType: '4g' }),
    });
} catch (e) {}

// Chrome runtime presence — bots usually miss this
window.chrome = window.chrome || { runtime: {} };

// permissions.query: honor 'notifications' the way real Chrome does
const _origQuery = navigator.permissions && navigator.permissions.query;
if (_origQuery) {
    navigator.permissions.query = (p) => (p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery.call(navigator.permissions, p));
}
"""


async def _harden_context(context: BrowserContext) -> None:
    """Apply all anti-detection tricks before any navigation happens."""
    await context.add_init_script(STEALTH_INIT_JS)


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


async def _launch_stealth(pl: PendingLogin) -> None:
    """Spin up a stealth-hardened browser context for this pending login."""
    pw_ctx = async_playwright()
    pl.pw_ctx = await pw_ctx.__aenter__()
    pl.browser = await pl.pw_ctx.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )
    pl.context = await pl.browser.new_context(
        # en-SA so the provider's country picker defaults to Saudi rather than US
        locale="en-SA",
        timezone_id="Asia/Riyadh",
        viewport={"width": 1366, "height": 768},
        user_agent=DESKTOP_UA,
        extra_http_headers={
            "Accept-Language": "en-SA,en;q=0.9,ar;q=0.8",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
        },
    )
    await _harden_context(pl.context)
    pl.page = await pl.context.new_page()


async def _start_uber(pl: PendingLogin, identifier: str) -> dict:
    """Uber accepts EITHER a phone number or email in the same input.

    Phone path is hard-blocked by SIGNUP_OTP_FRAUD_DENIED from datacenter
    IPs. Email path goes through email verification which doesn't share the
    same fraud engine — far more likely to succeed.
    """
    await _launch_stealth(pl)
    shots: list[dict] = []

    async def snap(label: str):
        shots.append({"label": label, "url": pl.page.url, "shot": await _screenshot_b64(pl.page)})

    await pl.page.goto("https://auth.uber.com/v2/", wait_until="domcontentloaded", timeout=45_000)
    await pl.page.wait_for_timeout(5000)
    await snap("01_loaded")

    # If the user provided an email, type as-is. Otherwise normalize to
    # international phone format.
    is_email = "@" in identifier
    if is_email:
        to_type = identifier.strip()
    else:
        digits = "".join(c for c in identifier if c.isdigit())
        if digits.startswith("00966"):
            digits = digits[2:]
        elif digits.startswith("0") and not digits.startswith("00"):
            digits = "966" + digits[1:]
        elif not digits.startswith("966") and len(digits) <= 10:
            digits = "966" + digits
        to_type = "+" + digits
    international = to_type

    sel = "#PHONE_NUMBER_or_EMAIL_ADDRESS"
    await pl.page.click(sel)
    await pl.page.fill(sel, "")
    await pl.page.type(sel, international, delay=80)
    # Blur the input so React's onChange/onBlur finishes its validation cycle
    await pl.page.evaluate("document.activeElement && document.activeElement.blur()")
    await pl.page.wait_for_timeout(1500)
    await snap("02_typed")

    typed_value = await pl.page.input_value(sel)
    pl.note = f"input_value={typed_value!r}"

    # Capture network requests during the submit attempt so we can see if
    # Uber's auth backend is rejecting the request.
    submit_traffic: list[dict] = []

    def _on_req(req):
        if "uber.com" in req.url and req.method != "GET":
            submit_traffic.append({"url": req.url, "method": req.method, "headers": dict(req.headers), "post": (req.post_data or "")[:600]})

    async def _on_resp(resp):
        if "uber.com" not in resp.url:
            return
        entry = next((t for t in submit_traffic if t["url"] == resp.url), None)
        if not entry:
            return
        entry["status"] = resp.status
        # Capture body for the auth-relevant endpoints (skip _events analytics)
        if "/_events" in resp.url:
            return
        try:
            entry["resp_body"] = (await resp.text())[:2000]
        except Exception:
            pass

    pl.page.on("request", _on_req)
    pl.page.on("response", lambda r: asyncio.create_task(_on_resp(r)))

    # Strategy 1: try the most React-friendly submit — find the submit-typed
    # button by DOM walk and call .click() from inside the page context.
    click_result = await pl.page.evaluate("""
        () => {
            const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
            const submit = buttons.find(b => b.type === 'submit') ||
                           buttons.find(b => (b.innerText || '').trim() === 'Continue');
            if (!submit) return {clicked: false, reason: 'no submit button found'};
            const disabled = submit.disabled || submit.getAttribute('aria-disabled') === 'true';
            submit.click();
            return {clicked: true, disabled, text: (submit.innerText||'').trim().slice(0, 40)};
        }
    """)
    pl.note += f" | js_click={click_result}"
    await pl.page.wait_for_timeout(3000)
    await snap("03_after_js_click")

    # Strategy 2: if still on same URL, try form.requestSubmit() as a fallback
    if "auth.uber.com/v2/" in pl.page.url and pl.page.url.endswith("v2/"):
        rs_result = await pl.page.evaluate("""
            () => {
                const forms = Array.from(document.querySelectorAll('form'));
                if (!forms.length) return 'no form found';
                const f = forms[0];
                if (f.requestSubmit) { f.requestSubmit(); return 'requestSubmit fired'; }
                else { f.submit(); return 'submit() fired'; }
            }
        """)
        pl.note += f" | form_submit={rs_result}"
        await pl.page.wait_for_timeout(3000)
        await snap("04_after_form_submit")

    # Strategy 3: keyboard Enter on the focused input
    if "auth.uber.com/v2/" in pl.page.url and pl.page.url.endswith("v2/"):
        await pl.page.focus(sel)
        await pl.page.keyboard.press("Enter")
        await pl.page.wait_for_timeout(3000)
        await snap("05_after_enter")

    await pl.page.wait_for_timeout(4000)
    await snap("06_settled")

    state = await _detect_next_step(pl.page)
    state["shots"] = shots
    state["note"] = pl.note
    state["typed_value"] = typed_value
    state["submit_traffic"] = submit_traffic
    return state


async def _start_careem(pl: PendingLogin, phone: str) -> dict:
    await _launch_stealth(pl)
    await pl.page.goto("https://app.careem.com/", wait_until="domcontentloaded", timeout=45_000)
    await pl.page.wait_for_timeout(4500)

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
                has_captcha: /captcha|i'm not a robot|recaptcha|hcaptcha|challenge[- ]?press|protecting your account|solve this puzzle|start puzzle|real person|arkose|funcaptcha/i.test(document.body.innerText) || !!document.querySelector('iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], iframe[src*="arkose" i], iframe[src*="funcaptcha" i], iframe[src*="bda-frame" i]'),
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
    shots: list[dict] = []
    submit_traffic: list[dict] = []

    async def snap(label: str):
        shots.append({"label": label, "url": page.url, "shot": await _screenshot_b64(page)})

    def _on_req(req):
        if "uber.com" in req.url and req.method != "GET" and "/_events" not in req.url:
            submit_traffic.append({"url": req.url, "method": req.method, "post": (req.post_data or "")[:600]})

    async def _on_resp(resp):
        if "uber.com" not in resp.url or "/_events" in resp.url:
            return
        entry = next((t for t in submit_traffic if t["url"] == resp.url), None)
        if not entry:
            return
        entry["status"] = resp.status
        try:
            entry["resp_body"] = (await resp.text())[:2000]
        except Exception:
            pass

    page.on("request", _on_req)
    page.on("response", lambda r: asyncio.create_task(_on_resp(r)))

    await snap("01_otp_loaded")

    # Locate OTP input(s). Many providers split OTP into one-digit-per-field,
    # so we have to detect that pattern first.
    sel_info = await page.evaluate("""
        () => {
            const inputs = Array.from(document.querySelectorAll('input')).filter(i => i.offsetParent !== null);
            // Multi-input OTP: ids like EMAIL_OTP_CODE-0, OTP_CODE-0, code-0, etc.
            const numbered = inputs.filter(i => {
                const id = i.id || '';
                const name = i.name || '';
                return /(otp|code|verif|pin|one[-_]?time)[-_]\\d+$/i.test(id) ||
                       /(otp|code|verif|pin|one[-_]?time)[-_]\\d+$/i.test(name);
            });
            if (numbered.length > 1) {
                numbered.sort((a, b) => {
                    const na = parseInt((a.id || a.name).match(/\\d+$/)[0], 10);
                    const nb = parseInt((b.id || b.name).match(/\\d+$/)[0], 10);
                    return na - nb;
                });
                return {
                    multi: true,
                    selectors: numbered.map(n => n.id ? '#' + n.id : `input[name="${n.name}"]`),
                    count: numbered.length,
                };
            }
            // Single-input OTP fallback
            let pick = inputs.find(i => (i.autocomplete||'').toLowerCase().includes('one-time'));
            if (!pick) {
                const re = /(otp|code|verif|pin|one.time)/i;
                pick = inputs.find(i => re.test(i.name||'') || re.test(i.id||'') || re.test(i.placeholder||'') || re.test(i.getAttribute('aria-label')||''));
            }
            if (!pick) {
                pick = inputs.find(i => ['tel','number','text','password'].includes(i.type));
            }
            if (!pick) return null;
            return {
                multi: false,
                tag: pick.tagName, type: pick.type, id: pick.id, name: pick.name,
                placeholder: pick.placeholder, ac: pick.autocomplete,
                aria: pick.getAttribute('aria-label')||'',
            };
        }
    """)
    if not sel_info:
        return {"ok": False, "reason": "no OTP input found", "shots": shots, "submit_traffic": submit_traffic}

    typed = ""
    if sel_info.get("multi"):
        # OTP UIs auto-advance focus on each keystroke. Re-clicking each input
        # races with React's own focus management and times out. Instead: click
        # the first input ONCE, then type all digits via the keyboard — React
        # distributes them across the fields naturally.
        digits = [c for c in otp if c.strip()]
        sels = sel_info["selectors"]
        try:
            await page.click(sels[0])
            await page.wait_for_timeout(150)
            for ch in digits[: len(sels)]:
                await page.keyboard.type(ch, delay=80)
                await page.wait_for_timeout(120)
            typed = "".join(digits[: len(sels)])
        except Exception as e:
            return {"ok": False, "reason": f"multi-fill failed: {e}", "selectors": sels, "input": sel_info, "shots": shots, "submit_traffic": submit_traffic}
        sel = sels[-1]
    else:
        if sel_info.get("id"):
            sel = f"#{sel_info['id']}"
        elif sel_info.get("name"):
            sel = f'input[name="{sel_info["name"]}"]'
        else:
            sel = 'input[type="tel"], input[type="number"], input[type="text"]'
        try:
            await page.click(sel)
            await page.fill(sel, "")
            await page.type(sel, otp, delay=80)
        except Exception as e:
            return {"ok": False, "reason": f"fill failed: {e}", "selector": sel, "input": sel_info, "shots": shots, "submit_traffic": submit_traffic}
        try:
            typed = await page.input_value(sel)
        except Exception:
            typed = ""

    await page.wait_for_timeout(1000)
    await snap("02_otp_typed")

    # Many OTP UIs auto-submit when the last digit is typed — give it time
    # before we try anything else.
    await page.wait_for_timeout(2500)
    await snap("03_after_autosubmit_wait")

    # Submit via JS click of the submit button (most React-friendly)
    click_result = None
    if "auth.uber.com/v2/" in page.url:
        try:
            click_result = await page.evaluate("""
                () => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const submit = buttons.find(b => b.type === 'submit') ||
                                   buttons.find(b => /verify|continue|next|submit|confirm/i.test((b.innerText||'').trim()));
                    if (!submit) return {clicked: false};
                    submit.click();
                    return {clicked: true, text: (submit.innerText||'').trim().slice(0, 40), disabled: submit.disabled};
                }
            """)
        except Exception as e:
            click_result = {"err": str(e)}
        await page.wait_for_timeout(3500)
        await snap("04_after_click")

    # Fallback: form.requestSubmit
    if "auth.uber.com/v2/" in page.url:
        try:
            await page.evaluate("""
                () => {
                    const f = document.querySelector('form');
                    if (!f) return 'no form';
                    if (f.requestSubmit) { f.requestSubmit(); return 'requestSubmit'; }
                    f.submit(); return 'submit';
                }
            """)
            await page.wait_for_timeout(3500)
            await snap("05_after_form_submit")
        except Exception:
            pass

    await page.wait_for_timeout(4000)
    await snap("06_settled")

    final_url = page.url
    looks_authenticated = (
        "auth.uber.com" not in final_url and "auth.careem.com" not in final_url
        and "/login" not in final_url
    )
    if looks_authenticated:
        storage = await pl.context.storage_state()
        return {"ok": True, "storage_state": storage, "url": final_url, "shots": shots, "submit_traffic": submit_traffic}

    # Still on the auth host — but the OTP may have been accepted and Uber is
    # asking for an additional verification step (e.g., last digits of a card
    # on file). Parse the most recent submit-form response to find out.
    challenge = _parse_next_challenge(submit_traffic)
    if challenge:
        return {
            "ok": False, "needs_step": challenge["screen_type"],
            "challenge": challenge,
            "url": final_url, "shots": shots, "submit_traffic": submit_traffic,
        }
    return {
        "ok": False, "reason": "still on auth page", "url": final_url,
        "selector": sel, "input": sel_info, "typed": typed,
        "click_result": click_result, "shots": shots, "submit_traffic": submit_traffic,
    }


def _parse_next_challenge(submit_traffic: list[dict]) -> dict | None:
    """Inspect submit-form responses for a SIGN_IN step-up screen we recognize."""
    import json as _json

    for entry in reversed(submit_traffic):
        if "submit-form" not in entry.get("url", ""):
            continue
        body = entry.get("resp_body") or ""
        if not body:
            continue
        try:
            data = _json.loads(body)
        except Exception:
            continue
        # screenErrors mean Uber rejected our answer — surface that
        if isinstance(data.get("screenErrors"), list) and data["screenErrors"]:
            err = data["screenErrors"][0]
            return {
                "screen_type": "error",
                "title": err.get("supportForm", {}).get("title", "Error"),
                "message": err.get("supportForm", {}).get("message", "Unknown error"),
            }
        form = data.get("form")
        if not isinstance(form, dict):
            continue
        screens = form.get("screens") or []
        if not screens:
            continue
        screen = screens[0]
        screen_type = screen.get("screenType", "")
        if screen_type == "PAYMENT_CARD_NUMBER_SUFFIX":
            fields = screen.get("fields", [])
            field = fields[0] if fields else {}
            cc = field.get("creditCardChallenge", {})
            hint = (cc.get("creditCardHints") or [{}])[0]
            profile = field.get("profileHint", {})
            return {
                "screen_type": "card_suffix",
                "card_type": hint.get("displayableCardType") or hint.get("cardType", "card"),
                "last4": hint.get("cardNumber", ""),
                "first_name": profile.get("firstName", ""),
                "prompt_label": "Last 8 digits of card",
                "max_length": 8,
                "input_mode": "numeric",
            }
        # Unknown screen — surface enough for the UI/diag
        return {
            "screen_type": "unknown",
            "raw_screen_type": screen_type,
            "raw_screen": screen,
        }
    return None


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
    return await _submit_with_login(login_id, otp, label="verify")


async def answer(login_id: str, value: str) -> dict:
    """Submit a follow-up step's answer (e.g., card-suffix challenge)."""
    return await _submit_with_login(login_id, value, label="answer")


async def _submit_with_login(login_id: str, value: str, label: str) -> dict:
    async with _LOCK:
        pl = PENDING.get(login_id)
    if not pl:
        return {"ok": False, "reason": "login_id not found or expired"}
    if pl.expired():
        await _cleanup(pl)
        PENDING.pop(login_id, None)
        return {"ok": False, "reason": "login expired — start again"}
    try:
        result = await _submit_otp_generic(pl, value)
    except Exception as e:
        return {"ok": False, "reason": f"{label} failed: {e}"}
    if result.get("ok"):
        PENDING.pop(login_id, None)
        await _cleanup(pl)
        result["provider"] = pl.provider
    elif result.get("needs_step"):
        # Keep the pending login alive — caller will submit the next step
        result["login_id"] = login_id
        result["provider"] = pl.provider
    return result

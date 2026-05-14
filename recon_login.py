"""Find phone + OTP selectors on Uber/Careem login pages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).parent / "recon_out"
OUT.mkdir(exist_ok=True)


async def inspect(url: str, label: str, dwell_ms: int = 6000):
    print(f"\n=== {label}: {url} ===", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="en-SA",
            timezone_id="Asia/Riyadh",
            viewport={"width": 412, "height": 915},
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(dwell_ms)

        info = await page.evaluate("""
            () => {
                const out = { url: location.href, title: document.title };
                out.inputs = Array.from(document.querySelectorAll('input, select')).map(el => ({
                    tag: el.tagName,
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    autocomplete: el.autocomplete || '',
                    aria: el.getAttribute('aria-label') || '',
                    inputmode: el.getAttribute('inputmode') || '',
                    visible: el.offsetParent !== null,
                }));
                out.buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]')).map(el => ({
                    tag: el.tagName,
                    type: el.type || '',
                    txt: (el.innerText || el.value || '').trim().slice(0, 60),
                    aria: el.getAttribute('aria-label') || '',
                    disabled: el.disabled || false,
                    visible: el.offsetParent !== null,
                }));
                out.forms = Array.from(document.querySelectorAll('form')).map(f => ({
                    action: f.action || '', method: f.method || '', id: f.id || '',
                }));
                return out;
            }
        """)
        print(f"  url:    {info['url']}", flush=True)
        print(f"  title:  {info['title']}", flush=True)
        print(f"  forms:  {info['forms']}", flush=True)
        print(f"  inputs ({len(info['inputs'])}):", flush=True)
        for i in info["inputs"]:
            if i["visible"]:
                print(f"    type={i['type']:8} name={i['name']!r:18} id={i['id']!r:18} placeholder={i['placeholder']!r:30} aria={i['aria']!r:25} im={i['inputmode']!r:10} ac={i['autocomplete']!r}", flush=True)
        print(f"  buttons:", flush=True)
        for b in info["buttons"]:
            if b["visible"] and (b["txt"] or b["aria"]):
                print(f"    type={b['type']:8} dis={str(b['disabled']):5} txt={b['txt']!r:30} aria={b['aria']!r}", flush=True)

        try:
            await page.screenshot(path=str(OUT / f"login_{label}.png"), full_page=True)
        except Exception:
            pass
        (OUT / f"login_{label}.json").write_text(json.dumps(info, indent=2))

        await ctx.close()
        await browser.close()


async def main():
    await inspect("https://auth.uber.com/v2/", "uber")
    await inspect("https://app.careem.com/", "careem")
    await inspect("https://identity.careem.com/login", "careem_identity")


if __name__ == "__main__":
    asyncio.run(main())

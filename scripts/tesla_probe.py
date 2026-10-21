"""One read of Tesla's careers board in a visible browser, reporting only statuses and counts.

Tesla's page loads its own board JSON (``/cua-api/apps/careers/state``); this probe waits for that
response instead of requesting anything itself. Run it under a display (``xvfb-run`` on Linux).
"""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

CAREERS_PAGE = "https://www.tesla.com/careers/search/"
STATE = "https://www.tesla.com/cua-api/apps/careers/state"


async def main(channel: str) -> int:
    async with async_playwright() as playwright:
        if channel == "chromium":
            browser = await playwright.chromium.launch(headless=False)
        else:
            browser = await playwright.chromium.launch(channel=channel, headless=False)
        page = await browser.new_page()
        navigation = None
        try:
            async with page.expect_response(
                lambda r: r.url.startswith(STATE), timeout=45_000
            ) as info:
                navigation = await page.goto(
                    CAREERS_PAGE, wait_until="domcontentloaded", timeout=45_000
                )
            response = await info.value
        except Exception as exc:  # a probe reports every failure the same way
            status = navigation.status if navigation else None
            print(f"{channel}: page {status}, board not loaded: {type(exc).__name__}")
            await browser.close()
            return 1
        listings = (await response.json()).get("listings", []) if response.status == 200 else []
        interns = sum("intern" in str(item.get("t", "")).casefold() for item in listings)
        print(
            f"{channel}: page {navigation.status if navigation else None}, board {response.status},"
            f" {len(listings)} listings, {interns} intern titles"
        )
        await browser.close()
        return 0 if response.status == 200 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "chrome")))

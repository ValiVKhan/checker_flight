"""
Qatar Airways upgrade price checker (GitHub Actions version)
---------------------------------------------------------------
Runs headless using real Chrome (not bundled Chromium) with
anti-fingerprint flags. Reads booking details and ntfy topic from
environment variables (set as GitHub Actions secrets).

Designed to always notify on failure, with a specific reason, rather
than fail silently - so a site layout change surfaces as a clear
"something changed" alert instead of a stack trace no one sees.
"""

import os
import sys
import time
import csv
import traceback
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.qatarairways.com/app/upgrade/en/retrieve?appCode=QRCode&platform=w"

BOOKING_REFERENCE = os.environ["BOOKING_REFERENCE"]
LAST_NAME = os.environ["LAST_NAME"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

SELECTOR_BOOKING_INPUT = "#mat-input-0"
SELECTOR_LASTNAME_INPUT = "#mat-input-1"
SELECTOR_COOKIE_ACCEPT = "#onetrust-accept-btn-handler"
SELECTOR_SUBMIT_BUTTON = "button[type='submit']"
SELECTOR_PRICE_OUTBOUND = "xpath=/html/body/qr-onup-root/div/qr-onup-online-upgrade/main/div/div/div[3]/div/div[1]/qr-aou-fare-card/div/div[1]/div[2]/div/div/span/div/div/span"
SELECTOR_PRICE_RETURN = "xpath=/html/body/qr-onup-root/div/qr-onup-online-upgrade/main/div/div/div[3]/div/div[2]/qr-aou-fare-card/div/div[1]/div[2]/div/div/span/div/div/span"
SELECTOR_ERROR_MESSAGE = ".error-message"

LOG_FILE = "price_log.csv"


def notify(message: str, title: str = "Qatar Upgrade Price"):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "default", "Tags": "airplane"},
            timeout=10,
        )
    except Exception as e:
        print(f"Notification failed (non-fatal): {e}")


def notify_urgent(message: str, title: str = "Qatar Upgrade Price - ACTION NEEDED"):
    """Loud, repeating Pushover alert that bypasses silent mode and keeps
    re-notifying every 60s for up to 1 hour until you acknowledge it.
    Falls back to a normal ntfy notification if Pushover isn't configured."""
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        print("Pushover not configured - falling back to normal notification.")
        notify(message, title=title)
        return
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_API_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title,
                "message": message,
                "priority": 2,       # Emergency - bypasses quiet hours/silent mode
                "retry": 60,         # re-send every 60 seconds
                "expire": 3600,      # ...for up to 1 hour, until acknowledged
                "sound": "siren",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"Pushover notification failed (non-fatal): {e}")
        notify(message, title=title)  # fall back to ntfy so it's never silent


class CheckFailed(Exception):
    """Raised with a human-readable reason so the caller always has a
    specific message to notify about, instead of a raw stack trace."""
    pass


def check_price(headless: bool = True) -> tuple[str, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            raise CheckFailed(f"Could not load the page at all (network/site down?): {e}")

        title = page.title()
        print(f"Page title: {title}")

        # Detect an outright block page (Akamai, Cloudflare, etc.) - this
        # is a different situation from the form/selectors changing.
        if "access denied" in title.lower() or "denied" in title.lower():
            raise CheckFailed(
                f"Page returned '{title}' - looks like the site blocked this "
                f"request (rate-limit or bot-detection), not a layout change."
            )

        try:
            page.wait_for_selector(SELECTOR_BOOKING_INPUT, timeout=30000, state="visible")
        except PlaywrightTimeoutError:
            page.screenshot(path="debug_screenshot.png", full_page=True)
            with open("debug_page.html", "w") as f:
                f.write(page.content())
            raise CheckFailed(
                "Could not find the booking reference field. The site's form "
                "may have changed - check debug_screenshot.png/debug_page.html."
            )

        try:
            page.wait_for_selector(SELECTOR_COOKIE_ACCEPT, timeout=8000, state="visible")
            page.click(SELECTOR_COOKIE_ACCEPT)
            print("Dismissed cookie banner.")
            page.wait_for_selector("#onetrust-consent-sdk", state="hidden", timeout=8000)
        except PlaywrightTimeoutError:
            print("No cookie banner found (or already dismissed) - continuing.")

        page.fill(SELECTOR_BOOKING_INPUT, BOOKING_REFERENCE, force=True)
        page.fill(SELECTOR_LASTNAME_INPUT, LAST_NAME, force=True)
        print("Filled in booking reference and last name.")

        try:
            page.click(SELECTOR_SUBMIT_BUTTON)
        except Exception:
            raise CheckFailed(
                "Could not find/click the submit button. The site's form "
                "may have changed - check debug_screenshot.png."
            )

        # Give the results a moment to render before we start checking for
        # either a price or an error message.
        page.wait_for_timeout(2000)

        # If the site shows an on-page error (e.g. "no upgrade offers
        # available", or booking not found), surface that message directly
        # rather than treating it as a scraping failure.
        try:
            err_text = page.inner_text(SELECTOR_ERROR_MESSAGE, timeout=3000)
            if err_text.strip():
                raise CheckFailed(f"Site reported: {err_text.strip()}")
        except PlaywrightTimeoutError:
            pass  # no error message shown, that's the expected good case

        outbound_price = None
        return_price = None

        def wait_for_price(selector: str, label: str, timeout: int = 20000) -> str | None:
            """Try to find a price, with one retry after a short pause -
            handles the results page occasionally rendering slowly."""
            for attempt in (1, 2):
                try:
                    page.wait_for_selector(selector, timeout=timeout, state="visible")
                    return page.inner_text(selector).strip()
                except PlaywrightTimeoutError:
                    if attempt == 1:
                        print(f"{label} price not found on first try, retrying once...")
                        page.wait_for_timeout(3000)
                    else:
                        print(f"{label} price still not found after retry.")
            return None

        outbound_price = wait_for_price(SELECTOR_PRICE_OUTBOUND, "Outbound")
        return_price = wait_for_price(SELECTOR_PRICE_RETURN, "Return")

        if not outbound_price and not return_price:
            page.screenshot(path="debug_screenshot.png", full_page=True)
            with open("debug_page.html", "w") as f:
                f.write(page.content())
            raise CheckFailed(
                "Reached the results page but couldn't find a price anywhere. "
                "The site's layout/offer format may have changed - check "
                "debug_screenshot.png/debug_page.html."
            )

        if not outbound_price or not return_price:
            page.screenshot(path="debug_screenshot.png", full_page=True)
            with open("debug_page.html", "w") as f:
                f.write(page.content())
            missing = "outbound" if not outbound_price else "return"
            raise CheckFailed(
                f"Found one price but not the other (missing: {missing}). "
                f"This could be a slow page load or a partial layout change - "
                f"check debug_screenshot.png/debug_page.html."
            )

        browser.close()
        return outbound_price or "not found", return_price or "not found"


def get_last_logged_prices() -> tuple[str | None, str | None]:
    try:
        with open(LOG_FILE, newline="") as f:
            rows = [row for row in csv.reader(f) if row]
        if rows:
            last_row = rows[-1]
            if len(last_row) >= 3:
                return last_row[1], last_row[2]
    except FileNotFoundError:
        pass
    return None, None


if __name__ == "__main__":
    try:
        outbound_price, return_price = check_price(headless=True)
        print(f"Outbound price: {outbound_price}")
        print(f"Return price: {return_price}")

        last_outbound, last_return = get_last_logged_prices()

        with open(LOG_FILE, "a", newline="") as f:
            csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M:%S"), outbound_price, return_price])

        # Always send the routine ntfy heartbeat, every run, regardless of
        # whether anything changed.
        notify(f"Outbound: {outbound_price}\nReturn: {return_price}")

        # On top of that, fire an additional loud/repeating Pushover alert
        # if the price actually changed.
        if (last_outbound is not None and outbound_price != last_outbound) or \
           (last_return is not None and return_price != last_return):
            notify_urgent(
                f"Outbound: {outbound_price} (was {last_outbound})\n"
                f"Return: {return_price} (was {last_return})",
                title="Qatar Upgrade Price - CHANGED",
            )

    except CheckFailed as e:
        # A known, specific failure - send both: the routine heartbeat so
        # you can see it in your normal feed, and an urgent repeating alert
        # so it doesn't get missed.
        print(f"CHECK FAILED: {e}")
        notify(f"Check failed: {e}", title="Qatar Upgrade Price - ACTION NEEDED")
        notify_urgent(f"Check failed: {e}")
        sys.exit(1)

    except Exception as e:
        # Anything unexpected (crash, network blip, Playwright internals,
        # etc.) - same dual notification approach.
        print("UNEXPECTED ERROR:")
        traceback.print_exc()
        short_trace = traceback.format_exc().strip().splitlines()[-1]
        notify(f"Unexpected error: {short_trace}", title="Qatar Upgrade Price - SCRIPT ERROR")
        notify_urgent(
            f"Unexpected error: {short_trace}",
            title="Qatar Upgrade Price - SCRIPT ERROR",
        )
        sys.exit(1)
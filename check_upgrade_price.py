"""
Qatar Airways upgrade price checker (GitHub Actions version)
---------------------------------------------------------------
Runs headless using real Chrome (not bundled Chromium) with
anti-fingerprint flags. Reads booking details and ntfy topic from
environment variables (set as GitHub Actions secrets).
"""

import os
import sys
import time
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.qatarairways.com/app/upgrade/en/retrieve?appCode=QRCode&platform=w"

BOOKING_REFERENCE = os.environ["BOOKING_REFERENCE"]
LAST_NAME = os.environ["LAST_NAME"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

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


def check_price(headless: bool = True) -> tuple[str | None, str | None]:
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

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        print(f"Page title: {page.title()}")

        try:
            page.wait_for_selector(SELECTOR_BOOKING_INPUT, timeout=30000, state="visible")
        except PlaywrightTimeoutError:
            print("Could not find booking input. Saving screenshot + HTML for debugging.")
            page.screenshot(path="debug_screenshot.png", full_page=True)
            with open("debug_page.html", "w") as f:
                f.write(page.content())
            browser.close()
            return None, None

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

        page.click(SELECTOR_SUBMIT_BUTTON)

        outbound_price = None
        return_price = None

        try:
            page.wait_for_selector(SELECTOR_PRICE_OUTBOUND, timeout=30000, state="visible")
            outbound_price = page.inner_text(SELECTOR_PRICE_OUTBOUND)
        except PlaywrightTimeoutError:
            print("Could not find outbound price.")

        try:
            page.wait_for_selector(SELECTOR_PRICE_RETURN, timeout=10000, state="visible")
            return_price = page.inner_text(SELECTOR_PRICE_RETURN)
        except PlaywrightTimeoutError:
            print("Could not find return price.")

        if outbound_price is None and return_price is None:
            try:
                err = page.inner_text(SELECTOR_ERROR_MESSAGE)
                print(f"No prices found, page showed an error instead: {err}")
            except PlaywrightTimeoutError:
                print("Timed out waiting for prices or an error message.")
                page.screenshot(path="debug_screenshot.png")
                print("Saved debug_screenshot.png for inspection.")

        browser.close()
        return outbound_price, return_price


if __name__ == "__main__":
    outbound_price, return_price = check_price(headless=True)
    if outbound_price or return_price:
        print(f"Outbound price: {outbound_price}")
        print(f"Return price: {return_price}")
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{outbound_price},{return_price}\n")
        notify(f"Outbound: {outbound_price}\nReturn: {return_price}")
    else:
        notify("Price check failed - see debug screenshot/log on the machine.")
        sys.exit(1)
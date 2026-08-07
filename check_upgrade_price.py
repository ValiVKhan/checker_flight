"""
Qatar Airways upgrade price checker
------------------------------------
Uses Playwright to load the upgrade lookup page, fill in a booking
reference + last name, submit, and scrape the resulting price.
Designed to run in GitHub Actions on a schedule; sends a push
notification via ntfy.sh only when the price changes.

REQUIRED ENVIRONMENT VARIABLES (set as GitHub Actions secrets):
    BOOKING_REFERENCE
    LAST_NAME
    NTFY_TOPIC
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
SELECTOR_PRICE_RESULT = "xpath=/html/body/qr-onup-root/div/qr-onup-online-upgrade/main/div[3]/div[2]/div[4]/div/div[1]/qr-onup-fare-card/div/div[1]/div[3]/div[2]/div[2]/span/div/div/span[2]"
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


def get_last_logged_price() -> str | None:
    if not os.path.exists(LOG_FILE):
        return None
    with open(LOG_FILE) as f:
        lines = [l.strip() for l in f if l.strip()]
    if not lines:
        return None
    return lines[-1].split(",", 1)[1]


def check_price(headless: bool = True) -> str | None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

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
            return None

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

        try:
            page.wait_for_selector(SELECTOR_PRICE_RESULT, timeout=30000, state="visible")
            price_text = page.inner_text(SELECTOR_PRICE_RESULT)
        except PlaywrightTimeoutError:
            try:
                err = page.inner_text(SELECTOR_ERROR_MESSAGE)
                print(f"No price found, page showed an error instead: {err}")
            except PlaywrightTimeoutError:
                print("Timed out waiting for a price or error message.")
                page.screenshot(path="debug_screenshot.png")
                print("Saved debug_screenshot.png for inspection.")
            price_text = None

        browser.close()
        return price_text


if __name__ == "__main__":
    result = check_price(headless=True)
    if result:
        print(f"Upgrade price: {result}")
        last_price = get_last_logged_price()
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{result}\n")
        if result != last_price:
            notify(f"Price changed to: {result}" if last_price else f"Current upgrade price: {result}")
        else:
            print("Price unchanged since last check - no notification sent.")
    else:
        sys.exit(1)

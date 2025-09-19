# ---------------------------------------------------------------------------
# Section: imports
# ---------------------------------------------------------------------------

# Core libraries, Selenium, and Google Sheets dependencies

import os
import re
import sys
import time
from datetime import datetime, date
from typing import List, Set, Tuple, Optional

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    WebDriverException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

print("Section 'imports' complete – modules loaded successfully.")

# ---------------------------------------------------------------------------
# Section: configuration & constants
# ---------------------------------------------------------------------------

# Load environment, validate required files, and set globals

def require_file(path: str, description: str) -> str:
    """Exit if a required file is missing."""
    if not os.path.isfile(path):
        sys.exit(f"Required {description} not found: '{path}' – aborting")
    return path


print("Function 'require_file' loaded – validates required files exist.")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
print("Environment loaded – .env variables available.")

# Assemble Citi account credentials from .env
ACCOUNTS: List[dict] = []
idx = 1
while os.getenv(f"CITI_USERNAME_{idx}"):
    ACCOUNTS.append({
        "user":   os.getenv(f"CITI_USERNAME_{idx}"),
        "pass":   os.getenv(f"CITI_PASSWORD_{idx}"),
        "holder": os.getenv(f"CITI_HOLDER_{idx}", f"Holder {idx}"),
    })
    idx += 1
if not ACCOUNTS:
    sys.exit("No Citi accounts found in .env – aborting")
print(f"Accounts loaded – {len(ACCOUNTS)} account(s) configured.")

# Citi URLs and window placement
LOGIN_URL  = "https://online.citi.com/US/login.do"
# IMPORTANT: official Merchant Offers URL – always target this.
OFFERS_URL = "https://online.citi.com/US/ag/products-offers/merchantoffers"
HOME_URL = "https://online.citi.com/US/home"
SECOND_MONITOR_OFFSET = (3440, 0)

# Tuneable navigation constants
PAGE_LOAD_PAUSE = float(os.getenv("CITI_PAGE_LOAD_PAUSE", "4.0"))
OFFERS_RETRY_MAX = int(os.getenv("CITI_OFFERS_RETRY_MAX", "8"))
NAV_MENU_FALLBACK = os.getenv("CITI_NAV_MENU_FALLBACK", "true").lower() == "true"
RESTART_BETWEEN_ACCOUNTS = os.getenv("CITI_RESTART_BETWEEN_ACCOUNTS", "true").lower() == "true"
print("Constants ready – navigation timing and retry settings applied.")

print("Section 'configuration & constants' complete – runtime config set.")

# ---------------------------------------------------------------------------
# Section: Google Sheets bootstrap
# ---------------------------------------------------------------------------

# Initialize Google Sheets client and ensure worksheets/headers

SA_PATH = os.getenv("GOOGLE_SA_PATH", os.path.join(PROJECT_ROOT, "service_account.json"))
require_file(SA_PATH, "Google service-account JSON")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
CREDS  = Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
SHEET  = gspread.authorize(CREDS).open("Credit Card Offers")
print("Google Sheets client initialized – workbook opened.")

OFFER_HEADERS = (
    "Card Holder", "Last Four", "Card Name", "Brand",
    "Discount", "Maximum Discount", "Minimum Spend",
    "Date Added", "Expiration", "Local"
)

def _ws(sheet, title: str, headers: Tuple[str, ...]):
    """Create/get a worksheet and ensure header row."""
    existing = {w.title: w for w in sheet.worksheets()}
    ws = existing.get(title) or sheet.add_worksheet(title=title, rows=2000, cols=len(headers))
    first_row = ws.row_values(1)
    if first_row != list(headers):
        if not first_row:
            ws.append_row(list(headers), value_input_option="RAW")
        else:
            ws.update("1:1", [headers], value_input_option="RAW")
    return ws


print("Function '_ws' loaded – worksheet bootstrap ready.")

OFFER_WS = _ws(SHEET, "Card Offers", OFFER_HEADERS)
LOG_WS   = _ws(SHEET, "Log", ("Time", "Level", "Function", "Message"))
print("Worksheets ready – 'Card Offers' and 'Log' ensured.")


def sheet_log(level: str, func: str, msg: str):
    """Append a log entry to the Log worksheet."""
    LOG_WS.append_row(
        [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, func, msg],
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS"
    )


print("Function 'sheet_log' loaded – spreadsheet logging enabled.")

def ensure_date_added_column():
    """Backfill the Date Added column if missing."""
    header = OFFER_WS.row_values(1)
    if "Date Added" not in header:
        idx = 8  # H
        sid = OFFER_WS._properties["sheetId"]
        SHEET.batch_update({
            "requests": [{
                "insertDimension": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": idx - 1, "endIndex": idx},
                    "inheritFromBefore": True
                }
            }]
        })
        OFFER_WS.update_cell(1, idx, "Date Added")


print("Function 'ensure_date_added_column' loaded – header self-heal ready.")
ensure_date_added_column()

def set_log_row_height():
    """Make log rows readable with a fixed row height."""
    sid = LOG_WS._properties["sheetId"]
    SHEET.batch_update({
        "requests": [{
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS"},
                "properties": {"pixelSize": 21}, "fields": "pixelSize"
            }
        }]
    })


print("Function 'set_log_row_height' loaded – log sheet formatting ready.")
set_log_row_height()

print("Section 'Google Sheets bootstrap' complete – Sheets initialized.")

# ---------------------------------------------------------------------------
# Section: Selenium driver
# ---------------------------------------------------------------------------

# Create a Chrome WebDriver with default waits

def build_driver() -> Tuple[webdriver.Chrome, WebDriverWait]:
    """Create a Chrome driver and WebDriverWait helper."""
    opts = Options()
    opts.add_argument("--start-maximized")
    drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        drv.set_window_position(*SECOND_MONITOR_OFFSET)
    except Exception:
        pass
    return drv, WebDriverWait(drv, 30)


print("Function 'build_driver' loaded – Selenium driver factory ready.")

driver, wait = build_driver()
print("Section 'Selenium driver' complete – driver and wait initialized.")


def restart_driver():
    """Fully restart the browser to isolate accounts."""
    global driver, wait
    try:
        driver.quit()
    except Exception:
        pass
    driver, wait = build_driver()
    print("Browser restarted – new driver instance created.")


print("Function 'restart_driver' loaded – between-account isolation ready.")

# ---------------------------------------------------------------------------
# Section: page helpers (offers detection, errors, and healing)
# ---------------------------------------------------------------------------

# Detect offers, detect error states, dismiss popups, and harden navigation

def offers_ready() -> bool:
    """Return True when offer tiles appear."""
    xp = ("//div[contains(@class,'offer-tile') or contains(@class,'mo-offer') or "
          "contains(@data-testid,'offer-tile')]")
    return bool(driver.find_elements(By.XPATH, xp))


print("Function 'offers_ready' loaded – detects when offers are visible.")


def error_banner_visible() -> bool:
    """Return True when a known inline error banner is present."""
    return bool(driver.find_elements(By.ID, "available-err-msg"))


print("Function 'error_banner_visible' loaded – banner error detector ready.")


def error_toast_visible() -> bool:
    """Return True when an alert/toast error is present."""
    xpath = (
        "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'trouble loading your offers')]"
        " | //*[@role='alert' or contains(@class,'alert') or contains(@class,'toast')]"
        "[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'error')]"
    )
    return bool(driver.find_elements(By.XPATH, xpath))


print("Function 'error_toast_visible' loaded – toast error detector ready.")


def page_not_found_visible() -> bool:
    """Detect a 404 / 'Page not found' experience."""
    xp = (
        "//*[self::h1 or self::h2]"
        "[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'page not found') or contains(.,'looks like that information isn')]"
        " | //*[contains(@class,'notFound') or contains(@class,'not-found') or contains(@class,'error')]"
        "[contains(.,'Page not found')]"
    )
    return bool(driver.find_elements(By.XPATH, xp))


print("Function 'page_not_found_visible' loaded – 404 detector ready.")


def click_no_thanks_if_present(timeout: int = 5) -> bool:
    """Dismiss common promo/pop-up modals that block interaction."""
    end = time.time() + timeout
    sels = [
        "//*[self::a or self::button][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'no thanks')]",
        "//*[self::a or self::button][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'not now')]",
        "//*[self::a or self::button][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'skip')]",
        "//*[self::a or self::button][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'dismiss')]",
        "//*[@role='dialog']//button[@aria-label='Close' or contains(@class,'close')]",
        "//button[@aria-label='Close' or contains(@class,'close')]",
    ]
    while time.time() < end:
        clicked = False
        for xp in sels:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        clicked = True
                        time.sleep(0.5)
                    except Exception:
                        pass
        if clicked:
            return True
        time.sleep(0.25)
    return False


print("Function 'click_no_thanks_if_present' loaded – popup dismissor ready.")

def logged_in() -> bool:
    """Heuristic to confirm we are authenticated."""
    u = driver.current_url
    return "/dashboard" in u or "merchantoffers" in u


print("Function 'logged_in' loaded – session state heuristic ready.")


def clear_web_storage():
    """Clear local/session storage in case Citi UI caches a bad route."""
    try:
        driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
    except Exception:
        pass


print("Function 'clear_web_storage' loaded – storage reset ready.")


def return_to_account_if_404(timeout: int = 6) -> bool:
    """If 404 page shows, click 'Return to your account' to re-enter app."""
    if not page_not_found_visible():
        return False
    try:
        btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((
            By.XPATH,
            "//*[self::a or self::button][contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'return to your account')]"
        )))
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.8)
        sheet_log("INFO", "nav", "Recovered via 'Return to your account'")
        return True
    except Exception:
        return False


print("Function 'return_to_account_if_404' loaded – 404 bounce recovery ready.")


def nav_via_rewards_menu(timeout: int = 10) -> bool:
    """Hover 'Rewards & Offers' and click 'Merchant Offers' to keep context."""
    try:
        nav = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((
            By.XPATH,
            "//*[self::a or self::button]"
            "[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'rewards & offers')]"
        )))
        ActionChains(driver).move_to_element(nav).pause(0.6).perform()
        time.sleep(0.8)
        link = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((
            By.XPATH,
            "//*[self::a or self::button]"
            "[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'merchant offers')"
            " or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'offers for you')"
            " or contains(@href,'merchantoffers')]"
        )))
        driver.execute_script("arguments[0].click();", link)
        time.sleep(PAGE_LOAD_PAUSE)
        return True
    except Exception:
        # Fallback: click any visible link to merchantoffers
        try:
            link2 = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((
                By.XPATH, "//a[contains(@href,'merchantoffers')]"
            )))
            driver.execute_script("arguments[0].click();", link2)
            time.sleep(PAGE_LOAD_PAUSE)
            return True
        except Exception:
            return False


print("Function 'nav_via_rewards_menu' loaded – menu-driven navigation ready.")


def go_home_then_back():
    """Visit Citi Home to re-anchor session, then retry Offers."""
    try:
        driver.get(HOME_URL)
        time.sleep(2.0)
    except Exception:
        pass
    try:
        driver.get(OFFERS_URL)
        time.sleep(PAGE_LOAD_PAUSE)
    except Exception:
        pass


print("Function 'go_home_then_back' loaded – home-path recovery ready.")


def robust_get(url: str, tries: int = 2) -> None:
    """Navigate to URL with soft retries if 'Not found' renders."""
    last_exc = None
    for i in range(1, tries + 1):
        try:
            driver.get(url)
            time.sleep(PAGE_LOAD_PAUSE)
            click_no_thanks_if_present(3)
            if not page_not_found_visible():
                return
            sheet_log("WARN", "nav", f"Not Found on try {i} for {url} – retrying")
            time.sleep(1.0)
        except WebDriverException as exc:
            last_exc = exc
            sheet_log("WARN", "nav", f"driver.get failed (try {i}): {exc}")
            time.sleep(1.0)
    if last_exc:
        raise last_exc


print("Function 'robust_get' loaded – guarded navigation helper ready.")


def goto_offers_page(max_tries: int = OFFERS_RETRY_MAX) -> bool:
    """Reach Merchant Offers reliably, healing 404s and slow loads."""

    def on_offers() -> bool:
        return ("merchantoffers" in driver.current_url) and (offers_ready() or not error_toast_visible())

    for attempt in range(1, max_tries + 1):
        # Clear any stale UI storage and bounce off 404 if present
        clear_web_storage()
        if return_to_account_if_404():
            time.sleep(1.0)

        # Always attempt the official URL first (Citi often needs 2 hits)
        try:
            robust_get(OFFERS_URL, tries=2)
        except Exception as exc:
            sheet_log("WARN", "nav", f"direct offers get failed (try {attempt}): {exc}")

        try:
            WebDriverWait(driver, 12).until(
                lambda _: offers_ready() or error_banner_visible() or error_toast_visible() or page_not_found_visible()
            )
        except Exception:
            pass

        if on_offers():
            sheet_log("INFO", "nav", f"offers ready (direct, try {attempt})")
            return True

        # If still not there, drive via in-site menu to maintain app context
        used_menu = False
        if NAV_MENU_FALLBACK:
            if nav_via_rewards_menu():
                used_menu = True
                try:
                    WebDriverWait(driver, 12).until(
                        lambda _: offers_ready() or error_banner_visible() or error_toast_visible()
                    )
                except Exception:
                    pass
                if on_offers():
                    sheet_log("INFO", "nav", f"offers ready (menu, try {attempt})")
                    return True

        # Stronger recovery on later attempts: visit Home then back to Offers
        if attempt >= 3:
            go_home_then_back()
            if on_offers():
                sheet_log("INFO", "nav", f"offers ready (home-bridge, try {attempt})")
                return True

        # If we got a 404 page again, click Return and try loop again
        if page_not_found_visible():
            if return_to_account_if_404():
                sheet_log("WARN", "nav", f"404 healed, retrying ({attempt}/{max_tries})")
                time.sleep(1.0)
                continue

        # Try a gentle refresh as a last nudge this attempt
        driver.refresh()
        time.sleep(PAGE_LOAD_PAUSE)
        if on_offers():
            src = "menu" if used_menu else "refresh"
            sheet_log("INFO", "nav", f"offers ready ({src}, try {attempt})")
            return True

        sheet_log("WARN", "nav", f"offers not ready – retrying ({attempt}/{max_tries})")
        time.sleep(1.0)

    sheet_log("ERROR", "nav", "could not reach merchant offers after login")
    return False


print("Function 'goto_offers_page' loaded – hardened navigation ready.")

print("Section 'page helpers' complete – detection and healing enabled.")


# ---------------------------------------------------------------------------
# Section: login
# ---------------------------------------------------------------------------

# Robust login flow using classic page first and fallbacks

def _type_or_js(el, text: str):
    """Enter text reliably, fallback to JS value-set if needed."""
    try:
        el.clear()
    except Exception:
        pass
    try:
        el.click()
    except Exception:
        pass
    el.send_keys(text)
    val = (el.get_attribute("value") or "").strip()
    if val != text:
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            el, text
        )


print("Function '_type_or_js' loaded – resilient typing enabled.")


def _find_input_any(selectors: List[Tuple[str, str]], timeout: int = 20):
    """Find a visible, enabled element in DOM or first-level iframes."""
    end = time.time() + timeout
    while time.time() < end:
        for by, val in selectors:
            try:
                el = driver.find_element(by, val)
                if el.is_displayed() and el.is_enabled():
                    return el
            except Exception:
                pass
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for f in frames:
            try:
                driver.switch_to.frame(f)
                for by, val in selectors:
                    try:
                        el = driver.find_element(by, val)
                        if el.is_displayed() and el.is_enabled():
                            driver.switch_to.default_content()
                            driver.switch_to.frame(f)
                            return el
                    except Exception:
                        pass
            finally:
                driver.switch_to.default_content()
        time.sleep(0.2)
    raise TimeoutException("Login element not found")


print("Function '_find_input_any' loaded – login element finder ready.")


def ensure_login_context(pre_wait: int = 3, max_wait: int = 20) -> None:
    """Prefer the classic login page; bounce via OFFERS_URL if necessary."""
    driver.switch_to.default_content()
    driver.get(LOGIN_URL)
    time.sleep(pre_wait)
    try:
        WebDriverWait(driver, max_wait).until(
            EC.presence_of_element_located((By.ID, "username"))
        )
        return
    except Exception:
        pass
    driver.get(OFFERS_URL)
    time.sleep(2)
    click_no_thanks_if_present(4)
    driver.get(LOGIN_URL)
    time.sleep(pre_wait)


print("Function 'ensure_login_context' loaded – classic login preference set.")


def login_once(username: str, password: str, pause: float) -> None:
    """Single login attempt with adjustable typing cadence."""
    user_el = _find_input_any([
        (By.ID, "username"),
        (By.NAME, "username"),
        (By.ID, "userId"),
        (By.NAME, "userId"),
        (By.CSS_SELECTOR, "input[placeholder*='User'][type='text']")
    ], timeout=25)
    _type_or_js(user_el, username)
    time.sleep(max(0.1, pause))

    try:
        wrap = driver.find_element(
            By.XPATH,
            "//input[@id='password' or @name='password' or @id='citi-input2-0' or @id='pwd']"
            "/ancestor::*[contains(@class,'input-switch-wrapper')]"
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", wrap)
        wrap.click()
        time.sleep(0.2)
    except Exception:
        pass

    pass_el = _find_input_any([
        (By.ID, "password"),
        (By.NAME, "password"),
        (By.ID, "pwd"),
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.ID, "citi-input2-0"),
    ], timeout=25)
    _type_or_js(pass_el, password)
    time.sleep(max(0.1, pause))

    try:
        btn = _find_input_any([
            (By.XPATH,
             "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign on')]"),
            (By.CSS_SELECTOR, "button[type='submit']")
        ], timeout=25)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        driver.execute_script("arguments[0].click();", btn)
    except TimeoutException:
        pass_el.send_keys(Keys.ENTER)


print("Function 'login_once' loaded – single-attempt login ready.")

def citi_login(username: str, password: str) -> bool:
    """Resilient login loop with multiple speeds and pop-up handling."""
    ensure_login_context(pre_wait=3, max_wait=20)
    for attempt, pause in enumerate((0.1, 0.5, 1.0), start=1):
        login_once(username, password, pause)
        try:
            WebDriverWait(driver, 12).until(lambda _: logged_in() or "login" in driver.current_url)
        except Exception:
            pass
        click_no_thanks_if_present(4)
        if logged_in():
            sheet_log("INFO", "login", f"{username} success (try {attempt})")
            return True
        time.sleep(2.0)
    sheet_log("ERROR", "login", f"{username} failed after retries on URL {driver.current_url}")
    return False


print("Function 'citi_login' loaded – resilient login flow ready.")

def citi_logout() -> None:
    """Log out and clear cookies to isolate sessions."""
    try:
        driver.get("https://online.citi.com/US/logout")
        time.sleep(3)
        driver.delete_all_cookies()
        clear_web_storage()
        sheet_log("INFO", "logout", "success")
    except Exception as exc:
        sheet_log("WARN", "logout", str(exc))


print("Function 'citi_logout' loaded – logout routine ready.")

print("Section 'login' complete – login and logout helpers ready.")

# ---------------------------------------------------------------------------
# Section: offer scraping
# ---------------------------------------------------------------------------

# Expand lists, parse offer details, and append rows to the sheet

def plus_icons():
    """Return enroll icons for unenrolled offers."""
    return driver.find_elements(By.XPATH, "//cds-icon[@name='plus-circle' and @arialabel='Enroll']")


print("Function 'plus_icons' loaded – enroll icon locator ready.")

def expand_all():
    """Click any 'Show more'/'Load more' buttons until all offers are visible."""
    while True:
        btns = [b for b in driver.find_elements(
            By.XPATH,
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more') "
            "or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]"
        ) if b.is_displayed()]
        if not btns:
            break
        for btn in btns:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.3)
            except Exception as exc:
                sheet_log("ERROR", "expand", str(exc))
        time.sleep(0.3)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'offer-tile')]")))


print("Function 'expand_all' loaded – offer list expander ready.")

def close_modal():
    """Close the offer details modal dialog."""
    sels = [
        "//button[contains(text(),'Close')]",
        "//button[@aria-label='Close']",
        "//button[contains(@class,'cds-modal-close')]",
        "//cds-icon/ancestor::button",
    ]
    for sel in sels:
        els = driver.find_elements(By.XPATH, sel)
        if els:
            try:
                driver.execute_script("arguments[0].click();", els[0])
                WebDriverWait(driver, 10).until(EC.invisibility_of_element_located(
                    (By.CSS_SELECTOR, ".mo-modal-img-merchant-name")))
                return
            except Exception:
                pass
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    WebDriverWait(driver, 10).until(EC.invisibility_of_element_located(
        (By.CSS_SELECTOR, ".mo-modal-img-merchant-name")))


print("Function 'close_modal' loaded – modal closer ready.")


def try_parse_date_any(s: str) -> Optional[date]:
    """Parse flexible date formats used by offers."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d,%Y", "%B %d,%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    m = re.match(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*$", s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, dd)
        except Exception:
            return None
    return None


print("Function 'try_parse_date_any' loaded – flexible date parser ready.")


def normalize_expiration_string(s: str) -> str:
    """Normalize expiration dates to 'Mon DD, YYYY'."""
    d = try_parse_date_any(s)
    return d.strftime("%b %d, %Y") if d else s


print("Function 'normalize_expiration_string' loaded – date normalizer ready.")

def parse_max_disc(text: str) -> Optional[str]:
    """Extract 'Max $X' values if present."""
    m = re.search(r"[Mm]ax[^$]{0,25}\$(\d[\d,]*)", text)
    return f"${m.group(1)}" if m else None


print("Function 'parse_max_disc' loaded – max discount parser ready.")

def parse_min_spend(text: str) -> Optional[str]:
    """Extract 'spend $X' or 'purchase $X' minimums if present."""
    m = re.search(r"(?:purchase|spend)[^$]{0,25}\$(\d[\d,]*)", text, re.I)
    return f"${m.group(1)}" if m else None


print("Function 'parse_min_spend' loaded – minimum spend parser ready.")

CARD_LABEL_CSS = "div#cds-dropdown-button-value.cds-dd2-pseudo-value"
BTN_DROPD_X = ("//button[@id='cds-dropdown' and contains(@class,'cds-dd2-button')]")
OPT_DROPD_X = ("//ul[@id='cds-dropdown-listbox']/li[not(contains(@class,'disabled'))]")

def open_card_dropdown() -> None:
    """Open the card selector dropdown."""
    wait.until(EC.element_to_be_clickable((By.XPATH, BTN_DROPD_X))).click()


print("Function 'open_card_dropdown' loaded – card selector opener ready.")

def get_label():
    """Return the current card label node."""
    return wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, CARD_LABEL_CSS)))


print("Function 'get_label' loaded – card label getter ready.")


def heal_offers_page(label_to_reselect: Optional[str] = None, tries: int = 3) -> bool:
    """Heal common offers load hiccups by tab toggles and refresh."""
    for _ in range(tries):
        if offers_ready() and not error_toast_visible():
            return True
        for tab_text in ("Enrolled", "All"):
            try:
                tab = driver.find_element(By.XPATH, f"//a[normalize-space()='{tab_text}']")
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(0.8)
            except Exception:
                pass
        if offers_ready() and not error_toast_visible():
            return True
        driver.refresh()
        time.sleep(1.5)
        if label_to_reselect:
            try:
                open_card_dropdown()
                wait.until(EC.element_to_be_clickable(
                    (By.XPATH, f"{OPT_DROPD_X}[normalize-space()='{label_to_reselect}']"))).click()
                WebDriverWait(driver, 8).until(lambda _: get_label().text.strip() == label_to_reselect)
            except Exception:
                pass
        try:
            WebDriverWait(driver, 10).until(lambda _: offers_ready() or error_toast_visible())
        except Exception:
            pass
        if offers_ready() and not error_toast_visible():
            return True
    return False


print("Function 'heal_offers_page' loaded – offers self-heal ready.")


def scrape_card(label: str, holder: str, seen: Set[Tuple]) -> bool:
    """Enroll all visible offers for a given card and capture details."""
    if get_label().text.strip() != label:
        open_card_dropdown()
        wait.until(EC.element_to_be_clickable((By.XPATH, f"{OPT_DROPD_X}[normalize-space()='{label}']"))).click()
        WebDriverWait(driver, 10).until(lambda _: get_label().text.strip() == label)

    if not heal_offers_page(label):
        sheet_log("WARN", "card", f"{label}: could not load offers after retries – aborting account")
        return False

    card, last4 = [s.strip() for s in label.rsplit("-", 1)]

    try:
        driver.execute_script("window.scrollTo(0,0);")
        expand_all()
    except TimeoutException:
        if not heal_offers_page(label):
            sheet_log("WARN", "card", f"{label}: offers never loaded – aborting account")
            return False
        driver.execute_script("window.scrollTo(0,0);")
        expand_all()

    new_rows: List[List[str]] = []
    try:
        while (icons := plus_icons()):
            ico = icons[0]
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ico)
            driver.execute_script("arguments[0].click();", ico)
            WebDriverWait(driver, 8).until(lambda _:
                                           driver.find_elements(By.XPATH, "//div[contains(@class,'enrolled')]") or
                                           driver.find_elements(By.CSS_SELECTOR, ".mo-modal-img-merchant-name"))

            brand = driver.find_element(By.CSS_SELECTOR, ".mo-modal-img-merchant-name").text.strip()
            disc = driver.find_element(By.CSS_SELECTOR, ".mo-modal-offer-title div").text.strip()
            body = driver.find_element(By.CSS_SELECTOR, "cds-column section").text
            maxd  = parse_max_disc(body) or ""
            mins  = parse_min_spend(body) or "None"
            exp_raw = driver.find_element(By.CSS_SELECTOR, ".mo-modal-header-date span").text.strip()
            exp = normalize_expiration_string(exp_raw)
            local = "Yes" if "philadelphia" in body.lower() else "No"
            added = datetime.today().strftime("%m/%d/%Y")

            row = (holder, last4, card, brand, disc, maxd, mins, added, exp, local)
            if row not in seen:
                new_rows.append(list(row))
                seen.add(row)

            close_modal()
            time.sleep(0.25)
    finally:
        if new_rows:
            try:
                OFFER_WS.append_rows(new_rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
                # --- NEW: refresh header filter after each batch append ---
                try:
                    reset_filters_full_range()
                except Exception as exc2:
                    sheet_log("WARN", "filters", f"refresh after append failed: {exc2}")
                # ---------------------------------------------------------
            except Exception as exc:
                sheet_log("ERROR", "append_rows", str(exc))

    return True


print("Function 'scrape_card' loaded – per-card enrollment and capture ready.")


def scrape_account(acct: dict) -> None:
    """Login, reach offers, iterate cards, and logout."""
    user, pwd, holder = acct["user"], acct["pass"], acct["holder"]
    if not citi_login(user, pwd):
        return
    if not goto_offers_page():
        citi_logout()
        return

    seen = {tuple(r) for r in OFFER_WS.get_all_values()[1:]}
    open_card_dropdown()
    labels = [li.text.strip() for li in driver.find_elements(By.XPATH, OPT_DROPD_X)
              if li.text.strip() and li.text.strip().lower() != "credit"]
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)

    for lab in labels:
        ok = scrape_card(lab, holder, seen)
        if not ok:
            break

    citi_logout()


print("Function 'scrape_account' loaded – account-level workflow ready.")

print("Section 'offer scraping' complete – enrollment and capture ready.")

# ---------------------------------------------------------------------------
# Section: sheet maintenance
# ---------------------------------------------------------------------------

# Remove expired offers, dedupe rows, and reset filters

def try_parse_date_any_for_expiration(s: str) -> Optional[date]:
    """Wrapper for expiration column parsing with safety."""
    try:
        return try_parse_date_any(s)
    except Exception:
        return None


def row_is_expired(row: List[str]) -> bool:
    """Return True when the Expiration date is before today."""
    try:
        d = try_parse_date_any_for_expiration(row[8])
        return bool(d and d < date.today())
    except Exception:
        return False


print("Function 'row_is_expired' loaded – expiration detector ready.")


def delete_expired_rows() -> None:
    """Delete expired rows from the sheet."""
    rows = OFFER_WS.get_all_values()
    sid  = OFFER_WS._properties["sheetId"]
    req  = []
    for i in range(len(rows) - 1, 0, -1):
        if row_is_expired(rows[i]):
            req.append({"deleteRange": {"range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1},
                                        "shiftDimension": "ROWS"}})
    if req:
        OFFER_WS.spreadsheet.batch_update({"requests": req})
        sheet_log("INFO", "cleanup", f"deleted {len(req)} expired row(s)")


print("Function 'delete_expired_rows' loaded – expiration cleanup ready.")

def dedupe_rows() -> None:
    """Remove duplicate rows while preserving first occurrence."""
    rows = OFFER_WS.get_all_values()
    seen = set()
    sid  = OFFER_WS._properties["sheetId"]
    req  = []
    for i in range(len(rows) - 1, 0, -1):
        key = tuple(rows[i])
        if key in seen:
            req.append({"deleteRange": {"range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1},
                                        "shiftDimension": "ROWS"}})
        else:
            seen.add(key)
    if req:
        OFFER_WS.spreadsheet.batch_update({"requests": req})
        sheet_log("INFO", "dedupe", f"removed {len(req)} duplicate row(s)")


print("Function 'dedupe_rows' loaded – duplicate removal ready.")


def reset_filters_full_range() -> None:
    """Re-apply filters to the full used range so dropdowns include new values."""
    values = OFFER_WS.get_all_values()
    last_row = max(1, len(values))
    last_col = len(OFFER_HEADERS)
    sid = OFFER_WS._properties["sheetId"]
    SHEET.batch_update({"requests": [
        {"clearBasicFilter": {"sheetId": sid}},
        {"setBasicFilter": {"filter": {
            "range": {
                "sheetId": sid,
                "startRowIndex": 0,
                "endRowIndex": last_row,
                "startColumnIndex": 0,
                "endColumnIndex": last_col
            }
        }}}]})
    sheet_log("INFO", "filters", f"basic filter reset for rows 1..{last_row}")


print("Function 'reset_filters_full_range' loaded – filter reset ready.")

print("Section 'sheet maintenance' complete – cleanup utilities ready.")

# ---------------------------------------------------------------------------
# Section: main & entrypoint
# ---------------------------------------------------------------------------

# Run accounts with driver isolation, then cleanup and finalize

def safe_quit():
    """Attempt to close the browser without raising on invalid session."""
    try:
        driver.quit()
    except InvalidSessionIdException:
        pass


print("Function 'safe_quit' loaded – graceful driver shutdown ready.")

def main() -> None:
    """Process accounts then perform sheet maintenance."""
    ACCOUNTS.sort(key=lambda a: a["holder"] != "Andrew")
    for i, acct in enumerate(ACCOUNTS, start=1):
        sheet_log("INFO", "account", f"start {acct['holder']}")
        try:
            scrape_account(acct)
        except Exception as exc:
            sheet_log("ERROR", "account", f"{acct['holder']} aborted: {type(exc).__name__}: {exc}")
            try:
                citi_logout()
            except Exception:
                pass
        finally:
            # Fully restart the browser between accounts to avoid stale state
            if RESTART_BETWEEN_ACCOUNTS and i < len(ACCOUNTS):
                restart_driver()

    delete_expired_rows()
    dedupe_rows()
    reset_filters_full_range()
    sheet_log("INFO", "main", "COMPLETE")
    print("Run complete – offers synced and sheet updated.")


print("Function 'main' loaded – orchestrator ready.")

if __name__ == "__main__":
    try:
        main()
    except (InvalidSessionIdException, WebDriverException):
        print("Browser window closed – script ended by user.")
        sheet_log("WARN", "main", "Browser closed – terminated by user")
        sys.exit(0)
    except Exception as exc:
        print(f"Fatal error – {type(exc).__name__}: {exc}")
        sheet_log("ERROR", "main", f"Fatal: {type(exc).__name__}: {exc}")
        sys.exit(1)
    finally:
        safe_quit()

print("Section 'main & entrypoint' complete – script ready for execution.")

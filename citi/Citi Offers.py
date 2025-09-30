# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

import os
import re
import sys
import time
from pathlib import Path
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
# Config & constants
# ---------------------------------------------------------------------------

def require_file(path: str, description: str) -> str:
    """Exit if a required file is missing (keeps hard failures obvious)."""
    if not os.path.isfile(path):
        sys.exit(f"Required {description} not found: '{path}' – aborting")
    return path

print("Function 'require_file' loaded – validates required files exist.")

# Project location
DEFAULT_PROJECT_ROOT = r"C:\Users\Andrew\PycharmProjects\Citi-Offers"
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", DEFAULT_PROJECT_ROOT)).resolve()
if not PROJECT_ROOT.exists():
    # Fallback: directory where the script lives
    PROJECT_ROOT = Path(__file__).resolve().parent
print(f"PROJECT_ROOT set to: {PROJECT_ROOT}")

# Load .env from PROJECT_ROOT (usernames/passwords, etc.)
load_dotenv(PROJECT_ROOT / ".env")
print("Environment loaded – .env variables available.")

# Pull Citi accounts from .env
ACCOUNTS: List[dict] = []
idx = 1
while os.getenv(f"CITI_USERNAME_{idx}"):
    ACCOUNTS.append({
        "user":   os.getenv(f"CITI_USERNAME_{idx}", ""),
        "pass":   os.getenv(f"CITI_PASSWORD_{idx}", ""),
        "holder": os.getenv(f"CITI_HOLDER_{idx}", f"Holder {idx}"),
    })
    idx += 1
if not ACCOUNTS:
    sys.exit("No Citi accounts found in .env – aborting")
print(f"Accounts loaded – {len(ACCOUNTS)} account(s) configured.")

# Citi URLs and window placement
LOGIN_URL  = "https://online.citi.com/US/login.do"
OFFERS_URL = "https://online.citi.com/US/ag/products-offers/merchantoffers"
HOME_URL   = "https://online.citi.com/US/home"
SECOND_MONITOR_OFFSET = (3440, 0)  # Move to your 2nd screen if you have one

# Tunable timings
PAGE_LOAD_PAUSE = float(os.getenv("CITI_PAGE_LOAD_PAUSE", "4.0"))
OFFERS_RETRY_MAX = int(os.getenv("CITI_OFFERS_RETRY_MAX", "8"))
NAV_MENU_FALLBACK = os.getenv("CITI_NAV_MENU_FALLBACK", "true").lower() == "true"
RESTART_BETWEEN_ACCOUNTS = os.getenv("CITI_RESTART_BETWEEN_ACCOUNTS", "true").lower() == "true"
NEW_WINDOW_SETTLE_PAUSE = 1.2   # one extra second after new browser opens
SWITCH_CARD_SETTLE_PAUSE = 1.0  # small pause after switching card selection
print("Constants ready – navigation timing and retry settings applied.")

print("Section 'configuration & constants' complete – runtime config set.")

# ---------------------------------------------------------------------------
# Google Sheets bootstrap
# ---------------------------------------------------------------------------

def resolve_service_account_path() -> str:
    """
    Find service_account.json in a sensible way:
    1) GOOGLE_SA_PATH env var (absolute or relative)
    2) PROJECT_ROOT/service_account.json
    3) CWD/service_account.json
    """
    env_path = os.getenv("GOOGLE_SA_PATH", "")
    if env_path:
        p = str(Path(env_path).expanduser().resolve())
        if os.path.isfile(p):
            print(f"Using GOOGLE_SA_PATH: {p}")
            return p
        print(f"GOOGLE_SA_PATH set but missing: {p} – falling back.")

    p2 = str((PROJECT_ROOT / "service_account.json").resolve())
    if os.path.isfile(p2):
        print(f"Using service_account.json from PROJECT_ROOT: {p2}")
        return p2

    p3 = str((Path.cwd() / "service_account.json").resolve())
    if os.path.isfile(p3):
        print(f"Using service_account.json from CWD: {p3}")
        return p3

    sys.exit(
        "Could not locate service_account.json.\n"
        f"Tried:\n"
        f" - GOOGLE_SA_PATH env var\n"
        f" - {p2}\n"
        f" - {p3}\n"
        "Set GOOGLE_SA_PATH or place service_account.json in PROJECT_ROOT."
    )

print("Function 'resolve_service_account_path' loaded – SA path resolver ready.")

SA_PATH = resolve_service_account_path()
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
    """Create or fetch a worksheet and ensure the header row matches."""
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
    """Append a log entry to the Log sheet (simple breadcrumb trail)."""
    try:
        LOG_WS.append_row(
            [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, func, msg],
            value_input_option="RAW",
            insert_data_option="INSERT_ROWS"
        )
    except Exception as exc:
        # Don’t crash if logging fails; just print so you see it.
        print(f"[LOG_FAIL] {level} {func}: {msg} ({type(exc).__name__}: {exc})")

print("Function 'sheet_log' loaded – spreadsheet logging enabled.")

def set_log_row_height():
    """Make log rows easier to read (fixed height)."""
    sid = LOG_WS.id  # public property, not _properties
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
# Selenium driver
# ---------------------------------------------------------------------------

def build_driver() -> Tuple[webdriver.Chrome, WebDriverWait]:
    """Create a Chrome driver and a WebDriverWait helper."""
    opts = Options()
    opts.add_argument("--start-maximized")
    drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        drv.set_window_position(*SECOND_MONITOR_OFFSET)
    except Exception:
        pass
    # small settle so first navigation isn't “too fast”
    time.sleep(NEW_WINDOW_SETTLE_PAUSE)
    return drv, WebDriverWait(drv, 30)

print("Function 'build_driver' loaded – Selenium driver factory ready.")

driver, wait = build_driver()
print("Section 'Selenium driver' complete – driver and wait initialized.")

def restart_driver():
    """Fully restart the browser between accounts (keeps sessions clean)."""
    global driver, wait
    try:
        driver.quit()
    except Exception:
        pass
    driver, wait = build_driver()
    print("Browser restarted – new driver instance created.")

print("Function 'restart_driver' loaded – between-account isolation ready.")

# ---------------------------------------------------------------------------
# Page helpers (detect offers, errors, and heal)
# ---------------------------------------------------------------------------

def offers_ready() -> bool:
    """True when offer tiles are visible on the page."""
    xp = ("//div[contains(@class,'offer-tile') or contains(@class,'mo-offer') or "
          "contains(@data-testid,'offer-tile')]")
    return bool(driver.find_elements(By.XPATH, xp))

print("Function 'offers_ready' loaded – detects when offers are visible.")

def error_banner_visible() -> bool:
    """Inline error in the offers grid (rare)."""
    return bool(driver.find_elements(By.ID, "available-err-msg"))

print("Function 'error_banner_visible' loaded – banner error detector ready.")

def error_toast_visible() -> bool:
    """Alert/toast error (more common)."""
    xpath = (
        "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'trouble loading your offers')]"
        " | //*[@role='alert' or contains(@class,'alert') or contains(@class,'toast')]"
        "[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'error')]"
    )
    return bool(driver.find_elements(By.XPATH, xpath))

print("Function 'error_toast_visible' loaded – toast error detector ready.")

def page_not_found_visible() -> bool:
    """Detect a 404 / ‘Page not found’ style page."""
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
    """Dismiss common popups that block clicks (“No thanks”, “Not now”, etc.)."""
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
                        time.sleep(0.4)
                    except Exception:
                        pass
        if clicked:
            return True
        time.sleep(0.2)
    return False

print("Function 'click_no_thanks_if_present' loaded – popup dismissor ready.")

def logged_in() -> bool:
    """Heuristic: URL contains /dashboard or merchantoffers."""
    u = driver.current_url
    return "/dashboard" in u or "merchantoffers" in u

print("Function 'logged_in' loaded – session state heuristic ready.")

def clear_web_storage():
    """Clear local/session storage in case the app caches a bad state."""
    try:
        driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
    except Exception:
        pass

print("Function 'clear_web_storage' loaded – storage reset ready.")

def return_to_account_if_404(timeout: int = 6) -> bool:
    """If a 404 page shows up, click 'Return to your account' to bounce back."""
    if not page_not_found_visible():
        return False
    try:
        btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((
            By.XPATH,
            "//*[self::a or self::button][contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'return to your account')]"
        )))
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.5)
        sheet_log("INFO", "nav", "Recovered via 'Return to your account'")
        return True
    except Exception:
        return False

print("Function 'return_to_account_if_404' loaded – 404 bounce recovery ready.")

def nav_via_rewards_menu(timeout: int = 10) -> bool:
    """Hover 'Rewards & Offers' and click 'Merchant Offers' to keep app context."""
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
        # Fallback: any visible link to merchantoffers
        try:
            link2 = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.XPATH, "//a[contains(@href,'merchantoffers')]")))
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
    """Navigate to URL with soft retries (heals 'Not Found')."""
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
    """Reach Merchant Offers reliably, healing 404s / unauthorized / slow loads."""

    def on_offers() -> bool:
        return ("merchantoffers" in driver.current_url) and (offers_ready() or not error_toast_visible())

    for attempt in range(1, max_tries + 1):
        clear_web_storage()
        if return_to_account_if_404():
            time.sleep(1.0)

        # Direct URL first (Citi often needs two hits)
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

        # In-app menu fallback keeps context
        used_menu = False
        if NAV_MENU_FALLBACK and nav_via_rewards_menu():
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

        # Later attempts: home then back
        if attempt >= 3:
            go_home_then_back()
            if on_offers():
                sheet_log("INFO", "nav", f"offers ready (home-bridge, try {attempt})")
                return True

        # Gentle refresh as a nudge
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
# Login
# ---------------------------------------------------------------------------

def _type_or_js(el, text: str):
    """Enter text; if the field fights back, set via JS and dispatch events."""
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
    """Prefer the classic login page; bounce via OFFERS_URL if it gives you trouble."""
    driver.switch_to.default_content()
    driver.get(LOGIN_URL)
    time.sleep(pre_wait)
    try:
        WebDriverWait(driver, max_wait).until(EC.presence_of_element_located((By.ID, "username")))
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
            (By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign on')]"),
            (By.CSS_SELECTOR, "button[type='submit']")
        ], timeout=25)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        driver.execute_script("arguments[0].click();", btn)
    except TimeoutException:
        pass_el.send_keys(Keys.ENTER)

print("Function 'login_once' loaded – single-attempt login ready.")

def citi_login(username: str, password: str) -> bool:
    """Resilient login with a couple of speeds; tiny pause after success."""
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
            time.sleep(1.0)  # settle after login
            return True
        time.sleep(2.0)
    sheet_log("ERROR", "login", f"{username} failed on URL {driver.current_url}")
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
        sheet_log("WARN", "logout", f"{type(exc).__name__}: {exc}")

print("Function 'citi_logout' loaded – logout routine ready.")
print("Section 'login' complete – login and logout helpers ready.")

# ---------------------------------------------------------------------------
# Offer scraping
# ---------------------------------------------------------------------------

def plus_icons():
    """Return the enroll icons for unenrolled offers."""
    return driver.find_elements(By.XPATH, "//cds-icon[@name='plus-circle' and @arialabel='Enroll']")

print("Function 'plus_icons' loaded – enroll icon locator ready.")

def expand_all():
    """Click 'Show more'/'Load more' until all offers are visible."""
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
                sheet_log("ERROR", "expand", f"{type(exc).__name__}: {exc}")
        time.sleep(0.3)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'offer-tile')]")))

print("Function 'expand_all' loaded – offer list expander ready.")

def close_modal():
    """Close the offer modal dialog (handles normal close and ESC)."""
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
    """Parse the various date formats Citi uses."""
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
    """Normalize all dates to 'Mon DD, YYYY'."""
    d = try_parse_date_any(s)
    return d.strftime("%b %d, %Y") if d else s

print("Function 'normalize_expiration_string' loaded – date normalizer ready.")

def parse_max_disc(text: str) -> Optional[str]:
    """
    Pull out the dollar cap for % offers.
    Looks for 'Max $50', 'up to $xx back', 'maximum of $xx', etc.
    """
    patterns = [
        r"[Mm]ax(?:imum)?[^$]{0,30}\$(\d[\d,]*)",
        r"up to[^$]{0,30}\$(\d[\d,]*)\s*(?:back|in savings|cash back)?",
        r"capped at[^$]{0,30}\$(\d[\d,]*)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return f"${m.group(1)}"
    return None

print("Function 'parse_max_disc' loaded – max discount parser ready.")

def parse_min_spend(text: str) -> Optional[str]:
    """Extract 'Spend $X' or 'Purchase $X' minimums if present."""
    m = re.search(r"(?:purchase|spend)[^$]{0,25}\$(\d[\d,]*)", text, re.I)
    return f"${m.group(1)}" if m else None

print("Function 'parse_min_spend' loaded – minimum spend parser ready.")

# Card selector (dropdown)
CARD_LABEL_CSS = "div#cds-dropdown-button-value.cds-dd2-pseudo-value"
BTN_DROPD_X = "//button[@id='cds-dropdown' and contains(@class,'cds-dd2-button')]"
OPT_DROPD_X = "//ul[@id='cds-dropdown-listbox']/li[not(contains(@class,'disabled'))]"

def open_card_dropdown() -> None:
    wait.until(EC.element_to_be_clickable((By.XPATH, BTN_DROPD_X))).click()

def get_label_text() -> str:
    return wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, CARD_LABEL_CSS))).text.strip()

print("Card dropdown helpers ready.")

def heal_offers_page(label_to_reselect: Optional[str] = None, tries: int = 3) -> bool:
    """Toggle tabs / refresh to break through slow loads."""
    for _ in range(tries):
        if offers_ready() and not error_toast_visible():
            return True
        for tab_text in ("Enrolled", "All"):
            try:
                tab = driver.find_element(By.XPATH, f"//a[normalize-space()='{tab_text}']")
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(0.6)
            except Exception:
                pass
        if offers_ready() and not error_toast_visible():
            return True
        driver.refresh()
        time.sleep(1.3)
        if label_to_reselect:
            try:
                open_card_dropdown()
                wait.until(EC.element_to_be_clickable(
                    (By.XPATH, f"{OPT_DROPD_X}[normalize-space()='{label_to_reselect}']"))).click()
                WebDriverWait(driver, 8).until(lambda _: get_label_text() == label_to_reselect)
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

# --- Helpers to backfill "Card Name" and "Last Four" from the modal ---
CARD_LINE_XPATH = (
    "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'offer for') "
    "or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'card - ')]"
)

CARD_LAST4_RE = re.compile(r"(?:\*\*|\b|-|\s)(\d{4})\b")
def card_name_and_last4_from_modal() -> Tuple[str, str]:
    """
    Citi modals usually include something like:
    "Offer For  Citi Strata℠ Card – 8549"
    We'll try to extract a readable card name and the last 4.
    """
    modal_text = driver.find_element(By.TAG_NAME, "body").text
    # crude but reliable: look for a line that mentions "Offer For" or "Card - "
    name = ""
    last4 = ""
    try:
        # last 4
        m = CARD_LAST4_RE.search(modal_text)
        if m:
            last4 = m.group(1)
        # card name – grab a nearby phrase before the last 4
        # heuristic: last capitalized run before the last4 hit
        if last4:
            before = modal_text[:modal_text.find(last4)]
            # take the last ~80 chars and clean up "Card –"
            chunk = before[-120:].splitlines()[-1].strip()
            # remove long prefixes
            chunk = re.sub(r"(?i)^offer\s*for[:\s-]+", "", chunk)
            chunk = chunk.replace("–", "-")
            name = chunk.split("-")[0].strip()
            # Some pages say "Products & Offers" instead of a card name; ignore that.
            if "products & offers" in name.lower():
                name = ""
    except Exception:
        pass
    return name, last4

# --- Click-handling for enrollment errors (your screenshot) ---
def enrollment_error_banner_visible() -> bool:
    """Detect the small 'Unable to enroll merchant offer' error overlay."""
    xp = "//*[contains(.,'Unable to enroll merchant offer') and (self::div or self::span or self::p)]"
    return bool(driver.find_elements(By.XPATH, xp))

def dismiss_enrollment_error_if_present() -> None:
    """Try to close the error overlay so the next offer can proceed."""
    # The overlay is usually dismissible via ESC or clicking outside.
    try:
        if enrollment_error_banner_visible():
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)
    except Exception:
        pass

# --- Main per-card worker ---
def scrape_card(dropdown_label: str, holder: str, seen: Set[Tuple]) -> bool:
    """
    Enroll all visible offers for a single card (as selected in the dropdown)
    and capture details into a batch, then append to the sheet once.
    """
    # Ensure the dropdown actually shows this label
    if get_label_text() != dropdown_label:
        open_card_dropdown()
        wait.until(EC.element_to_be_clickable((By.XPATH, f"{OPT_DROPD_X}[normalize-space()='{dropdown_label}']"))).click()
        WebDriverWait(driver, 10).until(lambda _: get_label_text() == dropdown_label)
        time.sleep(SWITCH_CARD_SETTLE_PAUSE)  # give the UI a second

    if not heal_offers_page(dropdown_label):
        sheet_log("WARN", "card", f"{dropdown_label}: could not load offers – aborting this account")
        return False

    # Parse card name/last4 from dropdown label; we'll backfill from modal if needed
    card_from_label, last4_from_label = [s.strip() for s in dropdown_label.rsplit("-", 1)] if "-" in dropdown_label else (dropdown_label, "")
    card_from_label = card_from_label.replace("Products & Offers", "").strip()

    # Expand list so all offers are clickable
    try:
        driver.execute_script("window.scrollTo(0,0);")
        expand_all()
    except TimeoutException:
        if not heal_offers_page(dropdown_label):
            sheet_log("WARN", "card", f"{dropdown_label}: offers never loaded – aborting account")
            return False
        driver.execute_script("window.scrollTo(0,0);")
        expand_all()

    new_rows: List[List[str]] = []
    try:
        while (icons := plus_icons()):
            ico = icons[0]
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ico)
            driver.execute_script("arguments[0].click();", ico)

            # Wait either for "enrolled" visuals or the modal details
            WebDriverWait(driver, 8).until(lambda _:
                                           driver.find_elements(By.XPATH, "//div[contains(@class,'enrolled')]") or
                                           driver.find_elements(By.CSS_SELECTOR, ".mo-modal-img-merchant-name") or
                                           enrollment_error_banner_visible())

            # Handle the rare "Unable to enroll" popup gracefully – one retry, then skip
            if enrollment_error_banner_visible():
                # small pause then retry the click once
                dismiss_enrollment_error_if_present()
                time.sleep(0.8)
                try:
                    driver.execute_script("arguments[0].click();", ico)
                    WebDriverWait(driver, 6).until(lambda _:
                                                   driver.find_elements(By.CSS_SELECTOR, ".mo-modal-img-merchant-name"))
                except Exception:
                    # Still failing; skip this icon
                    sheet_log("WARN", "enroll", "Offer enrollment error – skipping this one")
                    continue

            # Gather data from the modal
            brand_el = driver.find_elements(By.CSS_SELECTOR, ".mo-modal-img-merchant-name")
            brand = brand_el[0].text.strip() if brand_el else "Unknown Brand"
            disc_el = driver.find_elements(By.CSS_SELECTOR, ".mo-modal-offer-title div")
            disc = disc_el[0].text.strip() if disc_el else ""
            body_el = driver.find_elements(By.CSS_SELECTOR, "cds-column section")
            body = body_el[0].text if body_el else ""

            # Max & minimum parse with defaults
            maxd  = parse_max_disc(body) or ""
            mins  = parse_min_spend(body) or "None"

            exp_raw_el = driver.find_elements(By.CSS_SELECTOR, ".mo-modal-header-date span")
            exp_raw = exp_raw_el[0].text.strip() if exp_raw_el else ""
            exp = normalize_expiration_string(exp_raw)

            local = "Yes" if "philadelphia" in body.lower() else "No"
            added = datetime.today().strftime("%m/%d/%Y")

            # Backfill card & last4 from modal if dropdown label was missing/lying
            card_guess, last4_guess = card_name_and_last4_from_modal()
            card = card_from_label or card_guess or "Citi Card"
            last4 = last4_from_label or last4_guess or ""

            # Final row
            row = (holder, last4, card, brand, disc, maxd, mins, added, exp, local)
            if row not in seen:
                new_rows.append(list(row))
                seen.add(row)

            close_modal()
            time.sleep(0.25)
    except Exception as exc:
        sheet_log("ERROR", "scrape_card", f"{dropdown_label}: {type(exc).__name__}: {exc}")
    finally:
        # Batch append once per card; then refresh filters to keep them working
        if new_rows:
            try:
                OFFER_WS.append_rows(new_rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
                reset_filters_full_range()
            except Exception as exc:
                sheet_log("ERROR", "append_rows", f"{type(exc).__name__}: {exc}")

    return True

print("Function 'scrape_card' loaded – per-card enrollment and capture ready.")

def scrape_account(acct: dict) -> None:
    """Login, reach offers, iterate card labels, then logout."""
    user, pwd, holder = acct["user"], acct["pass"], acct["holder"]

    if not citi_login(user, pwd):
        return
    if not goto_offers_page():
        citi_logout()
        return

    seen = {tuple(r) for r in OFFER_WS.get_all_values()[1:]}
    # open dropdown and collect card labels
    try:
        open_card_dropdown()
        labels = [li.text.strip() for li in driver.find_elements(By.XPATH, OPT_DROPD_X)
                  if li.text.strip() and li.text.strip().lower() != "credit"]
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception as exc:
        sheet_log("ERROR", "card_list", f"{type(exc).__name__}: {exc}")
        labels = []

    for lbl in labels:
        ok = scrape_card(lbl, holder, seen)
        if not ok:
            break

    citi_logout()

print("Function 'scrape_account' loaded – account-level workflow ready.")
print("Section 'offer scraping' complete – enrollment and capture ready.")

# ---------------------------------------------------------------------------
# Sheet maintenance
# ---------------------------------------------------------------------------

def try_parse_date_any_for_expiration(s: str) -> Optional[date]:
    try:
        return try_parse_date_any(s)
    except Exception:
        return None

def row_is_expired(row: List[str]) -> bool:
    try:
        d = try_parse_date_any_for_expiration(row[8])
        return bool(d and d < date.today())
    except Exception:
        return False

print("Function 'row_is_expired' loaded – expiration detector ready.")

def delete_expired_rows() -> None:
    rows = OFFER_WS.get_all_values()
    sid  = OFFER_WS.id
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
    rows = OFFER_WS.get_all_values()
    seen = set()
    sid  = OFFER_WS.id
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
    sid = OFFER_WS.id
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
# Main & entrypoint
# ---------------------------------------------------------------------------

def safe_quit():
    """Attempt to close the browser without raising on invalid session."""
    try:
        driver.quit()
    except InvalidSessionIdException:
        pass

print("Function 'safe_quit' loaded – graceful driver shutdown ready.")

def main() -> None:
    """Run accounts (Andrew first), then do cleanup and finalize."""
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
    except (InvalidSessionIdException, WebDriverException) as exc:
        print("Browser window closed – script ended by user.")
        sheet_log("WARN", "main", f"Browser closed – {type(exc).__name__}")
        sys.exit(0)
    except Exception as exc:
        print(f"Fatal error – {type(exc).__name__}: {exc}")
        sheet_log("ERROR", "main", f"Fatal: {type(exc).__name__}: {exc}")
        sys.exit(1)
    finally:
        safe_quit()

print("Section 'main & entrypoint' complete – script ready for execution.")

# ---------------------------------------------------------------------------
# Chase Offers – stable, clean rewrite
# ---------------------------------------------------------------------------
# .env expected keys (examples only; don't paste your real ones here):
#   CHASE_USERNAME_1=...
#   CHASE_PASSWORD_1=...
#   CHASE_HOLDER=Andrew
#   GOOGLE_SA_PATH=C:\path\to\service_account.json
#   GOOGLE_SHEET_KEY=13M4YcJ5vPq4VEeNg1KOmE0QRyRVs9EDrroy66jH6iCs
#   (optional) WINDOW_OFFSET=3440,0
#   (optional) CLOSE_ON_EXIT=false
# ---------------------------------------------------------------------------

import os
import re
import sys
import time
from datetime import datetime, timedelta, date
from typing import List, Set, Tuple, Optional

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.common.exceptions import WebDriverException, InvalidSessionIdException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# -------------------------------
# Config & env
# -------------------------------
def require_file(path: str, description: str) -> str:
    if not os.path.isfile(path):
        sys.exit(f"Required {description} not found: '{path}' – aborting")
    return path

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
print("Env loaded.")

U1 = os.getenv("CHASE_USERNAME_1", "").strip()
P1 = os.getenv("CHASE_PASSWORD_1", "").strip()
if not (U1 and P1):
    sys.exit("Missing CHASE_USERNAME_1 / CHASE_PASSWORD_1 in .env")

HOLDER = os.getenv("CHASE_HOLDER", "").strip()
SHEET_KEY = os.getenv("GOOGLE_SHEET_KEY", "13M4YcJ5vPq4VEeNg1KOmE0QRyRVs9EDrroy66jH6iCs")
SA_PATH = os.getenv("GOOGLE_SA_PATH", os.path.join(PROJECT_ROOT, "service_account.json"))
require_file(SA_PATH, "Google service-account JSON")

# Card order (accountIds)
ACCOUNT_IDS = [
    "1091891200",  # Freedom Flex
    "571406113",   # IHG Classic
    "504430043",   # Ink Business Cash
    "1095857180",  # Ink Preferred
]

# URLs
CHASE_HOME_URL    = "https://www.chase.com/"
CHASE_POST_LOGIN  = "https://secure.chase.com/web/auth/dashboard#/"
CHASE_OFFER_HUB   = "https://secure.chase.com/web/auth/dashboard#/dashboard/merchantOffers/offer-hub"
CHASE_OFFERS_PAGE = "https://secure.chase.com/web/auth/dashboard#/dashboard/merchantOffers/offerCategoriesPage"
CHASE_2FA_FRAGMENT = "recognizeUser/provideAuthenticationCode"

# Timing
POLL_TICK        = float(os.getenv("CHASE_POLL_TICK", "0.06"))
PAGE_LOAD_PAUSE  = float(os.getenv("CHASE_PAGE_LOAD_PAUSE", "0.60"))
FAST_CLICK_DELAY = float(os.getenv("FAST_CLICK_DELAY", "0.25"))
FAST_BACK_WAIT   = float(os.getenv("FAST_BACK_WAIT", "0.25"))
FAST_BETWEEN     = float(os.getenv("FAST_BETWEEN", "0.25"))
CARD_LOAD_PAUSE  = float(os.getenv("CARD_LOAD_PAUSE", "1.4"))
LOGIN_WAIT_MAX   = int(os.getenv("CHASE_LOGIN_WAIT_MAX", "420"))

# Window behavior
CLOSE_ON_EXIT = os.getenv("CLOSE_ON_EXIT", "false").lower() == "true"
SECOND_MONITOR_OFFSET = tuple(int(x) for x in os.getenv("WINDOW_OFFSET", "3440,0").split(","))

# 2FA guards (module-level so they persist)
_TWOFA_PASSWORD_DONE = False
_TWOFA_LAST_ATTEMPT = 0.0

# Sheets buffers
APPEND_BUFFER: List[List[str]] = []
CURRENT_CARD_BUFFER: List[List[str]] = []
APPEND_CHUNK_SIZE = int(os.getenv("APPEND_CHUNK_SIZE", "400"))

# -------------------------------
# Sheets bootstrap
# -------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
CREDS  = Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
SHEET  = gspread.authorize(CREDS).open_by_key(SHEET_KEY)

OFFER_HEADERS = (
    "Card Holder", "Last Four", "Card Name", "Brand",
    "Discount", "Maximum Discount", "Minimum Spend",
    "Date Added", "Expiration", "Local"
)

def _ws(sheet, title: str, headers: Tuple[str, ...]):
    existing = {w.title: w for w in sheet.worksheets()}
    ws = existing.get(title) or sheet.add_worksheet(title=title, rows=5000, cols=len(headers))
    row1 = ws.row_values(1)
    if row1 != list(headers):
        if not row1:
            ws.append_row(list(headers), value_input_option="RAW")
        else:
            ws.update("1:1", [headers], value_input_option="RAW")
    return ws

OFFER_WS = _ws(SHEET, "Card Offers", OFFER_HEADERS)
LOG_WS   = _ws(SHEET, "Log", ("Time", "Level", "Function", "Message"))

def sheet_log(level: str, func: str, msg: str):
    try:
        LOG_WS.append_row(
            [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, func, msg],
            value_input_option="RAW", insert_data_option="INSERT_ROWS"
        )
    except Exception as exc:
        print(f"(Sheets log failed) {level} {func}: {msg} – {exc}")

# -------------------------------
# Driver
# -------------------------------
def build_driver() -> Tuple[webdriver.Chrome, WebDriverWait]:
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        drv.set_window_position(*SECOND_MONITOR_OFFSET)
    except Exception:
        pass
    drv.set_page_load_timeout(90)
    drv.implicitly_wait(0)
    return drv, WebDriverWait(drv, 10)

driver, wait = build_driver()
print("Driver ready.")

# -------------------------------
# Helpers: nav & typing
# -------------------------------
def robust_get(url: str, tries: int = 2) -> bool:
    last_exc = None
    for i in range(max(1, tries)):
        try:
            driver.get(url)
            time.sleep(PAGE_LOAD_PAUSE)
            print(f"[nav] GET {i+1}: {driver.current_url}")
            return True
        except WebDriverException as exc:
            last_exc = exc
            print(f"[nav] warning GET {i+1}: {exc}")
            time.sleep(POLL_TICK)
        try:
            driver.execute_script("window.location.replace(arguments[0]);", url)
            time.sleep(PAGE_LOAD_PAUSE)
            print(f"[nav] JS {i+1}: {driver.current_url}")
            return True
        except Exception as exc2:
            print(f"[nav] warning JS {i+1}: {exc2}")
            time.sleep(POLL_TICK)
    if last_exc:
        sheet_log("WARN", "nav", f"robust_get failed: {last_exc}")
    return False

def on_dashboard() -> bool:
    u = driver.current_url or ""
    return u.startswith(CHASE_POST_LOGIN)

def type_like_human(el, text: str, total_seconds: float = 2.0):
    """Click then type one char at a time over ~total_seconds."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
    except Exception:
        pass
    delay = max(0.03, total_seconds / max(1, len(text)))
    for ch in text:
        el.send_keys(ch)
        time.sleep(delay)

# -------------------------------
# Login + 2FA handling
# -------------------------------
def prefill_home_login(username: str, password: str):
    """Prefill user + pass on the chase.com home login widget."""
    # Username
    for by, val in [
        (By.ID, "userId-text-input-field"),
        (By.CSS_SELECTOR, "input[data-validate='userId']"),
        (By.NAME, "userId"),
        (By.XPATH, "//input[@data-validate='userId' or @id='userId-text-input-field']"),
    ]:
        try:
            el = driver.find_element(by, val)
            if el.is_displayed():
                try: el.clear()
                except: pass
                type_like_human(el, username, total_seconds=1.5)
                # Nudge framework to register value
                driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", el)
                print("[login] Username typed.")
                break
        except Exception:
            pass

    # Password
    for by, val in [
        (By.ID, "password-text-input-field"),
        (By.NAME, "password"),
        (By.CSS_SELECTOR, "input[type='password'][name='password']"),
        (By.XPATH, "//input[@id='password-text-input-field' or @name='password']"),
    ]:
        try:
            el = driver.find_element(by, val)
            if el.is_displayed():
                try: el.clear()
                except: pass
                type_like_human(el, password, total_seconds=2.0)
                driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", el)
                print("[login] Password typed.")
                break
        except Exception:
            pass

def maybe_fill_password_on_2fa(password: str):
    """
    If we land on the 'recognizeUser/provideAuthenticationCode' page,
    click the password container first, then type the password ONCE.
    Retries at most every 6s if it didn't stick.
    """
    global _TWOFA_PASSWORD_DONE, _TWOFA_LAST_ATTEMPT

    u = driver.current_url or ""
    if CHASE_2FA_FRAGMENT not in u:
        return
    if _TWOFA_PASSWORD_DONE:
        return

    now = time.time()
    if now - _TWOFA_LAST_ATTEMPT < 6.0:
        return
    _TWOFA_LAST_ATTEMPT = now

    try:
        # Click the outer container first to mimic a human focus action
        try:
            container = driver.find_element(By.ID, "password_input")
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", container)
            except Exception:
                pass
            try:
                container.click()
                time.sleep(0.15)
            except Exception:
                pass
        except Exception:
            pass

        # Now target the actual input
        el = driver.find_element(By.ID, "password_input-input-field")
        existing = (el.get_attribute("value") or "").strip()
        if existing:
            _TWOFA_PASSWORD_DONE = True
            return

        try:
            el.clear()
        except Exception:
            pass

        # Type like a human over ~2 seconds
        type_like_human(el, password, total_seconds=2.0)

        # Fire input/change events to satisfy client-side validation
        try:
            driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                el
            )
        except Exception:
            pass

        # Confirm it stuck
        if (el.get_attribute("value") or "").strip():
            _TWOFA_PASSWORD_DONE = True
            print("[2FA] Password filled on recognizeUser page.")
        else:
            print("[2FA] Password did not stick; will retry later.")

    except Exception as e:
        print(f"[2FA] Could not fill password: {type(e).__name__}")

def wait_for_post_login(timeout: int = LOGIN_WAIT_MAX) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if on_dashboard():
            print(f"[login] Post-login detected: {driver.current_url}")
            return True
        maybe_fill_password_on_2fa(P1)
        time.sleep(0.5)
    sheet_log("ERROR", "wait", "Timed out waiting for post-login.")
    return False

# -------------------------------
# Offer-page DOM helpers
# -------------------------------
def hub_shell_present() -> bool:
    sels = [
        "//button[@id='select-select-credit-card-account' or @id='select-credit-card-account']",
        "//*[@data-testid='select-credit-card-account' or @id='select-credit-card-account']",
        "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'chase offers')]",
    ]
    for xp in sels:
        if driver.find_elements(By.XPATH, xp):
            return True
    return False

def categories_shell_present() -> bool:
    sels = [
        "//*[@data-testid='offerCategoriesPage']",
        "//h1[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'offers for you')]",
        "//div[contains(@class,'offer') and .//button[contains(@aria-label,'Add') or contains(.,'Add')]]",
        "//*[@data-testid='loading-indicator' or contains(@class,'skeleton')]",
    ]
    for xp in sels:
        if driver.find_elements(By.XPATH, xp):
            return True
    return False

def add_buttons_present() -> bool:
    sels = [
        "button[aria-label*='Add offer']",
        "[data-testid='addOfferButton']",
        "mds-icon[data-testid='commerce-tile-button']",
        "button[aria-label^='Add ']",
        "//button[contains(.,'Add') and not(@disabled)]",
    ]
    for sel in sels:
        try:
            if sel.startswith("//"):
                if driver.find_elements(By.XPATH, sel):
                    return True
            else:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
        except Exception:
            pass
    return False

# -------------------------------
# Parsing helpers
# -------------------------------
BRAND_FALLBACK = "Unknown Brand"
CARD_NAME_DEFAULT = "Chase Card"

def try_parse_date_any(s: str) -> Optional[date]:
    if not s: return None
    s = s.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    m = re.search(r"(\d+)\s+days", s, re.I)
    if m:
        try:
            return (datetime.today() + timedelta(days=int(m.group(1)))).date()
        except Exception:
            return None
    return None

def normalize_date_out(s: Optional[str]) -> str:
    d = try_parse_date_any(s or "")
    return d.strftime("%b %d, %Y") if d else ""

def parse_discount_from_sources(*texts: str) -> str:
    pat = re.compile(
        r"(\$\d[\d,]*(?:\.\d{2})?\s*(?:cash\s*)?back|\$\d[\d,]*(?:\.\d{2})?\s*off|\d{1,3}%\s*(?:cash\s*)?back|\d{1,3}%\s*off)",
        re.I
    )
    for t in texts:
        if not t: continue
        m = pat.search(t)
        if m: return m.group(1).strip()
    return ""

def parse_card_and_last4_quick() -> Tuple[str, str]:
    try:
        el = driver.find_element(By.XPATH, "//span[starts-with(normalize-space(),'Pay with ')]")
        txt = el.text.strip()
        m = re.search(r"^Pay with\s+(.*?)\s*\((?:\.\.\.)?(\d{4})\)", txt)
        if m:
            return m.group(1).strip() + " Card", m.group(2)
    except Exception:
        pass
    body = ""
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        pass
    m2 = re.search(r"(?:ending in|ending\s*\*)\s*(\d{4})", body, re.I)
    if m2:
        return CARD_NAME_DEFAULT, m2.group(1)
    m3 = re.search(r"\(\.\.\.(\d{4})\)", body)
    if m3:
        return CARD_NAME_DEFAULT, m3.group(1)
    return CARD_NAME_DEFAULT, "XXXX"

def read_detail_text_quick(max_chars: int = 6000) -> str:
    sels = [
        "//*[@data-testid='offer-detail-text-and-disclaimer-link-container-id']",
        "//*[@data-cy='offer-detail-text-and-disclaimer-link-container']",
    ]
    for xp in sels:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            try:
                txt = "\n".join(e.text for e in els if e.text.strip())
                return txt[:max_chars]
            except Exception:
                pass
    try:
        return driver.find_element(By.TAG_NAME, "body").text[:max_chars]
    except Exception:
        return ""

def read_offer_header_quick() -> Tuple[str, str]:
    disc = ""
    limit = ""
    try:
        amt = driver.find_elements(By.CSS_SELECTOR, "[data-testid='offerAmount']")
        if amt and amt[0].text.strip():
            disc = amt[0].text.strip()
    except Exception:
        pass
    try:
        lim = driver.find_elements(By.CSS_SELECTOR, "[data-testid='limitations']")
        if lim and lim[0].text.strip():
            limit = lim[0].text.strip()
    except Exception:
        pass
    return disc, limit

def parse_limits_local_expiration(terms_text: str, hdr_limit: str = "") -> Tuple[str, str, str, str]:
    maxd = ""
    m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)\s*(?:cash\s*back\s*)?(?:maximum|max)\b", terms_text, re.I)
    if m: maxd = f"${m.group(1)}"
    if not maxd:
        m = re.search(r"[Mm]ax(?:imum)?[^$]{0,25}\$(\d[\d,]*(?:\.\d{2})?)", terms_text)
        if m: maxd = f"${m.group(1)}"

    mind = ""
    m = re.search(r"(?:spend|purchase)[^$]{0,25}\$(\d[\d,]*(?:\.\d{2})?)", terms_text, re.I)
    if m: mind = f"${m.group(1)}"
    if not mind and hdr_limit:
        m2 = re.search(r"\$(\d[\d,]*(?:\.\d{2})?)", hdr_limit)
        if m2: mind = f"${m2.group(1)}"

    exp = ""
    m = re.search(r"(?:Expires?|Offer expires|Exp\.)\s*(?:on\s*)?([A-Za-z]{3,9}\s+\d{1,2},\s*\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4})", terms_text, re.I)
    if m: exp = normalize_date_out(m.group(1))
    else:
        m2 = re.search(r"expires?\s+in\s+\d+\s+days", terms_text, re.I)
        if m2: exp = normalize_date_out(m2.group(0))

    local = "Yes" if re.search(r"Offer only applies to the following location", terms_text, re.I) else "No"
    if local == "No" and re.search(r"\n\d{2,5}\s+.+\n[A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}", terms_text):
        local = "Yes"

    return maxd, (mind or "None"), exp, local

def extract_brand_smart(tile_guess: str = "") -> str:
    try:
        js = """
        const added = Array.from(document.querySelectorAll('*'))
          .find(e => /added to card/i.test(e.textContent||''));
        if (added) {
            let p = added.previousElementSibling;
            while (p) {
                const t = (p.innerText||'').trim();
                if (t && t.length <= 80) return t;
                p = p.previousElementSibling;
            }
        }
        return '';
        """
        val = driver.execute_script(js) or ""
        if val and not re.search(r"cash\\s*back|\\$\\d", val, re.I):
            return val.strip()
    except Exception:
        pass
    for sel in ("[data-testid='merchantName']","[data-testid='brandName']","div[class*='merchant'] span","div[class*='brand'] span"):
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                txt = els[0].text.strip()
                if not re.search(r"cash\s*back|\$\d", txt, re.I):
                    return txt
        except Exception:
            pass
    try:
        heads = driver.find_elements(By.XPATH, "//*[self::h1 or self::h2 or self::h3 or @role='heading']")
        for h in heads[:6]:
            txt = (h.text or "").strip()
            if txt and not re.search(r"cash\s*back|\$\d|about this deal", txt, re.I) and 2 <= len(txt) <= 60:
                return txt
    except Exception:
        pass
    return tile_guess.strip() or BRAND_FALLBACK

# -------------------------------
# Offer actions
# -------------------------------
def find_add_buttons() -> list:
    sels = [
        "button[aria-label*='Add offer']",
        "[data-testid='addOfferButton']",
        "mds-icon[data-testid='commerce-tile-button']",
        "button[aria-label^='Add ']"
    ]
    found = []
    for sel in sels:
        try:
            found.extend(driver.find_elements(By.CSS_SELECTOR, sel))
        except Exception:
            pass
    # Prefer visible ones
    return [b for b in found if b.is_displayed()]

def click_add_target(el) -> bool:
    try:
        node = el
        for _ in range(5):
            if node.tag_name.lower() == "button" or node.get_attribute("role") == "button":
                break
            node = node.find_element(By.XPATH, "./..")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", node)
        time.sleep(FAST_CLICK_DELAY)
        driver.execute_script("arguments[0].click();", node)
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False

def close_enroll_error_if_present():
    try:
        xp = ("//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
              "'unable to enroll merchant offer')]/ancestor::*[@role='dialog' or contains(@class,'modal')]")
        modals = driver.find_elements(By.XPATH, xp)
        for m in modals:
            btns = m.find_elements(By.XPATH, ".//button[@aria-label='Close' or @aria-label='Dismiss' or contains(.,'Close') or .//cds-icon]")
            if btns:
                driver.execute_script("arguments[0].click();", btns[0])
                time.sleep(0.25)
    except Exception:
        pass

def quick_back():
    btns = driver.find_elements(By.CSS_SELECTOR, "[aria-label='Back']")
    if btns:
        try:
            driver.execute_script("arguments[0].click();", btns[0])
            time.sleep(FAST_BACK_WAIT)
            return
        except Exception:
            pass
    driver.execute_script("window.history.back();")
    time.sleep(FAST_BACK_WAIT)

def tile_fingerprint(tile) -> str:
    try:
        txt = (tile.text or "").strip()
        txt = re.sub(r"\s+", " ", txt)[:200]
        return txt
    except Exception:
        return str(time.time())

def expand_all_offers_if_present():
    # Click "Show more / Load more / See all offers" once if present
    for xp in (
        "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
        "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]",
        "//a[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'see all offers')]",
    ):
        try:
            btns = [b for b in driver.find_elements(By.XPATH, xp) if b.is_displayed()]
            if btns:
                driver.execute_script("arguments[0].click();", btns[0])
                time.sleep(0.5)
        except Exception:
            pass

def gentle_scroll_through():
    # helps lazy-load tiles
    try:
        h = driver.execute_script("return document.body.scrollHeight || document.documentElement.scrollHeight;")
        for y in range(0, int(h), 500):
            driver.execute_script("window.scrollTo(0, arguments[0]);", y)
            time.sleep(0.12)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.15)
    except Exception:
        pass

def enroll_all_offers_for_current_card(existing_rows: Set[Tuple[str,...]]) -> int:
    global APPEND_BUFFER, CURRENT_CARD_BUFFER
    CURRENT_CARD_BUFFER = []
    per_card_rows: List[List[str]] = []
    per_card_keys: Set[tuple] = set()
    processed_fps: Set[str] = set()
    added_total = 0

    expand_all_offers_if_present()
    gentle_scroll_through()

    # Give tiles a moment to appear
    t0 = time.time()
    while time.time() - t0 < 12.0:
        if add_buttons_present():
            break
        time.sleep(0.2)

    idle_cycles = 0
    safety_clicks = 0
    print(f"[offers] starting scan...")

    while True:
        buttons = find_add_buttons()
        picked = None
        picked_tile = None
        picked_fp = None

        for btn in buttons:
            try:
                # parent tile
                t = btn.find_element(By.XPATH, "./ancestor::*[self::div][1]")
                fp = tile_fingerprint(t)
                if fp in processed_fps:
                    # hide to avoid reprocessing
                    try: driver.execute_script("arguments[0].style.display='none';", btn)
                    except Exception: pass
                    continue
                picked = btn
                picked_tile = t
                picked_fp = fp
                break
            except Exception:
                continue

        if not picked:
            idle_cycles += 1
            if idle_cycles >= 3:
                break
            time.sleep(0.3)
            continue

        idle_cycles = 0
        safety_clicks += 1
        if safety_clicks > 200:
            print("[offers] safety stop: too many clicks.")
            break

        tile_text = (picked_tile.text or "")
        # Brand guess from tile
        tile_brand_guess = ""
        try:
            for sel in (".//h3", ".//h2", ".//div[@role='heading']"):
                els = picked_tile.find_elements(By.XPATH, sel)
                if els and els[0].text.strip():
                    tile_brand_guess = els[0].text.strip()
                    break
        except Exception:
            pass
        disc_tile = parse_discount_from_sources(tile_text)

        if not click_add_target(picked):
            # if can't click, mark processed to avoid loop
            processed_fps.add(picked_fp)
            try: driver.execute_script("arguments[0].style.display='none';", picked)
            except Exception: pass
            continue

        # Wait briefly for detail OR immediate "Added" state
        t1 = time.time()
        navigated = False
        while time.time() - t1 < 2.0:
            if driver.find_elements(By.XPATH, "//span[starts-with(normalize-space(),'Pay with ')]"):
                navigated = True
                break
            # or tile might change in place; short pause
            time.sleep(0.1)

        close_enroll_error_if_present()

        if navigated:
            # parse detail page
            header_disc, header_lim = read_offer_header_quick()
            card_name, last4 = parse_card_and_last4_quick()
            terms = read_detail_text_quick()
            maxd, mind, exp_norm, local = parse_limits_local_expiration(terms, hdr_limit=header_lim)
            discount = (parse_discount_from_sources(header_disc, disc_tile, terms, tile_text)
                        or header_disc or disc_tile or "Unknown")
            brand = extract_brand_smart(tile_brand_guess)
            row = [HOLDER, last4, card_name, brand, discount, maxd, mind,
                   datetime.today().strftime("%b %d, %Y"), exp_norm, local]
            key = tuple(row)
            if key not in existing_rows and key not in per_card_keys:
                per_card_rows.append(row); CURRENT_CARD_BUFFER.append(row)
                per_card_keys.add(key); existing_rows.add(key); added_total += 1
            quick_back()
            time.sleep(FAST_BETWEEN)
        else:
            # tile-only add; record minimal info using tile text
            card_name, last4 = CARD_NAME_DEFAULT, "XXXX"
            discount = disc_tile or "Unknown"
            brand = tile_brand_guess or extract_brand_smart(tile_brand_guess)
            maxd = mind = exp = ""; local = "No"
            row = [HOLDER, last4, card_name, brand, discount, maxd, (mind or "None"),
                   datetime.today().strftime("%b %d, %Y"), exp, local]
            key = tuple(row)
            if key not in existing_rows and key not in per_card_keys:
                per_card_rows.append(row); CURRENT_CARD_BUFFER.append(row)
                per_card_keys.add(key); existing_rows.add(key); added_total += 1

        processed_fps.add(picked_fp)

    if per_card_rows:
        try:
            OFFER_WS.append_rows(per_card_rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
            CURRENT_CARD_BUFFER = []
            print(f"[offers] appended {len(per_card_rows)} row(s).")
            reset_filters_full_range()
        except Exception as exc:
            APPEND_BUFFER.extend(per_card_rows)
            print(f"[offers] append failed; buffered {len(per_card_rows)} row(s): {exc}")

    print(f"[offers] done – {added_total} new row(s).")
    return added_total

# -------------------------------
# Card selection & navigation
# -------------------------------
def open_hub() -> bool:
    if not robust_get(CHASE_OFFER_HUB, tries=2):
        print("[hub] nav failed.")
        return False
    t0 = time.time()
    while time.time() - t0 < 8.0:
        if hub_shell_present():
            print("[hub] shell detected.")
            return True
        time.sleep(0.2)
    print("[hub] shell not detected.")
    return False

def select_account_by_id(acc_id: str) -> bool:
    print(f"[acct] selecting {acc_id}")
    try:
        trig = driver.find_elements(By.XPATH, "//button[@id='select-select-credit-card-account']")
        if trig:
            driver.execute_script("arguments[0].click();", trig[0])
        else:
            btn = driver.find_elements(By.XPATH, "//*[@id='select-credit-card-account']")
            if btn:
                driver.execute_script("arguments[0].click();", btn[0])
        time.sleep(0.25)

        js_click = """
        const accId = arguments[0];
        const opt = document.querySelector('mds-select#select-credit-card-account mds-select-option[value="'+accId+'"]');
        if (!opt) return 'no-option';
        const root = opt.shadowRoot; if (!root) return 'no-shadow';
        const hit = root.querySelector('.option'); if (!hit) return 'no-hit';
        hit.click(); return 'ok';
        """
        res = driver.execute_script(js_click, acc_id)
        print(f"[acct] shadow click: {res}")
        time.sleep(CARD_LOAD_PAUSE)
        return res == "ok"
    except Exception as exc:
        print(f"[acct] error: {exc}")
        return False

def go_to_categories_for(acc_id: str) -> bool:
    if not open_hub():
        return False
    _ = select_account_by_id(acc_id)  # best effort

    # Hash-only route change is more reliable in SPA:
    hash_route = f"/dashboard/merchantOffers/offerCategoriesPage?accountId={acc_id}&offerCategoryName=ALL"
    try:
        driver.execute_script("window.location.hash = arguments[0];", hash_route)
    except Exception:
        pass
    time.sleep(0.6)

    # Wait until correct account + tiles present
    t0 = time.time()
    while time.time() - t0 < 12.0:
        u = driver.current_url or ""
        on_cat = ("/merchantOffers/offerCategoriesPage" in u) and (acc_id in u)
        dom_ok = add_buttons_present() or categories_shell_present()
        if on_cat and dom_ok:
            time.sleep(0.3)
            return True
        time.sleep(0.2)

    # Last nudge: click any categories link once
    try:
        link = driver.find_elements(By.XPATH, "//a[contains(@href,'offerCategoriesPage')]")
        if link:
            driver.execute_script("arguments[0].click();", link[0])
            time.sleep(1.2)
            t1 = time.time()
            while time.time() - t1 < 6.0:
                u = driver.current_url or ""
                on_cat = ("/merchantOffers/offerCategoriesPage" in u) and (acc_id in u)
                dom_ok = add_buttons_present() or categories_shell_present()
                if on_cat and dom_ok:
                    return True
                time.sleep(0.2)
    except Exception:
        pass

    print("[cat] categories not confirmed.")
    return False

# -------------------------------
# Card loop
# -------------------------------
def process_cards():
    existing_rows: Set[Tuple[str, ...]] = {tuple(r) for r in OFFER_WS.get_all_values()[1:]}
    total_cards = 0
    total_rows  = 0

    for idx, acc_id in enumerate(ACCOUNT_IDS, start=1):
        print(f"\n----- Card {idx}/{len(ACCOUNT_IDS)} – accountId={acc_id}")
        try:
            if not go_to_categories_for(acc_id):
                print("[card] categories view not ready; skipping.")
                total_cards += 1
                continue

            added = enroll_all_offers_for_current_card(existing_rows)
            print(f"[card] {idx} -> {added} new row(s).")
            total_rows += max(0, added)
            total_cards += 1
        except Exception as exc:
            sheet_log("ERROR", "card", f"{acc_id}: {type(exc).__name__}: {exc}")
            print(f"[card] error {acc_id}: {exc}")
            continue

        time.sleep(0.7)

    print(f"[cards] complete – {total_cards} card(s), {total_rows} row(s).")

# -------------------------------
# Sheet maintenance
# -------------------------------
def normalize_sheet_dates():
    rows = OFFER_WS.get_all_values()
    updates = []
    for i in range(1, len(rows)):
        row = rows[i]
        for col_idx in (7, 8):
            raw = row[col_idx].strip() if col_idx < len(row) else ""
            norm = normalize_date_out(raw) if raw else ""
            if norm and norm != raw:
                updates.append((i + 1, col_idx + 1, norm))
    for (r, c, v) in updates:
        OFFER_WS.update_cell(r, c, v)
    print(f"[sheet] normalized {len(updates)} date cell(s).")

def dedupe_rows() -> int:
    rows = OFFER_WS.get_all_values()
    seen = set(); sid = OFFER_WS._properties["sheetId"]; req = []
    for i in range(len(rows) - 1, 0, -1):
        key = tuple(rows[i])
        if key in seen:
            req.append({"deleteRange": {"range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1},
                                        "shiftDimension": "ROWS"}})
        else:
            seen.add(key)
    if req:
        OFFER_WS.spreadsheet.batch_update({"requests": req})
    print(f"[sheet] deduped {len(req)} row(s).")
    return len(req)

def reset_filters_full_range():
    values = OFFER_WS.get_all_values()
    last_row = max(1, len(values))
    last_col = len(OFFER_HEADERS)
    sid = OFFER_WS._properties["sheetId"]
    SHEET.batch_update({"requests": [
        {"clearBasicFilter": {"sheetId": sid}},
        {"setBasicFilter": {"filter": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": last_col}
        }}}]})
    print(f"[sheet] filter 1..{last_row}.")

# -------------------------------
# Main
# -------------------------------
def flush_buffer():
    global APPEND_BUFFER, CURRENT_CARD_BUFFER
    if CURRENT_CARD_BUFFER:
        APPEND_BUFFER.extend(CURRENT_CARD_BUFFER)
        CURRENT_CARD_BUFFER = []
    if not APPEND_BUFFER:
        print("[flush] nothing to append.")
        return
    total = 0
    try:
        for i in range(0, len(APPEND_BUFFER), APPEND_CHUNK_SIZE):
            chunk = APPEND_BUFFER[i:i + APPEND_CHUNK_SIZE]
            OFFER_WS.append_rows(chunk, value_input_option="RAW", insert_data_option="INSERT_ROWS")
            total += len(chunk)
        print(f"[flush] appended {total} buffered row(s).")
        reset_filters_full_range()
    except Exception as exc:
        print(f"[flush] error: {exc}")
    finally:
        APPEND_BUFFER = []

def safe_quit():
    try:
        driver.quit()
    except InvalidSessionIdException:
        pass

def main():
    try:
        # 1) Home, prefill login
        robust_get(CHASE_HOME_URL, tries=1)
        prefill_home_login(U1, P1)
        print("[login] Finish MFA in browser (I'll fill the extra password once on the code page if shown).")

        # 2) Wait until dashboard/overview shows up
        if not wait_for_post_login(LOGIN_WAIT_MAX):
            print("[main] Post-login not detected – aborting.")
            return

        process_cards()

        # 3) Process cards
        process_cards()

        # 4) Sheet maintenance
        normalize_sheet_dates()
        dedupe_rows()
        reset_filters_full_range()

        print("[main] Run complete.")
    except KeyboardInterrupt:
        print("[main] Interrupted – flushing buffers.")
        sheet_log("WARN", "main", "Interrupted by user – flushing buffers.")
    except Exception as exc:
        print(f"[main] Fatal – {type(exc).__name__}: {exc}")
        sheet_log("ERROR", "main", f"Fatal: {type(exc).__name__}: {exc}")
    finally:
        flush_buffer()
        if CLOSE_ON_EXIT:
            safe_quit()
        else:
            print("[main] Browser left open – Ctrl+C here to exit.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print("[main] Exiting; leaving browser window as-is.")

if __name__ == "__main__":
    main()

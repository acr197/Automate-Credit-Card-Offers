# -*- coding: utf-8 -*-
# Amex Offers → Google Sheet (attach-to-Chrome, resilient tile parsing, micro-batched writes)
# Added: post-pass refresh cycles to surface >100 hidden offers

import os, re, sys, time, random, subprocess
from datetime import datetime
from typing import List, Tuple

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

# -------------------------------
# ENV
# -------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

AMEX_HOLDER = (os.getenv("AMEX_HOLDER") or "").strip() or "Card Holder"

SA_PATH = (os.getenv("GOOGLE_SA_PATH") or "").strip().strip('"').strip("'")
if not SA_PATH or not os.path.isfile(SA_PATH):
    sys.exit(f"Required Google service-account JSON not found: '{SA_PATH}' – aborting")

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Credit Card Offers").strip()
AUTO_CLOSE = os.getenv("AUTO_CLOSE", "0").strip() == "1"

# Attach-to-Chrome (remote debugging)
CHROME_DEBUG_PORT   = int(os.getenv("CHROME_DEBUG_PORT", "9222"))
CHROME_PROFILE_DIR  = os.getenv("CHROME_PROFILE_DIR", os.path.join(HERE, "chrome-profile"))
CHROME_PROFILE_NAME = os.getenv("CHROME_PROFILE_NAME", "Default")
CHROME_EXE          = os.getenv("CHROME_EXE", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
LAUNCH_DEBUG_CHROME = os.getenv("LAUNCH_DEBUG_CHROME", "1").strip() != "0"

# URLs
LOGIN_URL   = "https://www.americanexpress.com/en-us/account/login"
OVERVIEW    = "https://global.americanexpress.com/overview"
DASHBOARD   = "https://global.americanexpress.com/dashboard"
OFFERS_ROOT = "https://global.americanexpress.com/offers"
OFFERS_KEY  = "https://global.americanexpress.com/offers?account_key="

# Timing
PAGE_PAUSE   = 0.7
SMALL_PAUSE  = 0.25
CLICK_PAUSE  = 0.35
SCROLL_STEP  = 650
SCROLL_DELAY = 0.11
MICRO_BATCH  = 3  # flush to Sheets every 3 rows

# New: refresh rounds after a full pass (to reveal >100 offers)
MAX_REFRESH_ROUNDS = int(os.getenv("AMEX_REFRESH_ROUNDS", "3"))

# Buffer
APPEND_BUFFER: List[List[str]] = []

# -------------------------------
# Google Sheets
# -------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
CREDS  = Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
SHEET  = gspread.authorize(CREDS).open(SHEET_NAME)

HEADERS = (
    "Card Holder","Last Four","Card Name","Brand",
    "Discount","Maximum Discount","Minimum Spend",
    "Date Added","Expiration","Local"
)

def _ws(sheet, title, headers):
    ex = {w.title: w for w in sheet.worksheets()}
    ws = ex.get(title) or sheet.add_worksheet(title=title, rows=8000, cols=len(headers))
    row1 = ws.row_values(1)
    if row1 != list(headers):
        if not row1: ws.append_row(list(headers), value_input_option="RAW")
        else: ws.update("1:1", [headers], value_input_option="RAW")
    return ws

OFFERS_WS = _ws(SHEET, "Card Offers", HEADERS)
LOG_WS    = _ws(SHEET, "Log", ("Time","Level","Function","Message"))

def sheet_log(level, func, msg):
    try:
        LOG_WS.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, func, msg],
                          value_input_option="RAW", insert_data_option="INSERT_ROWS")
    except Exception:
        pass

def append_rows_now(rows: List[List[str]]):
    if not rows: return
    try:
        OFFERS_WS.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        print(f"Appended {len(rows)} row(s).")
    except Exception as exc:
        sheet_log("ERROR","append_rows",str(exc))

def flush_buffer():
    global APPEND_BUFFER
    if APPEND_BUFFER:
        append_rows_now(APPEND_BUFFER)
        APPEND_BUFFER = []

def dedupe_rows() -> int:
    rows = OFFERS_WS.get_all_values()
    seen = set(); sid = OFFERS_WS._properties["sheetId"]; req = []
    for i in range(len(rows)-1, 0, -1):
        key = tuple(rows[i])
        if key in seen:
            req.append({"deleteRange":{"range":{"sheetId":sid,"startRowIndex":i,"endRowIndex":i+1},"shiftDimension":"ROWS"}})
        else:
            seen.add(key)
    if req: OFFERS_WS.spreadsheet.batch_update({"requests": req})
    return len(req)

def reset_filters():
    values = OFFERS_WS.get_all_values()
    last_row = max(1, len(values)); last_col = len(HEADERS)
    sid = OFFERS_WS._properties["sheetId"]
    try:
        SHEET.batch_update({"requests":[
            {"clearBasicFilter":{"sheetId":sid}},
            {"setBasicFilter":{"filter":{"range":{
                "sheetId":sid,"startRowIndex":0,"endRowIndex":last_row,"startColumnIndex":0,"endColumnIndex":last_col
            }}}}
        ]})
    except Exception:
        pass

# -------------------------------
# Attach to Chrome
# -------------------------------
def ensure_debug_chrome():
    if not LAUNCH_DEBUG_CHROME:
        return
    try:
        os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
        subprocess.Popen([
            CHROME_EXE,
            f"--remote-debugging-port={CHROME_DEBUG_PORT}",
            f"--user-data-dir={CHROME_PROFILE_DIR}",
            f"--profile-directory={CHROME_PROFILE_NAME}",
            "--no-first-run",
            "--no-default-browser-check",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
    except Exception as exc:
        print(f"[attach] Couldn't auto-launch Chrome: {exc}")

def build_driver():
    ensure_debug_chrome()
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument("--lang=en-US,en")
    # No anti-bot switches; attach to real Chrome:
    opts.debugger_address = f"127.0.0.1:{CHROME_DEBUG_PORT}"
    print(f"[attach] Connecting to Chrome at {opts.debugger_address}")
    drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    drv.set_page_load_timeout(90)
    drv.implicitly_wait(0)
    return drv

driver = build_driver()

# -------------------------------
# Nav helpers
# -------------------------------
def robust_get(url: str) -> bool:
    try:
        driver.get(url); time.sleep(PAGE_PAUSE); return True
    except WebDriverException as exc:
        sheet_log("WARN","nav",str(exc))
        return False

def offers_tiles_present() -> bool:
    try:
        if driver.find_elements(By.CSS_SELECTOR, "button[data-testid='merchantOfferListAddButton']"):
            return True
        if driver.find_elements(By.XPATH, "//*[contains(.,'Expires') or @data-testid='expirationDate']"):
            return True
    except Exception:
        pass
    return False

def gentle_scroll_full():
    try:
        h = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);")
        y = 0
        while y < h + 400:
            driver.execute_script("window.scrollTo(0, arguments[0]);", y)
            time.sleep(SCROLL_DELAY + random.uniform(0.01,0.03))
            y += SCROLL_STEP
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.2)
    except Exception:
        pass

def wait_until_offers_ready():
    print("Log in in the attached Chrome and finish MFA. I’ll begin on the Offers page.")
    last_nav = 0.0
    while True:
        try:
            url = (driver.current_url or "").split("#")[0].lower()

            if url.startswith(OFFERS_KEY.lower()):
                t0 = time.time()
                while time.time() - t0 < 15:
                    if offers_tiles_present(): return
                    time.sleep(0.3)

            if url.startswith(OVERVIEW.lower()) or url.startswith(DASHBOARD.lower()):
                if time.time() - last_nav > 2.5:
                    last_nav = time.time()
                    robust_get(OFFERS_ROOT)

            if url.startswith(OFFERS_ROOT.lower()) and not url.startswith(OFFERS_KEY.lower()):
                t1 = time.time()
                while time.time() - t1 < 15:
                    u = (driver.current_url or "").lower()
                    if u.startswith(OFFERS_KEY.lower()) or offers_tiles_present():
                        return
                    time.sleep(0.3)

            time.sleep(0.3)
        except WebDriverException:
            raise

# -------------------------------
# Parsing helpers (robust to layout changes)
# -------------------------------
MONEY_RE = r"\$[\d,]+(?:\.\d{2})?"

def normalize_exp(raw: str) -> str:
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%b %d, %Y")
        except Exception:
            pass
    m = re.search(r"Expires[, ]+(.+)$", raw, re.I)
    if m:
        return normalize_exp(m.group(1))
    m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{2,4})", raw)
    if m:
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d, %y", "%B %d, %y"):
            try:
                return datetime.strptime(m.group(1), fmt).strftime("%b %d, %Y")
            except Exception:
                pass
    return raw

def parse_from_desc(desc: str) -> tuple:
    desc = desc or ""
    m = re.search(r"Spend\s*(" + MONEY_RE + ")", desc, re.I)
    min_spend = m.group(1) if m else "None"
    m = re.search(r"(?:earn|get)\s*((?:\d{1,3}%|" + MONEY_RE + r"))\s*back", desc, re.I)
    if not m: m = re.search(r"(\d{1,3}%\s*(?:cash\s*)?back)", desc, re.I)
    if not m: m = re.search(r"(" + MONEY_RE + r")\s*back", desc, re.I)
    discount = (f"{m.group(1)} back" if m and "%" not in m.group(1) else (m.group(1) if m else "Unknown"))
    m = re.search(r"total of\s*(" + MONEY_RE + ")", desc, re.I)
    max_total = m.group(1) if m else ""
    return discount, min_spend, max_total

def infer_brand_from_text(txt: str) -> str:
    lines = [l.strip() for l in (txt or "").splitlines() if l.strip()]
    for l in lines[:6]:
        if re.search(r"^Expires\b|^Spend\b|^Earn\b|View Details|Terms apply", l, re.I):
            continue
        if re.search(MONEY_RE, l) and re.search(r"back|spend|earn|total|expires", l, re.I):
            continue
        return l
    return "Unknown Brand"

def tile_root_from_button(btn):
    node = btn
    for _ in range(8):
        try:
            parent = node.find_element(By.XPATH, "./..")
        except Exception:
            break
        node = parent
        try:
            block_text = node.text or ""
        except Exception:
            block_text = ""
        if re.search(r"View Details|Terms apply|Expires", block_text, re.I):
            return node
    return btn

def extract_tile_data(tile) -> tuple:
    brand = "Unknown Brand"
    try:
        brand = tile.find_element(By.XPATH, ".//h3//span").text.strip() or brand
    except Exception:
        pass
    try:
        txt = tile.text or ""
    except Exception:
        txt = ""
    if brand == "Unknown Brand":
        brand = infer_brand_from_text(txt)
    desc = ""
    for line in sorted([l.strip() for l in txt.splitlines()], key=len, reverse=True):
        if re.search(r"\bSpend\b|\bEarn\b|\bback\b", line, re.I):
            desc = line
            break
    discount, min_spend, max_total = parse_from_desc(desc)
    exp = ""
    try:
        t = tile.find_element(By.CSS_SELECTOR, "[data-testid='expirationDate']").text.strip()
        if t: exp = normalize_exp(t)
    except Exception:
        pass
    if not exp:
        m = re.search(r"Expires\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4})", txt, re.I)
        if m: exp = normalize_exp(m.group(1))
    return brand, discount, min_spend, max_total, exp

# -------------------------------
# Card info
# -------------------------------
def current_card_info() -> Tuple[str, str]:
    name, last4 = "Amex Card", "XXXX"
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r"([A-Za-z0-9 &'’\-]+)\s*\n?[\u2022•\*]{3,}\s*(\d{4,5})", body)
        if m:
            name = m.group(1).strip()
            last4 = m.group(2)[-4:]
        else:
            m4 = re.search(r"[\u2022•\*]+\s*(\d{4,5})", body)
            if m4:
                last4 = m4.group(1)[-4:]
    except Exception:
        pass
    return name, last4

# -------------------------------
# Adding loop
# -------------------------------
def plus_buttons_snapshot() -> List:
    try:
        btns = driver.find_elements(By.CSS_SELECTOR, "button[data-testid='merchantOfferListAddButton']:not([data-processed='1'])")
        if not btns:
            btns = driver.find_elements(
                By.XPATH,
                "//button[.//*[name()='svg']//*[name()='path' and "
                "((contains(@d,'v-19') and contains(@d,'h-19')) or contains(@d,'-19v-19')) and "
                "not(@data-processed='1')]"
            )
        return [b for b in btns if b.is_displayed()]
    except Exception:
        return []

def wait_added_visual(tile, btn, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if tile.find_elements(By.XPATH, ".//*[name()='path' and (@fill-rule='evenodd' or @clip-rule='evenodd')]"):
                return True
        except Exception:
            pass
        try:
            if not btn.is_displayed():
                return True
        except Exception:
            return True
        time.sleep(0.15)
    return False

def expand_more_if_present():
    xps = [
        "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view more')]",
        "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]",
        "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
    ]
    for xp in xps:
        try:
            btns = [b for b in driver.find_elements(By.XPATH, xp) if b.is_displayed()]
            if btns:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btns[0])
                btns[0].click()
                time.sleep(0.8)
        except Exception:
            continue

def add_all_offers_for_current_card(holder: str) -> int:
    """Returns the number of offers clicked (added) in this pass."""
    global APPEND_BUFFER
    expand_more_if_present()
    gentle_scroll_full()

    card_name, last4 = current_card_info()
    today = datetime.today().strftime("%b %d, %Y")

    safety_clicks = 0
    idle_rounds = 0
    added_count = 0

    while True:
        btns = plus_buttons_snapshot()
        if not btns:
            idle_rounds += 1
            if idle_rounds >= 2:
                break
            expand_more_if_present()
            gentle_scroll_full()
            time.sleep(0.3)
            continue

        idle_rounds = 0
        btn = btns[0]
        tile = tile_root_from_button(btn)
        brand, discount, min_spend, max_total, exp = extract_tile_data(tile)

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(SMALL_PAUSE + random.uniform(0.03, 0.08))
            btn.click()
            time.sleep(CLICK_PAUSE + random.uniform(0.03, 0.08))
            wait_added_visual(tile, btn, timeout=3.0)
        except Exception as exc:
            sheet_log("WARN", "click_add", f"{type(exc).__name__}")

        try:
            driver.execute_script("arguments[0].setAttribute('data-processed','1'); arguments[0].style.display='none';", btn)
        except Exception:
            pass

        APPEND_BUFFER.append([
            holder,
            last4,
            card_name,
            brand or "Unknown Brand",
            discount or "Unknown",
            (max_total or ""),
            (min_spend or "None"),
            today,
            (exp or ""),
            "No"
        ])
        added_count += 1

        if len(APPEND_BUFFER) >= MICRO_BATCH:
            flush_buffer()

        safety_clicks += 1
        if safety_clicks > 800:
            sheet_log("WARN", "add_loop", "safety break")
            break

        time.sleep(0.12 + random.uniform(0.02, 0.06))

    return added_count

# -------------------------------
# Main
# -------------------------------
def main():
    try:
        robust_get(LOGIN_URL)
        print("Login window opened. Complete sign-in and MFA.")
        print("When I detect Offers, I’ll start adding and logging rows.")

        wait_until_offers_ready()

        t0 = time.time()
        while time.time() - t0 < 15:
            if offers_tiles_present(): break
            time.sleep(0.3)

        # ---- First pass
        total_added = add_all_offers_for_current_card(AMEX_HOLDER)
        flush_buffer()

        # ---- NEW: refresh cycles to reveal additional offers (>100)
        rounds = 0
        while rounds < MAX_REFRESH_ROUNDS:
            rounds += 1
            if total_added == 0:
                break  # nothing added in last pass; likely nothing more
            driver.refresh()
            time.sleep(1.5)

            # wait until tiles are present again
            t1 = time.time()
            while time.time() - t1 < 15:
                if offers_tiles_present():
                    break
                time.sleep(0.3)

            added = add_all_offers_for_current_card(AMEX_HOLDER)
            flush_buffer()
            total_added = added  # if zero on a round, we’ll break next loop

        removed = dedupe_rows()
        if removed: print(f"Removed {removed} duplicate row(s).")
        reset_filters()
        print("Done.")
    except (InvalidSessionIdException, WebDriverException) as exc:
        print(f"Browser session ended: {exc}")
        sheet_log("ERROR","main",f"WebDriver: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}")
        sheet_log("ERROR","main",f"Fatal: {type(exc).__name__}: {exc}")
    finally:
        flush_buffer()
        if AUTO_CLOSE:
            try: driver.quit()
            except Exception: pass
        else:
            try:
                input("Chrome left open – press Enter here to exit this script…")
            except EOFError:
                pass

if __name__ == "__main__":
    main()
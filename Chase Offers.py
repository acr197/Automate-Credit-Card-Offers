# --------------------------------------------------
# imports
# --------------------------------------------------
# Std-lib, Selenium, and Google-Sheets libraries.
# --------------------------------------------------
import os, re, sys, time, logging
from datetime import datetime, timedelta
from typing import List, Set, Tuple, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None; Credentials = None

print("imports loaded")
# --------------------------------------------------



# --------------------------------------------------
# config
# --------------------------------------------------
# URLs, env vars, dedup path, logger.
# --------------------------------------------------
from dotenv import load_dotenv; load_dotenv()

LOGIN_URL  = "https://secure.chase.com/web/auth/dashboard#/login"
OFFERS_URL = ("https://secure.chase.com/web/auth/dashboard#/"
              "dashboard/merchantOffers/offerCategoriesPage")

DEDUP_FILE = "added_offers.txt"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Credit Card Offers")
CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

print("config loaded")
# --------------------------------------------------



# --------------------------------------------------
# sheets
# --------------------------------------------------
# Worksheet creator, logger row helper, Sheet handles.
# --------------------------------------------------
def _ws(sheet, title: str, headers: Tuple[str, ...]):
    """Create/open worksheet and ensure header row exists."""
    ws = sheet.worksheet(title) if title in [w.title for w in sheet.worksheets()] \
         else sheet.add_worksheet(title=title, rows=2000, cols=len(headers))
    if not ws.row_values(1):
        ws.append_row(list(headers), value_input_option="RAW")
    return ws
print("_ws loaded")

def sheet_log(level: str, func: str, msg: str):
    """Append tiny log row to Google-Sheet."""
    if LOG_WS:
        LOG_WS.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           level, func, msg],
                          value_input_option="RAW",
                          insert_data_option="INSERT_ROWS")
print("sheet_log loaded")

if gspread and os.path.exists(CREDS_JSON):
    CREDS   = Credentials.from_service_account_file(CREDS_JSON, scopes=SCOPES)
    SHEET   = gspread.authorize(CREDS).open(SHEET_NAME)
    OFFER_WS = _ws(SHEET, "Card Offers",
                   ("Card Holder","Last Four","Card Name","Brand",
                    "Discount","Maximum Discount","Minimum Spend",
                    "Expiration","Local"))
    LOG_WS   = _ws(SHEET, "Log", ("Time","Level","Function","Message"))
    print("Sheets configured")
else:
    OFFER_WS = LOG_WS = None
    print("Sheets disabled")
# --------------------------------------------------



# --------------------------------------------------
# driver
# --------------------------------------------------
# Launch fresh Chrome profile and return driver + long wait.
# --------------------------------------------------
def build_driver():
    opts = Options(); opts.add_argument("--start-maximized")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-data-dir={os.getcwd()}/chase_{int(time.time())}")
    drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                           options=opts)
    drv.implicitly_wait(8); drv.set_page_load_timeout(60)
    return drv, WebDriverWait(drv, 25)
print("build_driver loaded")
# --------------------------------------------------



# --------------------------------------------------
# wait helpers
# --------------------------------------------------
# Detect correct page + presence of plus icons before scraping.
# --------------------------------------------------
def page_ready(drv): return drv.current_url.startswith(OFFERS_URL)
print("page_ready loaded")

def plus_icons(drv):
    """Return list of 'Add offer' plus icons (un-enrolled offers)."""
    return drv.find_elements(
        By.CSS_SELECTOR,
        "mds-icon[data-testid='commerce-tile-button']"
    )
print("plus_icons loaded")

def wait_for_login(drv, timeout: int = 300):
    """Block until user has logged in and offers grid is present."""
    end = time.time() + timeout
    log.info("Waiting for manual login and offers page…")
    while time.time() < end:
        if page_ready(drv) and plus_icons(drv):
            log.info("Offers page detected – starting scrape.")
            return
        time.sleep(2)
    log.error("Timed-out waiting for login/offers page."); sys.exit(1)
print("wait_for_login loaded")
# --------------------------------------------------



# --------------------------------------------------
# dedup
# --------------------------------------------------
def load_keys() -> Set[str]:
    """Load keys already processed this run / previous runs."""
    return {l.strip() for l in open(DEDUP_FILE,encoding="utf-8")} \
        if os.path.exists(DEDUP_FILE) else set()
print("load_keys loaded")

def save_key(k:str):
    """Persist newly processed key."""
    with open(DEDUP_FILE,"a",encoding="utf-8") as fh: fh.write(k+"\n")
print("save_key loaded")
# --------------------------------------------------



# --------------------------------------------------
# parsers
# --------------------------------------------------
def modal_body(drv): return drv.find_element(By.TAG_NAME,"body").text
print("modal_body loaded")

def parse_vals(text:str):
    """Return (max_discount, min_spend) strings."""
    maxd = re.search(r"[Mm]ax[^$]{0,25}\$(\d[\d,]*)", text)
    mind = re.search(r"(?:spend|purchase)[^$]{0,25}\$(\d[\d,]*)", text, re.I)
    return (f"${maxd.group(1)}" if maxd else "",
            f"${mind.group(1)}" if mind else "")
print("parse_vals loaded")
# --------------------------------------------------



# --------------------------------------------------
# offer loop
# --------------------------------------------------
def process(drv, wait, holder:str, card_name:str="Chase"):
    """Click every plus icon, scrape details, push to Sheet."""
    done = load_keys(); new_rows=[]
    while (icons := plus_icons(drv)):
        ico = icons[0]
        aria = ico.get_attribute("aria-label") or ""
        m = re.search(r"of \d+\s+(.*?)\s+(\d+%.*?)\s+(\d+)\s+days", aria)
        brand, disc, days = m.groups() if m else ("Unknown","", "0")
        last4_m = re.search(r"ending in (\d{4})", aria)
        last4 = last4_m.group(1) if last4_m else "XXXX"
        key = f"{holder}|{last4}|{brand}|{disc}"
        if key in done:
            icons.pop(0); continue
        drv.execute_script("arguments[0].scrollIntoView({block:'center'});", ico)
        time.sleep(0.3); ico.click()
        wait.until(lambda d: not plus_icons(d) or d.current_url!=OFFERS_URL)
        txt  = modal_body(drv)
        maxd, mind = parse_vals(txt)
        try:
            exp = (datetime.today()+timedelta(days=int(days))).strftime("%m/%d/%Y")
        except: exp = ""
        local = "Yes" if "philadelphia" in txt.lower() else "No"
        row   = (holder,last4,card_name,brand,disc,maxd,mind,exp,local)
        if OFFER_WS: new_rows.append(list(row))
        save_key(key); done.add(key)
        drv.back(); wait.until(lambda d: plus_icons(d))
    if new_rows:
        OFFER_WS.append_rows(new_rows,value_input_option="RAW",
                             insert_data_option="INSERT_ROWS")
    log.info("Added %d new offers", len(new_rows))
print("process loaded")
# --------------------------------------------------



# --------------------------------------------------
# main
# --------------------------------------------------
def main():
    drv, wait = build_driver()
    drv.get(LOGIN_URL)
    print("→ Log in manually, then open Chase Offers page – script will wait.")
    wait_for_login(drv)
    holder = os.getenv("CHASE_HOLDER_NAME","Primary")
    process(drv, wait, holder)
    print("✔ All offers processed – browser left open. Ctrl+C to exit.")
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Session ended %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
print("main loaded")

if __name__ == "__main__": main()

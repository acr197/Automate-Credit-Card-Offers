import os
import time
import random
from datetime import datetime
from typing import List

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

import gspread
from google.oauth2.service_account import Credentials

# --- Google Sheets helpers -------------------------------------------------

def get_sheet(sa_path: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    client = gspread.authorize(creds)
    # By default, open or create a spreadsheet named "Amex Offers"
    try:
        sh = client.open("Amex Offers")
    except gspread.SpreadsheetNotFound:
        sh = client.create("Amex Offers")
    worksheet = sh.sheet1
    return worksheet


def append_offer(worksheet, holder: str, offer: str):
    worksheet.append_row(
        [datetime.utcnow().isoformat(), holder, offer],
        value_input_option="USER_ENTERED",
    )
    # Reapply filter so new rows are included
    worksheet.set_basic_filter()


# --- Selenium helpers ------------------------------------------------------

LOGIN_URL = "https://www.americanexpress.com/en-us/account/login"
OFFERS_URL = "https://global.americanexpress.com/offers/enroll"


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--disable-notifications")
    # Comment out headless to watch the browser
    # options.add_argument("--headless")
    return webdriver.Chrome(options=options)


def login(driver: webdriver.Chrome, username: str, password: str):
    driver.get(LOGIN_URL)

    WebDriverWait(driver, 120).until(
        EC.presence_of_element_located((By.ID, "eliloUserID"))
    )
    user_el = driver.find_element(By.ID, "eliloUserID")
    pass_el = driver.find_element(By.ID, "eliloPassword")

    user_el.send_keys(username)
    pass_el.send_keys(password)

    driver.find_element(By.ID, "loginSubmit").click()

    # Wait until the home page is loaded or manual verification is completed
    try:
        WebDriverWait(driver, 600).until(
            EC.url_contains("americanexpress.com")
        )
    except TimeoutException:
        print("Timeout waiting for login to complete.")


# Expand all available offers if a "View More" button is present

def expand_offers(driver: webdriver.Chrome):
    while True:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, 'button[aria-label="View More"]')
            if not btn.is_displayed():
                break
            btn.click()
            time.sleep(1)
        except WebDriverException:
            break


def collect_buttons(driver: webdriver.Chrome) -> List:
    script = (
        "return [...document.querySelectorAll('button[title=\"Add to Card\"] span')]."
        "filter(s=>s.textContent.trim()==='Add to Card').map(s=>s.closest('button'));"
    )
    return driver.execute_script(script)


def add_offers(driver: webdriver.Chrome, worksheet, holder: str):
    while True:
        try:
            buttons = collect_buttons(driver)
            if not buttons:
                break
            btn = buttons[0]
            offer_name = "Unknown Offer"
            try:
                parent = btn.find_element(By.XPATH, "ancestor::*[contains(@class,'offer')]")
                offer_name = parent.text.split("\n")[0]
            except WebDriverException:
                pass
            btn.click()
            append_offer(worksheet, holder, offer_name)
            time.sleep(random.uniform(0.3, 1.8))
        except WebDriverException:
            print("Browser closed or navigation issue encountered.")
            break


def logout(driver: webdriver.Chrome):
    try:
        driver.get("https://www.americanexpress.com/logout")
        WebDriverWait(driver, 30).until(EC.url_contains("/login"))
    except WebDriverException:
        pass


# --- Main workflow ---------------------------------------------------------

def run_account(username: str, password: str, holder: str, worksheet):
    driver = build_driver()
    try:
        login(driver, username, password)
        driver.get(OFFERS_URL)
        expand_offers(driver)
        add_offers(driver, worksheet, holder)
        logout(driver)
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


def run_manual(holder: str, worksheet):
    driver = build_driver()
    try:
        driver.get(OFFERS_URL)
        input(
            "\nLog in to American Express and navigate to the offers page.\n"
            "When you are ready to add offers, press Enter here to continue..."
        )
        expand_offers(driver)
        add_offers(driver, worksheet, holder)
        logout(driver)
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


def main():
    load_dotenv()
    sa_path = os.getenv("GOOGLE_SA_PATH")
    worksheet = get_sheet(sa_path)

    holders = [os.getenv("AMEX_HOLDER_1"), os.getenv("AMEX_HOLDER_2")]

    for holder in holders:
        if not holder:
            continue
        run_manual(holder, worksheet)


if __name__ == "__main__":
    main()

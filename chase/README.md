# 🏦 Chase Offers Automation

This project started with a very relatable problem:  
manually clicking through Credit Card Offers takes forever, and my spreadsheet brain demanded better.  

So I built a Python + Selenium + Google Sheets automation that:  
- Logs into Chase (or Amex) through a real Chrome session  
- Scrolls, clicks, and parses every available offer  
- Cleans and dedupes the data  
- Streams results into Google Sheets in near-real time  

---

## ✨ Why this matters
On the surface, it’s about free coffee discounts. But under the hood, this is:  
- **Data Enablement** → raw web UI clicks transformed into structured rows and normalized fields  
- **Automation** → 100+ tedious actions done in seconds with zero manual effort  
- **AI-assisted development** → the code was co-written with ChatGPT to accelerate debugging, improve parsing logic, and enforce resilient error handling  
- **Business translation** → imagine replacing “Chase Offers” with any repetitive data capture or QA workflow in marketing, analytics, or ops. The same pattern applies:  
  - automate data ingestion  
  - normalize messy inputs  
  - reduce errors  
  - free up humans for higher-value work  

---

## ⚙️ Tech stack
- **Python** (Selenium, gspread, dotenv, etc.)  
- **Google Sheets API** for storage & reporting  
- **Environment-based secrets** (no passwords or keys in the repo — see `.env.example`)  
- **LLM-assisted coding** for parsing rules, edge-case handling, and workflow design  

---

## 📈 Business takeaway
What looks like “clicking coupons faster” is actually a demo of **scalable data enablement**: turning inconsistent, human-only processes into reproducible, automated, auditable pipelines.  

That’s exactly the kind of transformation I focus on in marketing/data/AI enablement roles — except with higher stakes than free tacos.  

---

## 🚀 Try it yourself
1. Clone the repo  
2. Copy `.env.example` → `.env` and fill in your creds (never commit the real file)  
3. Run the script with Python + Chrome installed  
4. Watch your Google Sheet fill with offers 🎉  

---

## ⚠️ Disclaimer
This was built for **personal educational purposes**. Automating bank logins is not endorsed by Chase/Amex/Citi, and you run it at your own risk.  

---

👋 Built by [Andrew Ryan](https://github.com/acr197) — data/AI enablement + automation enthusiast, spreadsheet whisperer, and occasional discount hunter.

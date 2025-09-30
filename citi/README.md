# Citi-Offers (Manual Offer Adder)

## Overview
This script helps you enroll in Citi card offers and log them to a Google Sheet. You manually log in and navigate to the offers page, and the script clicks each offer and records it.

## Tech Used
- ChatGPT (AI-assisted development)
- Python
- Selenium WebDriver
- Google Sheets API (required for offer tracking)

## How It Works
The script:
- Launches a browser (Chrome/Brave) using secure user-profile storage
- Waits for you to log in and open the offers page
- Scans and clicks all eligible offers, adapting to layout changes
- Logs enrolled offers to a connected Google Sheet to track which cards received which offers
- Includes basic error handling and fallback logic if pages fail to load or accounts have no offers

## Why I Built It
This task used to require manually checking and activating offers across multiple cards. I used an LLM to walk through each coding step, building a tool that now completes the task in seconds. It’s a personal automation with lessons that scale to workplace use cases.

## Business Value & Use Cases
While built for a personal task, this project illustrates:
- How LLMs can turn repetitive browser-based tasks into scalable scripts
- How non-developers can automate legacy workflows using AI
- The value of structured logging (via Sheets) for clear audit trails
- A blueprint for secure, compliant browser automation that respects login flows and adapts to change

Applicable for orgs looking to reduce low-value manual labor in any browser UI—especially where full API access is unavailable or overly complex.

## Notes
- Every line of code was AI-assisted, demonstrating real-world prompt engineering
- Login uses your browser profile instead of storing raw credentials in code
- Script tracks real-time outcomes, not just automation events
  

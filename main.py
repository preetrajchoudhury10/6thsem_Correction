import asyncio
import time
import aiohttp
import pytz
import urllib.parse
import os
from datetime import datetime
from playwright.async_api import async_playwright

# --- CONFIGURATION START ---

# [!] HARDCODED REGISTRATION NUMBER
TARGET_REG_NO = "22156148040"

# [!] SECURE VARIABLES (Loaded from Railway)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

EXAM_CONFIG = {
    "ordinal_sem": "6th",      
    "roman_sem": "VI",         
    "session": "2025",         
    "held_month": "November",  
    "held_year": "2025"
}

# [!] SUBJECT CONFIGURATION
TARGET_SUBJECT = {
    "code": "156606P",       
    "expected_marks": "68"   
}

# TIMING SETTINGS
CHECK_INTERVAL = 600       # Check result every 1 minute
POLL_INTERVAL = 2         # Check for /ping every 2 seconds
NOTIFY_INTERVAL = 3600   # "Still Pending" msg every 6 hours (1 * 60 * 60 = 21600)

# --- CONFIGURATION END ---

class ResultRepairMonitor:
    def __init__(self):
        self.ist_timezone = pytz.timezone('Asia/Kolkata')
        self.last_notify_time = time.time()
        self.last_update_id = 0
        self.stop_signal = False

    def get_indian_time(self) -> str:
        utc_now = datetime.now(pytz.utc)
        ist_now = utc_now.astimezone(self.ist_timezone)
        return ist_now.strftime("%d-%m-%Y %I:%M:%S %p IST")

    def construct_url(self):
        name_param = f"B.Tech. {EXAM_CONFIG['ordinal_sem']} Semester Examination, {EXAM_CONFIG['session']}"
        held_param = f"{EXAM_CONFIG['held_month']}/{EXAM_CONFIG['held_year']}"
        params = {
            'name': name_param,
            'semester': EXAM_CONFIG['roman_sem'],
            'session': EXAM_CONFIG['session'],
            'regNo': TARGET_REG_NO,
            'exam_held': held_param
        }
        return f"https://beu-bih.ac.in/result-three?{urllib.parse.urlencode(params)}"

    async def send_telegram_message(self, text: str):
        if not BOT_TOKEN or not CHAT_ID: return
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    return resp.status == 200
        except Exception as e:
            print(f"Telegram Error: {e}")

    async def send_telegram_photo(self, photo_bytes, caption):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field('chat_id', CHAT_ID)
        data.add_field('photo', photo_bytes, filename="result.png")
        data.add_field('caption', caption, parse_mode="HTML")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data) as resp:
                    return resp.status == 200
        except Exception as e:
            print(f"Photo Upload Error: {e}")

    # --- CORE SCRAPER FUNCTION ---
    async def get_page_data_and_screenshot(self, url):
        """Helper function to cleanly fetch text and screenshot."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=60000)
                await page.wait_for_selector(f"text={TARGET_REG_NO}", timeout=20000)
                
                text_content = await page.inner_text("body")
                screenshot = await page.screenshot(full_page=True)
                
                await browser.close()
                return text_content, screenshot
            except Exception as e:
                print(f"[!] Playwright Error: {e}")
                await browser.close()
                return None, None

    # --- LISTENER TASK ---
    async def listen_for_commands(self):
        """Constantly checks Telegram for /ping"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        print("[*] Bot Listener Started...")
        
        async with aiohttp.ClientSession() as session:
            while not self.stop_signal:
                try:
                    params = {"offset": self.last_update_id + 1, "timeout": 10}
                    async with session.get(url, params=params) as resp:
                        data = await resp.json()
                        
                        if data.get("ok") and data.get("result"):
                            for update in data["result"]:
                                self.last_update_id = update["update_id"]
                                message = update.get("message", {})
                                text = message.get("text", "").strip()
                                
                                # Handle /ping
                                if text == "/ping":
                                    timestamp = self.get_indian_time()
                                    await self.send_telegram_message("🏓 <b>Pong!</b>\nFetching current result screenshot. Please wait...")
                                    print(f"[cmd] /ping received at {timestamp}")
                                    
                                    # Fetch current screenshot directly
                                    target_url = self.construct_url()
                                    _, screenshot = await self.get_page_data_and_screenshot(target_url)
                                    
                                    if screenshot:
                                        caption = f"🟢 <b>Monitor is Active</b>\nTarget: {TARGET_REG_NO}\nTime: {timestamp}"
                                        await self.send_telegram_photo(screenshot, caption)
                                    else:
                                        await self.send_telegram_message(f"⚠️ <b>Error:</b> Monitor is active, but failed to load the website to take a screenshot at {timestamp}.")

                except Exception as e:
                    print(f"[!] Polling Error: {e}")
                    await asyncio.sleep(5) 
                
                await asyncio.sleep(POLL_INTERVAL)

    # --- MONITOR TASK ---
    async def check_for_correction(self):
        url = self.construct_url()
        timestamp = self.get_indian_time()
        print(f"[*] Checking Result at {timestamp}...")

        text_content, screenshot = await self.get_page_data_and_screenshot(url)
        
        if not text_content:
            return ("ERROR", None)

        target_code = TARGET_SUBJECT['code']
        target_marks = TARGET_SUBJECT['expected_marks']

        if "PASS" in text_content and "FAIL" not in text_content:
            return ("FIXED", screenshot)

        if target_marks in text_content and target_code in text_content:
            return ("FIXED", screenshot)

        if "FAIL" in text_content:
            return ("STILL_BROKEN", None)
        
        return ("UNCERTAIN", None)

    async def monitor_loop(self):
        print(f"[*] Monitor Started for {TARGET_REG_NO}")
        self.last_notify_time = time.time()

        while not self.stop_signal:
            status, evidence = await self.check_for_correction()
            timestamp = self.get_indian_time()
            current_time = time.time()

            if status == "FIXED":
                msg = (
                    f"✅ <b>RESULT UPDATED!</b>\n\n"
                    f"Time: {timestamp}\n"
                    f"Status: PASS detected or Marks '{TARGET_SUBJECT['expected_marks']}' found!"
                )
                if evidence:
                    await self.send_telegram_photo(evidence, msg)
                else:
                    await self.send_telegram_message(msg)
                
                print("Correction found! Stopping.")
                self.stop_signal = True 
                return

            # THE 6-HOUR FIX: 
            # Guaranteed heartbeat regardless of website status (Error, Uncertain, or Broken)
            if current_time - self.last_notify_time > NOTIFY_INTERVAL:
                await self.send_telegram_message(
                    f"ℹ️ <b>Status Report:</b>\nMonitor is running cleanly. Result not yet updated.\nLast check status: {status}\n{timestamp}"
                )
                self.last_notify_time = current_time 

            await asyncio.sleep(CHECK_INTERVAL)

    async def run(self):
        if "ENTER_YOUR" in TARGET_REG_NO:
            print("❌ ERROR: Set TARGET_REG_NO in main.py!")
            return

        await self.send_telegram_message(f"🕵️ <b>Monitor Started (Cloud)</b>\nTarget: {TARGET_REG_NO}\nChecking every 1 min.\nStatus update every 6 hours.\n<i>Send /ping to check status and get a screenshot manually.</i>")
        
        await asyncio.gather(
            self.monitor_loop(),
            self.listen_for_commands()
        )

if __name__ == "__main__":
    asyncio.run(ResultRepairMonitor().run())

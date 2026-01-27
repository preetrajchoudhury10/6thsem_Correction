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
CHECK_INTERVAL = 60       # Check result every 1 minute
POLL_INTERVAL = 2         # Check for /ping every 2 seconds
NOTIFY_INTERVAL = 18000   # "Still Pending" msg every 5 hours

# --- CONFIGURATION END ---

class ResultRepairMonitor:
    def __init__(self):
        self.ist_timezone = pytz.timezone('Asia/Kolkata')
        self.last_notify_time = 0
        self.last_update_id = 0
        self.stop_signal = False # Used to stop the listener when result is found

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
        data.add_field('photo', photo_bytes, filename="fixed_result.png")
        data.add_field('caption', caption)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data) as resp:
                    return resp.status == 200
        except Exception as e:
            print(f"Photo Upload Error: {e}")

    # --- NEW: LISTENER TASK ---
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
                                    await self.send_telegram_message(f"🏓 <b>Pong!</b>\n\nMonitor is Active 🟢\nTarget: {TARGET_REG_NO}\nTime: {timestamp}")
                                    print(f"[cmd] /ping received at {timestamp}")

                except Exception as e:
                    print(f"[!] Polling Error: {e}")
                    await asyncio.sleep(5) # Backoff on error
                
                await asyncio.sleep(POLL_INTERVAL)

    # --- MONITOR TASK (Unchanged Logic) ---
    async def monitor_loop(self):
        print(f"[*] Monitor Started for {TARGET_REG_NO}")
        
        while not self.stop_signal:
            # 1. Check the result
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
                self.stop_signal = True # Stops the listener too
                return

            elif status == "STILL_BROKEN":
                # 5-hour notification
                if current_time - self.last_notify_time > NOTIFY_INTERVAL:
                    if self.last_notify_time == 0:
                        self.last_notify_time = current_time
                    else:
                        await self.send_telegram_message(f"ℹ️ <b>Status Report:</b>\nStill showing FAIL/NA.\n{timestamp}")
                        self.last_notify_time = current_time
                if self.last_notify_time == 0:
                     self.last_notify_time = current_time

            elif status == "ERROR":
                print(f"    [!] Error checking result at {timestamp}")

            # Sleep 60 seconds
            await asyncio.sleep(CHECK_INTERVAL)

    # --- CORE CHECK LOGIC ---
    async def check_for_correction(self):
        url = self.construct_url()
        timestamp = self.get_indian_time()
        print(f"[*] Checking Result at {timestamp}...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=60000)
                try:
                    await page.wait_for_selector(f"text={TARGET_REG_NO}", timeout=20000)
                except:
                    await browser.close()
                    return ("ERROR", None)

                text_content = await page.inner_text("body")
                target_code = TARGET_SUBJECT['code']
                target_marks = TARGET_SUBJECT['expected_marks']

                if "PASS" in text_content and "FAIL" not in text_content:
                    screenshot = await page.screenshot(full_page=True)
                    await browser.close()
                    return ("FIXED", screenshot)

                if target_marks in text_content and target_code in text_content:
                    screenshot = await page.screenshot(full_page=True)
                    await browser.close()
                    return ("FIXED", screenshot)

                if "FAIL" in text_content:
                    await browser.close()
                    return ("STILL_BROKEN", None)
                
                await browser.close()
                return ("UNCERTAIN", None)
            except:
                await browser.close()
                return ("ERROR", None)

    async def run(self):
        if "ENTER_YOUR" in TARGET_REG_NO:
            print("❌ ERROR: Set TARGET_REG_NO in main.py!")
            return

        await self.send_telegram_message(f"🕵️ <b>Monitor Started (Cloud)</b>\nTarget: {TARGET_REG_NO}\n<i>Send /ping to check status.</i>")
        
        # Run both tasks simultaneously
        await asyncio.gather(
            self.monitor_loop(),
            self.listen_for_commands()
        )

if __name__ == "__main__":
    asyncio.run(ResultRepairMonitor().run())

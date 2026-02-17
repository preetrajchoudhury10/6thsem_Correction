import asyncio
import time
import aiohttp
import pytz
import urllib.parse
import os
from datetime import datetime
from playwright.async_api import async_playwright

# --- CONFIGURATION START ---

TARGET_REG_NO = "22156148040"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

EXAM_CONFIG = {
    "ordinal_sem": "6th",      
    "roman_sem": "VI",         
    "session": "2025",         
    "held_month": "November",  
    "held_year": "2025"
}

TARGET_SUBJECT = {
    "code": "156606P",       
    "expected_marks": "68"   
}

CHECK_INTERVAL = 600      # Check result every 10 minutes (Highly recommended for cloud)
POLL_INTERVAL = 2         # Check for /ping every 2 seconds
NOTIFY_INTERVAL = 3600    # 1 Hour Status Report

# --- CONFIGURATION END ---

class ResultRepairMonitor:
    def __init__(self):
        self.ist_timezone = pytz.timezone('Asia/Kolkata')
        self.last_notify_time = time.time()
        self.last_update_id = 0
        self.stop_signal = False
        # THE FIX: This lock prevents two browsers from opening at the same time and crashing RAM
        self.browser_lock = asyncio.Lock()

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
                await session.post(url, json=payload)
        except Exception as e:
            print(f"Telegram Message Error: {e}")

    async def send_telegram_photo(self, photo_bytes, caption):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field('chat_id', CHAT_ID)
        # Explicit content_type helps Telegram process the image
        data.add_field('photo', photo_bytes, filename="result.png", content_type="image/png")
        data.add_field('caption', caption, parse_mode="HTML")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        print(f"Telegram Rejected Image: {err}")
                        return False
                    return True
        except Exception as e:
            print(f"Photo Upload Error: {e}")
            return False

    async def get_page_data_and_screenshot(self, url):
        # Apply the lock so /ping and background checks patiently wait for each other
        async with self.browser_lock:
            try:
                async with async_playwright() as p:
                    # THE FIX: Added args to prevent Docker/Railway memory crashes
                    browser = await p.chromium.launch(
                        headless=True,
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                    page = await browser.new_page()
                    await page.goto(url, timeout=45000)
                    
                    # Wait briefly for the Reg No. If the site is slow, just snap a picture of whatever is there.
                    try:
                        await page.wait_for_selector(f"text={TARGET_REG_NO}", timeout=10000)
                    except:
                        pass
                    
                    text_content = await page.inner_text("body")
                    screenshot = await page.screenshot(full_page=True)
                    
                    await browser.close()
                    return text_content, screenshot
            except Exception as e:
                print(f"[!] Playwright Error: {e}")
                return None, None

    async def listen_for_commands(self):
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
                                
                                if text == "/ping":
                                    timestamp = self.get_indian_time()
                                    await self.send_telegram_message("🏓 <b>Pong!</b>\nFetching current result screenshot. Please wait...")
                                    
                                    target_url = self.construct_url()
                                    _, screenshot = await self.get_page_data_and_screenshot(target_url)
                                    
                                    if screenshot:
                                        caption = f"🟢 <b>Monitor is Active</b>\nTarget: {TARGET_REG_NO}\nTime: {timestamp}"
                                        success = await self.send_telegram_photo(screenshot, caption)
                                        if not success:
                                            await self.send_telegram_message("⚠️ <b>Error:</b> The screenshot was taken, but Telegram rejected the file. Check the Railway console logs.")
                                    else:
                                        await self.send_telegram_message(f"⚠️ <b>Error:</b> Monitor is active, but failed to load the website to take a screenshot at {timestamp}.")

                except Exception as e:
                    print(f"[!] Polling Error: {e}")
                    await asyncio.sleep(5) 
                
                await asyncio.sleep(POLL_INTERVAL)

    async def check_for_correction(self):
        url = self.construct_url()
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
                    success = await self.send_telegram_photo(evidence, msg)
                    if not success: await self.send_telegram_message(msg)
                else:
                    await self.send_telegram_message(msg)
                
                print("Correction found! Stopping.")
                self.stop_signal = True 
                return

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

        await self.send_telegram_message(f"🕵️ <b>Monitor Started (Cloud Optimized)</b>\nTarget: {TARGET_REG_NO}\nChecking every 10 mins.\nStatus update every 1 hour.\n<i>Send /ping to check status and get a screenshot manually.</i>")
        
        await asyncio.gather(
            self.monitor_loop(),
            self.listen_for_commands()
        )

if __name__ == "__main__":
    asyncio.run(ResultRepairMonitor().run())

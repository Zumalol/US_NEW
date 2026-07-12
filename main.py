import os
import re
import time
import html
import hashlib
import traceback
from datetime import datetime, timezone, timedelta  # เพิ่ม timedelta สำหรับคำนวณเวลา 1 วัน
from threading import Thread, Lock
from urllib.parse import quote_plus

import requests
import feedparser
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MAX_NEWS_PER_CYCLE = int(os.getenv("MAX_NEWS_PER_CYCLE", "5"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.1-8b-instant"
)

# =========================================================
# DIRECT PREMIUM FINANCIAL FEEDS (ตัด Google News ออกทั้งหมด)
# =========================================================

# คัดสรรเฉพาะสำนักข่าวการเงินชั้นนำระดับโลก ที่ให้ลิงก์ตรงและเปิดให้อ่านเนื้อหาหลักได้จริง
RSS_FEED_URLS = [
    "https://www.fxstreet.com/rss/news",                          # FXStreet (วิเคราะห์เชิงลึกทองคำ XAUUSD และตลาด Forex)
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",    # MarketWatch (ข่าวสารตลาดทุนสหรัฐฯ และนโยบายเศรษฐกิจ)
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",        # CNBC Economy (ข่าวด้านธนาคารกลาง Fed, ดอกเบี้ย และเงินเฟ้อ)
    "https://www.cnbc.com/id/15839069/device/rss/rss.html",        # CNBC Investing (ข่าววิเคราะห์ทิศทางทองคำ และสินค้าโภคภัณฑ์)
    "https://finance.yahoo.com/news/rssindex"                      # Yahoo Finance (สรุปภาพรวมข่าวเด่นเศรษฐกิจมหภาค)
]

# =========================================================
# KEYWORDS
# =========================================================

TARGET_KEYWORDS = [
    "gold",
    "xau",
    "xauusd",
    "bullion",

    "trump",
    "donald trump",

    "fed",
    "fomc",
    "federal reserve",
    "powell",
    "jerome powell",

    "interest rate",
    "rate cut",
    "rate hike",
    "monetary policy",

    "inflation",
    "cpi",
    "pce",
    "nfp",
    "nonfarm",
    "jobs report",
    "unemployment",

    "dollar",
    "usd",
    "treasury",
    "bond yield",

    "tariff",
    "tariffs",
    "trade war",
]

# =========================================================
# GLOBAL STATE
# =========================================================

app = Flask(__name__)

seen_news = set()
seen_lock = Lock()

bot_start_lock = Lock()

bot_started = False
bot_running = False

last_check_time = None
last_check_finished = None
last_news_count = 0
last_relevant_count = 0
last_error = None
last_status = "waiting"

total_cycles = 0
total_sent = 0

# =========================================================
# TIME
# =========================================================

def now_text():
    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

# =========================================================
# ROUTES
# =========================================================

@app.route("/")
def home():
    return """
    <h1>🤖 Verified Premium Financial News Bot</h1>
    <p>Status: Active (Filtering 100% Direct Premium Feeds | Past 24 Hours Only)</p>
    <ul>
        <li><a href="/health">/health</a></li>
        <li><a href="/test-news">/test-news</a></li>
        <li><a href="/run-now">/run-now</a></li>
    </ul>
    """

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_started": bot_started,
        "bot_running": bot_running,
        "last_status": last_status,
        "last_check_time": last_check_time,
        "last_check_finished": last_check_finished,
        "last_news_count": last_news_count,
        "last_relevant_count": last_relevant_count,
        "seen_news": len(seen_news),
        "total_cycles": total_cycles,
        "total_sent": total_sent,
        "rss_feeds": len(RSS_FEED_URLS),
        "check_interval": CHECK_INTERVAL,
        "last_error": last_error,
    })

# =========================================================
# CLEAN TEXT
# =========================================================

def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()

# =========================================================
# NEWS ID
# =========================================================

def create_news_id(title):
    normalized = re.sub(r"\s+", " ", title.lower().strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

# =========================================================
# FILTER
# =========================================================

def get_matched_keywords(title, description):
    text = f"{title} {description}".lower()
    matches = []
    for keyword in TARGET_KEYWORDS:
        if keyword.lower() in text:
            matches.append(keyword)
    return matches

# =========================================================
# FETCH ONE FEED (เน้นความเสถียรและดึงตรง)
# =========================================================

def fetch_one_feed(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    # METHOD 1: ดึงตรงจากสำนักข่าวหลัก (รวดเร็วและได้ลิงก์ดั้งเดิมแน่นอน)
    try:
        print("🔄 [Direct Fetch] กำลังดึงข้อมูลตรงจากสำนักข่าวหลัก...")
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                print(f"✅ สำเร็จ (Direct): พบ {len(entries)} ข่าว")
                return entries
        else:
            print(f"⚠️ Direct Fetch ได้รับ HTTP Status: {response.status_code}")
    except Exception as e:
        print(f"⚠️ วิธีดึงตรงติดขัด: {e}")

    # METHOD 2: ดึงผ่าน Proxy Fallback (เผื่อกรณี IP ของ Hosting โดนเซิร์ฟเวอร์ปลายทางตรวจสอบ)
    try:
        print("📡 [Proxy Fallback] ลองดึงผ่านเครือข่ายสำรองเพื่อความชัวร์...")
        encoded_url = quote_plus(url)
        proxy_url = f"https://api.allorigins.win/raw?url={encoded_url}"
        response = requests.get(proxy_url, headers=headers, timeout=20)
        
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                print(f"✅ สำเร็จ (Proxy): พบ {len(entries)} ข่าว")
                return entries
    except Exception as e:
        print(f"⚠️ Proxy Fallback ผิดพลาด: {e}")

    return []

# =========================================================
# FETCH ALL NEWS (เพิ่มระบบกรองเวลา 1 วันล่าสุด)
# =========================================================

def fetch_latest_news():
    global last_check_time
    global last_check_finished
    global last_news_count
    global last_error
    global last_status

    last_check_time = now_text()
    last_status = "fetching"
    last_error = None

    print("\n" + "=" * 70)
    print(f"🌍 START FETCH: {last_check_time}")
    print("=" * 70)

    all_entries = []

    for index, url in enumerate(RSS_FEED_URLS, start=1):
        print(f"\n🌐 FEED {index}/{len(RSS_FEED_URLS)}: {url}")

        try:
            entries = fetch_one_feed(url)
            all_entries.extend(entries)
        except Exception as e:
            print(f"❌ FEED ERROR: {e}")

        time.sleep(0.5)

    # เตรียมตัวแปรสำหรับคัดกรองเวลา (ย้อนหลัง 1 วัน)
    unique_entries = []
    cycle_ids = set()
    
    now_dt = datetime.now(timezone.utc)
    one_day_ago = now_dt - timedelta(days=1)  # เวลาจุดตัดย้อนหลัง 24 ชั่วโมง

    for entry in all_entries:
        title = clean_text(entry.get("title", ""))
        if not title:
            continue

        # --- ⏱️ เริ่มต้นกระบวนการกรองเวลาข่าว 1 วันก่อนหน้าจนถึงปัจจุบัน ---
        pub_parsed = entry.get("published_parsed")
        if pub_parsed:
            try:
                # แปลง struct_time ของ feedparser เป็น UTC datetime object
                pub_dt = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
                
                # หากเวลาของข่าวเก่ากว่า 1 วันที่แล้ว ให้คัดทิ้งข้ามไปทันที
                if pub_dt < one_day_ago:
                    continue
            except Exception as time_err:
                print(f"⚠️ พลาดการอ่านเวลาข่าวชิ้นนี้เนื่องจาก: {time_err}")
                # ปล่อยผ่านกรณีเกิด error ในการ parse เพื่อป้องกันข่าวตกหล่น หรือจะระบุให้ดึงต่อได้ตามต้องการ

        news_id = create_news_id(title)
        if news_id in cycle_ids:
            continue

        cycle_ids.add(news_id)
        unique_entries.append(entry)

    last_news_count = len(unique_entries)
    last_check_finished = now_text()
    last_status = "fetch_success" if unique_entries else "no_news_found"

    print("\n" + "=" * 70)
    print(f"📰 UNIQUE PREMIUM NEWS FOUND (PAST 24H): {len(unique_entries)}")
    print("=" * 70)

    return unique_entries

# =========================================================
# GET SOURCE
# =========================================================

def get_source(entry):
    link = entry.get("link", "").lower()
    if "fxstreet.com" in link:
        return "FXStreet"
    elif "marketwatch.com" in link:
        return "MarketWatch"
    elif "cnbc.com" in link:
        return "CNBC"
    elif "yahoo.com" in link:
        return "Yahoo Finance"
    
    try:
        source = entry.get("source", {})
        if isinstance(source, dict) and source.get("title"):
            return clean_text(source.get("title"))
    except Exception:
        pass
    return "Premium Financial News"

# =========================================================
# GROQ
# =========================================================

def analyze_with_groq(title, description, source):
    if not GROQ_API_KEY:
        return "⚠️ ไม่พบ GROQ_API_KEY"

    prompt = f"""
คุณคือนักวิเคราะห์ตลาดทองคำ XAUUSD และเศรษฐกิจสหรัฐฯ
วิเคราะห์ข่าวต่อไปนี้เป็นภาษาไทย

หัวข้อ:
{title}

รายละเอียด:
{description}

แหล่งข่าว:
{source}

ตอบตามรูปแบบนี้เท่านั้น:

📌 สรุปข่าว:
สรุปสั้น กระชับ

🏦 ผลต่อ Fed / ดอกเบี้ย:
วิเคราะห์ผลกระทบ

💵 ผลต่อ USD:
BULLISH / BEARISH / UNCERTAIN พร้อมเหตุผล

🥇 ผลต่อ GOLD / XAUUSD:
🟢 BULLISH หรือ 🔴 BEARISH หรือ 🟡 UNCERTAIN พร้อมเหตุผล

⚠️ ความสำคัญ:
LOW / MEDIUM / HIGH

ห้ามรับประกันทิศทางราคา
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 700,
    }

    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        return f"⚠️ Groq Error HTTP {response.status_code}"
    except Exception as e:
        print(f"❌ GROQ ERROR: {e}")
        return "⚠️ AI ไม่สามารถวิเคราะห์ได้ในขณะนี้"

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ TELEGRAM_TOKEN or CHAT_ID missing")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(text) > 4000:
        text = text[:3950] + "\n\n..."

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,  # เปิดไว้เพื่อให้แสดงพรีวิวหน้าเว็บของข่าวจริง
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ TELEGRAM ERROR: {e}")
        return False

# =========================================================
# PROCESS NEWS (คัดกรองลิงก์ที่เข้าอ่านได้จริงเท่านั้น)
# =========================================================

def process_news(entries):
    global last_relevant_count
    global total_sent

    relevant_count = 0
    sent_this_cycle = 0

    for entry in entries:
        if sent_this_cycle >= MAX_NEWS_PER_CYCLE:
            break

        title = clean_text(entry.get("title", ""))
        description = clean_text(entry.get("summary", entry.get("description", "")))
        link = entry.get("link", "").strip()
        source = get_source(entry)

        # ตรวจสอบ: ต้องมีหัวข้อข่าว และลิงก์ต้องเริ่มต้นด้วย http เท่านั้น (คัดกรองลิงก์ขยะ/ลิงก์เสีย)
        if not title or not link or not link.startswith("http"):
            continue

        news_id = create_news_id(title)

        with seen_lock:
            if news_id in seen_news:
                continue

        matches = get_matched_keywords(title, description)
        if not matches:
            continue

        relevant_count += 1
        print(f"\n🎯 MATCHED PREMIUM NEWS: {title}")

        analysis = analyze_with_groq(title, description, source)

        safe_title = html.escape(title)
        safe_source = html.escape(source)
        safe_analysis = html.escape(analysis)
        safe_link = html.escape(link, quote=True)

        message = (
            "🚨 <b>GOLD MARKET NEWS</b>\n\n"
            f"📰 <b>{safe_title}</b>\n\n"
            f"🏢 <b>Source:</b> {safe_source}\n\n"
            f"🤖 <b>AI Analysis:</b>\n{safe_analysis}\n\n"
            f"🔗 <a href=\"{safe_link}\">คลิกอ่านข่าวฉบับเต็ม</a>"
        )

        success = send_telegram(message)

        if success:
            with seen_lock:
                seen_news.add(news_id)
            sent_this_cycle += 1
            total_sent += 1
            print("✅ SENT TO TELEGRAM")

        time.sleep(2)

    last_relevant_count = relevant_count
    print(f"\n📊 Cycle Summary -> Matches: {relevant_count} | Sent: {sent_this_cycle}")
    return sent_this_cycle

# =========================================================
# RUN ONE CYCLE
# =========================================================

def run_news_cycle():
    global bot_running
    global last_error
    global last_status
    global total_cycles

    if bot_running:
        print("⚠️ Cycle already running")
        return

    bot_running = True
    last_status = "cycle_started"

    try:
        total_cycles += 1
        print(f"\n🚀 STARTING NEWS CYCLE #{total_cycles}")

        entries = fetch_latest_news()
        if not entries:
            print("❌ ไม่พบข่าวสดใหม่จาก Premium Feeds ในรอบนี้")
            last_status = "no_news_found"
            return

        last_status = "processing"
        process_news(entries)
        last_status = "cycle_complete"

    except Exception as e:
        last_error = str(e)
        last_status = "error"
        print("❌ CYCLE ERROR")
        traceback.print_exc()
    finally:
        bot_running = False

# =========================================================
# BACKGROUND LOOP
# =========================================================

def bot_loop():
    print("\n🚀 BACKGROUND BOT LOOP STARTED")
    time.sleep(3)

    while True:
        try:
            run_news_cycle()
        except Exception:
            traceback.print_exc()

        print(f"😴 WAIT {CHECK_INTERVAL} SECONDS FOR NEXT CYCLE")
        time.sleep(CHECK_INTERVAL)

# =========================================================
# ENDPOINTS
# =========================================================

@app.route("/test-news")
def test_news():
    try:
        entries = fetch_latest_news()
        result = []

        for entry in entries[:20]:
            title = clean_text(entry.get("title", ""))
            description = clean_text(entry.get("summary", entry.get("description", "")))
            matches = get_matched_keywords(title, description)
            link = entry.get("link", "")

            if link.startswith("http"):
                result.append({
                    "title": title,
                    "source": get_source(entry),
                    "link": link,
                    "matched_keywords": matches,
                })

        return jsonify({
            "success": True,
            "count": len(result),
            "news": result,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/run-now")
def run_now():
    if bot_running:
        return jsonify({"success": False, "message": "Bot cycle already running"})

    thread = Thread(target=run_news_cycle, daemon=True)
    thread.start()
    return jsonify({
        "success": True, 
        "message": "Premium news cycle (Past 24 Hours) started manually"
    })

def start_background_bot():
    global bot_started
    with bot_start_lock:
        if bot_started:
            return
        print("🎬 STARTING BACKGROUND BOT")
        thread = Thread(target=bot_loop, daemon=True)
        thread.start()
        bot_started = True

start_background_bot()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"🌐 STARTING FLASK ON PORT {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
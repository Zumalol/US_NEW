import os
import re
import time
import html
import hashlib
import traceback
from datetime import datetime, timezone
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
# SEARCH QUERIES & PREMIUM RELIABLE RSS FEEDS
# =========================================================

SEARCH_QUERIES = [
    "Donald Trump Federal Reserve",
    "Trump Powell Fed",
    "Federal Reserve interest rates",
    "FOMC Powell",
    "Gold Federal Reserve",
    "Gold XAUUSD Fed",
    "US CPI inflation Fed",
    "Trump tariffs gold dollar",
]

RSS_FEED_URLS = []

# 1. แหล่งข่าวเจาะจงรายประเด็นจาก Google News (จะเข้า Proxy อัตโนมัติเมื่อดึงข้อมูลเพื่อแก้ 503)
for query in SEARCH_QUERIES:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}"
        "&hl=en-US"
        "&gl=US"
        "&ceid=US:en"
    )
    RSS_FEED_URLS.append(url)

# 2. เพิ่มคัดสรรแหล่งข่าวการเงิน สถิติเศรษฐกิจ และทองคำระดับโลกที่มีความน่าเชื่อถือสูงโดยตรง
PREMIUM_FEEDS = [
    "https://www.fxstreet.com/rss/news",                          # FXStreet (ข้อมูลทองคำ XAUUSD และการเงินเชิงลึก)
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",    # MarketWatch (ข่าวสารตลาดทุนสหรัฐฯ และนโยบายเศรษฐกิจ)
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",        # CNBC Economy (ข่าวด้านธนาคารกลาง Fed และ Macroeconomics)
    "https://www.cnbc.com/id/15839069/device/rss/rss.html",        # CNBC Investing (ข่าววิเคราะห์ทิศทางทองคำ ตลาดเงิน ตลาดทุน)
    "https://finance.yahoo.com/news/rssindex",                     # Yahoo Finance (สรุปสถานการณ์ความเคลื่อนไหวเศรษฐกิจโลก)
]

RSS_FEED_URLS.extend(PREMIUM_FEEDS)

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
    <h1>🤖 Gold / Trump / Fed News Bot v2</h1>
    <p>Bot Status: Active & Anti-503 Layer Enabled</p>
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
# FETCH ONE FEED (ระบบ Multi-Layer ป้องกัน Error 503)
# =========================================================

def fetch_one_feed(url):
    # ใช้ User-Agent ฝั่งอุปกรณ์เคลื่อนที่เพื่อความแนบเนียนและผ่านการคัดกรองง่ายขึ้น
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    # หลีกเลี่ยงดึงตรงกับ Google News บนคลาวด์เพื่อข้าม 503 ทันที
    is_google_news = "news.google.com" in url

    # METHOD 1: ดึงตรง (ใช้กับ Premium Feeds อย่าง CNBC, FXStreet, WSJ)
    if not is_google_news:
        try:
            print("🔄 [Direct] กำลังดึงข้อมูลตรงจากแหล่งข่าวหลัก...")
            response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            print(f"HTTP {response.status_code} | {len(response.content)} bytes")
            
            if response.status_code in [403, 503]:
                print(f"⚠️ เจอสิทธิ์ระงับ {response.status_code} -> สลับเข้าระบบ Proxy...")
            else:
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                entries = list(getattr(feed, "entries", []))
                if entries:
                    print(f"✅ สำเร็จ (Direct): พบ {len(entries)} ข่าว")
                    return entries
        except Exception as e:
            print(f"⚠️ วิธีดึงตรงติดขัด: {e}")

    # METHOD 2: ดึงผ่าน Proxy หลัก (api.allorigins.win) เพื่อหลบเลี่ยง 503
    try:
        print("📡 [Proxy 1] กำลังดึงผ่านเครือข่าย api.allorigins.win...")
        encoded_url = quote_plus(url)
        proxy_url = f"https://api.allorigins.win/raw?url={encoded_url}"
        response = requests.get(proxy_url, headers=headers, timeout=20)
        
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                print(f"✅ สำเร็จ (Proxy 1): พบ {len(entries)} ข่าว")
                return entries
    except Exception as e:
        print(f"⚠️ Proxy 1 ผิดพลาด: {e}")

    # METHOD 3: ดึงผ่าน Proxy สำรอง (corsproxy.io) 
    try:
        print("📡 [Proxy 2] กำลังดึงผ่านเครือข่ายสำรอง corsproxy.io...")
        proxy_url_2 = f"https://corsproxy.io/?{quote_plus(url)}"
        response = requests.get(proxy_url_2, headers=headers, timeout=20)
        
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                print(f"✅ สำเร็จ (Proxy 2): พบ {len(entries)} ข่าว")
                return entries
    except Exception as e:
        print(f"⚠️ Proxy 2 ผิดพลาด: {e}")

    # METHOD 4: ทางเลือกสุดท้ายดึงด้วย Feedparser Direct
    try:
        print("🔄 [Feedparser] กำลังลองใช้ Feedparser ดึงตรงแบบดั้งเดิม...")
        feed = feedparser.parse(url)
        entries = list(getattr(feed, "entries", []))
        if entries:
            print(f"✅ สำเร็จ (Feedparser Direct): พบ {len(entries)} ข่าว")
            return entries
    except Exception as e:
        print(f"⚠️ Feedparser Direct ผิดพลาด: {e}")

    return []

# =========================================================
# FETCH ALL NEWS
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

    print("\n")
    print("=" * 70)
    print(f"🌍 START FETCH: {last_check_time}")
    print("=" * 70)

    all_entries = []

    for index, url in enumerate(RSS_FEED_URLS, start=1):
        print()
        print(f"🌐 FEED {index}/{len(RSS_FEED_URLS)}")
        print(url[:120] + "..." if len(url) > 120 else url)

        try:
            entries = fetch_one_feed(url)
            all_entries.extend(entries)
        except Exception as e:
            print(f"❌ FEED ERROR: {e}")

        time.sleep(0.4)

    # =====================================================
    # REMOVE DUPLICATES
    # =====================================================

    unique_entries = []
    cycle_ids = set()

    for entry in all_entries:
        title = clean_text(entry.get("title", ""))
        if not title:
            continue

        news_id = create_news_id(title)
        if news_id in cycle_ids:
            continue

        cycle_ids.add(news_id)
        unique_entries.append(entry)

    last_news_count = len(unique_entries)
    last_check_finished = now_text()

    if unique_entries:
        last_status = "fetch_success"
    else:
        last_status = "no_news_found"

    print()
    print("=" * 70)
    print(f"📦 RAW NEWS: {len(all_entries)}")
    print(f"📰 UNIQUE NEWS: {len(unique_entries)}")
    print("=" * 70)

    return unique_entries

# =========================================================
# GET SOURCE
# =========================================================

def get_source(entry):
    try:
        source = entry.get("source", {})
        if isinstance(source, dict):
            title = source.get("title")
            if title:
                return clean_text(title)
    except Exception:
        pass
    
    # พยายามตรวจสอบ Domain จาก Link หากไม่มีข้อมูล Source แนบมาตรงๆ
    link = entry.get("link", "")
    if "fxstreet.com" in link:
        return "FXStreet"
    elif "marketwatch.com" in link:
        return "MarketWatch"
    elif "cnbc.com" in link:
        return "CNBC"
    elif "yahoo.com" in link:
        return "Yahoo Finance"

    return "Financial News Source"

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
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }

    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
        print(f"🤖 GROQ HTTP {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

        print(response.text)
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
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        print(f"📨 TELEGRAM HTTP {response.status_code}")
        if response.status_code != 200:
            print(response.text)
            return False
        return True
    except Exception as e:
        print(f"❌ TELEGRAM ERROR: {e}")
        return False

# =========================================================
# PROCESS NEWS
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
        link = entry.get("link", "")
        source = get_source(entry)

        if not title:
            continue

        news_id = create_news_id(title)

        with seen_lock:
            if news_id in seen_news:
                continue

        matches = get_matched_keywords(title, description)
        if not matches:
            continue

        relevant_count += 1

        print()
        print(f"🎯 RELEVANT: {title}")
        print(f"🔑 MATCH: {', '.join(matches[:8])}")

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
        )

        if link:
            message += f"🔗 <a href=\"{safe_link}\">อ่านข่าว</a>"

        success = send_telegram(message)

        if success:
            with seen_lock:
                seen_news.add(news_id)
            sent_this_cycle += 1
            total_sent += 1
            print("✅ SENT")

        time.sleep(2)

    last_relevant_count = relevant_count
    print(f"🎯 RELEVANT NEWS: {relevant_count}")
    print(f"📨 SENT THIS CYCLE: {sent_this_cycle}")
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
        print()
        print(f"🚀 NEWS CYCLE #{total_cycles}")

        entries = fetch_latest_news()
        if not entries:
            print("❌ ไม่พบข่าวจากทุก Feed")
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
    print()
    print("🚀 BACKGROUND BOT LOOP STARTED")
    time.sleep(3)

    while True:
        try:
            run_news_cycle()
        except Exception:
            traceback.print_exc()

        print(f"😴 WAIT {CHECK_INTERVAL} SECONDS")
        time.sleep(CHECK_INTERVAL)

# =========================================================
# TEST NEWS ENDPOINT
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

            result.append({
                "title": title,
                "source": get_source(entry),
                "matched_keywords": matches,
            })

        return jsonify({
            "success": True,
            "count": len(entries),
            "news": result,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

# =========================================================
# RUN NOW ENDPOINT
# =========================================================

@app.route("/run-now")
def run_now():
    if bot_running:
        return jsonify({
            "success": False,
            "message": "Bot cycle already running",
        })

    thread = Thread(target=run_news_cycle, daemon=True)
    thread.start()
    return jsonify({
        "success": True,
        "message": "News cycle started",
    })

# =========================================================
# START BACKGROUND BOT
# =========================================================

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

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"🌐 STARTING FLASK ON PORT {port}")
    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
        use_reloader=False,
    )
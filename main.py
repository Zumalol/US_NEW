import os
import re
import time
import html
import hashlib
import traceback
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
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

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # ระยะเวลารีเฟรชข่าวอัตโนมัติ (วินาที)
MAX_NEWS_PER_CYCLE = int(os.getenv("MAX_NEWS_PER_CYCLE", "5"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.1-8b-instant"
)

# =========================================================
# DIRECT PREMIUM FINANCIAL FEEDS
# =========================================================

RSS_FEED_URLS = [
    "https://www.fxstreet.com/rss/news",                          # FXStreet
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",    # MarketWatch
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",        # CNBC Economy
    "https://www.cnbc.com/id/15839069/device/rss/rss.html",        # CNBC Investing
    "https://finance.yahoo.com/news/rssindex"                      # Yahoo Finance
]

# =========================================================
# KEYWORDS (เน้นข่าวเศรษฐกิจระดับสูงและภูมิรัฐศาสตร์)
# =========================================================

TARGET_KEYWORDS = [
    "gold", "xau", "xauusd", "bullion",
    "trump", "donald trump",
    "fed", "fomc", "federal reserve", "powell", "jerome powell",
    "interest rate", "rate cut", "rate hike", "monetary policy",
    "inflation", "cpi", "pce", "nfp", "nonfarm", "jobs report", "unemployment",
    "dollar", "usd", "treasury", "bond yield",
    "tariff", "tariffs", "trade war",
    # 🚨 หมวดสงครามและภูมิรัฐศาสตร์
    "iran", "iranian", "tehran", "war", "military", "missile", "strike", 
    "middle east", "escalation", "retaliation", "attack", "geopolitical"
]

# =========================================================
# GLOBAL STATE & LOCKS
# =========================================================

app = Flask(__name__)

seen_news = set()          # เก็บรหัสข่าวสารที่ส่งไปแล้วเพื่อไม่ให้ส่งซ้ำ
seen_lock = Lock()
current_bot_date = None    # บันทึกวันที่ปัจจุบันของบอทเพื่อเคลียร์หน่วยความจำ

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

# กำหนดเวลาประเทศไทย (GMT+7)
TZ_THAILAND = timezone(timedelta(hours=7))

def now_text():
    return datetime.now(TZ_THAILAND).strftime("%Y-%m-%d %H:%M:%S GMT+7")

# =========================================================
# ROUTES
# =========================================================

@app.route("/")
def home():
    return """
    <h1>🤖 High-Impact Financial & Geopolitical News Bot</h1>
    <p>Status: Active (Strict Today Only | High-Impact Filters Enabled)</p>
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
# UTILITIES & DATE PARSER
# =========================================================

def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()

def create_news_id(title):
    normalized = re.sub(r"\s+", " ", title.lower().strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def get_matched_keywords(title, description):
    text = f"{title} {description}".lower()
    matches = []
    for keyword in TARGET_KEYWORDS:
        if keyword.lower() in text:
            matches.append(keyword)
    return matches

def get_entry_datetime(entry):
    """แกะและแปลงเวลาจาก RSS Feed ให้เป็นเวลาไทย (GMT+7) อย่างแม่นยำ"""
    pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if pub_parsed:
        try:
            dt_utc = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
            return dt_utc.astimezone(TZ_THAILAND)
        except Exception:
            pass

    for date_field in ["published", "updated", "pubDate"]:
        raw_date = entry.get(date_field)
        if raw_date:
            try:
                dt = parsedate_to_datetime(str(raw_date))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(TZ_THAILAND)
            except Exception:
                pass
    return None

def is_high_impact_analysis(analysis_text):
    """ตรวจสอบว่า AI ประเมินผลกระทบเป็นระดับ HIGH หรือไม่"""
    if "⭐⭐⭐ HIGH" in analysis_text:
        return True
    
    # ตรวจสอบเพิ่มเติมในโซนระดับผลกระทบ
    lines = analysis_text.split("\n")
    for line in lines:
        if "ระดับผลกระทบต่อตลาด" in line or "Market Impact" in line:
            if "HIGH" in line.upper() and "LOW" not in line.upper() and "MEDIUM" not in line.upper():
                return True
    return False

# =========================================================
# FETCH ONE FEED
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

    try:
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                return entries
    except Exception as e:
        print(f"⚠️ ดึงข้อมูลตรงติดขัด: {e}")

    try:
        encoded_url = quote_plus(url)
        proxy_url = f"https://api.allorigins.win/raw?url={encoded_url}"
        response = requests.get(proxy_url, headers=headers, timeout=20)
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                return entries
    except Exception as e:
        print(f"⚠️ Proxy Fallback ผิดพลาด: {e}")

    return []

# =========================================================
# FETCH ALL NEWS (กรองเฉพาะข่าววันนี้ตามเวลาไทยเท่านั้น)
# =========================================================

def fetch_latest_news():
    global last_check_time, last_check_finished, last_news_count, last_error, last_status
    global seen_news, current_bot_date

    last_check_time = now_text()
    last_status = "fetching"
    last_error = None

    now_th = datetime.now(TZ_THAILAND)
    today_th = now_th.date()

    # เคลียร์ความจำข่าวเมื่อขึ้นวันใหม่
    with seen_lock:
        if current_bot_date != today_th:
            print(f"📅 เปลี่ยนวันใหม่เป็น {today_th}: เคลียร์ประวัติข่าวเก่าเรียบร้อย")
            seen_news.clear()
            current_bot_date = today_th

    print("\n" + "=" * 70)
    print(f"🌍 START FETCH: {last_check_time} (วันที่ตรวจจับ: {today_th})")
    print("=" * 70)

    all_entries = []
    for index, url in enumerate(RSS_FEED_URLS, start=1):
        print(f"🌐 FEED {index}/{len(RSS_FEED_URLS)}: {url}")
        try:
            entries = fetch_one_feed(url)
            all_entries.extend(entries)
        except Exception as e:
            print(f"❌ FEED ERROR: {e}")
        time.sleep(0.3)

    unique_entries = []
    cycle_ids = set()
    
    for entry in all_entries:
        title = clean_text(entry.get("title", ""))
        if not title:
            continue

        # ⏱️ คัดเลือกเฉพาะข่าวสารที่เกิดขึ้นใน "วันนี้" เท่านั้น
        pub_dt_th = get_entry_datetime(entry)
        if pub_dt_th:
            if pub_dt_th.date() != today_th:
                continue
        else:
            # หากไม่มีข้อมูลเวลาแน่ชัด ให้ข้ามเพื่อความแม่นยำ
            continue

        news_id = create_news_id(title)
        if news_id in cycle_ids:
            continue

        cycle_ids.add(news_id)
        unique_entries.append(entry)

    last_news_count = len(unique_entries)
    last_check_finished = now_text()
    last_status = "fetch_success" if unique_entries else "no_news_found"

    print(f"📰 UNIQUE TODAY NEWS FOUND: {len(unique_entries)}")
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
# GROQ AI ANALYZE (ปรับเน้นการคัดกรองความสำคัญสูงสุด)
# =========================================================

def analyze_with_groq(title, description, source):
    if not GROQ_API_KEY:
        return "⚠️ ไม่พบ GROQ_API_KEY"

    prompt = f"""
คุณคือนักวิเคราะห์เศรษฐกิจมหภาคและภูมิรัฐศาสตร์ระดับสถาบันการเงินมืออาชีพ 

หน้าที่ของคุณคือวิเคราะห์ข่าวสารและประเมินผลกระทบต่อตลาดอย่างตรงไปตรงมา โดยยึดหลักเกณฑ์ดังนี้:
1. อ้างอิงข้อเท็จจริง (Facts) ในข่าวเท่านั้น ห้ามเดาหรือแต่งข้อมูลเพิ่มเติม
2. ประเมินระดับผลกระทบต่อตลาด (Market Impact Level) อย่างเข้มงวดที่สุด:
   - ⭐⭐⭐ HIGH: เฉพาะข่าวที่มีผลกระทบอย่างรุนแรงและทันที เช่น ตัวเลข CPI/NFP หลุดคาดการณ์อย่างมาก, มติอัตราดอกเบี้ย Fed/FOMC, แถลงการณ์สำคัญของ Jerome Powell/Donald Trump, หรือเหตุการณ์สงคราม/การโจมตีทางทหารระดับรุนแรง
   - ⭐⭐ MEDIUM: ข่าวตัวเลขเศรษฐกิจทั่วไป หรือข่าวที่มีผลกระทบจำกัด
   - ⭐ LOW: ข่าวบทวิเคราะห์ทั่วไป ข่าวความคิดเห็น หรือข่าวที่มีผลกระทบต่ำมาก

วิเคราะห์เนื้อหาข่าวต่อไปนี้:
=========================
หัวข้อข่าว: {title}
รายละเอียดข่าว: {description}
แหล่งข่าว: {source}
=========================

แสดงผลลัพธ์ตาม Template ด้านล่างนี้อย่างเคร่งครัด:

📌 สรุปข่าว
• [สรุปประเด็นสำคัญไม่เกิน 3 บรรทัด]

🎯 ประเภทข่าว
[เลือก 1 ข้อ: Fed | Donald Trump | Inflation | Interest Rate | CPI | PPI | NFP | GDP | FOMC | Tariff | Geopolitics | Gold | USD | Other]

🏦 ผลต่อ Fed
• [วิเคราะห์ผลกระทบต่อแนวโน้มอัตราดอกเบี้ย Fed]

💵 ผลต่อ USD (ดัชนีดอลลาร์)
• **[🟢 BULLISH / 🔴 BEARISH / 🟡 UNCERTAIN]** 
• เหตุผล: [อธิบายสั้นๆ 1-2 บรรทัด]

🥇 ผลต่อ GOLD (XAUUSD)
• **[🟢 BULLISH / 🔴 BEARISH / 🟡 UNCERTAIN]** 
• เหตุผล: [อธิบายสั้นๆ 1-2 บรรทัด]

🌍 ความเสี่ยงทางภูมิรัฐศาสตร์ (Geopolitical Risk)
• **[LOW / MEDIUM / HIGH]**

📊 ระดับผลกระทบต่อตลาด (Market Impact Level)
• **[เลือกเพียง 1 ข้อ: ⭐ LOW / ⭐⭐ MEDIUM / ⭐⭐⭐ HIGH]**

⏳ คาดการณ์แนวโน้มทิศทางราคา
• **ระยะสั้น (0-24 ชม.):** [UP / DOWN / SIDEWAYS]

⚠️ ปัจจัยเสี่ยงที่ต้องเฝ้าระวัง (Watchlist)
• [ระบุเหตุการณ์ถัดไปที่ต้องจับตา]
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 800,
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
# TELEGRAM SEND
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
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ TELEGRAM ERROR: {e}")
        return False

# =========================================================
# PROCESS NEWS (กรองส่งเฉพาะข่าวความสำคัญสูงสุด HIGH IMPACT)
# =========================================================

def process_news(entries):
    global last_relevant_count, total_sent
    relevant_count = 0
    sent_this_cycle = 0

    for entry in entries:
        if sent_this_cycle >= MAX_NEWS_PER_CYCLE:
            break

        title = clean_text(entry.get("title", ""))
        description = clean_text(entry.get("summary", entry.get("description", "")))
        link = entry.get("link", "").strip()
        source = get_source(entry)

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
        print(f"🔎 ANALYZING TODAY NEWS: {title}")

        # ให้ AI วิเคราะห์ข่าวก่อน
        analysis = analyze_with_groq(title, description, source)

        # 🚨 ระบบคัดกรองความสำคัญ: ส่งเฉพาะข่าวที่มีผลกระทบระดับ ⭐⭐⭐ HIGH เท่านั้น
        if not is_high_impact_analysis(analysis):
            print(f"⏩ SKIP: คัดทิ้งเนื่องจากไม่ใช่ข่าวความสำคัญสูงสุด (Low/Medium Impact)")
            with seen_lock:
                seen_news.add(news_id)  # บันทึกว่าตรวจแล้ว ไม่ต้องนำกลับมาวิเคราะห์ซ้ำ
            continue

        print(f"🔥 HIGH IMPACT DETECTED! Preparing to send: {title}")

        safe_title = html.escape(title)
        safe_source = html.escape(source)
        safe_analysis = html.escape(analysis)
        safe_link = html.escape(link, quote=True)

        message = (
            "🚨 <b>HIGH-IMPACT NEWS ALERT</b>\n\n"
            f"📰 <b>{safe_title}</b>\n\n"
            f"🏢 <b>Source:</b> {safe_source}\n\n"
            f"{safe_analysis}\n\n"
            f"🔗 <a href=\"{safe_link}\">อ่านข่าวฉบับเต็ม</a>"
        )

        success = send_telegram(message)
        if success:
            with seen_lock:
                seen_news.add(news_id)
            sent_this_cycle += 1
            total_sent += 1
            print("✅ SENT HIGH IMPACT NEWS TO TELEGRAM")
        time.sleep(2)

    last_relevant_count = relevant_count
    print(f"📊 Cycle Summary -> Matches: {relevant_count} | High-Impact Sent: {sent_this_cycle}")
    return sent_this_cycle

# =========================================================
# RUN ONE CYCLE
# =========================================================

def run_news_cycle():
    global bot_running, last_error, last_status, total_cycles
    if bot_running:
        print("⚠️ Cycle already running, skipping...")
        return

    bot_running = True
    last_status = "cycle_started"

    try:
        total_cycles += 1
        print(f"\n🚀 STARTING NEWS CYCLE #{total_cycles}")

        entries = fetch_latest_news()
        if not entries:
            print("❌ ไม่พบข่าวสดใหม่ตรงเงื่อนไขของวันนี้")
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
    time.sleep(5)

    while True:
        try:
            run_news_cycle()
        except Exception as e:
            print(f"❌ CRITICAL ERROR IN LOOP: {e}")
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
        return jsonify({"success": True, "count": len(result), "news": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/run-now")
def run_now():
    if bot_running:
        return jsonify({"success": False, "message": "Bot cycle already running"})
    thread = Thread(target=run_news_cycle, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Manual cycle started"})

def start_background_bot():
    global bot_started
    with bot_start_lock:
        if bot_started:
            return
        print("🎬 STARTING BACKGROUND BOT THREAD")
        thread = Thread(target=bot_loop, daemon=True)
        thread.start()
        bot_started = True

start_background_bot()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"🌐 STARTING FLASK ON PORT {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
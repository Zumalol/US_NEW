import os
import re
import time
import html
import hashlib
import traceback
from datetime import datetime, timezone, timedelta  # สำหรับคำนวณเวลาย้อนหลัง 1 วัน
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
# DIRECT PREMIUM FINANCIAL FEEDS (ตัด Google News ออกทั้งหมด)
# =========================================================

RSS_FEED_URLS = [
    "https://www.fxstreet.com/rss/news",                          # FXStreet (วิเคราะห์เชิงลึกทองคำ XAUUSD และตลาด Forex)
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",    # MarketWatch (ข่าวสารตลาดทุนสหรัฐฯ และนโยบายเศรษฐกิจ)
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",        # CNBC Economy (ข่าวด้านธนาคารกลาง Fed, ดอกเบี้ย และเงินเฟ้อ)
    "https://www.cnbc.com/id/15839069/device/rss/rss.html",        # CNBC Investing (ข่าววิเคราะห์ทิศทางทองคำ และสินค้าโภคภัณฑ์)
    "https://finance.yahoo.com/news/rssindex"                      # Yahoo Finance (สรุปภาพรวมข่าวเด่นเศรษฐกิจมหภาค)
]

# =========================================================
# KEYWORDS (เพิ่มหมวดหมู่ข่าวสงครามและอิหร่าน)
# =========================================================

TARGET_KEYWORDS = [
    # ตลาดทองคำ
    "gold",
    "xau",
    "xauusd",
    "bullion",

    # การเมืองสหรัฐฯ
    "trump",
    "donald trump",

    # ธนาคารกลาง & นโยบายการเงิน
    "fed",
    "fomc",
    "federal reserve",
    "powell",
    "jerome powell",
    "interest rate",
    "rate cut",
    "rate hike",
    "monetary policy",

    # ตัวเลขเศรษฐกิจ
    "inflation",
    "cpi",
    "pce",
    "nfp",
    "nonfarm",
    "jobs report",
    "unemployment",

    # ค่าเงิน & ผลตอบแทนพันธบัตร
    "dollar",
    "usd",
    "treasury",
    "bond yield",

    # สงครามการค้า
    "tariff",
    "tariffs",
    "trade war",

    # 🚨 ข่าวสงครามความขัดแย้ง และอิหร่าน (Geopolitical Risks & Iran)
    "iran",
    "iranian",
    "tehran",
    "war",
    "military",
    "missile",
    "strike",
    "middle east",
    "escalation",
    "retaliation",
    "attack",
    "geopolitical"
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
        print("🔄 [Direct Fetch] กำลังดึงข้อมูลตรงจากสำนักข่าวหลัก...")
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            entries = list(getattr(feed, "entries", []))
            if entries:
                return entries
    except Exception as e:
        print(f"⚠️ วิธีดึงตรงติดขัด: {e}")

    try:
        print("📡 [Proxy Fallback] ลองดึงผ่านเครือข่ายสำรอง...")
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
# FETCH ALL NEWS (กรองเวลา 1 วันล่าสุด)
# =========================================================

def fetch_latest_news():
    global last_check_time, last_check_finished, last_news_count, last_error, last_status

    last_check_time = now_text()
    last_status = "fetching"
    last_error = None

    print("\n" + "=" * 70)
    print(f"🌍 START FETCH: {last_check_time}")
    print("=" * 70)

    all_entries = []
    for index, url in enumerate(RSS_FEED_URLS, start=1):
        print(f"🌐 FEED {index}/{len(RSS_FEED_URLS)}: {url}")
        try:
            entries = fetch_one_feed(url)
            all_entries.extend(entries)
        except Exception as e:
            print(f"❌ FEED ERROR: {e}")
        time.sleep(0.5)

    unique_entries = []
    cycle_ids = set()
    
    now_dt = datetime.now(timezone.utc)
    one_day_ago = now_dt - timedelta(days=1)  # กรองย้อนหลัง 24 ชั่วโมง เท่านั้น

    for entry in all_entries:
        title = clean_text(entry.get("title", ""))
        if not title:
            continue

        pub_parsed = entry.get("published_parsed")
        if pub_parsed:
            try:
                pub_dt = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
                if pub_dt < one_day_ago:
                    continue  # เก่ากว่า 1 วัน คัดทิ้งทันที
            except Exception as time_err:
                print(f"⚠️ ไม่สามารถแปลงเวลาข่าวได้: {time_err}")

        news_id = create_news_id(title)
        if news_id in cycle_ids:
            continue

        cycle_ids.add(news_id)
        unique_entries.append(entry)

    last_news_count = len(unique_entries)
    last_check_finished = now_text()
    last_status = "fetch_success" if unique_entries else "no_news_found"

    print(f"📰 UNIQUE NEWS FOUND (PAST 24H): {len(unique_entries)}")
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
# GROQ AI ANALYZE
# =========================================================

def analyze_with_groq(title, description, source):
    if not GROQ_API_KEY:
        return "⚠️ ไม่พบ GROQ_API_KEY"

    prompt = f"""
คุณคือนักวิเคราะห์เศรษฐกิจมหภาค (Macroeconomics), ตลาดทองคำ (XAUUSD), ค่าเงินดอลลาร์ (USD) และนโยบายการเงินของ Federal Reserve (Fed) ระดับสถาบันการเงินมืออาชีพ 

หน้าที่ของคุณคือวิเคราะห์ข่าวสารที่ได้รับ โดยยึดหลักเกณฑ์ที่เข้มงวดดังต่อไปนี้:
1. อ้างอิงข้อเท็จจริง (Facts) ที่ปรากฏในเนื้อหาข่าวเท่านั้น แยกออกจากบทวิเคราะห์หรือการคาดการณ์ (Speculation) อย่างเด็ดขาด
2. ห้ามแต่งข้อมูล หรือคาดเดาปัจจัยที่ไม่มีระบุในข่าว หากข้อมูลไม่เพียงพอสำหรับหัวข้อใด ให้ระบุตรงๆ ว่า "ข้อมูลยังไม่เพียงพอสำหรับหัวข้อนี้"
3. ห้ามใช้คำพูดที่การันตีหรือรับประกันการเคลื่อนไหวของราคา 100% ให้ใช้คำว่า "มีแนวโน้ม", "ส่งแรงหนุน" หรือ "กดดัน" ตามหลักสถิติและเศรษฐศาสตร์
4. หากเป็น Breaking News ให้ระบุเตือนว่า "⚠️ ตลาดอาจมีความผันผวนสูงในกรอบเวลาสั้น"
5. เน้นการเชื่อมโยงผลกระทบเข้าสู่ 3 แกนหลักเสมอ: นโยบาย Fed -> ดัชนีดอลลาร์ (USD) -> ราคาทองคำ (XAUUSD) ในฐานะสินทรัพย์ปลอดภัย (Safe Haven) หรือสินทรัพย์ที่ไม่มีผลตอบแทนในรูปดอกเบี้ย

วิเคราะห์เนื้อหาข่าวต่อไปนี้:
=========================
หัวข้อข่าว: {title}
รายละเอียดข่าว: {description}
แหล่งข่าว: {source}
=========================

ให้แสดงผลลัพธ์ในรูปแบบ (Template) ด้านล่างนี้อย่างเคร่งครัด ห้ามแก้ไขหัวข้อ:

📌 สรุปข่าว
• [สรุปประเด็นสำคัญที่สุดไม่เกิน 4 บรรทัด กระชับ และตรงประเด็นเชิงโครงสร้างเศรษฐกิจ]

🎯 ประเภทข่าว
[เลือกคำตอบที่ถูกต้องที่สุดเพียงข้อเดียวจากรายการนี้: Fed | Donald Trump | Inflation | Interest Rate | CPI | PPI | NFP | GDP | FOMC | Tariff | Geopolitics | Gold | USD | Other]

🏦 ผลต่อ Fed
• [วิเคราะห์เชิงลึกว่าข่าวนี้ส่งผลต่อทิศทางอัตราดอกเบี้ยของ Fed อย่างไร เช่น เพิ่มโอกาสในการ (ขึ้น/คง/ลด) ดอกเบี้ย พร้อมระบุเหตุผลทางเศรษฐศาสตร์จากข่าว]

💵 ผลต่อ USD (ดัชนีดอลลาร์)
• **[🟢 BULLISH / 🔴 BEARISH / 🟡 UNCERTAIN]** 
• เหตุผล: [อธิบายความสัมพันธ์ไม่เกิน 3 บรรทัด เช่น ตัวเลขเศรษฐกิจแข็งแกร่งกว่าคาด ส่งผลหนุนให้ดอลลาร์แข็งค่า]

🥇 ผลต่อ GOLD (XAUUSD)
• **[🟢 BULLISH / 🔴 BEARISH / 🟡 UNCERTAIN]** 
• เหตุผล: [อธิบายเหตุผลไม่เกิน 3 บรรทัด โดยระบุให้ชัดเจนว่าข่าวนี้ "หนุน" หรือ "กดดัน" ทองคำในฐานะ Safe Haven หรือผ่านกลไก Opportunity Cost ของอัตราผลตอบแทนพันธบัตร/ดอลลาร์]

🌍 ความเสี่ยงทางภูมิรัฐศาสตร์ (Geopolitical Risk)
• **[LOW / MEDIUM / HIGH]**
• เหตุผลย่อ: [อธิบายสั้นๆ ว่าข่าวนี้ส่งผลต่อความตึงเครียดระดับโลกหรือความปลอดภัยในระบบการเงินอย่างไร]

📊 ระดับผลกระทบต่อตลาด (Market Impact Level)
• **[⭐ LOW / ⭐⭐ MEDIUM / ⭐⭐⭐ HIGH]**
*(หมายเหตุ: หากเป็นข่าวเกี่ยวกับ Trump, Powell, Fed, CPI, NFP หรือเหตุการณ์สงคราม ให้พิจารณาปรับระดับเป็น MEDIUM ถึง HIGH เสมอ)*

⏳ คาดการณ์แนวโน้มทิศทางราคา
• **ระยะสั้น (0-24 ชั่วโมง):** [UP / DOWN / SIDEWAYS]
• **ระยะกลาง (1-7 วัน):** [UP / DOWN / SIDEWAYS]

📈 ความเชื่อมั่นของการวิเคราะห์ (Confidence Score)
• **ระดับความเชื่อมั่น:** [ระบุเป็นเปอร์เซ็นต์ 0-100%]
• **เหตุผล:** [อธิบายสั้นๆ ว่าทำไมจึงให้คะแนนเท่านี้ เช่น ข้อมูลในข่าวมีความชัดเจนสูง หรือข่าวยังขาดตัวเลขสถิติอ้างอิงเชิงปริมาณ]

⚠️ ปัจจัยเสี่ยงที่ต้องเฝ้าระวัง (Watchlist)
• [ระบุเหตุการณ์หรือตัวเลขเศรษฐกิจถัดไปที่อาจพลิกสถานการณ์หรือลบล้างผลการวิเคราะห์นี้ เช่น ตัวเลข CPI ที่จะประกาศศุกร์นี้, ถ้อยแถลงของ Powell, Breaking News ด้านสงคราม ฯลฯ]
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
# PROCESS NEWS
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
                continue  # ข้ามข่าวที่ส่งไปแล้วป้องกันการส่งซ้ำ

        matches = get_matched_keywords(title, description)
        if not matches:
            continue

        relevant_count += 1
        print(f"🎯 MATCHED NEWS: {title}")

        analysis = analyze_with_groq(title, description, source)

        safe_title = html.escape(title)
        safe_source = html.escape(source)
        safe_analysis = html.escape(analysis)
        safe_link = html.escape(link, quote=True)

        message = (
            "🚨 <b>GOLD & GEOPOLITICAL NEWS</b>\n\n"
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
    print(f"📊 Cycle Summary -> Matches: {relevant_count} | Sent: {sent_this_cycle}")
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
            print("❌ ไม่พบข่าวสดใหม่ตรงเงื่อนไขในรอบนี้")
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
        bot_running = False  # คลายล็อกเพื่อให้รอบถัดไปทำงานได้เสมอ

# =========================================================
# BACKGROUND LOOP (ระบบรันออโตเมติกวนลูป)
# =========================================================

def bot_loop():
    print("\n🚀 BACKGROUND BOT LOOP STARTED")
    time.sleep(5)  # รอระบบ Flask เริ่มทำงานให้สมบูรณ์

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
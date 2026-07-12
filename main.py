import time
import requests
import os
import feedparser
import html
import re
import hashlib
from threading import Thread, Lock
from flask import Flask, jsonify
from dotenv import load_dotenv
from urllib.parse import quote_plus

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MAX_NEWS_PER_CYCLE = int(os.getenv("MAX_NEWS_PER_CYCLE", "5"))
SEND_NEWS_ON_START = os.getenv("SEND_NEWS_ON_START", "true").lower() == "true"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ถ้า model เดิมใช้ไม่ได้ ให้เปลี่ยนใน .env
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


# =========================================================
# RSS SEARCH QUERIES
# =========================================================

SEARCH_QUERIES = [
    "Donald Trump Federal Reserve",
    "Trump Fed Powell",
    "Federal Reserve FOMC interest rates",
    "Jerome Powell interest rates",
    "Gold Federal Reserve",
    "Gold XAUUSD Fed",
    "US inflation CPI Federal Reserve",
    "US jobs NFP Federal Reserve",
    "Trump tariffs dollar gold",
]

RSS_FEED_URLS = [
    (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}"
        "&hl=en-US&gl=US&ceid=US:en"
    )
    for query in SEARCH_QUERIES
]

# เพิ่ม FXStreet เป็นแหล่งสำรอง
RSS_FEED_URLS.append("https://www.fxstreet.com/rss/news")


# =========================================================
# KEYWORDS
# =========================================================

TARGET_KEYWORDS = [
    # GOLD
    "gold",
    "xau",
    "xauusd",
    "bullion",

    # TRUMP
    "trump",
    "donald trump",

    # FED
    "fed",
    "fomc",
    "federal reserve",
    "powell",
    "jerome powell",

    # INTEREST RATE
    "interest rate",
    "rate cut",
    "rate cuts",
    "rate hike",
    "monetary policy",

    # ECONOMIC DATA
    "inflation",
    "cpi",
    "pce",
    "nonfarm",
    "non-farm",
    "nfp",
    "jobs report",
    "unemployment",

    # USD / BONDS
    "dollar",
    "usd",
    "treasury",
    "bond yield",
    "yields",

    # TRUMP POLICY
    "tariff",
    "tariffs",
    "trade war",
]


# =========================================================
# GLOBAL VARIABLES
# =========================================================

seen_news = set()
seen_news_lock = Lock()

bot_started = False
bot_lock = Lock()

last_check_time = None
last_news_count = 0

app = Flask(__name__)


# =========================================================
# WEB ROUTES
# =========================================================

@app.route("/")
def home():
    return """
    <h1>🤖 Gold / Trump / Fed News Bot</h1>
    <p>สถานะ: ทำงานปกติ</p>
    <p>ติดตาม Gold, Trump, Fed, FOMC, Powell, CPI, NFP</p>
    <p><a href="/health">ดูสถานะระบบ</a></p>
    """


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_started": bot_started,
        "seen_news": len(seen_news),
        "last_news_count": last_news_count,
        "last_check_time": last_check_time,
        "check_interval": CHECK_INTERVAL,
        "rss_feeds": len(RSS_FEED_URLS),
    })


# =========================================================
# VALIDATE ENV
# =========================================================

def validate_environment():

    print("=" * 60)
    print("🔧 ตรวจสอบ Environment Variables")

    if TELEGRAM_TOKEN:
        print("✅ TELEGRAM_TOKEN: OK")
    else:
        print("❌ TELEGRAM_TOKEN: NOT FOUND")

    if TELEGRAM_CHAT_ID:
        print("✅ TELEGRAM_CHAT_ID: OK")
    else:
        print("❌ TELEGRAM_CHAT_ID: NOT FOUND")

    if GROQ_API_KEY:
        print("✅ GROQ_API_KEY: OK")
    else:
        print("❌ GROQ_API_KEY: NOT FOUND")

    print("=" * 60)


# =========================================================
# CLEAN HTML DESCRIPTION
# =========================================================

def clean_description(description):

    if not description:
        return ""

    # ลบ HTML
    clean = re.sub(r"<[^>]+>", " ", description)

    # Decode HTML entities
    clean = html.unescape(clean)

    # ลบช่องว่างซ้ำ
    clean = re.sub(r"\s+", " ", clean)

    return clean.strip()


# =========================================================
# NEWS ID
# =========================================================

def create_news_id(title, link):

    # ใช้ title เป็นหลัก เพราะ Google News
    # อาจมี URL ต่างกันแต่เป็นข่าวเดียวกัน

    text = title.lower().strip()

    if not text:
        text = link

    return hashlib.md5(
        text.encode("utf-8")
    ).hexdigest()


# =========================================================
# FILTER NEWS
# =========================================================

def is_relevant_news(title, description):

    text = (
        f"{title} {description}"
    ).lower()

    matched_keywords = []

    for keyword in TARGET_KEYWORDS:

        if keyword.lower() in text:
            matched_keywords.append(keyword)

    if matched_keywords:

        print(
            f"   🎯 Keyword: "
            f"{', '.join(matched_keywords[:5])}"
        )

        return True

    return False


# =========================================================
# FETCH ONE RSS
# =========================================================

def fetch_single_feed(rss_url):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131.0 Safari/537.36"
        ),
        "Accept": (
            "application/rss+xml,"
            "application/xml,"
            "text/xml,"
            "*/*"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:

        response = requests.get(
            rss_url,
            headers=headers,
            timeout=20,
            allow_redirects=True,
        )

        print(
            f"   HTTP {response.status_code}"
        )

        if response.status_code != 200:
            return []

        if not response.content:
            print("   ⚠️ Response ว่าง")
            return []

        feed = feedparser.parse(
            response.content
        )

        if feed.bozo:
            print(
                f"   ⚠️ Feed warning: "
                f"{feed.bozo_exception}"
            )

        entries = list(
            getattr(feed, "entries", [])
        )

        print(
            f"   📥 พบ {len(entries)} ข่าว"
        )

        return entries

    except requests.exceptions.Timeout:

        print("   ❌ Timeout")
        return []

    except Exception as e:

        print(
            f"   ❌ Fetch Error: {e}"
        )

        return []


# =========================================================
# FETCH ALL NEWS
# =========================================================

def fetch_latest_news():

    global last_news_count
    global last_check_time

    print("\n" + "=" * 60)
    print("🌍 เริ่มค้นหาข่าวจากทุกแหล่ง")
    print("=" * 60)

    all_entries = []

    for index, rss_url in enumerate(
        RSS_FEED_URLS,
        start=1
    ):

        print(
            f"\n🌐 Feed "
            f"{index}/{len(RSS_FEED_URLS)}"
        )

        entries = fetch_single_feed(
            rss_url
        )

        all_entries.extend(entries)

        # เว้นระยะเล็กน้อย
        time.sleep(0.5)

    # =====================================================
    # REMOVE DUPLICATES
    # =====================================================

    unique_entries = []
    unique_ids = set()

    for entry in all_entries:

        title = entry.get(
            "title",
            ""
        ).strip()

        link = entry.get(
            "link",
            ""
        ).strip()

        if not title:
            continue

        news_id = create_news_id(
            title,
            link
        )

        if news_id in unique_ids:
            continue

        unique_ids.add(news_id)
        unique_entries.append(entry)

    last_news_count = len(unique_entries)

    last_check_time = time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print("\n" + "=" * 60)

    print(
        f"📊 ข่าวดิบทั้งหมด: "
        f"{len(all_entries)}"
    )

    print(
        f"📊 หลังตัดข่าวซ้ำ: "
        f"{len(unique_entries)}"
    )

    print("=" * 60)

    return unique_entries


# =========================================================
# GET SOURCE
# =========================================================

def get_news_source(entry):

    try:

        source = entry.get(
            "source",
            {}
        )

        if isinstance(source, dict):

            source_title = source.get(
                "title"
            )

            if source_title:
                return source_title

    except Exception:
        pass

    return "News"


# =========================================================
# GROQ AI
# =========================================================

def summarize_with_groq(
    news_title,
    news_description,
    news_source
):

    if not GROQ_API_KEY:

        return (
            "⚠️ ไม่พบ GROQ_API_KEY "
            "จึงยังไม่ได้วิเคราะห์ด้วย AI"
        )

    print(
        "🤖 กำลังวิเคราะห์ด้วย Groq..."
    )

    headers = {
        "Authorization":
            f"Bearer {GROQ_API_KEY}",

        "Content-Type":
            "application/json",
    }

    prompt = f"""
คุณคือนักวิเคราะห์ข่าวเศรษฐกิจมหภาค
และตลาดทองคำ XAUUSD

วิเคราะห์ข่าวนี้เป็นภาษาไทย

หัวข้อ:
{news_title}

รายละเอียด:
{news_description}

แหล่งข่าว:
{news_source}

ตอบตามรูปแบบนี้:

📌 สรุปข่าว:
สรุปใจความสำคัญแบบสั้นและชัดเจน

🏦 ผลต่อ Fed / ดอกเบี้ย:
วิเคราะห์ผลที่เป็นไปได้

💵 ผลต่อ USD:
ระบุ BULLISH / BEARISH / UNCERTAIN
พร้อมเหตุผล

🥇 ผลต่อ GOLD / XAUUSD:
ระบุเพียงหนึ่ง:
🟢 BULLISH
🔴 BEARISH
🟡 UNCERTAIN

พร้อมเหตุผล

⚠️ ความสำคัญ:
LOW / MEDIUM / HIGH

ห้ามรับประกันทิศทางราคา
และต้องแยกข้อเท็จจริงออกจากการคาดการณ์
"""

    payload = {
        "model": GROQ_MODEL,

        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional "
                    "macroeconomic and gold "
                    "market news analyst."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],

        "temperature": 0.2,

        "max_tokens": 800,
    }

    try:

        response = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )

        print(
            f"🤖 Groq HTTP: "
            f"{response.status_code}"
        )

        if response.status_code == 200:

            data = response.json()

            return (
                data["choices"][0]
                ["message"]
                ["content"]
                .strip()
            )

        print(
            f"❌ Groq Error: "
            f"{response.text}"
        )

        return (
            f"⚠️ AI วิเคราะห์ไม่สำเร็จ "
            f"HTTP {response.status_code}"
        )

    except Exception as e:

        print(
            f"❌ Groq Exception: {e}"
        )

        return (
            "⚠️ ไม่สามารถเชื่อมต่อ "
            "AI ได้ในขณะนี้"
        )


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram_message(text):

    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN ไม่มี")
        return False

    if not TELEGRAM_CHAT_ID:
        print("❌ TELEGRAM_CHAT_ID ไม่มี")
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    # Telegram จำกัดข้อความ
    if len(text) > 4000:
        text = (
            text[:3950]
            + "\n\n...ข้อความถูกตัด"
        )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=30,
        )

        print(
            f"📡 Telegram HTTP: "
            f"{response.status_code}"
        )

        if response.status_code != 200:

            print(
                f"❌ Telegram Error: "
                f"{response.text}"
            )

            return False

        return True

    except Exception as e:

        print(
            f"❌ Telegram Exception: {e}"
        )

        return False


# =========================================================
# PROCESS NEWS
# =========================================================

def process_news(
    entries,
    max_send=5
):

    sent_count = 0

    # ข่าวล่าสุดมาก่อน
    for entry in entries:

        if sent_count >= max_send:
            break

        title = entry.get(
            "title",
            "ไม่มีหัวข้อ"
        ).strip()

        raw_description = entry.get(
            "summary",
            entry.get(
                "description",
                ""
            )
        )

        description = clean_description(
            raw_description
        )

        link = entry.get(
            "link",
            ""
        ).strip()

        source = get_news_source(
            entry
        )

        news_id = create_news_id(
            title,
            link
        )

        # -------------------------------
        # CHECK SEEN
        # -------------------------------

        with seen_news_lock:

            if news_id in seen_news:
                continue

        print(
            f"\n🔎 ตรวจข่าว: {title}"
        )

        # -------------------------------
        # FILTER
        # -------------------------------

        if not is_relevant_news(
            title,
            description
        ):

            print(
                "   ⏭️ ไม่ตรง Keyword"
            )

            with seen_news_lock:
                seen_news.add(news_id)

            continue

        print(
            "   ✅ ข่าวตรงเงื่อนไข"
        )

        # -------------------------------
        # AI
        # -------------------------------

        summary = summarize_with_groq(
            title,
            description,
            source
        )

        # -------------------------------
        # SAFE HTML
        # -------------------------------

        safe_title = html.escape(
            title
        )

        safe_source = html.escape(
            source
        )

        safe_summary = html.escape(
            summary
        )

        safe_link = html.escape(
            link,
            quote=True
        )

        # -------------------------------
        # MESSAGE
        # -------------------------------

        message = (
            "🚨 <b>GOLD MARKET UPDATE</b>\n\n"

            f"📰 <b>หัวข้อ:</b>\n"
            f"{safe_title}\n\n"

            f"🏢 <b>แหล่งข่าว:</b> "
            f"{safe_source}\n\n"

            f"🤖 <b>AI วิเคราะห์:</b>\n"
            f"{safe_summary}\n\n"
        )

        if link:

            message += (
                f"🔗 <a href=\"{safe_link}\">"
                "อ่านข่าว"
                "</a>"
            )

        # -------------------------------
        # SEND
        # -------------------------------

        success = send_telegram_message(
            message
        )

        if success:

            with seen_news_lock:
                seen_news.add(news_id)

            sent_count += 1

            print(
                f"✅ ส่งข่าวสำเร็จ "
                f"({sent_count}/{max_send})"
            )

        else:

            print(
                "⚠️ ส่งไม่สำเร็จ "
                "จะลองใหม่รอบหน้า"
            )

        time.sleep(2)

    print(
        f"\n📨 รอบนี้ส่งข่าวทั้งหมด "
        f"{sent_count} ข่าว"
    )

    return sent_count


# =========================================================
# BOT LOOP
# =========================================================

def bot_loop():

    print("\n🚀 GOLD NEWS BOT STARTED")

    validate_environment()

    send_telegram_message(
        "✅ <b>Gold / Trump / Fed News Bot เริ่มทำงานแล้ว</b>\n\n"
        "🔎 กำลังค้นหาข่าวทันที...\n"
        "🥇 Gold / XAUUSD\n"
        "🇺🇸 Donald Trump\n"
        "🏦 Fed / FOMC / Powell\n"
        "📊 CPI / PCE / NFP\n"
        "💵 USD / Treasury Yields"
    )

    first_run = True

    while True:

        try:

            print(
                "\n🔄 เริ่มรอบค้นหาข่าวใหม่"
            )

            entries = fetch_latest_news()

            if not entries:

                print(
                    "⚠️ ไม่พบข่าวจาก RSS "
                    "จะลองใหม่รอบถัดไป"
                )

            else:

                print(
                    f"🔍 กำลังตรวจสอบ "
                    f"{len(entries)} ข่าว"
                )

                if first_run:

                    if SEND_NEWS_ON_START:

                        print(
                            "🔥 รอบแรก: "
                            "ส่งข่าวล่าสุดทันที"
                        )

                        process_news(
                            entries,
                            MAX_NEWS_PER_CYCLE
                        )

                    else:

                        print(
                            "📝 รอบแรก: "
                            "บันทึกข่าวโดยไม่ส่ง"
                        )

                        for entry in entries:

                            title = entry.get(
                                "title",
                                ""
                            )

                            link = entry.get(
                                "link",
                                ""
                            )

                            news_id = create_news_id(
                                title,
                                link
                            )

                            with seen_news_lock:
                                seen_news.add(
                                    news_id
                                )

                    first_run = False

                else:

                    process_news(
                        entries,
                        MAX_NEWS_PER_CYCLE
                    )

            print(
                f"\n😴 รอ {CHECK_INTERVAL} "
                "วินาทีก่อนค้นหาใหม่"
            )

            time.sleep(
                CHECK_INTERVAL
            )

        except Exception as e:

            print(
                f"❌ BOT LOOP ERROR: {e}"
            )

            time.sleep(60)


# =========================================================
# START BOT SAFELY
# =========================================================

def start_bot():

    global bot_started

    with bot_lock:

        if bot_started:
            return

        print(
            "🎬 กำลังเริ่ม Background Bot..."
        )

        thread = Thread(
            target=bot_loop,
            daemon=True
        )

        thread.start()

        bot_started = True


# เริ่มทันทีเมื่อ Python process ทำงาน
start_bot()


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
        use_reloader=False,
    )


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
# SEARCH QUERIES
# =========================================================

SEARCH_QUERIES = [
    "Donald Trump Federal Reserve",
    "Trump Powell Fed",
    "Federal Reserve interest rates",
    "FOMC Powell",
    "Gold Federal Reserve",
    "Gold XAUUSD Fed",
    "US CPI inflation Fed",
    "US NFP jobs Fed",
    "Trump tariffs gold dollar",
]

RSS_FEED_URLS = []

for query in SEARCH_QUERIES:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}"
        "&hl=en-US"
        "&gl=US"
        "&ceid=US:en"
    )
    RSS_FEED_URLS.append(url)

# RSS สำรอง
RSS_FEED_URLS.extend([
    "https://www.fxstreet.com/rss/news",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
])

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
    <h1>🤖 Gold / Trump / Fed News Bot</h1>

    <p>Bot is running.</p>

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

    value = re.sub(
        r"<[^>]+>",
        " ",
        str(value)
    )

    value = html.unescape(value)

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()

# =========================================================
# NEWS ID
# =========================================================

def create_news_id(title):

    normalized = re.sub(
        r"\s+",
        " ",
        title.lower().strip()
    )

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()

# =========================================================
# FILTER
# =========================================================

def get_matched_keywords(title, description):

    text = (
        f"{title} {description}"
    ).lower()

    matches = []

    for keyword in TARGET_KEYWORDS:

        if keyword.lower() in text:
            matches.append(keyword)

    return matches

# =========================================================
# FETCH RSS METHOD 1
# =========================================================

def fetch_with_requests(url):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),

        "Accept": (
            "application/rss+xml,"
            "application/xml,"
            "text/xml,"
            "*/*"
        ),

        "Accept-Language":
            "en-US,en;q=0.9",

        "Cache-Control":
            "no-cache",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=20,
        allow_redirects=True,
    )

    print(
        f"HTTP {response.status_code} | "
        f"{len(response.content)} bytes"
    )

    response.raise_for_status()

    feed = feedparser.parse(
        response.content
    )

    return list(
        getattr(
            feed,
            "entries",
            []
        )
    )

# =========================================================
# FETCH RSS METHOD 2
# =========================================================

def fetch_with_feedparser_direct(url):

    print(
        "🔄 ลอง feedparser direct..."
    )

    feed = feedparser.parse(url)

    return list(
        getattr(
            feed,
            "entries",
            []
        )
    )

# =========================================================
# FETCH ONE FEED
# =========================================================

def fetch_one_feed(url):

    # METHOD 1
    try:

        entries = fetch_with_requests(
            url
        )

        if entries:

            print(
                f"✅ requests พบ "
                f"{len(entries)} ข่าว"
            )

            return entries

    except Exception as e:

        print(
            f"⚠️ requests error: {e}"
        )

    # METHOD 2
    try:

        entries = (
            fetch_with_feedparser_direct(
                url
            )
        )

        if entries:

            print(
                f"✅ direct พบ "
                f"{len(entries)} ข่าว"
            )

            return entries

    except Exception as e:

        print(
            f"⚠️ direct error: {e}"
        )

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

    # อัปเดตทันที
    last_check_time = now_text()
    last_status = "fetching"
    last_error = None

    print("\n")
    print("=" * 70)
    print(
        f"🌍 START FETCH: "
        f"{last_check_time}"
    )
    print("=" * 70)

    all_entries = []

    for index, url in enumerate(
        RSS_FEED_URLS,
        start=1
    ):

        print()
        print(
            f"🌐 FEED "
            f"{index}/{len(RSS_FEED_URLS)}"
        )

        print(
            url[:180]
        )

        try:

            entries = fetch_one_feed(
                url
            )

            all_entries.extend(
                entries
            )

        except Exception as e:

            print(
                f"❌ FEED ERROR: {e}"
            )

        # อย่ายิง request เร็วเกินไป
        time.sleep(0.3)

    # =====================================================
    # REMOVE DUPLICATES
    # =====================================================

    unique_entries = []
    cycle_ids = set()

    for entry in all_entries:

        title = clean_text(
            entry.get(
                "title",
                ""
            )
        )

        if not title:
            continue

        news_id = create_news_id(
            title
        )

        if news_id in cycle_ids:
            continue

        cycle_ids.add(
            news_id
        )

        unique_entries.append(
            entry
        )

    last_news_count = len(
        unique_entries
    )

    last_check_finished = now_text()

    if unique_entries:
        last_status = "fetch_success"
    else:
        last_status = "no_news_found"

    print()
    print("=" * 70)

    print(
        f"📦 RAW NEWS: "
        f"{len(all_entries)}"
    )

    print(
        f"📰 UNIQUE NEWS: "
        f"{len(unique_entries)}"
    )

    print("=" * 70)

    return unique_entries

# =========================================================
# GET SOURCE
# =========================================================

def get_source(entry):

    try:

        source = entry.get(
            "source",
            {}
        )

        if isinstance(source, dict):

            title = source.get(
                "title"
            )

            if title:
                return clean_text(
                    title
                )

    except Exception:
        pass

    return "News Source"

# =========================================================
# GROQ
# =========================================================

def analyze_with_groq(
    title,
    description,
    source
):

    if not GROQ_API_KEY:

        return (
            "⚠️ ไม่พบ GROQ_API_KEY"
        )

    prompt = f"""
คุณคือนักวิเคราะห์ตลาดทองคำ XAUUSD
และเศรษฐกิจสหรัฐฯ

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
BULLISH / BEARISH / UNCERTAIN
พร้อมเหตุผล

🥇 ผลต่อ GOLD / XAUUSD:
🟢 BULLISH
หรือ
🔴 BEARISH
หรือ
🟡 UNCERTAIN

พร้อมเหตุผล

⚠️ ความสำคัญ:
LOW / MEDIUM / HIGH

ห้ามรับประกันทิศทางราคา
"""

    headers = {
        "Authorization":
            f"Bearer {GROQ_API_KEY}",

        "Content-Type":
            "application/json",
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

        response = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )

        print(
            f"🤖 GROQ HTTP "
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
            response.text
        )

        return (
            f"⚠️ Groq Error "
            f"HTTP {response.status_code}"
        )

    except Exception as e:

        print(
            f"❌ GROQ ERROR: {e}"
        )

        return (
            "⚠️ AI ไม่สามารถวิเคราะห์"
            "ได้ในขณะนี้"
        )

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(text):

    if not TELEGRAM_TOKEN:

        print(
            "❌ TELEGRAM_TOKEN missing"
        )

        return False

    if not TELEGRAM_CHAT_ID:

        print(
            "❌ TELEGRAM_CHAT_ID missing"
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    if len(text) > 4000:

        text = (
            text[:3950]
            + "\n\n..."
        )

    payload = {
        "chat_id":
            TELEGRAM_CHAT_ID,

        "text":
            text,

        "parse_mode":
            "HTML",

        "disable_web_page_preview":
            True,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=30,
        )

        print(
            f"📨 TELEGRAM HTTP "
            f"{response.status_code}"
        )

        if response.status_code != 200:

            print(
                response.text
            )

            return False

        return True

    except Exception as e:

        print(
            f"❌ TELEGRAM ERROR: {e}"
        )

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

        title = clean_text(
            entry.get(
                "title",
                ""
            )
        )

        description = clean_text(
            entry.get(
                "summary",
                entry.get(
                    "description",
                    ""
                )
            )
        )

        link = entry.get(
            "link",
            ""
        )

        source = get_source(
            entry
        )

        if not title:
            continue

        news_id = create_news_id(
            title
        )

        # ALREADY SEEN
        with seen_lock:

            if news_id in seen_news:
                continue

        # KEYWORD MATCH
        matches = get_matched_keywords(
            title,
            description
        )

        if not matches:

            continue

        relevant_count += 1

        print()
        print(
            f"🎯 RELEVANT: {title}"
        )

        print(
            f"🔑 MATCH: "
            f"{', '.join(matches[:8])}"
        )

        # AI
        analysis = analyze_with_groq(
            title,
            description,
            source
        )

        safe_title = html.escape(
            title
        )

        safe_source = html.escape(
            source
        )

        safe_analysis = html.escape(
            analysis
        )

        safe_link = html.escape(
            link,
            quote=True
        )

        message = (
            "🚨 <b>GOLD MARKET NEWS</b>\n\n"

            f"📰 <b>{safe_title}</b>\n\n"

            f"🏢 <b>Source:</b> "
            f"{safe_source}\n\n"

            f"🤖 <b>AI Analysis:</b>\n"
            f"{safe_analysis}\n\n"
        )

        if link:

            message += (
                f"🔗 <a href=\""
                f"{safe_link}"
                f"\">อ่านข่าว</a>"
            )

        success = send_telegram(
            message
        )

        if success:

            with seen_lock:

                seen_news.add(
                    news_id
                )

            sent_this_cycle += 1
            total_sent += 1

            print(
                "✅ SENT"
            )

        time.sleep(2)

    last_relevant_count = (
        relevant_count
    )

    print(
        f"🎯 RELEVANT NEWS: "
        f"{relevant_count}"
    )

    print(
        f"📨 SENT THIS CYCLE: "
        f"{sent_this_cycle}"
    )

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

        print(
            "⚠️ Cycle already running"
        )

        return

    bot_running = True
    last_status = "cycle_started"

    try:

        total_cycles += 1

        print()
        print(
            f"🚀 NEWS CYCLE "
            f"#{total_cycles}"
        )

        entries = fetch_latest_news()

        if not entries:

            print(
                "❌ ไม่พบข่าวจากทุก Feed"
            )

            last_status = "no_news_found"

            return

        last_status = "processing"

        process_news(
            entries
        )

        last_status = "cycle_complete"

    except Exception as e:

        last_error = str(e)

        last_status = "error"

        print(
            "❌ CYCLE ERROR"
        )

        traceback.print_exc()

    finally:

        bot_running = False

# =========================================================
# BACKGROUND LOOP
# =========================================================

def bot_loop():

    print()
    print(
        "🚀 BACKGROUND BOT LOOP STARTED"
    )

    # รอ Flask/Gunicorn พร้อมก่อนเล็กน้อย
    time.sleep(3)

    while True:

        try:

            run_news_cycle()

        except Exception:

            traceback.print_exc()

        print(
            f"😴 WAIT "
            f"{CHECK_INTERVAL} SECONDS"
        )

        time.sleep(
            CHECK_INTERVAL
        )

# =========================================================
# TEST NEWS ENDPOINT
# =========================================================

@app.route("/test-news")
def test_news():

    try:

        entries = fetch_latest_news()

        result = []

        for entry in entries[:20]:

            title = clean_text(
                entry.get(
                    "title",
                    ""
                )
            )

            description = clean_text(
                entry.get(
                    "summary",
                    entry.get(
                        "description",
                        ""
                    )
                )
            )

            matches = get_matched_keywords(
                title,
                description
            )

            result.append({
                "title": title,
                "source": get_source(
                    entry
                ),
                "matched_keywords":
                    matches,
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
            "message":
                "Bot cycle already running",
        })

    thread = Thread(
        target=run_news_cycle,
        daemon=True
    )

    thread.start()

    return jsonify({
        "success": True,
        "message":
            "News cycle started",
    })

# =========================================================
# START BACKGROUND BOT
# =========================================================

def start_background_bot():

    global bot_started

    with bot_start_lock:

        if bot_started:
            return

        print(
            "🎬 STARTING BACKGROUND BOT"
        )

        thread = Thread(
            target=bot_loop,
            daemon=True
        )

        thread.start()

        bot_started = True

# เริ่มบอททันที
start_background_bot()

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    print(
        f"🌐 STARTING FLASK "
        f"ON PORT {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
        use_reloader=False,
    )


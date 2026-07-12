import time
import requests
import os
import feedparser
import html
from threading import Thread, Lock
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# ตั้งค่าระบบ
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-8b-8192")


# =========================================================
# RSS FEEDS
# =========================================================

RSS_FEED_URLS = [
    # Trump + Federal Reserve
    "https://news.google.com/rss/search?q=Trump+Federal+Reserve+OR+Fed&hl=en-US&gl=US&ceid=US:en",

    # Fed + FOMC + Interest Rates
    "https://news.google.com/rss/search?q=Federal+Reserve+interest+rates+FOMC+Powell&hl=en-US&gl=US&ceid=US:en",

    # Gold + Fed
    "https://news.google.com/rss/search?q=Gold+XAUUSD+Federal+Reserve+Fed&hl=en-US&gl=US&ceid=US:en",

    # Trump + Gold + Dollar
    "https://news.google.com/rss/search?q=Trump+Gold+Dollar+Tariffs&hl=en-US&gl=US&ceid=US:en",
]


# =========================================================
# KEYWORDS
# =========================================================

TARGET_KEYWORDS = [
    # Gold
    "gold",
    "xau",
    "xauusd",
    "ทอง",

    # Trump
    "trump",
    "donald trump",
    "ทรัมป์",

    # Federal Reserve
    "fed",
    "fomc",
    "powell",
    "jerome powell",
    "federal reserve",

    # Monetary policy
    "interest rate",
    "rate cut",
    "rate hike",
    "inflation",

    # US economy / USD
    "dollar",
    "usd",
    "treasury",
    "bond yield",

    # Trump policies
    "tariff",
    "trade war",
]


# =========================================================
# GLOBAL VARIABLES & LOCKS
# =========================================================

seen_news = set()
seen_news_lock = Lock()

bg_started = False
bg_lock = Lock()

app = Flask(__name__)


# =========================================================
# WEB SERVER
# =========================================================

@app.route("/")
def home():
    return """
    <h1>🤖 Gold News Bot</h1>
    <p>สถานะ: ทำงานปกติ 24/7</p>
    <p>ติดตามข่าว: Gold / Fed / FOMC / Powell / Trump</p>
    """


@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot": "Gold/Fed/Trump News Bot",
        "seen_news": len(seen_news),
        "check_interval": CHECK_INTERVAL,
    }


# =========================================================
# ตรวจสอบ Environment Variables
# =========================================================

def validate_environment():
    missing = []

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")

    if missing:
        print("⚠️ Environment Variables ที่ยังไม่มี:")
        for item in missing:
            print(f"   - {item}")
        return False

    print("✅ Environment Variables พร้อมใช้งาน")
    return True


# =========================================================
# กรองข่าว
# =========================================================

def is_relevant_news(title, description):
    text_to_check = f"{title} {description}".lower()

    return any(
        keyword.lower() in text_to_check
        for keyword in TARGET_KEYWORDS
    )


# =========================================================
# ดึงข่าวจากหลาย RSS (พร้อมระบบป้องกันและหลบเลี่ยง 503)
# =========================================================

def fetch_latest_news():
    all_entries = []

    # ปรับเป็น Mobile User-Agent เพื่อให้กลมกลืนเหมือนการเปิดอ่านข่าวบน iPhone
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
            "Mobile/15E148 Safari/604.1"
        )
    }

    for rss_url in RSS_FEED_URLS:
        feed = None
        try:
            print(f"🌐 กำลังโหลด RSS (Direct): {rss_url}")
            response = requests.get(rss_url, headers=headers, timeout=15)
            
            # ตรวจสอบว่าโดนบล็อก IP หรือไม่
            if response.status_code == 503:
                print("⚠️ เจอข้อผิดพลาด 503 (IP ของ Render ถูกกั้น) -> กำลังเปิดใช้ Proxy สำรอง...")
                raise requests.exceptions.HTTPError("503 Blocked")
                
            response.raise_for_status()
            feed = feedparser.parse(response.content)

        except Exception as e:
            print(f"🔄 วิธีดึงตรงล้มเหลว ({e}) -> กำลังสลับไปดึงผ่าน Proxy บังตา...")
            try:
                # เข้ารหัส URL ป้องกันอักขระพิเศษพัง
                encoded_url = requests.utils.quote(rss_url)
                
                # ลอง Proxy ตัวที่ 1
                proxy_url = f"https://api.allorigins.win/raw?url={encoded_url}"
                print(f"📡 กำลังโหลดผ่าน Proxy: api.allorigins.win")
                proxy_response = requests.get(proxy_url, headers=headers, timeout=20)
                
                if proxy_response.status_code == 200:
                    feed = feedparser.parse(proxy_response.content)
                else:
                    # ลอง Proxy ตัวที่ 2 สำรองหากตัวแรกติดขัด
                    proxy_url_2 = f"https://corsproxy.io/?{encoded_url}"
                    print(f"📡 กำลังโหลดผ่าน Proxy สำรองตัวที่สอง: corsproxy.io")
                    proxy_response_2 = requests.get(proxy_url_2, headers=headers, timeout=20)
                    feed = feedparser.parse(proxy_response_2.content)
                    
            except Exception as proxy_err:
                print(f"❌ ระบบ Proxy ก็ไม่สามารถเข้าถึงได้ในรอบนี้: {proxy_err}")
                continue

        # ตรวจสอบว่าได้ข่าวกลับมาไหม
        if feed and hasattr(feed, "entries") and feed.entries:
            print(f"📥 พบ {len(feed.entries)} ข่าวจาก RSS นี้")
            all_entries.extend(feed.entries)
        else:
            print("⚠️ ไม่พบข้อมูลข่าวสารในฟีดนี้ในรอบนี้")

    # -----------------------------------------------------
    # ตัดข่าวซ้ำระหว่าง Feed ต่างๆ
    # -----------------------------------------------------
    unique_entries = []
    unique_keys = set()

    for entry in all_entries:
        link = entry.get("link", "")
        title = entry.get("title", "")

        key = link if link else title.lower().strip()

        if key and key not in unique_keys:
            unique_keys.add(key)
            unique_entries.append(entry)

    print(f"📰 รวมข่าวทั้งหมดหลังตัดข่าวซ้ำสำเร็จ: {len(unique_entries)} ข่าว")
    return unique_entries


# =========================================================
# วิเคราะห์ข่าวด้วย Groq
# =========================================================

def summarize_with_groq(news_title, news_description, news_source=""):
    print("🤖 กำลังให้ Groq วิเคราะห์ข่าว...")

    if not GROQ_API_KEY:
        return "❌ 不พบ GROQ_API_KEY"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    prompt = f"""
คุณคือนักวิเคราะห์ตลาดการเงินและทองคำระดับมืออาชีพ

วิเคราะห์ข่าวต่อไปนี้เป็นภาษาไทย

หัวข้อข่าว:
{news_title}

รายละเอียดข่าว:
{news_description}

แหล่งข่าว:
{news_source}

ให้ตอบตามรูปแบบนี้:

📌 สรุปข่าว:
สรุปสั้น กระชับ และเข้าใจง่าย

🏦 ผลกระทบต่อ Fed / ดอกเบี้ย:
อธิบายว่าข่าวนี้อาจส่งผลต่อแนวโน้มดอกเบี้ยของ Federal Reserve อย่างไร

💵 ผลกระทบต่อ USD:
วิเคราะห์ว่าเป็นบวกหรือลบต่อดอลลาร์

🥇 ผลกระทบต่อ GOLD / XAUUSD:
เลือกเพียงหนึ่งแนวโน้มหลัก:
🟢 BULLISH
🔴 BEARISH
🟡 UNCERTAIN

อธิบายเหตุผลสั้น ๆ

⚠️ ความสำคัญของข่าว:
เลือก LOW / MEDIUM / HIGH

ห้ามรับประกันว่าราคาจะขึ้นหรือลงแน่นอน
ต้องแยกข้อเท็จจริงจากการคาดการณ์
"""

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a professional macroeconomic and gold market analyst.",
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
            json=payload,
            headers=headers,
            timeout=45,
        )

        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

        print(f"❌ Groq HTTP {response.status_code}: {response.text}")
        return f"❌ Groq Error HTTP {response.status_code}"

    except Exception as e:
        print(f"❌ ไม่สามารถเชื่อมต่อ Groq: {e}")
        return f"❌ ไม่สามารถเชื่อมต่อกับ Groq ได้: {e}"


# =========================================================
# ส่ง Telegram
# =========================================================

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ ไม่มี TELEGRAM_TOKEN หรือ TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    if len(text) > 4000:
        text = text[:4000] + "\n\n...ข้อความถูกตัด"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=20)
        print(f"📡 Telegram HTTP: {response.status_code}")
        
        if response.status_code != 200:
            print(f"❌ Telegram Error: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ Error ตอนส่ง Telegram: {e}")
        return False


# =========================================================
# ดึงชื่อ Source ข่าว
# =========================================================

def get_news_source(entry):
    try:
        source = entry.get("source", {})
        if isinstance(source, dict):
            return source.get("title", "Google News")
    except Exception:
        pass
    return "Google News"


# =========================================================
# เตรียมข่าวเก่า ตอนเปิดบอท
# =========================================================

def initialize_seen_news():
    print("🔄 กำลังโหลดประวัติข่าวสารรอบแรกเพื่อเตรียมระบบป้องกันการส่งย้อนหลัง...")
    entries = fetch_latest_news()

    with seen_news_lock:
        for entry in entries:
            link = entry.get("link", "")
            if link:
                seen_news.add(link)

    print(f"✅ บันทึกประวัติข่าวเก่าเข้าหน่วยความจำชั่วคราวแล้ว {len(seen_news)} ข่าว")


# =========================================================
# BOT MAIN LOOP
# =========================================================

def bot_loop():
    print("🚀 [Background] Gold News Bot เริ่มทำงานลูปสแกนหลัก...")
    validate_environment()
    
    # ดึงข่าวมารอไว้ก่อนเพื่อเลี่ยงสแปมข่าวเก่าตอนกดเปิด
    initialize_seen_news()

    print("📲 ส่งข้อความแจ้งเปิดบอท...")
    send_telegram_message(
        "✅ <b>Gold News Bot V2 (Proxy Anti-503 Enabled)</b>\n\n"
        "🥇 มอนิเตอร์ตลาดทองคำและนโยบายสหรัฐฯ:\n"
        "1. Trump & Fed Dynamics\n"
        "2. Interest Rates & FOMC Insights\n"
        "3. Gold/XAUUSD Macro Focus\n"
        "4. Global Trade & Tariffs Impact\n\n"
        "🤖 ระบบหลบเลี่ยงการบล็อกทำงานอัตโนมัติ 24 ชั่วโมง"
    )

    while True:
        try:
            print(f"\n🔄 ตรวจสอบข่าวเวลา {time.strftime('%Y-%m-%d %H:%M:%S')}")
            entries = fetch_latest_news()

            for entry in reversed(entries):
                title = entry.get("title", "ไม่มีหัวข้อ")
                description = entry.get("description", "")
                link = entry.get("link", "")
                source = get_news_source(entry)

                news_id = link if link else title.lower().strip()

                with seen_news_lock:
                    already_seen = (news_id in seen_news)

                if already_seen:
                    continue

                if not is_relevant_news(title, description):
                    with seen_news_lock:
                        seen_news.add(news_id)
                    continue

                print(f"\n📰 พบข่าวใหม่และตรงเงื่อนไข: {title}")
                print(f"🏢 Source: {source}")

                summary = summarize_with_groq(title, description, source)

                # ทำความสะอาดข้อความกัน HTML พัง
                safe_title = html.escape(title)
                safe_source = html.escape(source)
                safe_summary = html.escape(summary)
                safe_link = html.escape(link, quote=True)

                message = (
                    "🚨 <b>GOLD MARKET UPDATE</b>\n\n"
                    f"📰 <b>หัวข้อ:</b>\n{safe_title}\n\n"
                    f"🏢 <b>แหล่งข่าว:</b> {safe_source}\n\n"
                    f"🤖 <b>AI วิเคราะห์:</b>\n{safe_summary}\n\n"
                )

                if link:
                    message += f"🔗 <a href=\"{safe_link}\">อ่านข่าวต้นฉบับ</a>"

                success = send_telegram_message(message)

                if success:
                    with seen_news_lock:
                        seen_news.add(news_id)
                    print("✅ ส่งข่าวเรียบร้อย")
                else:
                    print("⚠️ ส่งข้อความไม่สำเร็จ จะทบไปพยายามใหม่รอบหน้า")

                time.sleep(3)

            print(f"😴 รอ {CHECK_INTERVAL} วินาที ก่อนเริ่มต้นรอบถัดไป...")
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"❌ Error เกิดข้อผิดพลาดในลูปบอทหลัก: {e}")
            time.sleep(60)


# =========================================================
# LAZY INITIALIZATION 
# =========================================================

@app.before_request
def start_background_bot_safely():
    global bg_started
    if not bg_started:
        with bg_lock:
            if not bg_started:
                print("🎬 [Flask Init] ตรวจพบเว็บทราฟฟิกแรก เริ่มต้น Thread บอทข่าวในกระบวนการที่เสถียร...")
                bot_thread = Thread(target=bot_loop, daemon=True)
                bot_thread.start()
                bg_started = True


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(
        host="0.0.0.0",
        port=port,
    )
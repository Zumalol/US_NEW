import time
import requests
import os
import feedparser
from threading import Thread
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ================= ตั้งค่าระบบคลาวด์ =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama3-8b-8192" 

RSS_FEED_URL = 'https://www.fxstreet.com/rss/news'
seen_news = set()

TARGET_KEYWORDS = [
    'gold', 'xau', 'xauusd', 'ทอง', 
    'trump', 'ทรัมป์', 
    'fed', 'fomc', 'powell', 'jerome', 'federal reserve', 'interest rate'
]

app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 บอทข่าวทองคำทำงานอยู่ปกติ 24/7!"

# =========================================================

def is_relevant_news(title, description):
    text_to_check = (title + " " + description).lower()
    for keyword in TARGET_KEYWORDS:
        if keyword in text_to_check:
            return True
    return False

def fetch_latest_news():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(RSS_FEED_URL, headers=headers, timeout=10)
        feed = feedparser.parse(response.content)
        return feed.entries
    except Exception as e:
        print(f"❌ Error fetching news: {e}")
        return []

def summarize_with_groq(news_title, news_description):
    print("⏳ กำลังให้ Groq (Llama3) สรุปข่าว...")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = (
        f"คุณคือนักวิเคราะห์ตลาดทองคำระดับโลก ช่วยสรุปข่าวนี้เป็น 'ภาษาไทย' สั้นๆ กระชับเข้าใจง่าย "
        f"และวิเคราะห์เจาะจงเลยว่าข่าวนี้ (โดยเฉพาะถ้าเกี่ยวกับนโยบาย Fed หรือ Trump) "
        f"'จะส่งผลกระทบต่อราคาทองคำ (Gold/XAUUSD)' ในทิศทางใด (พุ่งขึ้น, ร่วงลง, หรือไม่แน่นอน เพราะอะไร)\n\n"
        f"หัวข้อข่าว: {news_title}\n"
        f"รายละเอียด: {news_description}"
    )
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }
    try:
        response = requests.post(GROQ_URL, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].strip()
        else:
            return f"❌ Groq Error: {response.text}"
    except Exception as e:
        return f"❌ ไม่สามารถเชื่อมต่อกับ Groq ได้: {e}"

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload)
        print(f"📡 ส่งข้อความไป Telegram ผลลัพธ์ HTTP: {res.status_code}")
    except Exception as e:
        print(f"❌ Error ระบบตอนส่ง Telegram: {e}")

def bot_loop():
    """ลูปเช็คข่าวสาร"""
    print("🚀 [Background] บอทข่าวเริ่มสตาร์ทลูปค้นหาข่าวแล้ว...")
    
    initial_entries = fetch_latest_news()
    if len(initial_entries) > 0:
        for entry in initial_entries[1:]: 
            seen_news.add(entry.link)
            
    print("📲 กำลังส่งข้อความเปิดบอทเข้า Telegram...")
    send_telegram_message("✅ <b>บอทข่าวทองคำ (Gold/Fed/Trump) เริ่มทำงานบน Cloud แล้ว!</b> กำลังเฝ้าตลาดให้คุณปกติ 24 ชั่วโมง...")

    while True:
        try:
            print(f"🔄 ตรวจสอบข่าวเวลา {time.strftime('%H:%M:%S')}")
            entries = fetch_latest_news()
            
            for entry in reversed(entries):
                if entry.link not in seen_news:
                    title = entry.get('title', 'ไม่มีหัวข้อ')
                    description = entry.get('description', '')
                    link = entry.get('link', '')
                    
                    if not is_relevant_news(title, description):
                        seen_news.add(entry.link)
                        continue
                    
                    print(f"\n📰 พบข่าวใหม่: {title}")
                    summary = summarize_with_groq(title, description)
                    
                    message = (
                        f"🚨 <b>Gold Market Update</b>\n\n"
                        f"<b>หัวข้อ:</b> {title}\n\n"
                        f"🤖 <b>วิเคราะห์ผลกระทบทองคำ:</b>\n{summary}\n\n"
                        f"🔗 <a href='{link}'>อ่านข่าวต้นฉบับ</a>"
                    )
                    
                    send_telegram_message(message)
                    seen_news.add(entry.link)
                    time.sleep(2)
            
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"❌ Error ในลูปบอท: {e}")
            time.sleep(60)

# ================= ย้ายมาไว้ตรงนี้เพื่อให้ Gunicorn รันบอททันทีตอนเริ่มสคริปต์ =================
bot_thread = Thread(target=bot_loop)
bot_thread.daemon = True
bot_thread.start()
# =================================================================================

if __name__ == "__main__":
    # บรรทัดนี้จะทำงานเฉพาะตอนรันในคอมตัวเอง (python main.py) เท่านั้น
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
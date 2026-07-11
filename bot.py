import time
import requests
import os
import feedparser
from dotenv import load_dotenv  # เพิ่มไลบรารีสำหรับอ่านไฟล์ .env

# ================= โหลดค่าจาก Environment (Configuration) =================
# สั่งให้ระบบโหลดไฟล์ .env เข้ามาในโปรแกรม
load_dotenv()

# ดึงค่าจากไฟล์ .env มาใช้งาน
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

# Ollama Endpoint
OLLAMA_URL = 'http://localhost:11434/api/generate'

# RSS Feed ของ FXStreet
RSS_FEED_URL = 'https://www.fxstreet.com/rss/news'

seen_news = set()

# คีย์เวิร์ดสำหรับกรองข่าว
TARGET_KEYWORDS = [
    'gold', 'xau', 'xauusd', 'ทอง', 
    'trump', 'ทรัมป์', 
    'fed', 'fomc', 'powell', 'jerome', 'federal reserve', 'interest rate'
]
# =========================================================================

def is_relevant_news(title, description):
    """ตรวจสอบว่าข่าวนี้เกี่ยวกับ Gold, Trump หรือ Fed หรือไม่"""
    text_to_check = (title + " " + description).lower()
    for keyword in TARGET_KEYWORDS:
        if keyword in text_to_check:
            return True
    return False

def fetch_latest_news():
    """ดึงข่าวล่าสุดจาก RSS Feed"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
        response = requests.get(RSS_FEED_URL, headers=headers, timeout=10)
        feed = feedparser.parse(response.content)
        return feed.entries
    except Exception as e:
        print(f"❌ Error fetching news: {e}")
        return []

def summarize_with_ollama(news_title, news_description):
    """ส่งเนื้อหาข่าวไปให้ Ollama สรุป"""
    print("⏳ กำลังให้ Ollama สรุปข่าว...")
    prompt = (
        f"คุณคือนักวิเคราะห์ตลาดทองคำระดับโลก ช่วยสรุปข่าวนี้เป็น 'ภาษาไทย' สั้นๆ กระชับเข้าใจง่าย "
        f"และวิเคราะห์เจาะจงเลยว่าข่าวนี้ (โดยเฉพาะถ้าเกี่ยวกับนโยบาย Fed หรือ Trump) "
        f"'จะส่งผลกระทบต่อราคาทองคำ (Gold/XAUUSD)' ในทิศทางใด (พุ่งขึ้น, ร่วงลง, หรือไม่แน่นอน เพราะอะไร)\n\n"
        f"หัวข้อข่าว: {news_title}\n"
        f"รายละเอียด: {news_description}"
    )
    
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }
    
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        if response.status_code == 200:
            return response.json().get('response', '').strip()
        else:
            return f"❌ Ollama Error: {response.status_code}"
    except Exception as e:
        return f"❌ ไม่สามารถเชื่อมต่อกับ Ollama ได้ ตรวจสอบว่าเปิด Ollama ไว้หรือไม่ (Error: {e})"

def send_telegram_message(text):
    """ส่งข้อความเข้า Telegram และเช็คสถานะว่าส่งผ่านหรือไม่"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print("✅ ส่งข้อความเข้า Telegram สำเร็จ!")
        else:
            print(f"❌ ส่งเข้า Telegram ไม่สำเร็จ! (HTTP {response.status_code})")
            print(f"👉 สาเหตุจาก Telegram: {response.text}")
    except Exception as e:
        print(f"❌ Error ระบบตอนส่ง Telegram: {e}")

def main():
    print("🤖 เริ่มทำงาน: บอทตรวจสอบข่าว Gold, Fed, และ Trump (ระบบโหลดไฟล์ .env เรียบร้อย)")
    
    if not TELEGRAM_TOKEN:
        print("❌ ไม่พบ TELEGRAM_TOKEN ในไฟล์ .env กรุณาตรวจสอบความถูกต้อง")
        return
    if not TELEGRAM_CHAT_ID:
        print("❌ ไม่พบ TELEGRAM_CHAT_ID ในไฟล์ .env กรุณาตรวจสอบความถูกต้อง")
        return

    print("📲 กำลังส่งข้อความทดสอบเข้า Telegram...")
    send_telegram_message("✅ <b>บอทข่าวทองคำ (Gold/Fed/Trump) เริ่มทำงานแล้ว!</b> ระบบดาวน์โหลด Env สำเร็จ กำลังสแตนด์บายรอข่าวใหม่...")
    
    initial_entries = fetch_latest_news()
    
    if len(initial_entries) > 0:
        for entry in initial_entries[1:]: 
            seen_news.add(entry.link)
    
    print(f"✅ โหลดประวัติข่าวเริ่มต้น {len(seen_news)} ข่าวเรียบร้อยแล้ว บอทกำลังสแตนด์บาย...")

    while True:
        try:
            print(f"🔄 กำลังตรวจสอบข่าวใหม่เวลา {time.strftime('%H:%M:%S')}")
            entries = fetch_latest_news()
            
            for entry in reversed(entries):
                if entry.link not in seen_news:
                    title = entry.get('title', 'ไม่มีหัวข้อ')
                    description = entry.get('description', '')
                    link = entry.get('link', '')
                    
                    if not is_relevant_news(title, description):
                        seen_news.add(entry.link)
                        continue
                    
                    print(f"\n📰 พบข่าวใหม่ที่ตรงเงื่อนไข: {title}")
                    
                    summary = summarize_with_ollama(title, description)
                    
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
            
        except KeyboardInterrupt:
            print("\n🛑 ปิดการทำงานบอท")
            break
        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดในระบบหลัก: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
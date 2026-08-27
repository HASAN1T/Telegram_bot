import logging
import os
import tempfile
import asyncio
from urllib.parse import urljoin, urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
import yt_dlp
import requests
from playwright.async_api import async_playwright

# --- الإعدادات ---
BOT_TOKEN =  os.getenv("BOT_TOKEN")  # ← ضع توكن بوتك هنا
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# تخزين جلسة المستخدم
USER_DATA = {}

# --- تسجيل الأخطاء ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# --- دالة التحقق من نوع الرابط ---
def get_media_type(url: str):
    url = url.lower()
    if any(url.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif')):
        return 'image'
    if url.endswith('.pdf'):
        return 'pdf'
    return None

# --- استخراج الصور وPDF من صفحة HTML ---
async def extract_media_from_page(url: str, timeout: int = 15):
    media = {'images': [], 'pdfs': []}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            await page.wait_for_timeout(3000)

            # استخراج الصور
            img_elements = await page.query_selector_all("img")
            for img in img_elements:
                src = await img.get_attribute("src") or await img.get_attribute("data-src")
                if src:
                    full_url = urljoin(url, src)
                    if get_media_type(full_url) == 'image':
                        media['images'].append(full_url)

            # استخراج روابط PDF
            link_elements = await page.query_selector_all("a[href]")
            for link in link_elements:
                href = await link.get_attribute("href")
                if href and href.lower().endswith('.pdf'):
                    full_url = urljoin(url, href)
                    media['pdfs'].append(full_url)

            await browser.close()
    except Exception as e:
        logging.error(f"Playwright error: {e}")
    # إزالة التكرارات وتحديد حد أقصى
    media['images'] = list(dict.fromkeys(media['images']))[:10]
    media['pdfs'] = list(dict.fromkeys(media['pdfs']))[:5]
    return media

# --- الأوامر ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً! أرسل رابط:\n"
        "• فيديو (YouTube, TikTok, Instagram...)\n"
        "• صورة مباشرة (.jpg, .png...)\n"
        "• ملف PDF مباشر (.pdf)\n"
        "• أو صفحة ويب تحتوي على صور/PDF"
    )

# --- معالجة الرسائل ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user_id = update.effective_user.id

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("يرجى إرسال رابط صحيح يبدأ بـ http:// أو https://")
        return

    msg = await update.message.reply_text("جاري التحليل...")

    # --- حالة 1: صورة مباشرة ---
    if get_media_type(url) == 'image':
        await msg.edit_text("جارٍ تنزيل الصورة...")
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            if len(resp.content) > MAX_FILE_SIZE:
                await msg.edit_text("الصورة كبيرة جدًا (أكثر من 50 ميجابايت).")
                return
            await update.message.reply_photo(photo=resp.content)
        except Exception as e:
            await msg.edit_text(f"فشل التنزيل: {str(e)}")
        return

    # --- حالة 2: PDF مباشر ---
    if get_media_type(url) == 'pdf':
        await msg.edit_text("جارٍ تنزيل ملف PDF...")
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            if len(resp.content) > MAX_FILE_SIZE:
                await msg.edit_text("ملف PDF كبير جدًا (أكثر من 50 ميجابايت).")
                return
            await update.message.reply_document(document=resp.content, filename="document.pdf")
        except Exception as e:
            await msg.edit_text(f"فشل التنزيل: {str(e)}")
        return

    # --- حالة 3: محاولة yt-dlp (فيديو/صوت) ---
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'noplaylist': True}) as ydl:
            info = ydl.extract_info(url, download=False)
        has_audio = info.get('acodec') != 'none' or info.get('vcodec') != 'none'
        keyboard = [[InlineKeyboardButton("🎥 تنزيل الفيديو", callback_data="video")]]
        if has_audio:
            keyboard[0].append(InlineKeyboardButton("🎵 تنزيل الصوت", callback_data="audio"))
        USER_DATA[user_id] = {"last_url": url}
        await msg.edit_text("اختر التنسيق:", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    except Exception:
        pass  # الموقع غير مدعوم → جرّب كصفحة HTML

    # --- حالة 4: صفحة HTML تحتوي صور/PDF ---
    await msg.edit_text("جارٍ فحص الصفحة لاستخراج الصور وملفات PDF...")
    try:
        media = await extract_media_from_page(url)
        images = media['images']
        pdfs = media['pdfs']
        if not images and not pdfs:
            await msg.edit_text("لم يتم العثور على صور أو ملفات PDF في هذه الصفحة.")
            return

        USER_DATA[user_id] = {
            "last_url": url,
            "extracted_images": images,
            "extracted_pdfs": pdfs
        }

        buttons = []
        for i, pdf_url in enumerate(pdfs):
            buttons.append([InlineKeyboardButton(f"📄 PDF {i+1}", callback_data=f"pdf_{i}")])
        for i, img_url in enumerate(images[:5]):  # أول 5 صور
            buttons.append([InlineKeyboardButton(f"🖼️ صورة {i+1}", callback_data=f"img_{i}")])

        await msg.edit_text(
            f"تم العثور على {len(pdfs)} ملفات PDF و{len(images)} صور.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        await msg.edit_text(f"فشل تحليل الصفحة: {str(e)}")

# --- معالجة الأزرار ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    user_data = USER_DATA.get(user_id)
    if not user_data:
        await query.edit_message_text("انتهت الجلسة. أعد إرسال الرابط.")
        return

    url = user_data["last_url"]

    # --- تنزيل PDF ---
    if data.startswith("pdf_"):
        await query.edit_message_text("جارٍ تنزيل ملف PDF...")
        try:
            idx = int(data.split("_")[1])
            pdf_url = user_data["extracted_pdfs"][idx]
            resp = requests.get(pdf_url, timeout=20)
            resp.raise_for_status()
            if len(resp.content) > MAX_FILE_SIZE:
                await query.message.reply_text("ملف PDF كبير جدًا (أكثر من 50 ميجابايت).")
            else:
                await query.message.reply_document(document=resp.content, filename="document.pdf")
        except Exception as e:
            await query.message.reply_text(f"فشل تنزيل PDF: {str(e)[:150]}")
        return

    # --- تنزيل صورة ---
    if data.startswith("img_"):
        await query.edit_message_text("جارٍ تنزيل الصورة...")
        try:
            idx = int(data.split("_")[1])
            img_url = user_data["extracted_images"][idx]
            resp = requests.get(img_url, timeout=20)
            resp.raise_for_status()
            if len(resp.content) > MAX_FILE_SIZE:
                await query.message.reply_text("الصورة كبيرة جدًا.")
            else:
                await query.message.reply_photo(photo=resp.content)
        except Exception as e:
            await query.message.reply_text(f"فشل تنزيل الصورة: {str(e)[:150]}")
        return

    # --- تنزيل فيديو/صوت ---
    await query.edit_message_text("جارٍ التنزيل...")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            if data == "audio":
                ydl_opts = {
                    'outtmpl': os.path.join(temp_dir, '%(title).50s.%(ext)s'),
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '128',
                    }],
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'nocheckcertificate': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    # 'ffmpeg_location': r'C:\ffmpeg\bin',  # فعّله إذا لزم الأمر
                }
            else:  # video
                ydl_opts = {
                    'outtmpl': os.path.join(temp_dir, '%(title).50s.%(ext)s'),
                    'format': 'bestvideo+bestaudio/best',
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'merge_output_format': 'mp4',
                    'nocheckcertificate': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    # 'ffmpeg_location': r'C:\ffmpeg\bin',
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
                if data == "audio":
                    file_path = os.path.splitext(file_path)[0] + '.mp3'

            if not os.path.exists(file_path):
                raise FileNotFoundError("فشل إنشاء الملف.")

            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                await query.edit_message_text("الملف كبير جدًا (أكثر من 50 ميجابايت).")
                return

            await query.edit_message_text("جارٍ الرفع...")
            with open(file_path, 'rb') as f:
                if data == "audio":
                    await query.message.reply_audio(audio=f)
                else:
                    await query.message.reply_video(video=f)
    except Exception as e:
        await query.edit_message_text(f"فشل التنزيل: {str(e)[:200]}")

# --- التشغيل ---
def main():
    # إنشاء التطبيق
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # تشغيل كـ Webhook
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    
    if RENDER_EXTERNAL_URL:
        # تنظيف الرابط من أي أحرف غير مرغوب فيها
        if RENDER_EXTERNAL_URL.startswith("https://"):
            RENDER_EXTERNAL_URL = RENDER_EXTERNAL_URL[8:]
        if RENDER_EXTERNAL_URL.startswith("http://"):
            RENDER_EXTERNAL_URL = RENDER_EXTERNAL_URL[7:]
        if RENDER_EXTERNAL_URL.endswith("/"):
            RENDER_EXTERNAL_URL = RENDER_EXTERNAL_URL[:-1]
            
        webhook_url = f"https://{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
        print(f"🔍 Final Webhook URL: {webhook_url}")
        
        # تحقق من صحة الرابط
        if " " in webhook_url:
            print("❌ ERROR: Webhook URL contains spaces!")
            return
            
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 8000)),
            url_path=BOT_TOKEN,
            webhook_url=webhook_url
        )
    else:
        print("⚠️ RENDER_EXTERNAL_URL not set! Falling back to polling...")
        app.run_polling()

if __name__ == "__main__":
    main()
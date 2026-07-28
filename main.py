# main.py
# Telegram Multi Downloader Bot - نسخه اصلاح‌شده (v2: yt-dlp + ارسال فایل به گروه ادمین)

import os
import re
import json
import uuid
import logging
import tempfile
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

try:
    import jdatetime
except ImportError:
    jdatetime = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# ============================================
# تنظیمات (از متغیرهای محیطی خوانده می‌شوند - هرگز مقدار مستقیم اینجا ننویسید)
# ============================================
TOKEN = os.environ.get('TOKEN')
ADMIN_GROUP_ID = os.environ.get('ADMIN_GROUP_ID')  # سوپرگروه باید با -100 شروع شود
CHANNEL_ID = os.environ.get('CHANNEL_ID', '@my_channel')
BOT_NAME = os.environ.get('BOT_NAME', 'دانلودر شوگوت')
BOT_USERNAME = os.environ.get('BOT_USERNAME', '')  # بدون @ - برای لینک عمیق ربات در کپشن، مثل instatoolboxbot
PORT = int(os.environ.get('PORT', 8080))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')

if not TOKEN:
    raise RuntimeError(
        "متغیر محیطی TOKEN تنظیم نشده است. توکن را هرگز مستقیم در کد ننویسید - "
        "آن را در تنظیمات Environment Variables سرویس میزبان (مثلاً Railway) اضافه کنید."
    )

# ============================================
# لاگ
# ============================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================
# حافظه ساده (فقط برای اجرای تک-پروسه/تک-worker مناسب است)
# ============================================
memory = {}

def set_memory(chat_id, data):
    memory[str(chat_id)] = data

def get_memory(chat_id):
    return memory.get(str(chat_id))

def delete_memory(chat_id):
    key = str(chat_id)
    if key in memory:
        del memory[key]

# ============================================
# زمان تهران - محلی، بدون تماس شبکه‌ای
# ============================================
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

# نام ماه‌ها را دستی نگه می‌داریم چون %B در jdatdatetime بدون تنظیم locale
# ممکن است نام انگلیسی/تلفظی (Farvardin) برگرداند نه نام فارسی
PERSIAN_MONTHS = [
    'فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
    'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند'
]

def get_tehran_datetime():
    return datetime.now(timezone.utc) + TEHRAN_OFFSET

RLM = '\u200f'  # نشانه‌ی راست‌به‌چپ - فقط برای راست‌چین‌کردن خط تاریخ/ساعت استفاده می‌شود

def get_current_time():
    dt = get_tehran_datetime()
    if jdatetime:
        j = jdatetime.datetime.fromgregorian(datetime=dt.replace(tzinfo=None))
        month_name = PERSIAN_MONTHS[j.month - 1]
        # نشانه‌ی نامرئی راست‌به‌چپ (RLM) فقط همینجا اضافه می‌شود تا فقط همین
        # خط تاریخ/ساعت راست‌چین شود، بدون تأثیر روی بقیه‌ی پیام
        return f"{RLM}{j.day} {month_name} {j.year}، ساعت {dt.strftime('%H:%M:%S')}"
    return f"{RLM}" + dt.strftime('%Y-%m-%d، ساعت %H:%M:%S') + ' (میلادی - jdatetime نصب نیست)'

# ============================================
# توابع کمکی
# ============================================
def extract_youtube_id(url):
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([^&]+)',
        r'youtube\.com\/embed\/([^\/]+)',
        r'youtube\.com\/v\/([^\/]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def extract_instagram_id(url):
    patterns = [
        r'instagram\.com\/(?:p|reel|tv)\/([^\/?]+)',
        r'instagram\.com\/p\/([^\/]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def cleanup_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"Could not remove temp file {path}: {e}")

def build_caption(meta):
    """
    کپشن را به‌شکل: متن کپشن پست / خط خالی / لینک پست / لینک ربات می‌سازد -
    مثل نمونه‌ای که خواسته شده بود.
    meta: {'caption': متن کپشن اصلی پست, 'username': نام کاربری صاحب پست, 'url': لینک پست}
    """
    parts = []

    caption_text = (meta.get('caption') or '').strip()
    if caption_text:
        # محدودیت کپشن ویدیو در تلگرام ۱۰۲۴ کاراکتر است؛ کمی جا برای لینک‌ها نگه می‌داریم
        if len(caption_text) > 900:
            caption_text = caption_text[:900].rstrip() + '…'
        parts.append(caption_text)
        parts.append('')

    username = (meta.get('username') or '').strip()
    post_url = (meta.get('url') or '').strip()
    if username and post_url:
        parts.append(f'🔗 <a href="{post_url}">{username}</a>')
    elif post_url:
        parts.append(f'🔗 {post_url}')

    if BOT_USERNAME:
        parts.append('')
        bot_link = f'https://t.me/{BOT_USERNAME}?start=welcome'
        parts.append(f'📲 <a href="{bot_link}">@{BOT_USERNAME}</a>')

    return '\n'.join(parts) if parts else '✅ دانلود موفق'

# ============================================
# توابع دانلود
# ============================================

DOWNLOAD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

def download_tiktok(url):
    """
    تیک‌تاک همچنان از tikwm استفاده می‌کند.
    خروجی: (video_url, meta) - meta شامل کپشن و نام‌کاربری صاحب پست است.
    """
    try:
        response = requests.get(
            f'https://www.tikwm.com/api/?url={url}',
            timeout=15,
            headers=DOWNLOAD_HEADERS
        )
        data = response.json()
        if data.get('code') == 0 and data.get('data'):
            d = data['data']
            video_url = d.get('play') or d.get('wmplay') or d.get('hdplay')
            if video_url:
                author = d.get('author') or {}
                meta = {
                    'caption': d.get('title') or '',
                    'username': author.get('unique_id') or author.get('nickname') or '',
                    'url': url,
                }
                return video_url, meta

        response = requests.get(
            f'https://tikmate.online/api/j/convert?url={url}',
            timeout=15,
            headers=DOWNLOAD_HEADERS
        )
        data = response.json()
        if data.get('video_url'):
            return data['video_url'], {'caption': '', 'username': '', 'url': url}

        raise Exception('ویدیو پیدا نشد')
    except Exception as e:
        logger.error(f"TikTok error: {e}")
        raise Exception("خطا در دانلود تیک‌تاک")

def _ytdlp_download(source_url, quality_num=None):
    """
    دانلود با yt-dlp روی یک فایل محلی موقت.
    فرمت طوری انتخاب می‌شود که یک فایل mp4 آماده (بدون نیاز به merge با ffmpeg) بگیرد،
    چون سرور ممکن است ffmpeg نصب نداشته باشد.
    خروجی: مسیر فایل دانلودشده روی دیسک.
    """
    if yt_dlp is None:
        raise Exception('کتابخانه yt-dlp نصب نیست')

    out_dir = tempfile.gettempdir()
    out_template = os.path.join(out_dir, f"dl_{uuid.uuid4().hex}.%(ext)s")

    if quality_num:
        fmt = f'best[height<={quality_num}][ext=mp4]/best[height<={quality_num}]/best[ext=mp4]/best'
    else:
        fmt = 'best[ext=mp4]/best'

    ydl_opts = {
        'format': fmt,
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'max_filesize': 49 * 1024 * 1024,  # محدودیت آپلود بات تلگرام تقریباً ۵۰ مگابایت است
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            raise Exception('فایل دانلودشده پیدا نشد')
        meta = {
            'caption': (info.get('description') or info.get('title') or '').strip(),
            'username': info.get('uploader') or info.get('channel') or info.get('uploader_id') or '',
            'url': info.get('webpage_url') or source_url,
        }
        return filename, meta

def download_youtube_file(youtube_url, quality):
    """دانلود یوتیوب با yt-dlp - (مسیر فایل محلی, متادیتای کپشن/کاربر) را برمی‌گرداند."""
    quality_num = re.sub(r'\D', '', quality) or None
    try:
        return _ytdlp_download(youtube_url, quality_num)
    except Exception as e:
        logger.error(f"Youtube (yt-dlp) error: {e}")
        # پیام خطای واقعی yt-dlp را کوتاه‌شده نشان می‌دهیم تا علت دقیق مشخص شود
        raise Exception(f"خطا در دانلود یوتیوب — جزئیات: {str(e)[:300]}")

def download_instagram_file(url):
    """دانلود اینستاگرام با yt-dlp - (مسیر فایل محلی, متادیتای کپشن/کاربر) را برمی‌گرداند."""
    try:
        return _ytdlp_download(url)
    except Exception as e:
        logger.error(f"Instagram (yt-dlp) error: {e}")
        raise Exception(f"خطا در دانلود اینستاگرام — جزئیات: {str(e)[:300]}")

# ============================================
# توابع ارسال به تلگرام
# ============================================
def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        response = requests.post(url, data=data, timeout=10)
        return response.json()
    except Exception as e:
        logger.error(f"Send message error: {e}")
        return None

def edit_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    data = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        response = requests.post(url, data=data, timeout=10)
        return response.json()
    except Exception as e:
        logger.error(f"Edit message error: {e}")
        return None

def send_video_by_url(chat_id, video_url, caption, reply_markup=None):
    """برای مواردی که فقط لینک مستقیم ویدیو داریم (مثلاً تیک‌تاک)."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendVideo"
    data = {
        'chat_id': chat_id,
        'video': video_url,
        'caption': caption,
        'parse_mode': 'HTML',
        'supports_streaming': True
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        response = requests.post(url, data=data, timeout=60)
        return response.json()
    except Exception as e:
        logger.error(f"Send video by url error: {e}")
        return None

def send_video_file(chat_id, file_path, caption, reply_markup=None):
    """برای فایل‌های دانلودشده محلی (یوتیوب/اینستاگرام با yt-dlp) - آپلود مستقیم."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendVideo"
    data = {
        'chat_id': chat_id,
        'caption': caption,
        'parse_mode': 'HTML',
        'supports_streaming': True
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        with open(file_path, 'rb') as f:
            files = {'video': f}
            response = requests.post(url, data=data, files=files, timeout=120)
        return response.json()
    except Exception as e:
        logger.error(f"Send video file error: {e}")
        return None

def forward_message(to_chat_id, from_chat_id, message_id):
    """پیام ویدیوی ارسال‌شده به کاربر را بدون آپلود دوباره، به گروه ادمین forward می‌کند."""
    url = f"https://api.telegram.org/bot{TOKEN}/forwardMessage"
    data = {'chat_id': to_chat_id, 'from_chat_id': from_chat_id, 'message_id': message_id}
    try:
        response = requests.post(url, data=data, timeout=30)
        return response.json()
    except Exception as e:
        logger.error(f"Forward message error: {e}")
        return None

def notify_admin_group(video_send_result, source_chat_id, extra_text):
    """پیام متنی گزارش را می‌فرستد و در صورت موفقیت، خود ویدیو را هم به گروه ادمین forward می‌کند."""
    if not ADMIN_GROUP_ID:
        return
    send_message(ADMIN_GROUP_ID, extra_text)
    if video_send_result and video_send_result.get('ok'):
        video_message_id = video_send_result['result']['message_id']
        forward_message(ADMIN_GROUP_ID, source_chat_id, video_message_id)

def answer_callback(callback_id):
    url = f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery"
    try:
        response = requests.post(url, data={'callback_query_id': callback_id}, timeout=5)
        return response.json()
    except Exception:
        return None

def delete_message(chat_id, message_id):
    url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    try:
        response = requests.post(url, data={'chat_id': chat_id, 'message_id': message_id}, timeout=5)
        return response.json()
    except Exception:
        return None

# ============================================
# کیبوردها
# ============================================
def get_keyboard():
    return {
        'inline_keyboard': [
            [
                {'text': '🎵 TikTok', 'callback_data': 'download_TikTok'},
                {'text': '🎬 Youtube', 'callback_data': 'download_Youtube'},
            ],
            [
                {'text': '📸 Instagram', 'callback_data': 'download_Instagram'},
                {'text': '📢 کانال', 'callback_data': 'channel'},
            ],
        ]
    }

def get_back_keyboard():
    return {'inline_keyboard': [[{'text': '🔙 بازگشت', 'callback_data': 'main_menu'}]]}

def get_quality_keyboard(youtube_id):
    qualities = ['720p', '480p', '360p', '240p']
    return {
        'inline_keyboard': [
            [{'text': f'📺 {q}', 'callback_data': f'quality_{youtube_id}_{q}'} for q in qualities[:2]],
            [{'text': f'📺 {q}', 'callback_data': f'quality_{youtube_id}_{q}'} for q in qualities[2:]],
            [{'text': '🔙 بازگشت', 'callback_data': 'main_menu'}]
        ]
    }

# ============================================
# هندلرها
# ============================================
def handle_start(chat_id):
    current_time = get_current_time()
    message = f"""👋 به <b>{BOT_NAME}</b> خوش آمدید!

🎯 این ربات به شما کمک می‌کند تا ویدیوهای مورد نظر خود را دانلود کنید:

🎵 TikTok
🎬 Youtube (با انتخاب کیفیت)
📸 Instagram (پست و ریلز)

📌 از منوی زیر یکی را انتخاب کنید.

⏰ {current_time}"""
    send_message(chat_id, message, get_keyboard())

def handle_callback(data, chat_id, message_id, callback_id):
    answer_callback(callback_id)

    if data == 'main_menu':
        handle_start(chat_id)
        return

    if data == 'channel':
        current_time = get_current_time()
        message = f"""📢 <b>کانال رسمی {BOT_NAME}</b>

🔗 {CHANNEL_ID}

📱 منتظر شما هستیم! 🎬

⏰ {current_time}"""
        edit_message(chat_id, message_id, message, get_back_keyboard())
        return

    if data.startswith('quality_'):
        parts = data.split('_')
        youtube_id = parts[1]
        quality = parts[2]

        state = get_memory(chat_id)
        if not state:
            send_message(chat_id, '⚠️ نشست منقضی شده. لطفاً دوباره از منو شروع کنید.', get_keyboard())
            return

        url = state.get('link', '')

        processing_msg = send_message(chat_id, f'⏳ در حال دانلود با کیفیت {quality}...', None)
        processing_msg_id = processing_msg['result']['message_id'] if processing_msg and processing_msg.get('ok') else None

        file_path = None
        try:
            file_path, meta = download_youtube_file(url, quality)
            caption = build_caption(meta)
            video_result = send_video_file(chat_id, file_path, caption, get_back_keyboard())

            admin_msg = f"""📤 دانلود جدید

🎯 Youtube
📺 کیفیت: {quality}
👤 chat_id: {chat_id}
📅 {get_current_time()}"""
            notify_admin_group(video_result, chat_id, admin_msg)

            if processing_msg_id:
                delete_message(chat_id, processing_msg_id)

        except Exception as e:
            logger.error(f"Download error: {e}")
            send_message(chat_id, f"❌ خطا: {str(e)}", get_back_keyboard())
        finally:
            cleanup_file(file_path)
            delete_memory(chat_id)
        return

    if data.startswith('download_'):
        platform = data.replace('download_', '')
        set_memory(chat_id, {'platform': platform, 'step': 'waiting_for_link', 'chat_id': chat_id})

        current_time = get_current_time()
        examples = {
            'TikTok': 'https://www.tiktok.com/@user/video/123456789',
            'Youtube': 'https://www.youtube.com/watch?v=VIDEO_ID',
            'Instagram': 'https://www.instagram.com/reel/VIDEO_ID/'
        }
        message = f"""📥 دانلود از <b>{platform}</b>

🔗 لینک ویدیو را ارسال کنید:

مثال:
{examples.get(platform, '')}

⏰ {current_time}"""
        edit_message(chat_id, message_id, message, get_back_keyboard())

def detect_platform(text):
    """پلتفرم را مستقیم از روی خود لینک تشخیص می‌دهد - نیازی به انتخاب قبلی از منو نیست."""
    patterns = {
        'tiktok': r'(https?://)?(www\.)?(tiktok\.com|vm\.tiktok\.com)/\S+',
        'youtube': r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+',
        'instagram': r'(https?://)?(www\.)?(instagram\.com|instagr\.am)/\S+',
    }
    for platform, pattern in patterns.items():
        if re.search(pattern, text, re.I):
            return platform
    return None

def process_link(chat_id, platform, text):
    """دانلود واقعی را بر اساس پلتفرم تشخیص‌داده‌شده انجام می‌دهد."""
    set_memory(chat_id, {'platform': platform, 'link': text, 'step': 'processing', 'chat_id': chat_id})

    if platform == 'youtube':
        youtube_id = extract_youtube_id(text)
        if youtube_id:
            state = get_memory(chat_id) or {}
            state['youtube_id'] = youtube_id
            set_memory(chat_id, state)
            current_time = get_current_time()
            message = f"""🎬 کیفیت مورد نظر را انتخاب کنید:

⏰ {current_time}"""
            send_message(chat_id, message, get_quality_keyboard(youtube_id))
        else:
            send_message(chat_id, '❌ لینک یوتیوب معتبر نیست.', get_back_keyboard())
            delete_memory(chat_id)
        return

    platform_label = {'tiktok': 'TikTok', 'instagram': 'Instagram'}.get(platform, platform)
    msg = send_message(chat_id, f'⏳ در حال دانلود از <b>{platform_label}</b>...', None)
    processing_msg_id = msg['result']['message_id'] if msg and msg.get('ok') else None

    file_path = None
    try:
        video_url = None
        video_result = None

        if platform == 'tiktok':
            video_url, meta = download_tiktok(text)
            if not video_url:
                raise Exception('ویدیو پیدا نشد')
            caption = build_caption(meta)
            video_result = send_video_by_url(chat_id, video_url, caption, get_back_keyboard())

        elif platform == 'instagram':
            file_path, meta = download_instagram_file(text)
            caption = build_caption(meta)
            video_result = send_video_file(chat_id, file_path, caption, get_back_keyboard())

        admin_msg = f"""📤 دانلود جدید

🎯 {platform_label}
🔗 {text}
👤 chat_id: {chat_id}
📅 {get_current_time()}"""
        notify_admin_group(video_result, chat_id, admin_msg)

        if processing_msg_id:
            delete_message(chat_id, processing_msg_id)

    except Exception as e:
        logger.error(f"Download error: {e}")
        send_message(chat_id, f"❌ خطا: {str(e)}", get_back_keyboard())
    finally:
        cleanup_file(file_path)
        delete_memory(chat_id)

def handle_message(chat_id, text):
    # اول همیشه سعی می‌کنیم پلتفرم را مستقیم از روی خود لینک تشخیص بدهیم -
    # چه کاربر تازه /start زده باشد، چه از منو انتخاب کرده باشد، چه دانلود قبلی
    # را همین الان تمام کرده باشد. این یعنی دیگر لازم نیست هر بار از منو
    # پلتفرم را دوباره انتخاب کند.
    platform = detect_platform(text)
    if platform:
        process_link(chat_id, platform, text)
        return

    state = get_memory(chat_id)
    if state and state.get('step') == 'waiting_for_link':
        send_message(chat_id, '❌ لینک معتبر نیست. دوباره تلاش کنید.')
    else:
        send_message(
            chat_id,
            '⚠️ لینک تیک‌تاک، یوتیوب یا اینستاگرام را مستقیم برایم بفرستید، یا از منوی زیر انتخاب کنید.',
            get_keyboard()
        )

# ============================================
# Flask App
# ============================================
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    if WEBHOOK_SECRET:
        incoming_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if incoming_secret != WEBHOOK_SECRET:
            logger.warning("Webhook request with invalid secret token rejected")
            return 'Forbidden', 403

    try:
        data = request.get_json(force=True, silent=True) or {}

        if 'message' in data:
            msg = data['message']
            chat_id = msg['chat']['id']
            if 'text' in msg:
                text = msg['text']
                if text == '/start':
                    handle_start(chat_id)
                else:
                    handle_message(chat_id, text)

        elif 'callback_query' in data:
            cb = data['callback_query']
            chat_id = cb['message']['chat']['id']
            message_id = cb['message']['message_id']
            callback_id = cb['id']
            data_cb = cb['data']
            handle_callback(data_cb, chat_id, message_id, callback_id)

        return 'OK', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': get_current_time()})

@app.route('/time', methods=['GET'])
def get_time():
    return jsonify({'time': get_current_time(), 'api_status': 'success'})

@app.route('/set-webhook', methods=['GET'])
def set_webhook():
    try:
        if not WEBHOOK_URL:
            return jsonify({'error': 'WEBHOOK_URL not set'}), 400

        webhook_url = f"{WEBHOOK_URL}/webhook"
        params = {'url': webhook_url}
        if WEBHOOK_SECRET:
            params['secret_token'] = WEBHOOK_SECRET

        response = requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params=params, timeout=10)
        return jsonify(response.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return jsonify({'name': BOT_NAME, 'status': 'running', 'time': get_current_time()})

# ============================================
# اجرا
# ============================================
if __name__ == '__main__':
    if WEBHOOK_URL:
        try:
            webhook_url = f"{WEBHOOK_URL}/webhook"
            params = {'url': webhook_url}
            if WEBHOOK_SECRET:
                params['secret_token'] = WEBHOOK_SECRET
            response = requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params=params, timeout=10)
            logger.info(f"✅ Webhook set: {response.json()}")
        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
    else:
        logger.warning("⚠️ WEBHOOK_URL not set")

    app.run(host='0.0.0.0', port=PORT)

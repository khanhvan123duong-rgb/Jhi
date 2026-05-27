import os
import sys
import time
import json
import types
import threading
import inspect
import requests
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ─── STUB zlapi & config ───────────────────────────────────────────────────────
class _Message:
    def __init__(self, text='', **kwargs):
        self.text = text
        for k, v in kwargs.items():
            setattr(self, k, v)

_zlapi_pkg    = types.ModuleType('zlapi')
_zlapi_models = types.ModuleType('zlapi.models')
_zlapi_models.Message = _Message
_zlapi_pkg.models     = _zlapi_models
sys.modules['zlapi']        = _zlapi_pkg
sys.modules['zlapi.models'] = _zlapi_models

_config_mod        = types.ModuleType('config')
_config_mod.PREFIX = '/'
sys.modules['config'] = _config_mod

os.makedirs(os.path.join(BASE_DIR, 'modules', 'cache'), exist_ok=True)

# ─── IMPORT TOOLS ─────────────────────────────────────────────────────────────
def safe_import(name):
    try:
        import importlib
        return importlib.import_module(name)
    except Exception as e:
        print(f'[WARN] {name}: {e}')
        return None

scll_mod = safe_import('scll')
tt_mod   = safe_import('searchtiktok')
otp_mod  = safe_import('Otp')

otp_functions = []
if otp_mod:
    otp_functions = [
        (name, fn)
        for name, fn in inspect.getmembers(otp_mod, inspect.isfunction)
        if name.startswith('send_otp_via_')
    ]
    print(f'[OK] {len(otp_functions)} OTP functions')

# ─── CONFIG ───────────────────────────────────────────────────────────────────
from flask import Flask
import telebot

TOKEN      = '8604849365:AAGvRZK_KE9Dqa6nqZoE2vr3Sf--OweJn2Y'
PORT       = int(os.environ.get('PORT', 3000))
SELF_URL   = os.environ.get('RENDER_EXTERNAL_URL', '')
ADMIN_ID   = int(os.environ.get('ADMIN_ID', 8401914033))
KEY_SERVER = 'https://vkhanhdev2026-kvuf.onrender.com'
API_VERIFY = f'{KEY_SERVER}/api/verify'
API_GETKEY = f'{KEY_SERVER}/api/getkey'
MAX_GETKEY_PER_DAY = 3

# ─── WEB SERVER ───────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return 'xin chào tôi là văn Khánh'

@app.route('/ping')
def ping_route():
    return 'pong'

def run_flask():
    app.run(host='0.0.0.0', port=PORT, use_reloader=False, threaded=True)

# ─── BOT ──────────────────────────────────────────────────────────────────────
bot = telebot.TeleBot(TOKEN, parse_mode='HTML')

active_ops   = {}
user_states  = {}
user_keys    = {}   # chat_id -> {"key": str, "exp": float | -1}
getkey_usage = {}   # chat_id -> {"date": str, "count": int}
all_users    = set()

# ─── AUTO XÓA TIN NHẮN ────────────────────────────────────────────────────────
def auto_delete(chat_id, message_id, delay=60):
    def _do():
        time.sleep(delay)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()

# ─── KEY HELPERS ──────────────────────────────────────────────────────────────
def _today():
    return datetime.now().strftime('%Y-%m-%d')

def format_time_remaining(raw_date):
    """
    Phân tích thời hạn key từ server — theo đúng logic file From_Kích_Hoạt.
    raw_date có thể là:
      - None / "" / "null" / "0" / "Vĩnh Viễn" / "lifetime" → Vĩnh Viễn
      - Số Unix timestamp (float/int/str)
      - Chuỗi ISO datetime "2025-06-30T12:00:00"
    Trả về chuỗi tiếng Việt hiển thị cho user.
    """
    if raw_date is None:
        return None  # caller xử lý: None = không có field trong response

    s = str(raw_date).strip()
    if s in ('', 'null', 'None', '0', 'Vĩnh Viễn', 'Vinh Vien',
             'lifetime', 'forever', 'permanent', 'infinity', '-1', '-1.0'):
        return 'Vĩnh Viễn'

    try:
        # Thử parse số (Unix timestamp)
        if s.replace('.', '', 1).replace('-', '', 1).isdigit():
            ts = float(s)
            if ts <= 0:
                return 'Vĩnh Viễn'
            exp_dt = datetime.fromtimestamp(ts)
        else:
            # Chuỗi ISO datetime
            clean = s.replace('T', ' ').replace('Z', '').split('.')[0]
            exp_dt = datetime.fromisoformat(clean)

        now = datetime.now()
        if exp_dt < now:
            return 'Đã hết hạn'

        diff     = exp_dt - now
        days_tot = diff.days
        secs     = diff.seconds
        years    = days_tot // 365
        days     = days_tot % 365
        hours    = secs // 3600
        minutes  = (secs % 3600) // 60

        parts = []
        if years:   parts.append(f'{years} Năm')
        if days:    parts.append(f'{days} Ngày')
        if hours:   parts.append(f'{hours} Giờ')
        if minutes: parts.append(f'{minutes} Phút')
        if not parts:
            return 'Dưới 1 phút'

        expire_str = exp_dt.strftime('%d/%m/%Y %H:%M')
        return ' '.join(parts) + f' (Hết hạn: {expire_str})'

    except Exception:
        return str(raw_date)  # trả về raw nếu parse thất bại

def _exp_timestamp(raw_date):
    """
    Chuyển raw_date → float Unix timestamp để lưu vào user_keys.
    Trả về -1.0 nếu vĩnh viễn.
    """
    if raw_date is None:
        return -1.0
    s = str(raw_date).strip()
    if s in ('', 'null', 'None', '0', '-1', '-1.0', 'Vĩnh Viễn',
             'lifetime', 'forever', 'permanent', 'infinity'):
        return -1.0
    try:
        if s.replace('.', '', 1).replace('-', '', 1).isdigit():
            ts = float(s)
            return ts if ts > 0 else -1.0
        else:
            clean = s.replace('T', ' ').replace('Z', '').split('.')[0]
            dt = datetime.fromisoformat(clean)
            return dt.timestamp()
    except Exception:
        return -1.0

def _fmt_expiry_from_ts(exp_ts):
    """exp_ts = Unix float hoặc -1.0. Trả về chuỗi hiển thị."""
    if exp_ts is None:
        return '❓ Không xác định'
    try:
        exp_ts = float(exp_ts)
    except Exception:
        return '❓ Không xác định'
    if exp_ts == -1.0:
        return '♾️ Vĩnh Viễn'
    now = time.time()
    remaining = exp_ts - now
    if remaining <= 0:
        return '❌ Đã hết hạn'
    days    = int(remaining // 86400)
    hours   = int((remaining % 86400) // 3600)
    minutes = int((remaining % 3600) // 60)
    parts = []
    if days:    parts.append(f'{days} ngày')
    if hours:   parts.append(f'{hours} giờ')
    if minutes: parts.append(f'{minutes} phút')
    if not parts:
        return '⏳ < 1 phút'
    expire_dt = datetime.fromtimestamp(exp_ts).strftime('%d/%m/%Y %H:%M')
    return f'⏳ Còn {" ".join(parts)} (hết {expire_dt})'

def is_activated(chat_id):
    """Admin luôn kích hoạt. User thường phải có key hợp lệ chưa hết hạn."""
    if int(chat_id) == int(ADMIN_ID):
        return True
    info = user_keys.get(chat_id)
    if not info:
        return False
    exp = info.get('exp')
    if exp is None:
        return False
    try:
        exp = float(exp)
    except Exception:
        return False
    if exp == -1.0:
        return True
    return time.time() < exp

def is_admin(chat_id):
    return int(chat_id) == int(ADMIN_ID)

def footer(chat_id=None):
    lines = []
    if chat_id and chat_id in user_keys:
        exp = user_keys[chat_id].get('exp')
        if exp is not None:
            lines.append(f'🔑 {_fmt_expiry_from_ts(exp)}')
    lines.append('👤 Admin: @vkhanh3010')
    return '\n\n<i>' + ' | '.join(lines) + '</i>'

def verify_key_api(key, hwid):
    """
    POST /api/verify  body: {"key": "...", "hwid": "..."}
    
    Theo đúng logic From_Kích_Hoạt.py:
    - Kiểm tra status: success/true/ok/1/approved
    - Lấy expire từ: expire_date / expiration_date / expiry / expired_at / time_left / dev_exp
    
    Trả về: {"status": "success"|"expired"|"invalid"|"error", "exp_ts": float, "exp_str": str, "raw": dict}
    """
    try:
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        r = requests.post(API_VERIFY,
                          json={'key': str(key), 'hwid': str(hwid)},
                          headers=headers, timeout=15)
        raw_text = r.text or ''
        data = {}
        try:
            d = r.json()
            data = d if isinstance(d, dict) else {}
        except Exception:
            pass

        # Log đầy đủ để debug trên Render console
        print(f'[API] POST /api/verify key={key!r} hwid={hwid!r}')
        print(f'[API] HTTP {r.status_code} → {raw_text[:400]}')

        status_val = str(data.get('status', '')).lower()

        # Theo From_Kích_Hoạt: kiểm tra các giá trị success
        if status_val in ('success', 'true', 'ok', '1', 'approved'):
            # Tìm raw_expire theo đúng thứ tự trong From_Kích_Hoạt.py
            raw_expire = (
                data.get('expire_date')
                or data.get('expiration_date')
                or data.get('expiry')
                or data.get('expired_at')
                or data.get('time_left')
                or data.get('dev_exp')       # thêm cả dev_exp phòng khi server dùng
                or data.get('exp')
            )
            print(f'[API] raw_expire field = {raw_expire!r}')

            exp_str = format_time_remaining(raw_expire)
            exp_ts  = _exp_timestamp(raw_expire)

            if exp_str is None:
                # Không có field expiry trong response
                exp_str = 'Vĩnh Viễn (Server không set hạn)'
                exp_ts  = -1.0

            print(f'[API] exp_str={exp_str!r} exp_ts={exp_ts}')
            return {'status': 'success', 'exp_ts': exp_ts, 'exp_str': exp_str, 'raw': data}

        # Kiểm tra đặc biệt "expired"
        msg_val = str(data.get('message', '')).lower()
        if 'expired' in status_val or 'expired' in msg_val or 'hết hạn' in msg_val:
            return {'status': 'expired', 'exp_ts': None, 'exp_str': None, 'raw': data}

        if 'limit' in status_val or 'limit' in msg_val or 'device' in msg_val:
            return {'status': 'device_limit', 'exp_ts': None, 'exp_str': None, 'raw': data}

        # Mọi trường hợp còn lại → invalid
        return {'status': 'invalid', 'exp_ts': None, 'exp_str': None, 'raw': data}

    except Exception as e:
        print(f'[API] verify error: {e}')
        return {'status': 'error', 'exp_ts': None, 'exp_str': None, 'raw': {}}

def notify_admin(text):
    try:
        bot.send_message(ADMIN_ID, text, parse_mode='HTML')
    except Exception:
        pass

def user_info_text(msg):
    u     = msg.from_user
    name  = (u.first_name or '') + (' ' + u.last_name if u.last_name else '')
    uname = f'@{u.username}' if u.username else 'Không có'
    return (
        f'👤 <b>Tên:</b> {name.strip()}\n'
        f'🆔 <b>ID:</b> <code>{u.id}</code>\n'
        f'📛 <b>Username:</b> {uname}'
    )

def require_key_check(cid):
    """True nếu đã có key hợp lệ (hoặc admin). False + gửi thông báo nếu chưa."""
    if is_activated(cid):
        return True
    m = bot.send_message(cid,
        '🔒 <b>BẠN CHƯA KÍCH HOẠT KEY!</b>\n\n'
        '⛔ Không thể dùng lệnh này khi chưa có key.\n\n'
        '📌 <b>Bước 1:</b> /getkey → nhận link key miễn phí\n'
        '📌 <b>Bước 2:</b> <code>/key &lt;key_nhận_được&gt;</code>\n\n'
        '📊 Mỗi ngày được lấy tối đa <b>3 link</b>.'
        f'{footer()}'
    )
    auto_delete(cid, m.message_id, 90)
    return False

def track_user(msg):
    all_users.add(msg.chat.id)

# ─── /start ───────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def cmd_start(msg):
    cid  = msg.chat.id
    track_user(msg)
    name = msg.from_user.first_name or 'bạn'
    m = bot.send_message(cid,
        f'👋 Xin chào <b>{name}</b>!\n\n'
        f'🤖 Bot đa năng của <b>Văn Khánh</b>\n'
        f'Hỗ trợ: OTP · TikTok · SoundCloud\n\n'
        f'🔒 <b>Mọi lệnh đều cần kích hoạt KEY!</b>\n\n'
        f'  1️⃣ Lấy link key: /getkey\n'
        f'  2️⃣ Kích hoạt: <code>/key &lt;key&gt;</code>\n\n'
        f'📋 Sau khi kích hoạt gõ /menu'
        f'{footer()}'
    )
    auto_delete(cid, m.message_id, 180)
    notify_admin(
        f'🆕 <b>USER MỚI</b>\n\n{user_info_text(msg)}\n'
        f'📅 {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}'
    )

# ─── /getkey ──────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['getkey'])
def cmd_getkey(msg):
    cid   = msg.chat.id
    track_user(msg)
    today = _today()
    usage = getkey_usage.get(cid, {'date': '', 'count': 0})
    if usage['date'] != today:
        usage = {'date': today, 'count': 0}
    if usage['count'] >= MAX_GETKEY_PER_DAY:
        m = bot.send_message(cid,
            f'⚠️ Bạn đã lấy key <b>{MAX_GETKEY_PER_DAY} lần</b> hôm nay!\n'
            f'Thử lại vào ngày mai.'
            f'{footer(cid)}'
        )
        auto_delete(cid, m.message_id, 60)
        return
    wait = bot.send_message(cid, f'⏳ Đang lấy link key...{footer(cid)}')
    try:
        r    = requests.get(API_GETKEY, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        data = r.json() if r.status_code == 200 else {}
        status_val = str(data.get('status', '')).lower()
        link = data.get('link', '')
        if status_val == 'success' and link:
            usage['count'] += 1
            getkey_usage[cid] = usage
            remaining = MAX_GETKEY_PER_DAY - usage['count']
            bot.edit_message_text(
                f'✅ <b>Link Key của bạn:</b>\n\n'
                f'🔗 {link}\n\n'
                f'📌 Bấm link → hoàn thành → nhận key\n'
                f'📌 Sau đó: <code>/key &lt;key&gt;</code>\n\n'
                f'📊 Hôm nay còn <b>{remaining}</b> lần lấy key'
                f'{footer(cid)}',
                cid, wait.message_id
            )
            auto_delete(cid, wait.message_id, 180)
        else:
            reason = data.get('message', 'Server không trả về link hợp lệ')
            bot.edit_message_text(f'❌ Lỗi: {reason}{footer(cid)}', cid, wait.message_id)
            auto_delete(cid, wait.message_id, 60)
    except Exception as e:
        try:
            bot.edit_message_text(f'❌ Lỗi kết nối: {e}{footer(cid)}', cid, wait.message_id)
            auto_delete(cid, wait.message_id, 60)
        except Exception:
            pass

# ─── /key ─────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['key'])
def cmd_key(msg):
    cid   = msg.chat.id
    track_user(msg)
    parts = msg.text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        m = bot.send_message(cid,
            '❌ Thiếu key!\n\n'
            '📌 Cú pháp: <code>/key &lt;key&gt;</code>\n'
            '📌 Lấy key: /getkey'
            f'{footer(cid)}'
        )
        auto_delete(cid, m.message_id, 60)
        return

    key  = parts[1].strip()
    wait = bot.send_message(cid, f'🔍 Đang xác minh key...{footer(cid)}')
    # Dùng chat_id làm HWID (định danh thiết bị)
    result = verify_key_api(key, hwid=str(cid))
    status = result['status']

    if status == 'success':
        exp_ts  = result['exp_ts']
        exp_str = result['exp_str']
        user_keys[cid] = {'key': key, 'exp': exp_ts}

        bot.edit_message_text(
            f'✅ <b>Kích hoạt thành công!</b>\n\n'
            f'🔑 Key: <code>{key}</code>\n'
            f'⏱ Thời hạn còn lại: <b>{exp_str}</b>\n\n'
            f'📋 Gõ /menu để xem danh sách lệnh'
            f'{footer(cid)}',
            cid, wait.message_id
        )
        auto_delete(cid, wait.message_id, 120)
        notify_admin(
            f'✅ <b>KEY KÍCH HOẠT</b>\n\n{user_info_text(msg)}\n'
            f'🔑 Key: <code>{key}</code>\n'
            f'⏱ {exp_str}\n'
            f'🕐 {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}'
        )

    elif status == 'expired':
        user_keys.pop(cid, None)
        bot.edit_message_text(
            f'❌ <b>Key đã hết hạn!</b>\n\nLấy key mới: /getkey{footer(cid)}',
            cid, wait.message_id
        )
        auto_delete(cid, wait.message_id, 60)

    elif status == 'device_limit':
        bot.edit_message_text(
            f'⚠️ <b>Key đã đạt giới hạn thiết bị!</b>\n\nLấy key khác: /getkey{footer(cid)}',
            cid, wait.message_id
        )
        auto_delete(cid, wait.message_id, 60)

    elif status == 'error':
        bot.edit_message_text(
            f'❌ Không kết nối được server. Thử lại sau.{footer(cid)}',
            cid, wait.message_id
        )
        auto_delete(cid, wait.message_id, 60)

    else:  # invalid
        raw_msg = result['raw'].get('message', 'Key không hợp lệ hoặc không tồn tại')
        bot.edit_message_text(
            f'❌ <b>Xác minh thất bại!</b>\n\n{raw_msg}\n\nLấy key mới: /getkey{footer(cid)}',
            cid, wait.message_id
        )
        auto_delete(cid, wait.message_id, 60)
        notify_admin(
            f'❌ <b>KEY THẤT BẠI</b>\n\n{user_info_text(msg)}\n'
            f'🔑 Key: <code>{key}</code>\n'
            f'📋 {raw_msg}'
        )

# ─── /debugapi (chỉ admin) ────────────────────────────────────────────────────
@bot.message_handler(commands=['debugapi'])
def cmd_debugapi(msg):
    """Xem raw JSON server trả về khi verify key — admin only."""
    cid = msg.chat.id
    if not is_admin(cid):
        m = bot.send_message(cid, '❌ Không có quyền!')
        auto_delete(cid, m.message_id, 15)
        return
    parts = msg.text.strip().split(None, 1)
    if len(parts) < 2:
        m = bot.send_message(cid,
            '📌 Cú pháp: <code>/debugapi &lt;key&gt;</code>\n'
            'VD: <code>/debugapi 7DAY-XXXX-XXXX</code>'
        )
        auto_delete(cid, m.message_id, 60)
        return
    key  = parts[1].strip()
    wait = bot.send_message(cid, f'🔍 Gọi API với key: <code>{key}</code>...')
    try:
        r        = requests.post(API_VERIFY,
                                 json={'key': key, 'hwid': str(cid)},
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
                                 timeout=15)
        raw_text = r.text or '(trống)'
        try:
            data      = r.json()
            formatted = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            data      = {}
            formatted = raw_text

        # Phân tích
        raw_expire = (
            data.get('expire_date') or data.get('expiration_date')
            or data.get('expiry') or data.get('expired_at')
            or data.get('time_left') or data.get('dev_exp') or data.get('exp')
        )
        exp_str = format_time_remaining(raw_expire)
        exp_ts  = _exp_timestamp(raw_expire)

        bot.edit_message_text(
            f'🔍 <b>DEBUG /api/verify</b>\n\n'
            f'🔑 Key: <code>{key}</code>\n'
            f'📡 HTTP: {r.status_code}\n\n'
            f'📄 <b>Raw JSON:</b>\n<pre>{formatted[:1800]}</pre>\n\n'
            f'🕐 raw_expire field: <code>{raw_expire!r}</code>\n'
            f'📊 exp_ts: <code>{exp_ts}</code>\n'
            f'🗓 Hiển thị: <b>{exp_str or "None"}</b>',
            cid, wait.message_id
        )
        auto_delete(cid, wait.message_id, 300)
    except Exception as e:
        bot.edit_message_text(f'❌ Lỗi: {e}', cid, wait.message_id)
        auto_delete(cid, wait.message_id, 60)

# ─── /menu (cần key) ──────────────────────────────────────────────────────────
@bot.message_handler(commands=['menu'])
def cmd_menu(msg):
    cid = msg.chat.id
    track_user(msg)
    if not require_key_check(cid):
        return
    m = bot.send_message(cid,
        '📋 <b>DANH SÁCH LỆNH</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n'
        '📱 <b>OTP TOOL</b>\n'
        '┌ <code>/otp &lt;sdt&gt; &lt;số_lần&gt;</code>\n'
        '└ VD: <code>/otp 0901234567 20</code>\n\n'
        '🎵 <b>SOUNDCLOUD</b>\n'
        '┌ <code>/scl &lt;tên bài hát&gt;</code>\n'
        '└ VD: <code>/scl shape of you</code>\n\n'
        '🎬 <b>TIKTOK HD</b>\n'
        '┌ <code>/tiktok &lt;từ khóa hoặc link&gt;</code>\n'
        '└ VD: <code>/tiktok mèo cute</code>\n\n'
        '🔑 <b>KEY</b>\n'
        '┌ <code>/getkey</code> — Lấy link key (3 lần/ngày)\n'
        '└ <code>/key &lt;key&gt;</code> — Kích hoạt key\n\n'
        '🛑 <code>/stop</code> — Dừng tác vụ đang chạy\n'
        '━━━━━━━━━━━━━━━━━━━━━━━━━━'
        f'{footer(cid)}'
    )
    auto_delete(cid, m.message_id, 120)

# ─── /admin (chỉ admin) ───────────────────────────────────────────────────────
@bot.message_handler(commands=['admin'])
def cmd_admin(msg):
    cid = msg.chat.id
    if not is_admin(cid):
        m = bot.send_message(cid, '❌ <b>Không có quyền!</b>')
        auto_delete(cid, m.message_id, 15)
        return
    m = bot.send_message(cid,
        '👑 <b>BẢNG ĐIỀU KHIỂN ADMIN</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n'
        '📢 <code>/tb &lt;nội dung&gt;</code> — Broadcast\n\n'
        '📊 <code>/stats</code> — Thống kê\n\n'
        '🔍 <code>/debugapi &lt;key&gt;</code>\n'
        '   → Xem raw JSON server trả về\n'
        '   → Debug lỗi hạn sử dụng\n\n'
        '━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
        f'👥 Hiện có <b>{len(all_users)}</b> người dùng.\n'
        f'<i>⚠️ Admin ID {ADMIN_ID} luôn bypass key check.</i>'
    )
    auto_delete(cid, m.message_id, 180)

# ─── /tb (broadcast, chỉ admin) ───────────────────────────────────────────────
@bot.message_handler(commands=['tb'])
def cmd_tb(msg):
    cid = msg.chat.id
    if not is_admin(cid):
        m = bot.send_message(cid, '❌ <b>Không có quyền!</b>')
        auto_delete(cid, m.message_id, 15)
        return
    parts = msg.text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        m = bot.send_message(cid, '❌ <code>/tb &lt;nội dung&gt;</code>')
        auto_delete(cid, m.message_id, 30)
        return
    content = parts[1].strip()
    btext = (
        f'📢 <b>THÔNG BÁO TỪ ADMIN</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n'
        f'{content}\n\n'
        f'━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
        f'<i>@vkhanh3010</i>'
    )
    wait = bot.send_message(cid, f'📤 Đang gửi tới <b>{len(all_users)}</b> người...')
    def do_broadcast():
        ok = fail = 0
        for uid in list(all_users):
            if uid == ADMIN_ID:
                continue
            try:
                bot.send_message(uid, btext)
                ok += 1
                time.sleep(0.05)
            except Exception:
                fail += 1
        try:
            bot.edit_message_text(
                f'✅ Gửi xong!\n✔️ Thành công: <b>{ok}</b>\n✖️ Thất bại: <b>{fail}</b>',
                cid, wait.message_id
            )
            auto_delete(cid, wait.message_id, 120)
        except Exception:
            pass
    threading.Thread(target=do_broadcast, daemon=True).start()

# ─── /stats (chỉ admin) ───────────────────────────────────────────────────────
@bot.message_handler(commands=['stats'])
def cmd_stats(msg):
    cid = msg.chat.id
    if not is_admin(cid):
        m = bot.send_message(cid, '❌ <b>Không có quyền!</b>')
        auto_delete(cid, m.message_id, 15)
        return
    activated = sum(1 for uid in all_users if uid in user_keys)
    m = bot.send_message(cid,
        f'📊 <b>THỐNG KÊ BOT</b>\n\n'
        f'👥 Tổng users: <b>{len(all_users)}</b>\n'
        f'🔑 Đã kích hoạt: <b>{activated}</b>\n'
        f'📅 {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}'
    )
    auto_delete(cid, m.message_id, 120)

# ─── /otp (cần key) ───────────────────────────────────────────────────────────
def detect_carrier(phone):
    clean = phone.strip()
    if clean.startswith('+84'):
        clean = '0' + clean[3:]
    elif clean.startswith('84') and len(clean) >= 11:
        clean = '0' + clean[2:]
    if not clean.startswith('0'):
        clean = '0' + clean
    pre = clean[:3]
    table = {
        '🔴 Viettel':      ['032','033','034','035','036','037','038','039','086','096','097','098'],
        '🔵 Mobifone':     ['070','076','077','078','079','089','090','093'],
        '🟢 Vinaphone':    ['081','082','083','084','085','088','091','094'],
        '🟡 Vietnamobile': ['052','056','058','092'],
        '🟠 Gmobile':      ['059','099'],
        '⚪ Reddi':        ['055'],
    }
    for carrier, prefixes in table.items():
        if pre in prefixes:
            return carrier
    return '❓ Không xác định'

def make_otp_status(phone, carrier, count, current, status, cid):
    return (
        f'📱 <b>SĐT:</b> <code>{phone}</code>\n'
        f'📡 <b>Nhà Mạng:</b> {carrier}\n'
        f'🔢 <b>Số Lần:</b> {current}/{count}\n'
        f'✅ <b>Trạng Thái:</b> {status}\n\n'
        f'⛔ <code>/stop</code> để dừng'
        f'{footer(cid)}'
    )

@bot.message_handler(commands=['otp'])
def cmd_otp(msg):
    cid = msg.chat.id
    track_user(msg)
    if not require_key_check(cid):
        return
    parts = msg.text.strip().split()
    if len(parts) < 3:
        m = bot.send_message(cid,
            '❌ <b>Thiếu tham số!</b>\n\n'
            '📌 Cú pháp: <code>/otp &lt;sdt&gt; &lt;số_lần&gt;</code>\n'
            '📌 VD: <code>/otp 0901234567 20</code>'
            f'{footer(cid)}'
        )
        auto_delete(cid, m.message_id, 60)
        return
    phone = parts[1]
    try:
        count = int(parts[2])
        if count <= 0 or count > 9999:
            raise ValueError()
    except ValueError:
        m = bot.send_message(cid, f'❌ Số lần không hợp lệ (1–9999).{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    digits = phone.lstrip('+').replace(' ', '').replace('-', '')
    if not digits.isdigit() or len(digits) < 9:
        m = bot.send_message(cid, f'❌ Số điện thoại không hợp lệ!{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    if cid in active_ops:
        m = bot.send_message(cid, f'⚠️ Đang có tác vụ! Gõ /stop trước.{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    if not otp_functions:
        m = bot.send_message(cid, f'❌ OTP module chưa sẵn sàng.{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    carrier = detect_carrier(phone)
    sent    = bot.send_message(cid, make_otp_status(phone, carrier, count, 0, '⏳ Chuẩn bị...', cid))
    mid     = sent.message_id
    stop_ev = threading.Event()
    active_ops[cid] = stop_ev
    notify_admin(
        f'🚨 <b>OTP</b>\n\n{user_info_text(msg)}\n'
        f'📱 <code>{phone}</code> · {carrier} · {count} lần'
    )
    def otp_worker():
        current = 0
        try:
            while current < count and not stop_ev.is_set():
                fn_name, fn = otp_functions[current % len(otp_functions)]
                service = fn_name.replace('send_otp_via_', '').replace('_', ' ').title()
                try:
                    bot.edit_message_text(
                        make_otp_status(phone, carrier, count, current,
                                        f'📨 Gửi qua <b>{service}</b>...', cid),
                        cid, mid
                    )
                except Exception:
                    pass
                try:
                    fn(phone)
                except Exception:
                    pass
                current += 1
                time.sleep(1.5)
            final = '🛑 Đã dừng!' if stop_ev.is_set() else f'✅ Xong! Đã gửi <b>{current}</b> lần.'
            try:
                bot.edit_message_text(
                    make_otp_status(phone, carrier, count, current, final, cid),
                    cid, mid
                )
                auto_delete(cid, mid, 120)
            except Exception:
                pass
        finally:
            active_ops.pop(cid, None)
    threading.Thread(target=otp_worker, daemon=True).start()

# ─── /scl (cần key) ───────────────────────────────────────────────────────────
@bot.message_handler(commands=['scl'])
def cmd_scl(msg):
    cid = msg.chat.id
    track_user(msg)
    if not require_key_check(cid):
        return
    parts = msg.text.strip().split(None, 1)
    if len(parts) < 2:
        m = bot.send_message(cid,
            '❌ <b>Thiếu tên bài!</b>\n\n'
            '📌 Cú pháp: <code>/scl &lt;tên bài hát&gt;</code>\n'
            '📌 VD: <code>/scl shape of you</code>'
            f'{footer(cid)}'
        )
        auto_delete(cid, m.message_id, 60)
        return
    if not scll_mod:
        m = bot.send_message(cid, f'❌ SoundCloud module chưa sẵn sàng.{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    query    = parts[1].strip()
    wait_msg = bot.send_message(cid, f'🔍 Đang tìm: <b>{query}</b>...{footer(cid)}')
    def do_search():
        try:
            songs = scll_mod.search_songs(query)
        except Exception as e:
            try:
                bot.edit_message_text(f'❌ Lỗi tìm kiếm: {e}{footer(cid)}', cid, wait_msg.message_id)
                auto_delete(cid, wait_msg.message_id, 60)
            except Exception:
                pass
            return
        if not songs:
            try:
                bot.edit_message_text(
                    f'😔 Không tìm thấy: <b>{query}</b>{footer(cid)}',
                    cid, wait_msg.message_id
                )
                auto_delete(cid, wait_msg.message_id, 60)
            except Exception:
                pass
            return
        user_states[cid] = {'type': 'scl', 'songs': songs, 'ts': time.time()}
        text = f'🎵 <b>SoundCloud — {len(songs)} bài:</b>\n━━━━━━━━━━━━━━━━━━\n\n'
        for i, (link, title, _) in enumerate(songs[:10], 1):
            text += f'<b>{i}.</b> {title}\n'
        text += f'\n━━━━━━━━━━━━━━━━━━\n💬 Gửi <b>số thứ tự</b> để tải (hết hạn 120 giây){footer(cid)}'
        cover_url = songs[0][2] if songs and songs[0][2] else None
        try:
            bot.delete_message(cid, wait_msg.message_id)
        except Exception:
            pass
        if cover_url:
            try:
                m = bot.send_photo(cid, cover_url, caption=text)
                auto_delete(cid, m.message_id, 120)
                return
            except Exception:
                pass
        m = bot.send_message(cid, text)
        auto_delete(cid, m.message_id, 120)
    threading.Thread(target=do_search, daemon=True).start()

# ─── /tiktok (cần key) ────────────────────────────────────────────────────────
@bot.message_handler(commands=['tiktok'])
def cmd_tiktok(msg):
    cid = msg.chat.id
    track_user(msg)
    if not require_key_check(cid):
        return
    parts = msg.text.strip().split(None, 1)
    if len(parts) < 2:
        m = bot.send_message(cid,
            '❌ <b>Thiếu từ khóa hoặc link!</b>\n\n'
            '📌 Tìm: <code>/tiktok &lt;từ khóa&gt;</code>\n'
            '📌 Tải: <code>/tiktok &lt;link&gt;</code>'
            f'{footer(cid)}'
        )
        auto_delete(cid, m.message_id, 60)
        return
    if not tt_mod:
        m = bot.send_message(cid, f'❌ TikTok module chưa sẵn sàng.{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    query    = parts[1].strip()
    is_url   = 'tiktok.com' in query or query.startswith('http')
    wait_msg = bot.send_message(cid, f'🔍 Đang xử lý: <b>{query[:60]}</b>...{footer(cid)}')
    def do_tiktok():
        try:
            bot.delete_message(cid, wait_msg.message_id)
        except Exception:
            pass
        if is_url:
            _tiktok_download_url(cid, query)
        else:
            _tiktok_search(cid, query)
    threading.Thread(target=do_tiktok, daemon=True).start()

def _tiktok_download_url(cid, url):
    try:
        data = tt_mod._tikwm_by_url(url)
        if not data or not data.get('data'):
            m = bot.send_message(cid, f'❌ Không lấy được thông tin video.{footer(cid)}')
            auto_delete(cid, m.message_id, 60)
            return
        _send_tiktok_item(cid, data['data'])
    except Exception as e:
        m = bot.send_message(cid, f'❌ Lỗi: {e}{footer(cid)}')
        auto_delete(cid, m.message_id, 60)

def _tiktok_search(cid, query):
    try:
        data   = tt_mod._tikwm_search(query, count=5)
        videos = tt_mod._get_videos_from_search_payload(data)
        if not videos:
            m = bot.send_message(cid, f'😔 Không tìm thấy: <b>{query}</b>{footer(cid)}')
            auto_delete(cid, m.message_id, 60)
            return
        user_states[cid] = {'type': 'tiktok', 'videos': videos, 'ts': time.time()}
        text = f'🎬 <b>TikTok — {query}</b>\n━━━━━━━━━━━━━━━━━━\n\n'
        for i, v in enumerate(videos[:5], 1):
            info  = tt_mod._parse_item_fields(v)
            t     = info['title']
            short = (t[:55] + '…') if len(t) > 55 else t
            text += (
                f'<b>{i}.</b> {short}\n'
                f'   👁 {tt_mod._fmt_num(info["play"])}  ❤️ {tt_mod._fmt_num(info["like"])}  ⏱ {info["dur"]}\n\n'
            )
        text += f'━━━━━━━━━━━━━━━━━━\n💬 Gửi số (1–{min(5, len(videos))}) để tải HD{footer(cid)}'
        list_img = None
        try:
            r = tt_mod._build_list_image(query, videos)
            if isinstance(r, str) and os.path.exists(r):
                list_img = r
            elif r and hasattr(r, 'save'):
                cache_dir = os.path.join(BASE_DIR, 'modules', 'cache')
                os.makedirs(cache_dir, exist_ok=True)
                list_img = os.path.join(cache_dir, f'list_{int(time.time())}.jpg')
                r.save(list_img)
        except Exception:
            list_img = None
        if list_img and os.path.exists(list_img):
            try:
                with open(list_img, 'rb') as f:
                    m = bot.send_photo(cid, f, caption=text)
                auto_delete(cid, m.message_id, 120)
                try:
                    os.remove(list_img)
                except Exception:
                    pass
                return
            except Exception:
                try:
                    os.remove(list_img)
                except Exception:
                    pass
        m = bot.send_message(cid, text)
        auto_delete(cid, m.message_id, 120)
    except Exception as e:
        m = bot.send_message(cid, f'❌ Lỗi: {e}{footer(cid)}')
        auto_delete(cid, m.message_id, 60)

def _send_tiktok_item(cid, item):
    info      = tt_mod._parse_item_fields(item)
    video_url = item.get('hdplay') or item.get('play') or item.get('wmplay') or ''
    caption   = (
        f'🎬 <b>{(info["title"][:200] or "TikTok Video")}</b>\n'
        f'👤 {info["nickname"] or "TikTok"}'
        + (f' (@{info["unique_id"]})' if info["unique_id"] else '') + '\n'
        f'👁 {tt_mod._fmt_num(info["play"])}  ❤️ {tt_mod._fmt_num(info["like"])}\n'
        f'⏱ {info["dur"]}'
        f'{footer(cid)}'
    )
    if not video_url:
        m = bot.send_message(cid, f'❌ Không có link video.{footer(cid)}')
        auto_delete(cid, m.message_id, 60)
        return
    cache_dir  = os.path.join(BASE_DIR, 'modules', 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    vid_path   = os.path.join(cache_dir, f'tt_{cid}_{int(time.time())}.mp4')
    downloaded = False
    try:
        r = requests.get(video_url, timeout=120, stream=True)
        r.raise_for_status()
        with open(vid_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        downloaded = True
    except Exception:
        downloaded = False
    sent = False
    if downloaded and os.path.exists(vid_path) and os.path.getsize(vid_path) > 10000:
        try:
            with open(vid_path, 'rb') as vf:
                m = bot.send_video(cid, vf, caption=caption, supports_streaming=True, timeout=120)
            auto_delete(cid, m.message_id, 300)
            sent = True
        except Exception:
            pass
    if not sent:
        try:
            m = bot.send_video(cid, video_url, caption=caption, supports_streaming=True, timeout=60)
            auto_delete(cid, m.message_id, 300)
        except Exception:
            m = bot.send_message(cid, f'🔗 Link HD:\n{video_url}\n{caption}')
            auto_delete(cid, m.message_id, 120)
    try:
        os.remove(vid_path)
    except Exception:
        pass

# ─── /stop ────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['stop'])
def cmd_stop(msg):
    cid = msg.chat.id
    if cid in active_ops:
        active_ops[cid].set()
        m = bot.send_message(cid, f'🛑 Đã dừng!{footer(cid)}')
    else:
        m = bot.send_message(cid, f'⚠️ Không có tác vụ đang chạy.{footer(cid)}')
    auto_delete(cid, m.message_id, 30)

# ─── XỬ LÝ CHỌN SỐ (SoundCloud / TikTok) ─────────────────────────────────────
@bot.message_handler(content_types=['text'])
def handle_text(msg):
    cid  = msg.chat.id
    text = msg.text.strip()
    if text.startswith('/') or not text.isdigit():
        return
    if not is_activated(cid):
        m = bot.send_message(cid,
            '🔒 Chưa kích hoạt key!\n/getkey → /key &lt;key&gt;'
        )
        auto_delete(cid, m.message_id, 30)
        return
    num   = int(text)
    state = user_states.get(cid)
    if not state:
        return
    if time.time() - state.get('ts', 0) > 120:
        user_states.pop(cid, None)
        m = bot.send_message(cid, f'⏰ Hết hạn. Tìm lại.{footer(cid)}')
        auto_delete(cid, m.message_id, 30)
        return

    # ── SoundCloud ────────────────────────────────────────────────────────
    if state['type'] == 'scl':
        songs = state.get('songs', [])
        if num < 1 or num > len(songs):
            m = bot.send_message(cid, f'❌ Số không hợp lệ (1–{len(songs)}).{footer(cid)}')
            auto_delete(cid, m.message_id, 30)
            return
        link, title, _ = songs[num - 1]
        user_states.pop(cid, None)
        wait = bot.send_message(cid, f'⏳ Đang tải: <b>{title}</b>...{footer(cid)}')
        def dl_scl():
            audio_path = None
            try:
                audio_url = scll_mod.get_music_stream_url(link)
                if not audio_url:
                    try:
                        bot.edit_message_text(f'❌ Không thể tải bài này.{footer(cid)}', cid, wait.message_id)
                        auto_delete(cid, wait.message_id, 60)
                    except Exception:
                        pass
                    return
                cover_url  = scll_mod.get_track_cover(link)
                cache_dir  = os.path.join(BASE_DIR, 'modules', 'cache')
                os.makedirs(cache_dir, exist_ok=True)
                audio_path = os.path.join(cache_dir, f'scl_{cid}_{int(time.time())}.mp3')
                r = requests.get(audio_url, timeout=120, stream=True)
                r.raise_for_status()
                with open(audio_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                try:
                    bot.delete_message(cid, wait.message_id)
                except Exception:
                    pass
                if cover_url:
                    try:
                        m = bot.send_photo(cid, cover_url,
                                           caption=f'🎵 <b>{title}</b>{footer(cid)}')
                        auto_delete(cid, m.message_id, 600)
                    except Exception:
                        pass
                voice_sent = False
                try:
                    with open(audio_path, 'rb') as af:
                        m = bot.send_voice(cid, af,
                                           caption=f'🎵 <b>{title}</b>{footer(cid)}',
                                           timeout=180)
                    auto_delete(cid, m.message_id, 600)
                    voice_sent = True
                except Exception:
                    pass
                if not voice_sent:
                    try:
                        with open(audio_path, 'rb') as af:
                            m = bot.send_audio(cid, af, title=title,
                                               caption=f'🎵 <b>{title}</b>{footer(cid)}',
                                               timeout=180)
                        auto_delete(cid, m.message_id, 600)
                    except Exception as e2:
                        err_m = bot.send_message(cid, f'❌ Gửi nhạc thất bại: {e2}{footer(cid)}')
                        auto_delete(cid, err_m.message_id, 60)
            except Exception as e:
                try:
                    bot.edit_message_text(f'❌ Lỗi: {e}{footer(cid)}', cid, wait.message_id)
                    auto_delete(cid, wait.message_id, 60)
                except Exception:
                    pass
            finally:
                if audio_path:
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass
        threading.Thread(target=dl_scl, daemon=True).start()

    # ── TikTok ────────────────────────────────────────────────────────────
    elif state['type'] == 'tiktok':
        if not tt_mod:
            return
        videos = state.get('videos', [])
        if num < 1 or num > len(videos):
            m = bot.send_message(cid, f'❌ Số không hợp lệ (1–{len(videos)}).{footer(cid)}')
            auto_delete(cid, m.message_id, 30)
            return
        item = videos[num - 1]
        user_states.pop(cid, None)
        wait = bot.send_message(cid, f'⏳ Đang tải TikTok HD...{footer(cid)}')
        def dl_tiktok():
            try:
                bot.delete_message(cid, wait.message_id)
            except Exception:
                pass
            _send_tiktok_item(cid, item)
        threading.Thread(target=dl_tiktok, daemon=True).start()

# ─── PING (tránh ngủ đông Render) ─────────────────────────────────────────────
def ping_worker(worker_id):
    time.sleep(30 + worker_id * 10)
    while True:
        try:
            target = f'{SELF_URL}/ping' if SELF_URL else f'http://localhost:{PORT}/ping'
            requests.get(target, timeout=10)
        except Exception:
            pass
        time.sleep(240 + worker_id * 12)

# ─── ĐĂNG KÝ COMMANDS ─────────────────────────────────────────────────────────
try:
    bot.set_my_commands([
        telebot.types.BotCommand('/start',    'Bắt đầu'),
        telebot.types.BotCommand('/getkey',   'Lấy link key (3 lần/ngày)'),
        telebot.types.BotCommand('/key',      'Kích hoạt key'),
        telebot.types.BotCommand('/menu',     'Xem lệnh (cần key)'),
        telebot.types.BotCommand('/otp',      'OTP spam (cần key)'),
        telebot.types.BotCommand('/scl',      'Tải nhạc SoundCloud voice (cần key)'),
        telebot.types.BotCommand('/tiktok',   'Tải TikTok HD (cần key)'),
        telebot.types.BotCommand('/stop',     'Dừng tác vụ'),
    ])
except Exception:
    pass

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    print(f'[Flask] Port {PORT}')

    for i in range(5):
        threading.Thread(target=ping_worker, args=(i,), daemon=True).start()
    print('[Ping] 5 threads khởi động')

    print(f'[Bot] Bắt đầu | TOKEN={TOKEN[:20]}... | ADMIN_ID={ADMIN_ID}')
    bot.infinity_polling(timeout=10, long_polling_timeout=5)

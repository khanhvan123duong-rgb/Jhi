from zlapi.models import Message
import requests
import os
import time
import json
import logging
import re
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import quote_plus
from config import PREFIX

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

des = {
    "version": "12.0.0",
    "credits": "Đặng Quang Tình",
    "description": "TikTok ULTRA Stable: UI ảnh list + chọn số + tải link + local-first + auto + FIX WinError32 + FIX sendRemoteVideo signature",
    "power": "Thành viên"
}

TIKWM_SEARCH_ENDPOINT = "https://www.tikwm.com/api/feed/search"
TIKWM_BY_URL_ENDPOINTS = [
    "https://www.tikwm.com/api/?url={}&hd=1",
    "https://www.tikwm.com/api/?url={}"
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://www.tikwm.com/"
}

CACHE_DIR = "modules/cache"
SETTINGS_PATH = f"{CACHE_DIR}/tiktok_settings.json"

CACHE_TTL_SEC = 300
AUTO_TTL_SEC = 20
LIST_TTL_MS = 120000

TIKTOK_URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

_search_cache = {}
_user_cooldown = {}
_auto_cooldown = {}
_settings = None

# ====================== UTILS ======================

def _now():
    return int(time.time())

def _ensure_dirs():
    os.makedirs(CACHE_DIR, exist_ok=True)

def _session():
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s

def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def _fmt_num(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)

def _fmt_duration(seconds):
    try:
        s = int(float(seconds))
    except Exception:
        return "00:00"
    m = s // 60
    r = s % 60
    return f"{m:02}:{r:02}"

def _fmt_time(ts):
    try:
        ts = int(ts)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return ""

def _first_url(text):
    if not text:
        return ""
    m = TIKTOK_URL_RE.search(text)
    if not m:
        return ""
    return m.group(1).strip().rstrip(").,]}>\"'")

def _looks_like_tiktok_url(url):
    u = (url or "").lower()
    return ("tiktok.com" in u) or ("vt.tiktok.com" in u) or ("vm.tiktok.com" in u)

def _resolve_url(url, timeout=12):
    if not url:
        return url
    try:
        r = _session().get(url, timeout=timeout, allow_redirects=True)
        return r.url or url
    except Exception:
        return url

def _cooldown_ok(key, seconds):
    t = _user_cooldown.get(key, 0)
    n = _now()
    if n - t < seconds:
        return False
    _user_cooldown[key] = n
    return True

def _autocooldown_ok(key, seconds):
    t = _auto_cooldown.get(key, 0)
    n = _now()
    if n - t < seconds:
        return False
    _auto_cooldown[key] = n
    return True

def _cleanup_cache_files(prefixes=("tiktok_list_", "list_", "tiktok_", "tiktok_cover_"), max_age_sec=900):
    _ensure_dirs()
    now = _now()
    try:
        for fn in os.listdir(CACHE_DIR):
            if not any(fn.startswith(p) for p in prefixes):
                continue
            path = os.path.join(CACHE_DIR, fn)
            try:
                st = os.stat(path)
                if now - int(st.st_mtime) > max_age_sec:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

def _safe_remove(path, tries=5, delay=0.25):
    if not path:
        return
    for _ in range(tries):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except Exception:
            time.sleep(delay)

# ====================== SETTINGS ======================

def _load_settings():
    global _settings
    if _settings is not None:
        return _settings
    _ensure_dirs()
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                _settings = json.load(f)
        except Exception:
            _settings = {}
    else:
        _settings = {}
    if not isinstance(_settings, dict):
        _settings = {}
    if "auto_threads" not in _settings or not isinstance(_settings.get("auto_threads"), dict):
        _settings["auto_threads"] = {}
    return _settings

def _save_settings():
    _ensure_dirs()
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(_settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ====================== API ======================

def _tikwm_search(keywords, count=10, cursor=0, max_retries=3):
    kw = (keywords or "").strip()
    if not kw:
        return None
    s = _session()
    last = None
    for attempt in range(max_retries):
        try:
            params = {"keywords": kw, "count": max(1, min(int(count), 30)), "cursor": cursor}
            r = s.get(TIKWM_SEARCH_ENDPOINT, params=params, timeout=18)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    logger.error(f"tikwm_search failed: {last}")
    return None

def _tikwm_by_url(url, max_retries=3):
    u = (url or "").strip()
    if not u:
        return None
    resolved = _resolve_url(u)
    encoded = quote_plus(resolved)
    s = _session()
    last = None
    for attempt in range(max_retries):
        for ep in TIKWM_BY_URL_ENDPOINTS:
            try:
                r = s.get(ep.format(encoded), timeout=20)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("code", 1) == 0 and data.get("data"):
                    return data
            except Exception as e:
                last = e
                continue
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    logger.error(f"tikwm_by_url failed: {last}")
    return None

def _get_videos_from_search_payload(data):
    if not isinstance(data, dict):
        return []
    d = data.get("data") or {}
    vids = d.get("videos") or []
    return vids if isinstance(vids, list) else []

# ====================== CACHE ======================

def _cache_put(thread_id, author_id, keywords, videos):
    _search_cache[(str(thread_id), str(author_id))] = {
        "ts": _now(),
        "keywords": keywords,
        "videos": videos[:5] if isinstance(videos, list) else []
    }

def _cache_get(thread_id, author_id):
    k = (str(thread_id), str(author_id))
    v = _search_cache.get(k)
    if not v:
        return None
    if _now() - _safe_int(v.get("ts", 0)) > CACHE_TTL_SEC:
        try:
            del _search_cache[k]
        except Exception:
            pass
        return None
    return v

# ====================== IMAGE UI ======================

def _load_font(size, bold=False):
    candidates = []
    if bold:
        candidates = [
            f"{CACHE_DIR}/font/BeVietnamPro-Bold.ttf",
            "/system/fonts/Roboto-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    else:
        candidates = [
            f"{CACHE_DIR}/font/BeVietnamPro-Regular.ttf",
            "/system/fonts/Roboto-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for p in candidates:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()

def _fetch_image(url, timeout=12):
    if not url:
        return None
    try:
        r = _session().get(url, timeout=timeout)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        return None

def _fit_center_crop(im, size):
    w, h = im.size
    tw, th = size
    if w <= 0 or h <= 0:
        return Image.new("RGB", size, (45, 45, 50))
    src = w / h
    dst = tw / th
    if src > dst:
        new_h = h
        new_w = int(h * dst)
    else:
        new_w = w
        new_h = int(w / dst)
    left = (w - new_w) // 2
    top = (h - new_h) // 2
    im = im.crop((left, top, left + new_w, top + new_h))
    return im.resize((tw, th))

def _parse_item_fields(item):
    title = (item.get("title") or "Không có tiêu đề").strip()
    cover_url = (item.get("cover") or item.get("thumbnail") or "").strip()

    author = item.get("author") or item.get("author_info") or item.get("authorInfo") or {}
    nickname = ""
    unique_id = ""
    if isinstance(author, dict):
        nickname = (author.get("nickname") or author.get("name") or "").strip()
        unique_id = (author.get("unique_id") or author.get("uniqueId") or author.get("uniqueid") or "").strip()

    play = _safe_int(item.get("play_count", item.get("views", 0)))
    like = _safe_int(item.get("digg_count", item.get("likes", 0)))
    cmt = _safe_int(item.get("comment_count", item.get("comments", 0)))
    share = _safe_int(item.get("share_count", item.get("shares", 0)))

    duration = item.get("duration")
    if duration is None:
        mi = item.get("music_info") or item.get("musicInfo") or {}
        if isinstance(mi, dict):
            duration = mi.get("duration")
    dur_str = _fmt_duration(duration or 0)

    create_time = item.get("create_time") or item.get("createTime") or item.get("created_at") or item.get("create")
    created = _fmt_time(create_time) if create_time else ""

    music = item.get("music_info") or item.get("musicInfo") or {}
    music_title = ""
    music_author = ""
    if isinstance(music, dict):
        music_title = (music.get("title") or music.get("music_title") or "").strip()
        music_author = (music.get("author") or music.get("author_name") or music.get("artist") or "").strip()

    return {
        "title": title,
        "cover": cover_url,
        "nickname": nickname,
        "unique_id": unique_id,
        "play": play,
        "like": like,
        "cmt": cmt,
        "share": share,
        "dur": dur_str,
        "created": created,
        "music_title": music_title,
        "music_author": music_author
    }

def _build_list_image(keywords, videos, max_items=5):
    _ensure_dirs()
    vids = (videos or [])[:max_items]

    W = 900
    PAD = 28
    header_h = 140
    card_h = 170
    gap = 18
    H = header_h + (card_h + gap) * len(vids) + 60

    base = Image.new("RGB", (W, H), (12, 13, 16))
    draw = ImageDraw.Draw(base)

    f_big = _load_font(36, bold=True)
    f_title = _load_font(24, bold=True)
    f_sub = _load_font(20, bold=False)
    f_small = _load_font(18, bold=False)
    f_num = _load_font(44, bold=True)

    draw.text((W // 2, 46), f"KẾT QUẢ: {keywords}", font=f_big, fill=(245, 245, 245), anchor="mm")
    draw.text(
        (W // 2, 94),
        f"Trả lời ảnh này bằng số để tải (1-5). Random Quality. Hết hạn {LIST_TTL_MS//1000}s.",
        font=f_small,
        fill=(180, 185, 195),
        anchor="mm"
    )

    y = header_h
    for i, it in enumerate(vids, start=1):
        info = _parse_item_fields(it)

        x1 = PAD
        x2 = W - PAD
        y1 = y
        y2 = y + card_h

        draw.rounded_rectangle([x1, y1, x2, y2], radius=22, fill=(22, 24, 29))

        thumb_x = x1 + 16
        thumb_y = y1 + 16
        thumb_w = 250
        thumb_h = 138

        thumb = _fetch_image(info["cover"])
        if thumb is None:
            thumb = Image.new("RGB", (thumb_w, thumb_h), (45, 45, 50))
        else:
            thumb = _fit_center_crop(thumb, (thumb_w, thumb_h))

        mask = Image.new("L", (thumb_w, thumb_h), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle([0, 0, thumb_w, thumb_h], radius=18, fill=255)
        base.paste(thumb, (thumb_x, thumb_y), mask)

        tx = thumb_x + thumb_w + 18
        ty = y1 + 16

        title = info["title"].strip()
        if len(title) > 58:
            title = title[:58].rstrip() + "…"
        draw.text((tx, ty), title, font=f_title, fill=(245, 245, 245))
        ty += 38

        ch_name = info["nickname"] or info["unique_id"] or "TikTok"
        ch_id = f"@{info['unique_id']}" if info["unique_id"] else ""
        draw.text((tx, ty), f"👤 {ch_name} {ch_id}".strip(), font=f_sub, fill=(200, 205, 215))
        ty += 28

        stats = f"👁️ {_fmt_num(info['play'])}   ❤️ {_fmt_num(info['like'])}   💬 {_fmt_num(info['cmt'])}   🔁 {_fmt_num(info['share'])}"
        draw.text((tx, ty), stats, font=f_small, fill=(180, 185, 195))
        ty += 26

        meta = f"⏱️ {info['dur']}"
        if info["created"]:
            meta += f"   📅 {info['created']}"
        draw.text((tx, ty), meta, font=f_small, fill=(165, 170, 182))
        ty += 26

        if info["music_title"] or info["music_author"]:
            mt = info["music_title"] or "Unknown"
            ma = info["music_author"] or ""
            line = f"🎵 {mt}"
            if ma:
                line += f" — {ma}"
            if len(line) > 64:
                line = line[:64].rstrip() + "…"
            draw.text((tx, ty), line, font=f_small, fill=(155, 160, 170))

        draw.text((x2 - 14, y1 + card_h // 2), str(i), font=f_num, fill=(235, 235, 235), anchor="rm")

        y += card_h + gap

    out_path = f"{CACHE_DIR}/tiktok_list_{_now()}_{os.getpid()}.jpg"
    base.save(out_path, quality=92)
    return out_path

# ====================== SEND (STABLE) ======================

def _download_to_file(url, path, timeout=60):
    s = _session()
    with s.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)

def _extract_best_link(item, fallback=""):
    share_url = (item.get("share_url") or item.get("share") or "").strip()
    if share_url:
        return share_url
    author = item.get("author") or item.get("author_info") or item.get("authorInfo") or {}
    unique_id = ""
    if isinstance(author, dict):
        unique_id = (author.get("unique_id") or author.get("uniqueId") or author.get("uniqueid") or "").strip()
    vid = (item.get("video_id") or item.get("id") or item.get("aweme_id") or "").strip()
    if unique_id and vid:
        return f"https://www.tiktok.com/@{unique_id}/video/{vid}"
    if vid:
        return f"https://www.tiktok.com/video/{vid}"
    return (fallback or "").strip()

def _build_info_text(item, keywords=None, link=None):
    info = _parse_item_fields(item)
    head = f"🔍 Kết quả cho: '{keywords}'\n\n" if keywords else "🎬 TikTok\n\n"
    ch = info["nickname"] or info["unique_id"] or "TikTok"
    uid = f"@{info['unique_id']}" if info["unique_id"] else ""
    stats = f"👁️ {_fmt_num(info['play'])} | ❤️ {_fmt_num(info['like'])} | 💬 {_fmt_num(info['cmt'])} | 🔁 {_fmt_num(info['share'])}"
    meta = f"⏱️ {info['dur']}"
    if info["created"]:
        meta += f" | 📅 {info['created']}"
    music = ""
    if info["music_title"] or info["music_author"]:
        music = f"\n🎵 {info['music_title'] or 'Unknown'}"
        if info["music_author"]:
            music += f" — {info['music_author']}"
    link_line = f"\n🔗 Link: {link}" if link else ""
    return f"{head}{info['title']}\n\n👤 {ch} {uid}\n{stats}\n{meta}{music}{link_line}".strip()

def _send_remote_video_safe(client, thread_id, thread_type, video_url, thumb_url, duration_ms, width, height, text, ttl):
    msg = Message(text=text)

    # 1) Signature phổ biến: sendRemoteVideo(videoUrl=..., thumbnailUrl=..., duration=..., thread_id=..., thread_type=..., width=..., height=..., message=..., ttl=...)
    try:
        client.sendRemoteVideo(
            videoUrl=video_url,
            thumbnailUrl=thumb_url,
            duration=int(duration_ms),
            thread_id=thread_id,
            thread_type=thread_type,
            width=int(width),
            height=int(height),
            message=msg,
            ttl=ttl
        )
        return True
    except TypeError:
        pass
    except Exception:
        pass

    # 2) Một số bản: sendRemoteVideo(videoUrl, thumbnailUrl, duration, thread_id, thread_type, width, height, message, ttl)
    try:
        client.sendRemoteVideo(
            video_url,
            thumb_url,
            int(duration_ms),
            thread_id,
            thread_type,
            int(width),
            int(height),
            msg,
            ttl
        )
        return True
    except TypeError:
        pass
    except Exception:
        pass

    # 3) Tối giản (nếu lib hỗ trợ)
    try:
        client.sendRemoteVideo(videoUrl=video_url, thumbnailUrl=thumb_url, duration=int(duration_ms), thread_id=thread_id, thread_type=thread_type, message=msg, ttl=ttl)
        return True
    except Exception:
        return False

def _send_video_local_first(client, thread_id, thread_type, video_url, thumb_url, duration_ms, width, height, text, ttl=240000):
    _ensure_dirs()
    tmp = f"{CACHE_DIR}/tiktok_{thread_id}_{_now()}_{os.getpid()}.mp4"
    try:
        if hasattr(client, "sendLocalVideo"):
            try:
                _download_to_file(video_url, tmp, timeout=70)
                try:
                    client.sendLocalVideo(
                        tmp,
                        thread_id=thread_id,
                        thread_type=thread_type,
                        width=int(width),
                        height=int(height),
                        duration=int(duration_ms),
                        message=Message(text=text),
                        ttl=ttl
                    )
                    return True
                except TypeError:
                    client.sendLocalVideo(
                        tmp,
                        thread_id=thread_id,
                        thread_type=thread_type,
                        message=Message(text=text),
                        ttl=ttl
                    )
                    return True
            except Exception:
                pass

        return _send_remote_video_safe(client, thread_id, thread_type, video_url, thumb_url, duration_ms, width, height, text, ttl)
    finally:
        _safe_remove(tmp)

def _send_list_image(client, thread_id, thread_type, keywords, top5):
    _cleanup_cache_files()
    img_path = _build_list_image(keywords, top5, max_items=5)

    # FIX WINERROR 32: tuyệt đối không giữ file open khi gửi + không xóa ngay
    w, h = 900, 1200
    try:
        with Image.open(img_path) as im:
            w, h = im.size
    except Exception:
        pass

    try:
        try:
            client.sendLocalImage(
                img_path,
                thread_id=thread_id,
                thread_type=thread_type,
                width=int(w),
                height=int(h),
                message=Message(text="Trả lời bằng số để tải (1-5)."),
                ttl=LIST_TTL_MS
            )
        except TypeError:
            client.sendLocalImage(
                img_path,
                thread_id=thread_id,
                thread_type=thread_type,
                message=Message(text="Trả lời bằng số để tải (1-5)."),
                ttl=LIST_TTL_MS
            )
    except Exception as e:
        raise e
    finally:
        # KHÔNG xóa ngay (Windows/Zalo PC hay giữ file đang dùng) -> dọn rác bằng _cleanup_cache_files ở các lần chạy sau
        pass

def _send_item(client, thread_id, thread_type, item, keywords=None):
    play_url = (item.get("play") or item.get("no_watermark") or "").strip()
    wmplay_url = (item.get("wmplay") or "").strip()
    cover_url = (item.get("cover") or item.get("thumbnail") or "").strip()
    chosen_video_url = play_url if play_url else wmplay_url

    duration_ms = 15000
    duration = item.get("duration")
    if duration is None:
        mi = item.get("music_info") or item.get("musicInfo") or {}
        if isinstance(mi, dict):
            duration = mi.get("duration")
    try:
        if duration is not None:
            duration_ms = max(3000, int(float(duration) * 1000))
    except Exception:
        duration_ms = 15000

    width, height = 1080, 1920
    link = _extract_best_link(item, fallback=chosen_video_url)
    info_text = _build_info_text(item, keywords=keywords, link=link)

    if chosen_video_url:
        ok = _send_video_local_first(
            client=client,
            thread_id=thread_id,
            thread_type=thread_type,
            video_url=chosen_video_url,
            thumb_url=cover_url or "",
            duration_ms=duration_ms,
            width=width,
            height=height,
            text=info_text,
            ttl=240000
        )
        if ok:
            try:
                client.sendMessage(Message(text=f"🔗 Link video: {link}"), thread_id, thread_type, ttl=240000)
            except Exception:
                pass
            return

    try:
        client.sendMessage(Message(text=info_text), thread_id, thread_type, ttl=120000)
        client.sendMessage(Message(text=f"🔗 Link video: {link}"), thread_id, thread_type, ttl=240000)
    except Exception:
        pass

# ====================== COMMANDS ======================

def handle_stiktok_command(message, message_object, thread_id, thread_type, author_id, client):
    key = (str(thread_id), str(author_id), "stiktok")
    if not _cooldown_ok(key, 4):
        return

    parts = (message or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        client.sendMessage(
            Message(text=f"Vui lòng nhập từ khóa hoặc số (1-5) hoặc link.\nVí dụ: {PREFIX}stiktok hoa lyric"),
            thread_id, thread_type, ttl=60000
        )
        return

    arg = parts[1].strip()

    url_in_arg = _first_url(arg)
    if url_in_arg and _looks_like_tiktok_url(url_in_arg):
        client.sendMessage(Message(text="⏳ Đang xử lý link TikTok..."), thread_id, thread_type, ttl=60000)
        data = _tikwm_by_url(url_in_arg, max_retries=3)
        if not isinstance(data, dict) or data.get("code", 1) != 0 or not data.get("data"):
            client.sendMessage(Message(text="❌ Không lấy được video từ link này."), thread_id, thread_type, ttl=12000)
            return
        _send_item(client, thread_id, thread_type, data.get("data") or {}, keywords=None)
        return

    if arg.isdigit():
        idx = int(arg)
        if idx < 1 or idx > 5:
            client.sendMessage(Message(text="Vui lòng chọn số từ 1 đến 5."), thread_id, thread_type, ttl=12000)
            return
        cached = _cache_get(thread_id, author_id)
        if not cached or not cached.get("videos"):
            client.sendMessage(Message(text=f"Chưa có danh sách. Hãy search trước: {PREFIX}stiktok <từ khóa>"), thread_id, thread_type, ttl=12000)
            return
        videos = cached.get("videos") or []
        if idx > len(videos):
            client.sendMessage(Message(text="Số bạn chọn vượt quá kết quả hiện có."), thread_id, thread_type, ttl=12000)
            return
        item = videos[idx - 1]
        _send_item(client, thread_id, thread_type, item, keywords=cached.get("keywords"))
        return

    keywords = arg
    client.sendMessage(Message(text=f"🔎 Đang tìm kiếm TikTok: '{keywords}' ..."), thread_id, thread_type, ttl=60000)

    data = _tikwm_search(keywords, count=10, cursor=0, max_retries=3)
    videos = _get_videos_from_search_payload(data)

    if not videos:
        client.sendMessage(
            Message(text="❌ Không lấy được kết quả.\nGợi ý: thử từ khóa khác hoặc mạng/DNS đang chặn tikwm.com"),
            thread_id, thread_type, ttl=12000
        )
        return

    top5 = videos[:5]
    _cache_put(thread_id, author_id, keywords, top5)

    try:
        _send_list_image(client, thread_id, thread_type, keywords, top5)
    except Exception as e:
        logger.error(f"send list image failed: {e}")
        lines = [f"🔍 Kết quả cho: '{keywords}'", ""]
        for i, it in enumerate(top5, start=1):
            info = _parse_item_fields(it)
            lines.append(f"{i}️⃣ {info['title'][:70]}")
            lines.append(f"👤 {info['nickname'] or info['unique_id'] or 'TikTok'} | ⏱️ {info['dur']} | 👁️ {_fmt_num(info['play'])}")
            lines.append("")
        lines.append(f"Chọn số để tải: {PREFIX}stiktok 1-5")
        try:
            client.sendMessage(Message(text="\n".join(lines).strip()), thread_id, thread_type, ttl=120000)
        except Exception:
            pass

def handle_tiktok_link_command(message, message_object, thread_id, thread_type, author_id, client):
    key = (str(thread_id), str(author_id), "tiktok")
    if not _cooldown_ok(key, 4):
        return

    parts = (message or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        client.sendMessage(
            Message(text=f"Ví dụ: {PREFIX}tiktok https://vt.tiktok.com/..."),
            thread_id, thread_type, ttl=60000
        )
        return

    url = _first_url(parts[1].strip()) or parts[1].strip()
    if not _looks_like_tiktok_url(url):
        client.sendMessage(Message(text="❌ Link không giống TikTok."), thread_id, thread_type, ttl=12000)
        return

    client.sendMessage(Message(text="⏳ Đang tải video TikTok..."), thread_id, thread_type, ttl=60000)
    data = _tikwm_by_url(url, max_retries=3)
    if not isinstance(data, dict) or data.get("code", 1) != 0 or not data.get("data"):
        client.sendMessage(Message(text="❌ Không lấy được video từ link này."), thread_id, thread_type, ttl=12000)
        return
    _send_item(client, thread_id, thread_type, data.get("data") or {}, keywords=None)

def handle_tt_download_command(message, message_object, thread_id, thread_type, author_id, client):
    # alias: !ttdl <link>
    key = (str(thread_id), str(author_id), "ttdl")
    if not _cooldown_ok(key, 4):
        return

    parts = (message or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        client.sendMessage(
            Message(text=f"Ví dụ: {PREFIX}ttdl https://vt.tiktok.com/..."),
            thread_id, thread_type, ttl=60000
        )
        return

    url = _first_url(parts[1].strip()) or parts[1].strip()
    if not _looks_like_tiktok_url(url):
        client.sendMessage(Message(text="❌ Link không giống TikTok."), thread_id, thread_type, ttl=12000)
        return

    client.sendMessage(Message(text="⏳ Đang tải video TikTok..."), thread_id, thread_type, ttl=60000)
    data = _tikwm_by_url(url, max_retries=3)
    if not isinstance(data, dict) or data.get("code", 1) != 0 or not data.get("data"):
        client.sendMessage(Message(text="❌ Không lấy được video từ link này."), thread_id, thread_type, ttl=12000)
        return
    _send_item(client, thread_id, thread_type, data.get("data") or {}, keywords=None)

def handle_ttauto_command(message, message_object, thread_id, thread_type, author_id, client):
    parts = (message or "").split(maxsplit=1)
    mode = (parts[1].strip().lower() if len(parts) > 1 else "")
    s = _load_settings()
    tid = str(thread_id)

    if mode not in ("on", "off", "1", "0", "true", "false"):
        cur = s["auto_threads"].get(tid, False)
        client.sendMessage(
            Message(text=f"Auto TikTok hiện đang: {'ON' if cur else 'OFF'}\nDùng: {PREFIX}ttauto on | off"),
            thread_id, thread_type, ttl=60000
        )
        return

    enable = mode in ("on", "1", "true")
    s["auto_threads"][tid] = bool(enable)
    _save_settings()
    client.sendMessage(Message(text=f"✅ Auto TikTok: {'ON' if enable else 'OFF'}"), thread_id, thread_type, ttl=60000)

def on_message_auto_tiktok(message_text, message_object, thread_id, thread_type, author_id, client):
    s = _load_settings()
    if not s["auto_threads"].get(str(thread_id), False):
        return

    if not message_text:
        return
    if message_text.strip().startswith(PREFIX):
        return

    url = _first_url(message_text)
    if not url or not _looks_like_tiktok_url(url):
        return

    cd_key = (str(thread_id), str(author_id), "auto")
    if not _autocooldown_ok(cd_key, AUTO_TTL_SEC):
        return

    data = _tikwm_by_url(url, max_retries=2)
    if not isinstance(data, dict) or data.get("code", 1) != 0 or not data.get("data"):
        return
    _send_item(client, thread_id, thread_type, data.get("data") or {}, keywords=None)

# ====================== REGISTER ======================

def QTinh():
    return {
        "stiktok": handle_stiktok_command,
        "searchtiktok": handle_stiktok_command,
        "dllink": handle_tiktok_link_command,
        "ttdl": handle_tt_download_command,
        "ttauto": handle_ttauto_command,
        "tiktokauto": handle_ttauto_command,
        "on_message_auto_tiktok": on_message_auto_tiktok
    }
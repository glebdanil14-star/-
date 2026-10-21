#!/usr/bin/env python3
"""
Telegram-бот «Твистед дня» для Dandy's World.

Берёт текущего твистеда с Daily Twisted Board с фанатской вики
(dandys-world-robloxhorror.fandom.com) и отправляет сообщение в Telegram.

Запуск:
    python bot.py              — обычный запуск (по расписанию)
    python bot.py --force      — отправить сообщение, даже если сегодня уже отправляли
    python bot.py --dry-run    — только показать сообщение, ничего не отправлять

Нужны переменные окружения:
    TELEGRAM_BOT_TOKEN  — токен от @BotFather
    TELEGRAM_CHAT_ID    — @имя_канала или числовой id чата
Необязательные:
    LOCAL_TZ            — часовой пояс для времени смены доски (по умолчанию Europe/Moscow)
    LOCAL_TZ_LABEL      — подпись к нему (по умолчанию МСК)
    PIN_MESSAGE=0       — не закреплять сообщение
    SEND_ART=0          — отправлять только текст, без картинки
    PINTEREST_USER      — твой ник на Pinterest (для артов с досок)
    ART_CREDIT=0        — не добавлять ссылку на источник арта с Pinterest

Арт: сначала бот ищет свою картинку в папке art/ (например art/Pebble.png),
потом случайный пин с доски Pinterest pinterest.com/<ник>/twisted-pebble,
если ничего нет — берёт картинку твистеда с вики. Прозрачные картинки
оформляются в карточку на фоне (нужен Pillow: pip install pillow;
без него картинка уйдёт как есть).
"""

import html
import io
import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # очень старый Python
    ZoneInfo = None

WIKI = "https://dandys-world-robloxhorror.fandom.com"
# Откуда пробуем взять твистеда: сначала шаблон, потом главная страница вики
SOURCE_PAGES = ["Template:TwistedOfTheDay", "Main_Page"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
ART_DIR = os.path.join(BASE_DIR, "art")
TELEGRAM_API = "https://api.telegram.org"
PINTEREST = "https://www.pinterest.com"

# Доска меняется в 00:00 UTC. Вики обновляют вручную, иногда с опозданием.
# Если до этого часа (UTC) на вики всё ещё вчерашний твистед — ждём следующего запуска.
# После этого часа отправляем как есть (твистед может и правда повториться).
LAST_TRY_HOUR_UTC = 6

USER_AGENT = "DandyTwistedOfTheDayBot/1.0 (Telegram bot, runs once a day)"

# Слово в имени твистеда: с большой буквы, но не служебное слово
_WORD = r"(?!Twisted\b|Render\b|The\b|Daily\b)[A-Z0-9][\w'’\-]*"
NAME_RE = re.compile(rf"Twisted\s+({_WORD}(?:\s+(?:&|and)\s+{_WORD}|\s+{_WORD})*)")


# ---------- получение данных с вики ----------

def http_get_json(url: str, params: dict) -> dict:
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_page(page: str) -> tuple[str, str]:
    """Возвращает (готовый HTML, вики-разметку) страницы через MediaWiki API."""
    data = http_get_json(
        f"{WIKI}/api.php",
        {
            "action": "parse",
            "page": page,
            "prop": "text|wikitext",
            "format": "json",
            "formatversion": "2",
        },
    )
    if "error" in data:
        raise RuntimeError(f"Вики вернула ошибку для {page}: {data['error']}")
    parsed = data.get("parse", {})
    return parsed.get("text", ""), parsed.get("wikitext", "")


def html_to_text(raw_html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw_html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_twisted(raw_html: str, wikitext: str) -> str | None:
    """Достаёт имя твистеда (без слова Twisted), например 'Teagan' или 'Razzle & Dazzle'."""
    text = html_to_text(raw_html)

    # 1) Фраза с вики: "the Daily Twisted Board is occupied by Twisted Teagan."
    m = re.search(r"occupied by\s+(?:the\s+)?(.{0,200})", text, flags=re.I)
    if m:
        segment = re.split(r"\.\s|\.$|\bThe Daily\b", m.group(1))[0]
        names = NAME_RE.findall(segment)
        if names:
            return names[-1].strip()

    # 2) Ссылка в разметке шаблона: [[Twisted Teagan]] или [[Twisted Teagan|...]]
    links = re.findall(r"\[\[\s*Twisted[ _]([^\]|#]+)", wikitext or "")
    links = [l.replace("_", " ").strip() for l in links if not l.lower().startswith("board")]
    if links:
        return links[0]

    # 3) Последний шанс: первое упоминание "Twisted X" в тексте шаблона
    names = NAME_RE.findall(text)
    if names:
        return names[0].strip()
    return None


def get_current_twisted() -> tuple[str | None, str]:
    """Возвращает (имя твистеда, HTML страницы, где его нашли — для поиска картинки)."""
    for page in SOURCE_PAGES:
        try:
            raw_html, wikitext = fetch_page(page)
        except Exception as e:  # сеть, 404, изменился формат — пробуем следующий источник
            print(f"[warn] Не удалось прочитать {page}: {e}", file=sys.stderr)
            continue
        name = extract_twisted(raw_html, wikitext)
        if name:
            print(f"[info] {page}: твистед дня — Twisted {name}")
            return name, raw_html
        print(f"[warn] На странице {page} не нашёл твистеда", file=sys.stderr)
    return None, ""


# ---------- арт ----------

def _norm(s: str) -> str:
    """'Razzle & Dazzle' -> 'razzledazzle' — чтобы сравнивать имена с именами файлов."""
    s = re.sub(r"\band\b", "", urllib.parse.unquote(s).lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag, flags=re.I)
    return html.unescape(m.group(1)) if m else ""


def find_wiki_image_url(raw_html: str, name: str) -> str | None:
    """Ищет на странице картинку именно этого твистеда и возвращает ссылку на полный размер."""
    key = _norm(name)
    best, best_score = None, 0
    for tag in re.findall(r"<img\b[^>]*>", raw_html, flags=re.I):
        # Fandom подгружает картинки лениво: настоящая ссылка часто в data-src
        url = _attr(tag, "data-src") or _attr(tag, "src")
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http") or ".svg" in url.lower():
            continue
        # Сравниваем только имя файла и alt: в домене вики есть слово «dandys»
        file_name = urllib.parse.urlparse(url).path.split("/revision/")[0].rsplit("/", 1)[-1]
        haystack = _norm(file_name) + " " + _norm(_attr(tag, "alt"))
        if key not in haystack:
            continue
        score = 2 if "twisted" in haystack else 1
        if score > best_score:
            best, best_score = url, score
    if not best:
        return None
    # Убираем уменьшение (…/scale-to-width-down/150) — берём оригинал
    return re.sub(r"/revision/latest/[^?]*", "/revision/latest", best)


def download(url: str, limit: int = 15 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("картинка слишком большая")
    return data


def find_own_art(name: str) -> str | None:
    """Своя картинка: art/Pebble.png, art/Twisted Pebble.jpg, art/razzle_dazzle.webp и т.п."""
    if not os.path.isdir(ART_DIR):
        return None
    key = _norm(name)
    for fname in sorted(os.listdir(ART_DIR)):
        stem, ext = os.path.splitext(fname)
        if ext.lower() in (".png", ".jpg", ".jpeg", ".webp") and _norm(stem) in (key, "twisted" + key):
            return os.path.join(ART_DIR, fname)
    return None


def make_card(raw: bytes) -> bytes:
    """Прозрачный рендер → карточка 1080×1080 на тёмном фоне со свечением цвета персонажа.
    Обычные (непрозрачные) картинки — фан-арты и т.п. — отправляются как есть."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return raw
    import colorsys

    src = Image.open(io.BytesIO(raw))
    has_alpha = src.mode in ("RGBA", "LA", "P") and src.convert("RGBA").getchannel("A").getextrema()[0] < 250
    if not has_alpha:
        img = src.convert("RGB")
        img.thumbnail((2560, 2560))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=92)
        return out.getvalue()

    char = src.convert("RGBA")
    bbox = char.getchannel("A").getbbox()
    if bbox:
        char = char.crop(bbox)

    # Цвет свечения — из самых ярких и насыщенных частей персонажа
    # (твистеды в основном чёрные, а цвет у них — в глазах и деталях)
    small = char.resize((64, 64))
    scored = []
    for p in small.getdata():
        if p[3] > 200:
            h, s, v = colorsys.rgb_to_hsv(p[0] / 255, p[1] / 255, p[2] / 255)
            scored.append((s * v, p))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = [p for sv, p in scored[: max(1, len(scored) // 8)] if sv > 0.15]
    if top:
        r, g, b = (sum(p[i] for p in top) / len(top) / 255 for i in range(3))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        r, g, b = colorsys.hsv_to_rgb(h, max(s, 0.6), max(v, 0.85))
        accent = (int(r * 255), int(g * 255), int(b * 255))
    else:
        accent = (170, 110, 230)  # если персонаж совсем без цвета — фиолетовое свечение

    W = H = 1080
    # Фон: вертикальный градиент от тёмно-фиолетового к почти чёрному
    top, bottom = (30, 18, 46), (8, 6, 12)
    grad = Image.linear_gradient("L").resize((W, H))
    bg = Image.composite(Image.new("RGB", (W, H), bottom), Image.new("RGB", (W, H), top), grad)

    # Мягкое свечение за персонажем
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((W * 0.2, H * 0.18, W * 0.8, H * 0.82), fill=accent + (175,))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    bg = Image.alpha_composite(bg.convert("RGBA"), glow)

    # Персонаж: вписываем в 84% карточки
    scale = min(W * 0.84 / char.width, H * 0.84 / char.height)
    char = char.resize((max(1, int(char.width * scale)), max(1, int(char.height * scale))), Image.LANCZOS)
    x, y = (W - char.width) // 2, (H - char.height) // 2 + 20

    # Тень под персонажем
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_mask = char.getchannel("A").point(lambda a: int(a * 0.6))
    shadow.paste((0, 0, 0, 255), (x + 14, y + 22), shadow_mask)
    bg = Image.alpha_composite(bg, shadow.filter(ImageFilter.GaussianBlur(18)))

    bg.alpha_composite(char, (x, y))
    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


def board_slugs(name: str) -> list[str]:
    """Варианты адреса доски: 'Razzle & Dazzle' → twisted-razzle-and-dazzle, twisted-razzle-dazzle."""
    base = name.lower().replace("&", " and ")
    with_and = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    without_and = re.sub(r"-and-", "-", with_and)
    slugs = []
    for s in (with_and, without_and):
        for candidate in (f"twisted-{s}", s):
            if candidate not in slugs:
                slugs.append(candidate)
    return slugs


def pinterest_pins(user: str, name: str) -> tuple[str, list[tuple[str, str]]]:
    """Читает RSS доски твистеда. Возвращает (адрес доски, [(картинка, ссылка на пин), ...])."""
    import xml.etree.ElementTree as ET

    for slug in board_slugs(name):
        feed = f"{PINTEREST}/{user}/{slug}.rss"
        try:
            req = urllib.request.Request(feed, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                root = ET.fromstring(resp.read())
        except Exception:
            continue  # такой доски нет — пробуем следующий вариант адреса
        pins = []
        for item in root.iter("item"):
            desc = item.findtext("description") or ""
            m = re.search(r'<img[^>]+src="([^"]+)"', desc)
            if m:
                pins.append((html.unescape(m.group(1)), (item.findtext("link") or "").strip()))
        if pins:
            return f"pinterest.com/{user}/{slug}", pins
    return "", []


def get_pinterest_art(name: str, last_pin: str | None) -> tuple[bytes, str] | None:
    """Случайный арт с доски Pinterest (не тот же, что в прошлый раз)."""
    import random

    user = os.environ.get("PINTEREST_USER", "").strip().strip("/")
    if not user:
        return None
    board, pins = pinterest_pins(user, name)
    if not pins:
        print(f"[info] Доски Pinterest для Twisted {name} нет или она пустая — "
              f"ждал адрес вроде pinterest.com/{user}/{board_slugs(name)[0]}")
        return None
    fresh = [p for p in pins if p[1] != last_pin] or pins
    img_url, pin_link = random.choice(fresh)
    print(f"[info] Беру арт с доски {board}: {pin_link or img_url}")
    # В RSS лежат уменьшенные картинки (236x) — пробуем побольше
    for variant in (re.sub(r"/\d+x/", "/736x/", img_url), img_url):
        try:
            return download(variant), pin_link
        except Exception:
            continue
    return None


def get_art(name: str, page_html: str, last_pin: str | None = None) -> tuple[bytes | None, str | None]:
    """Возвращает (картинка, ссылка на пин или None).
    Порядок: своя картинка из art/ → доска Pinterest → картинка с вики.
    Никогда не падает: при любой ошибке переходит к следующему источнику."""
    try:
        own = find_own_art(name)
        if own:
            print(f"[info] Беру свой арт: {own}")
            with open(own, "rb") as f:
                return make_card(f.read()), None
    except Exception as e:
        print(f"[warn] Не получилось взять свой арт: {e}", file=sys.stderr)

    try:
        found = get_pinterest_art(name, last_pin)
        if found:
            raw, pin_link = found
            return make_card(raw), pin_link
    except Exception as e:
        print(f"[warn] Не получилось взять арт с Pinterest: {e}", file=sys.stderr)

    try:
        url = find_wiki_image_url(page_html, name)
        if not url:
            print(f"[warn] На вики не нашёл картинку для Twisted {name}", file=sys.stderr)
            return None, None
        print(f"[info] Беру арт с вики: {url}")
        return make_card(download(url)), None
    except Exception as e:
        print(f"[warn] Не получилось взять арт с вики: {e}", file=sys.stderr)
        return None, None


# ---------- русские имена и эмодзи ----------
# Английское имя с вики → (русское имя, эмодзи).
# Если появится новый твистед, которого тут нет, бот напишет английское имя и 🖤.
# Можно спокойно править: имена и эмодзи — на твой вкус.
TOONS = {
    "Astro": ("Астро", "🌙"),
    "Bassie": ("Басси", "🧺"),
    "Blot": ("Блот", "🖋️"),
    "Bobette": ("Бобетт", "🎄"),
    "Boxten": ("Бокстен", "📦"),
    "Brightney": ("Брайтни", "💡"),
    "Brusha": ("Бруша", "🖌️"),
    "Coal": ("Коул", "🪨"),
    "Cocoa": ("Коко", "☕"),
    "Connie": ("Конни", "🍦"),
    "Cosmo": ("Космо", "🥐"),
    "Dandy": ("Денди", "🌼"),
    "Dyle": ("Дайл", "🕰️"),
    "Eclipse": ("Эклипс", "🌑"),
    "Eggson": ("Эггсон", "🥚"),
    "Finn": ("Финн", "🐟"),
    "Flutter": ("Флаттер", "🦋"),
    "Flyte": ("Флайт", "🪽"),
    "Gigi": ("Гиги", "🎰"),
    "Ginger": ("Джинджер", "🍪"),
    "Glisten": ("Глистен", "🪞"),
    "Goob": ("Губ", "🧸"),
    "Gourdy": ("Гурди", "🎃"),
    "Looey": ("Луи", "🎈"),
    "Pebble": ("Пебл", "🐶"),
    "Poppy": ("Поппи", "🌺"),
    "Razzle & Dazzle": ("Раззл и Даззл", "🎭"),
    "Ribecca": ("Рибекка", "🎀"),
    "Rodger": ("Роджер", "🔍"),
    "Rudie": ("Руди", "🦌"),
    "Scraps": ("Скрапс", "✂️"),
    "Shelly": ("Шелли", "🦕"),
    "Shrimpo": ("Шримпо", "🦐"),
    "Soulvester": ("Соулвестер", "👻"),
    "Sprout": ("Спраут", "🍓"),
    "Squirm": ("Сквирм", "🐛"),
    "Teagan": ("Тиган", "🫖"),
    "Tisha": ("Тиша", "🧻"),
    "Toodles": ("Тудлс", "🐾"),
    "Vee": ("Ви", "📺"),
    "Yatta": ("Ятта", "🎉"),
}

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def ru_toon(name: str) -> tuple[str, str]:
    """Возвращает (русское имя, эмодзи). Ищет без учёта регистра и 'and' вместо '&'."""
    key = name.replace(" and ", " & ").strip().lower()
    for eng, value in TOONS.items():
        if eng.lower() == key:
            return value
    print(f"[warn] Нет русского имени для «{name}» — добавь его в TOONS", file=sys.stderr)
    return name, "🖤"


# ---------- сообщение и отправка ----------

def build_message(name: str, now_utc: datetime, pin_link: str | None = None) -> str:
    next_reset = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tz_name = os.environ.get("LOCAL_TZ", "Europe/Moscow")
    tz_label = os.environ.get("LOCAL_TZ_LABEL", "МСК")

    reset_text = "00:00 UTC"
    if ZoneInfo is not None:
        try:
            local = next_reset.astimezone(ZoneInfo(tz_name))
            reset_text = f"{local:%H:%M} {tz_label}"
        except Exception:
            pass

    ru_name, emoji = ru_toon(name)
    date_text = f"{now_utc.day} {MONTHS_RU[now_utc.month - 1]}"

    text = (
        "🎲 <b>DANDY'S WORLD · ТВИСТЕД ДНЯ</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"{emoji} <b>Твистед {html.escape(ru_name)}</b>\n"
        f"<i>Twisted {html.escape(name)}</i>\n\n"
        "<blockquote>📈 Весь день этот твистед будет попадаться на этажах чаще обычного. "
        "Самое время добить исследование!</blockquote>\n\n"
        f"📅 {date_text}\n"
        f"⏳ Смена доски в <b>{reset_text}</b>"
    )
    # Ссылка на источник арта с Pinterest (уважение к художникам). ART_CREDIT=0 — убрать.
    if pin_link and os.environ.get("ART_CREDIT", "1") != "0":
        text += f'\n🎨 <a href="{html.escape(pin_link, quote=True)}">источник арта</a>'
    return text


def telegram(token: str, method: str, params: dict, photo: bytes | None = None):
    """Вызывает метод Telegram Bot API и возвращает поле result.
    Если передан photo — отправляет multipart/form-data с файлом."""
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    headers = {"User-Agent": USER_AGENT}
    if photo is None:
        body = urllib.parse.urlencode(params).encode("utf-8")
    else:
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in params.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="twisted.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n".encode("utf-8") + photo + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram ({method}) ответил ошибкой {e.code}: {details}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram ({method}) не принял запрос: {result}")
    return result.get("result")


def send_telegram(token: str, chat_id: str, text: str) -> int:
    """Отправляет сообщение и возвращает его message_id."""
    msg = telegram(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    return msg["message_id"]


def send_photo(token: str, chat_id: str, photo: bytes, caption: str) -> int:
    """Отправляет картинку с подписью и возвращает message_id."""
    msg = telegram(
        token,
        "sendPhoto",
        {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        photo=photo,
    )
    return msg["message_id"]


def repin(token: str, chat_id: str, new_id: int, old_id: int | None) -> None:
    """Закрепляет новое сообщение и открепляет вчерашнее.
    Ошибки только печатаются: сообщение уже отправлено, из-за закрепа не падаем."""
    try:
        # disable_notification — без лишнего уведомления «бот закрепил сообщение»
        telegram(token, "pinChatMessage",
                 {"chat_id": chat_id, "message_id": new_id, "disable_notification": "true"})
        print(f"[ok] Закрепил сообщение {new_id}")
    except Exception as e:
        print(f"[warn] Не смог закрепить: {e}\n"
              "       Проверь, что у бота есть право закреплять/редактировать сообщения.",
              file=sys.stderr)
        return  # старое не трогаем, чтобы в канале остался хоть какой-то закреп

    if old_id and old_id != new_id:
        try:
            telegram(token, "unpinChatMessage", {"chat_id": chat_id, "message_id": old_id})
            print(f"[ok] Открепил старое сообщение {old_id}")
        except Exception as e:
            print(f"[warn] Не смог открепить старое сообщение {old_id}: {e}", file=sys.stderr)


# ---------- состояние (чтобы не слать дважды) ----------

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- главное ----------

def main() -> int:
    force = "--force" in sys.argv
    dry_run = "--dry-run" in sys.argv

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = load_state()

    if state.get("date") == today and not force:
        print(f"[info] Сегодня ({today}) уже отправлял: Twisted {state.get('twisted')}. Выхожу.")
        return 0

    name, page_html = get_current_twisted()
    if not name:
        print("[error] Не получилось узнать твистеда дня. Возможно, вики изменила формат.", file=sys.stderr)
        return 1

    if name == state.get("twisted") and now.hour < LAST_TRY_HOUR_UTC and not force:
        print(
            f"[info] На вики всё ещё Twisted {name}, как вчера. "
            "Похоже, вики ещё не обновили — попробую при следующем запуске."
        )
        return 0

    photo, pin_link = None, None
    if os.environ.get("SEND_ART", "1") != "0":
        photo, pin_link = get_art(name, page_html, state.get("last_pin"))
    text = build_message(name, now, pin_link)

    if dry_run:
        print("----- сообщение (dry run, не отправлено) -----")
        print(text)
        if photo:
            preview = os.path.join(BASE_DIR, "preview.jpg")
            with open(preview, "wb") as f:
                f.write(photo)
            print(f"----- картинка сохранена: {preview}")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[error] Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    message_id = None
    if photo:
        try:
            message_id = send_photo(token, chat_id, photo, text)
        except Exception as e:
            print(f"[warn] Не получилось отправить с картинкой, отправляю только текст: {e}",
                  file=sys.stderr)
    if message_id is None:
        text = build_message(name, now)  # без картинки ссылка на арт не нужна
        message_id = send_telegram(token, chat_id, text)
    # Сохраняем сразу после отправки, чтобы при сбое закрепа не отправить второй раз
    save_state({"date": today, "twisted": name, "message_id": message_id,
                "last_pin": pin_link or state.get("last_pin")})
    print(f"[ok] Отправил: Twisted {name} (сообщение {message_id})")

    if os.environ.get("PIN_MESSAGE", "1") != "0":
        repin(token, chat_id, message_id, state.get("message_id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

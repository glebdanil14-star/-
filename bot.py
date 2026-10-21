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

Использует только стандартную библиотеку Python 3.9+, ничего ставить не нужно.
"""

import html
import json
import os
import re
import sys
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

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

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


def get_current_twisted() -> str | None:
    for page in SOURCE_PAGES:
        try:
            raw_html, wikitext = fetch_page(page)
        except Exception as e:  # сеть, 404, изменился формат — пробуем следующий источник
            print(f"[warn] Не удалось прочитать {page}: {e}", file=sys.stderr)
            continue
        name = extract_twisted(raw_html, wikitext)
        if name:
            print(f"[info] {page}: твистед дня — Twisted {name}")
            return name
        print(f"[warn] На странице {page} не нашёл твистеда", file=sys.stderr)
    return None


# ---------- сообщение и отправка ----------

def build_message(name: str, now_utc: datetime) -> str:
    next_reset = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tz_name = os.environ.get("LOCAL_TZ", "Europe/Moscow")
    tz_label = os.environ.get("LOCAL_TZ_LABEL", "МСК")
    local_part = ""
    if ZoneInfo is not None:
        try:
            local = next_reset.astimezone(ZoneInfo(tz_name))
            local_part = f" / {local:%H:%M} {tz_label}"
        except Exception:
            pass

    page = urllib.parse.quote(f"Twisted_{name.replace(' ', '_')}")
    wiki_link = f"{WIKI}/wiki/{page}"
    safe_name = html.escape(name)

    return (
        "🎲 <b>Твистед дня в Dandy's World</b>\n\n"
        f"Сегодня на доске: <b>Twisted {safe_name}</b>\n"
        "Шанс встретить его на этажах повышен.\n\n"
        f"⏰ Доска сменится в 00:00 UTC{local_part}\n"
        f'📖 <a href="{wiki_link}">Twisted {safe_name} на вики</a>'
    )


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram ответил ошибкой {e.code}: {details}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram не принял сообщение: {result}")


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

    name = get_current_twisted()
    if not name:
        print("[error] Не получилось узнать твистеда дня. Возможно, вики изменила формат.", file=sys.stderr)
        return 1

    if name == state.get("twisted") and now.hour < LAST_TRY_HOUR_UTC and not force:
        print(
            f"[info] На вики всё ещё Twisted {name}, как вчера. "
            "Похоже, вики ещё не обновили — попробую при следующем запуске."
        )
        return 0

    text = build_message(name, now)

    if dry_run:
        print("----- сообщение (dry run, не отправлено) -----")
        print(text)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[error] Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    send_telegram(token, chat_id, text)
    save_state({"date": today, "twisted": name})
    print(f"[ok] Отправил: Twisted {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

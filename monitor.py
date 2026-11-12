#!/usr/bin/env python3
"""
Ticket watcher:
  * London - The Nutcracker, Royal Ballet and Opera (Royal Opera House)
  * Paris  - Hammer, Théâtre du Châtelet (main shop + Châtelet's official resale
             exchange). Paris settings: PARIS_TARGET_SEATS, PARIS_ONLY_DATES
             (e.g. "9 March, 13 March"), PARIS_ENABLED / LONDON_ENABLED (0 = off).

Details for London below; Paris works the same way.

What it does
  * Every few minutes it opens The Nutcracker page in a real (headless) browser
    and reads the status shown next to every performance date.
  * It opens performance seat maps (any performance whose status just changed,
    plus a few others in rotation) and looks for available seats in your
    target rows - by default Orchestra Stalls rows A-G and Stalls Circle row A.
  * It sends you a Telegram message the moment matching seats appear.
  * In Telegram you can send /status to see what it currently sees, /check to
    force a check right now, and /help.

It only reads public pages, politely, a few times an hour. It never books,
never logs in and never tries to get around waiting rooms or captchas.

Settings come from environment variables (or a .env file next to this script):
  TELEGRAM_BOT_TOKEN   token from @BotFather                        (required)
  TELEGRAM_CHAT_ID     your chat id; several allowed, comma-separated (required)
  TARGET_SEATS         default "Orchestra Stalls: A-G; Stalls Circle: A"
  ONLY_DATES           optional, e.g. "19 December, 23 December"
  CHECK_EVERY_MINUTES  default 3
  SEATMAPS_PER_CYCLE   default 3  (seat maps opened per check, in rotation)
  RUN_FOR_MINUTES      default 0 = run forever (GitHub Actions sets ~330)
  PRODUCTION_URL       default The Nutcracker 2026/27 page

Command-line:
  python monitor.py                 run the watcher
  python monitor.py --once          one check, print what it sees, no Telegram
  python monitor.py --test-telegram send a test message and exit
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"
LONDON = ZoneInfo("Europe/London")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value and value.strip() else default


PRODUCTION_URL = _env("PRODUCTION_URL", "https://www.rbo.org.uk/production/the-nutcracker-peter-wright")
BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [c.strip() for c in _env("TELEGRAM_CHAT_ID").split(",") if c.strip()]
TARGETS_RAW = _env("TARGET_SEATS", "Orchestra Stalls: A-G; Stalls Circle: A")
ONLY_DATES_RAW = _env("ONLY_DATES")
CHECK_EVERY_MINUTES = float(_env("CHECK_EVERY_MINUTES", "3"))
SEATMAPS_PER_CYCLE = int(_env("SEATMAPS_PER_CYCLE", "3"))
RUN_FOR_MINUTES = float(_env("RUN_FOR_MINUTES", "0"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def log(*parts) -> None:
    stamp = dt.datetime.now(LONDON).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}]", *parts, flush=True)


def norm(text: str) -> str:
    """Lower-case, single spaces, no commas - used for keys and matching."""
    text = (text or "").lower().replace(",", " ").replace(":", ".")
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Target rows
# --------------------------------------------------------------------------

def parse_targets(raw: str) -> list[tuple[str, set[str]]]:
    """'Orchestra Stalls: A-G; Stalls Circle: A' -> [('orchestra stalls', {A..G}), ...]"""
    targets = []
    for part in raw.split(";"):
        if ":" not in part:
            continue
        section, rows = part.split(":", 1)
        row_set: set[str] = set()
        for item in rows.split(","):
            item = item.strip().upper()
            if not item:
                continue
            m = re.fullmatch(r"([A-Z])\s*-\s*([A-Z])", item)
            n = re.fullmatch(r"(\d+)\s*-\s*(\d+)", item)
            if m:
                for code in range(ord(m.group(1)), ord(m.group(2)) + 1):
                    row_set.add(chr(code))
            elif n:
                for number in range(int(n.group(1)), int(n.group(2)) + 1):
                    row_set.add(str(number))
            else:
                row_set.add(item.lstrip("0") or item)
        if section.strip() and row_set:
            targets.append((norm(section), row_set))
    return targets


TARGETS = parse_targets(TARGETS_RAW)
ONLY_DATES = [norm(d) for d in ONLY_DATES_RAW.split(",") if d.strip()]


def _plain(text: str) -> str:
    """lower case, accents removed: 'Nuñez' -> 'nunez'"""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


class RBOShow:
    """One Royal Ballet and Opera production to watch."""

    def __init__(self, key, name, url, targets_raw, only_dates_raw="", cast_raw=""):
        self.key, self.name, self.url = key, name, url
        self.targets_raw = targets_raw
        self.targets = parse_targets(targets_raw)
        self.only_dates_raw = only_dates_raw
        self.only_dates = [norm(d) for d in only_dates_raw.split(",") if d.strip()]
        self.cast_raw = cast_raw
        self.cast = [_plain(c.strip()) for c in cast_raw.split(",") if c.strip()]

    def state(self, state: dict) -> dict:
        # The Nutcracker keeps its state at the top level (as before); other shows get their own section.
        return state if self.key == "london" else state.setdefault(self.key, {})

    def watched(self, key: str, perf: dict | None = None) -> bool:
        if self.only_dates and not any(
                re.search(r"(?<!\d)" + re.escape(d) + r"(?![\w])", key) for d in self.only_dates):
            return False
        if self.cast:
            text = _plain((perf or {}).get("text", ""))
            return any(name in text for name in self.cast)
        return True

    def describe(self) -> str:
        return describe_targets(self.targets)


def _describe_rows(rows: list) -> str:
    if len(rows) > 2 and all(
        (int(b) - int(a) if a.isdigit() and b.isdigit() else ord(b) - ord(a)) == 1
        for a, b in zip(rows, rows[1:])
    ):
        return f"rows {rows[0]}–{rows[-1]}"
    return ("row " if len(rows) == 1 else "rows ") + ", ".join(rows)


def describe_targets(targets=None) -> str:
    parts = []
    for section, rows in (TARGETS if targets is None else targets):
        numbers = sorted((r for r in rows if r.isdigit()), key=int)
        letters = sorted(r for r in rows if not r.isdigit())
        # When both "1-6" and "A-F" are given, show the numbers (same rows).
        shown = numbers or letters
        parts.append(f"{section.title()} {_describe_rows(shown)}")
    return " or ".join(parts)


def seat_is_target(seat: dict, targets=None) -> bool:
    section = norm(seat.get("section", ""))
    row = (seat.get("row") or "").strip().upper()
    row = row.lstrip("0") or row
    if not section or not row or "standing" in section:
        return False
    return any(t_section in section and row in t_rows
               for t_section, t_rows in (TARGETS if targets is None else targets))


def date_is_watched(key: str) -> bool:
    if not ONLY_DATES:
        return True
    return any(re.search(r"(?<!\d)" + re.escape(d) + r"(?![\w])", key) for d in ONLY_DATES)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _telegram(method: str, params: dict, timeout: float = 20) -> dict | None:
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # network blips must never kill the watcher
        log(f"Telegram {method} failed: {exc}")
        return None


def send(text: str, silent: bool = False, chat_ids: list[str] | None = None) -> None:
    for chat_id in chat_ids or CHAT_IDS:
        _telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            },
        )
    log(("[silent] " if silent else "[alert] ") + re.sub(r"<[^>]+>", "", text).replace("\n", " | "))


def poll_commands(state: dict, wait_seconds: float) -> list[tuple[str, str]]:
    """Long-poll Telegram; returns [(chat_id, command)] from allowed chats."""
    if not BOT_TOKEN:
        time.sleep(max(0.0, wait_seconds))
        return []
    timeout = int(max(0, min(25, wait_seconds)))
    result = _telegram(
        "getUpdates",
        {"offset": state.get("tg_offset", 0), "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 15,
    )
    if not result or not result.get("ok"):
        time.sleep(min(5, max(0.0, wait_seconds)))
        return []
    commands = []
    for update in result.get("result", []):
        state["tg_offset"] = update["update_id"] + 1
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if chat_id not in CHAT_IDS:
            log(f"Ignoring message from chat {chat_id} (not in TELEGRAM_CHAT_ID)")
            continue
        if text.startswith("/"):
            commands.append((chat_id, text.split()[0].split("@")[0].lower()))
    return commands


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("state.json unreadable - starting fresh")
    return {}


def save_state(state: dict) -> None:
    state["heartbeat"] = dt.datetime.now(LONDON).date().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Reading the production page
# --------------------------------------------------------------------------

DAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE_SRC = (
    rf"(?:{DAYS})\s+\d{{1,2}}\s+(?:{MONTHS})(?:\s+\d{{4}})?,?\s+"
    r"\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm)"
)

# Runs inside the page: finds each performance row by its date label
# ("Thursday 3 December, 7.30pm") and returns the row's visible text + links.
EXTRACT_ROWS_JS = r"""
(src) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const dates = t => {
    const re = new RegExp(src, 'gi');
    return Array.from(new Set((norm(t).match(re) || []).map(x => norm(x).toLowerCase())));
  };
  const monthRe = /(January|February|March|April|May|June|July|August|September|October|November|December)/i;
  const cands = [];
  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName.toUpperCase();
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') continue;
    const tc = el.textContent || '';
    if (tc.length > 400 || !monthRe.test(tc)) continue;
    const t = norm(el.innerText);
    if (!t || t.length > 120) continue;
    if (dates(t).length === 1) cands.push(el);
  }
  const set = new Set(cands);
  const labels = cands.filter(el => !Array.from(el.querySelectorAll('*')).some(c => set.has(c)));
  const out = [];
  const seen = new Set();
  for (const label of labels) {
    const d = dates(label.innerText)[0];
    let row = label;
    while (row.parentElement && row.parentElement !== document.body) {
      const p = row.parentElement;
      if (dates(p.innerText).length > 1) break;
      row = p;
    }
    if (seen.has(row)) continue;
    seen.add(row);
    const links = Array.from(row.querySelectorAll('a[href]')).map(a => ({
      text: norm(a.innerText || a.getAttribute('aria-label') || ''), href: a.href }));
    const buttons = Array.from(row.querySelectorAll('button')).map(b => ({
      text: norm(b.innerText || b.getAttribute('aria-label') || ''),
      disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true' }));
    out.push({ date: d, text: norm(row.innerText).slice(0, 700), links, buttons });
  }
  return out;
}
"""

SOLD_OUT_RE = re.compile(r"sold\s*out|no tickets|returns only|wait\s*list|waiting list", re.I)
UNAVAILABLE_RE = re.compile(r"unavailable|not available|off sale|not on sale|on sale soon|cancelled", re.I)
AVAILABLE_RE = re.compile(
    r"limited availability|few (?:tickets|seats)|last few|tickets available|available|"
    r"buy tickets|book now|book tickets|select seats|choose seats|buy now|\bbook\b|\bbuy\b",
    re.I,
)
BOOK_LINK_TEXT_RE = re.compile(r"buy|book|ticket|select|seat|choose", re.I)
BOOK_LINK_HREF_RE = re.compile(r"seat|book|basket|checkout|performance|purchase|tnew|ticket", re.I)


def classify(text: str) -> str:
    if SOLD_OUT_RE.search(text):
        return "sold_out"
    if UNAVAILABLE_RE.search(text):
        return "unavailable"
    if AVAILABLE_RE.search(text):
        return "available"
    return "unknown"


def pick_link(row: dict, production_url: str = "") -> str | None:
    production_path = urllib.parse.urlparse(production_url or PRODUCTION_URL).path.rstrip("/")
    links = [
        l for l in row.get("links", [])
        if l.get("href", "").startswith("http")
        and "#" not in l["href"]
        and urllib.parse.urlparse(l["href"]).path.rstrip("/") != production_path
    ]
    for link in links:
        if BOOK_LINK_TEXT_RE.search(link.get("text", "")) and "/people/" not in link["href"]:
            return link["href"]
    for link in links:
        if BOOK_LINK_HREF_RE.search(link["href"]) and "/people/" not in link["href"]:
            return link["href"]
    return None


def rows_to_performances(rows: list[dict], production_url: str = "") -> dict[str, dict]:
    performances: dict[str, dict] = {}
    for row in rows:
        key = norm(row["date"])
        perf = {
            "label": re.sub(r"\s+", " ", row["date"]).strip().title().replace("Am", "am").replace("Pm", "pm"),
            "status": classify(row.get("text", "")),
            "text": row.get("text", "")[:500],
            "link": pick_link(row, production_url),
        }
        old = performances.get(key)
        # The same date can appear twice (e.g. a calendar strip and the list);
        # keep whichever copy says more.
        if old is None or (old["status"] == "unknown" and perf["status"] != "unknown") or (
            not old["link"] and perf["link"] and perf["status"] != "unknown"
        ):
            performances[key] = perf
    return performances


def _accept_cookies(page) -> None:
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept cookies')",
        "button:has-text('Allow all')",
    ):
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=800):
                button.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def _expand_all_dates(page) -> None:
    pattern = re.compile(r"show all dates|expand all dates|show more dates|more dates|load more", re.I)
    for _ in range(6):
        clicked = False
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=pattern)
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible():
                        item.click(timeout=3000)
                        page.wait_for_timeout(1500)
                        clicked = True
            except Exception:
                pass
        if not clicked:
            return


def in_waiting_room(page) -> bool:
    url = (page.url or "").lower()
    return ("queue-it" in url or "queue.rbo" in url or "queue.roh" in url
            or "/waitingroom" in url or "/waiting-room" in url or "/queue" in url)


def read_production_page(context, url: str = "") -> tuple[dict[str, dict], dict]:
    url = url or PRODUCTION_URL
    page = context.new_page()
    info = {"waiting_room": False, "page_sold_out_banner": False}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if in_waiting_room(page):
            info["waiting_room"] = True
            return {}, info
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _accept_cookies(page)
        _expand_all_dates(page)
        page.wait_for_timeout(1500)
        rows = page.evaluate(EXTRACT_ROWS_JS, DATE_RE_SRC)
        body_text = page.inner_text("body")
        info["page_sold_out_banner"] = bool(re.search(r"all tickets are sold out", body_text, re.I))
        if not rows:
            _dump_debug("production_page", page.url, body_text[:20000], page.content()[:400000])
        return rows_to_performances(rows, url), info
    finally:
        page.close()


# --------------------------------------------------------------------------
# Reading a seat map
# --------------------------------------------------------------------------

ROW_KEYS = {"row", "seatrow", "rowname", "rowlabel", "rowletter", "rowdescription",
            "rang", "rangee", "rownumber"}
NUM_KEYS = {"seat", "seatnumber", "seatnum", "number", "seatno", "seatname", "seatlabel", "seatnumberdisplay",
            "place", "numero", "siege", "placenumber"}
SECTION_KEYS = {
    "section", "sectionname", "sectiondescription", "sectiondesc", "area", "areaname",
    "zone", "zonename", "zonedescription", "level", "levelname", "block", "blockname",
    "sectionid", "zoneid", "areaid", "levelid", "floor", "floorname", "bloc", "niveau",
    "areadescription", "physicalzone", "physicalzonename",
}
STATUS_KEYS = {
    "status", "seatstatus", "seatstatusid", "statusid", "statuscode", "seatstatusdescription",
    "available", "isavailable", "availability", "state",
    "issold", "sold", "isbooked", "booked", "isreserved", "issoldout", "isunavailable",
}
NEGATIVE_BOOL_KEYS = {"issold", "sold", "isbooked", "booked", "isreserved", "issoldout", "isunavailable"}
NAME_KEYS = ("description", "name", "label", "title", "shortdescription", "displayname")
ID_KEYS = ("id", "sectionid", "zoneid", "areaid", "levelid", "statusid", "seatstatusid")
AREA_WORD_RE = re.compile(
    r"stalls|circle|tier|balcony|amphitheatre|box|slips|standing|orchestra|"
    r"orchestre|parterre|corbeille|balcon|loge|galerie|amphith|baignoire|gallery",
    re.I,
)
AREA_NAME_RE = re.compile(
    r"orchestra stalls|stalls circle|donald gordon grand tier|grand tier|balcony(?: stalls)?|"
    r"amphitheatre|stalls boxes|balcony boxes|lower slips|upper slips|slips|stalls|"
    r"orchestre|parterre|corbeille|(?:1er|2e|3e|premier|deuxi[eè]me|troisi[eè]me) balcon|balcon|"
    r"loges?|baignoires?|galerie|amphith[ée][aâ]tre",
    re.I,
)
STATUS_WORD_RE = re.compile(
    r"avail|sold|book|reserv|hold|held|open|lock|taken|free|kill|libre|vendu|dispon|occup|bloqu|r[ée]serv", re.I)
UNAVAILABLE_SEAT_RE = re.compile(
    r"unavail|not[ -]?available|\bsold|booked|taken|reserved|\bheld\b|\bhold\b|"
    r"locked|blocked|occupied|disabled|\bkill|indisponible|vendu|occup[ée]|r[ée]serv[ée]|bloqu[ée]|non disponible",
    re.I,
)
AVAILABLE_SEAT_RE = re.compile(r"avail|^open$|\bfree\b|for ?sale|on ?sale|\blibre\b|disponible|en vente", re.I)


def _lower_keys(d: dict) -> dict:
    return {str(k).lower().replace("_", "").replace("-", ""): v for k, v in d.items()}


def _name_of(d: dict) -> str | None:
    for key in NAME_KEYS:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _status_to_available(value, status_names: dict) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        name = status_names.get(str(int(value)))
        return _status_to_available(name, status_names) if name else None
    if isinstance(value, dict):
        lowered = _lower_keys(value)
        name = _name_of(lowered) or lowered.get("code") or lowered.get("statuscode")
        return _status_to_available(name, status_names) if name else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and text in status_names:
            return _status_to_available(status_names[text], status_names)
        if UNAVAILABLE_SEAT_RE.search(text):
            return False
        if AVAILABLE_SEAT_RE.search(text):
            return True
    return None


def extract_seats_from_json(payloads: list) -> list[dict]:
    """Best-effort search of seat-map JSON for seats (row, number, section, available)."""
    area_names: dict[str, str] = {}
    status_names: dict[str, str] = {}

    def collect(obj):
        if isinstance(obj, dict):
            lowered = _lower_keys(obj)
            name = _name_of(lowered)
            if name:
                for id_key in ID_KEYS:
                    ident = lowered.get(id_key)
                    if isinstance(ident, (int, str)) and str(ident).strip():
                        if AREA_WORD_RE.search(name):
                            area_names.setdefault(str(ident), name)
                        if STATUS_WORD_RE.search(name):
                            status_names.setdefault(str(ident), name)
            for value in obj.values():
                collect(value)
        elif isinstance(obj, list):
            for value in obj:
                collect(value)

    for payload in payloads:
        collect(payload)

    seats: list[dict] = []

    def section_value(value) -> str:
        if isinstance(value, dict):
            name = _name_of(_lower_keys(value))
            return name or ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return area_names.get(str(int(value)), "")
        if isinstance(value, str):
            if value.strip() in area_names:
                return area_names[value.strip()]
            return value.strip()
        return ""

    def walk(obj, context_section: str):
        if isinstance(obj, dict):
            lowered = _lower_keys(obj)
            name = _name_of(lowered)
            here_section = context_section
            if name and AREA_NAME_RE.search(name):
                here_section = name
            row_key = next((k for k in lowered if k in ROW_KEYS), None)
            num_key = next((k for k in lowered if k in NUM_KEYS), None)
            if row_key and num_key and not isinstance(lowered[row_key], (dict, list)):
                section = ""
                for key in lowered:
                    if key in SECTION_KEYS:
                        section = section_value(lowered[key])
                        if section:
                            break
                section = section if AREA_WORD_RE.search(section or "") else (here_section or section)
                available = None
                for key in lowered:
                    if key in STATUS_KEYS:
                        result = _status_to_available(lowered[key], status_names)
                        if result is not None:
                            available = (not result) if key in NEGATIVE_BOOL_KEYS and isinstance(lowered[key], bool) else result
                            break
                seats.append({
                    "row": str(lowered[row_key]).strip(),
                    "number": str(lowered[num_key]).strip(),
                    "section": section or "",
                    "available": available,
                })
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    walk(value, here_section)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, context_section)

    for payload in payloads:
        walk(payload, "")
    return seats


# Runs inside the page: collects seat-like elements (SVG circles, buttons...)
# that describe themselves with aria-label / title / data-* attributes.
EXTRACT_SEAT_ELEMENTS_JS = r"""
() => {
  const out = [];
  const els = document.querySelectorAll('[aria-label],[title],[data-seat],[data-row],[data-seat-id],[data-seatid]');
  for (const el of els) {
    const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    const data = Object.entries(el.dataset || {}).map(([k, v]) => k + '=' + v).join(' ');
    const s = (label + ' ' + data).trim();
    if (!/row|rang/i.test(s) || !/seat|place|si[eè]ge|fauteuil/i.test(s)) continue;
    const cls = (el.getAttribute('class') || '') + ' ' +
                ((el.parentElement && el.parentElement.getAttribute('class')) || '');
    out.push({ s: s.slice(0, 300), cls: cls.slice(0, 200),
               disabled: el.getAttribute('aria-disabled') === 'true' || el.disabled === true });
    if (out.length >= 6000) break;
  }
  return out;
}
"""


def extract_seats_from_dom(elements: list[dict]) -> list[dict]:
    seats = []
    for el in elements:
        s = el.get("s", "")
        row = re.search(r"(?:row|rang(?:ée)?)[\s:=#-]*([A-Z]{1,2}|\d{1,3})\b", s, re.I)
        number = re.search(
            r"(?:seat|place|si[eè]ge|fauteuil)(?:number|no|num)?[\s:=#-]*(?:n[°o]\s*)?([A-Z]?\d+[A-Z]?)\b", s, re.I)
        if not row or not number:
            continue
        section = AREA_NAME_RE.search(s)
        unavailable = el.get("disabled") or UNAVAILABLE_SEAT_RE.search(s + " " + el.get("cls", ""))
        seats.append({
            "row": row.group(1).upper(),
            "number": number.group(1),
            "section": section.group(0) if section else "",
            "available": not unavailable,
        })
    return seats


def summarise_seats(seats: list[dict], targets=None) -> dict:
    """Turn raw seats into what the alert logic needs."""
    known = [s for s in seats if s.get("available") is not None]
    with_section = [s for s in seats if s.get("section")]
    readable = len(known) >= 10 and len(with_section) >= 10
    available = [s for s in known if s["available"]]
    matching = sorted(
        {f"{s['section']}|{s['row'].upper()}|{s['number']}" for s in available if seat_is_target(s, targets)},
        key=lambda x: (x.split("|")[0], x.split("|")[1], int(re.sub(r"\D", "", x.split("|")[2]) or 0)),
    )
    sections = sorted({s["section"] for s in with_section})
    return {
        "readable": readable,
        "total_seats": len(seats),
        "available_count": len(available),
        "matching": matching,
        "sections": sections[:12],
    }


SOLD_OUT_PAGE_RE = re.compile(
    r"sold\s*out|no longer available|[ée]puis[ée]|\bcomplet\b|plus disponible", re.I)
OPEN_MAP_RE = re.compile(
    r"select (?:in|on) the seat ?map|seat ?map|choose (?:your )?seats?|plan de salle|"
    r"choisir (?:sur le plan|mes places|vos places|les places)|s[ée]lectionner sur le plan",
    re.I,
)


def _try_open_seat_map(page) -> None:
    """Some shops show 'best available' first - click through to the seat map."""
    for role in ("tab", "link", "button"):
        try:
            loc = page.get_by_role(role, name=OPEN_MAP_RE)
            for i in range(min(loc.count(), 3)):
                item = loc.nth(i)
                if item.is_visible():
                    item.click(timeout=3000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)
                    return
        except Exception:
            pass


def check_seat_map(context, url: str, key: str, targets=None, open_map: bool = False,
                   snapshot_name: str | None = None) -> dict:
    page = context.new_page()
    responses = []
    page.on("response", lambda r: responses.append(r))
    result = {"readable": False, "matching": [], "available_count": 0, "error": None, "waiting_room": False}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if in_waiting_room(page):
            result["waiting_room"] = True
            return result
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except Exception:
            pass
        _accept_cookies(page)
        page.wait_for_timeout(3000)
        if open_map:
            _try_open_seat_map(page)
        body_text = page.inner_text("body")
        result["page_says_sold_out"] = bool(SOLD_OUT_PAGE_RE.search(body_text))

        payloads, captured = [], []
        for resp in responses:
            try:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    continue
                if "json" not in (resp.headers.get("content-type") or ""):
                    continue
                body = resp.text()
                if len(body) > 10_000_000:
                    continue
                payloads.append(json.loads(body))
                captured.append((resp.url, body))
            except Exception:
                continue

        seats = extract_seats_from_json(payloads)
        source = "json"
        summary = summarise_seats(seats, targets)
        dom_elements = []
        if not summary["readable"]:
            dom_elements = page.evaluate(EXTRACT_SEAT_ELEMENTS_JS)
            dom_seats = extract_seats_from_dom(dom_elements)
            dom_summary = summarise_seats(dom_seats, targets)
            if dom_summary["readable"] or len(dom_seats) > len(seats):
                seats, summary, source = dom_seats, dom_summary, "page"
        result.update(summary)
        result["source"] = source
        if not summary["readable"]:
            _dump_debug(
                "seatmap_" + re.sub(r"\W+", "_", key)[:40],
                page.url,
                body_text[:20000],
                page.content()[:400000],
                captured,
            )
        if snapshot_name:
            save_snapshot(snapshot_name, page.url, body_text, captured,
                          extra={"dom_seat_like_elements": dom_elements[:300],
                                 "seats_found": len(seats), "readable": summary["readable"],
                                 "sample_seats": seats[:40]})
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
    finally:
        page.close()


SNAPSHOT_DIR = ROOT / "snapshots"
SECRET_KEY_RE = re.compile(r"token|session|csrf|jsessionid|cookie|auth|password|secret", re.I)


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("[removed]" if SECRET_KEY_RE.search(str(k)) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def save_snapshot(name: str, url: str, text: str, captured=None, extra=None) -> None:
    """Keeps ONE copy of what a page looked like (public pages only), in snapshots/,
    so the page reader can be tuned later. Existing snapshots are never overwritten,
    so the folder doesn't grow over time. Delete a file to get a fresh copy."""
    try:
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        path = SNAPSHOT_DIR / f"{name}.json"
        if path.exists():
            return
        responses = []
        budget = 600_000
        for resp_url, body in (captured or [])[:40]:
            try:
                data = _redact(json.loads(body))
                body_out = json.dumps(data)[:200_000]
            except Exception:
                body_out = body[:20_000]
            if budget - len(body_out) < 0:
                break
            budget -= len(body_out)
            responses.append({"url": re.sub(r"(token|session|jsessionid)=[^&;]+", r"\1=removed", resp_url, flags=re.I),
                              "body": body_out})
        payload = {
            "saved": dt.datetime.now(LONDON).isoformat(timespec="seconds"),
            "url": re.sub(r"(token|session|jsessionid)=[^&;]+", r"\1=removed", url, flags=re.I),
            "page_text": (text or "")[:30_000],
            "json_responses": responses,
            **(extra or {}),
        }
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        log(f"Saved snapshot {path.name}")
    except Exception as exc:
        log(f"Could not save snapshot {name}: {exc}")


_DUMPED: set[str] = set()


def _dump_debug(name: str, url: str, text: str, page_html: str, captured=None) -> None:
    """Saves what the page looked like, so the reader can be tuned if the site changes.
    At most one snapshot per page per run, and 12 in total."""
    if name in _DUMPED or len(_DUMPED) >= 12:
        return
    _DUMPED.add(name)
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = dt.datetime.now(LONDON).strftime("%Y%m%d-%H%M%S")
        base = DEBUG_DIR / f"{stamp}_{name}"
        base.with_suffix(".txt").write_text(f"URL: {url}\n\n{text}", encoding="utf-8")
        base.with_suffix(".html").write_text(page_html, encoding="utf-8")
        if captured:
            sample = [{"url": u, "body": b[:200_000]} for u, b in captured[:40]]
            base.with_suffix(".json").write_text(json.dumps(sample, indent=1), encoding="utf-8")
        log(f"Saved debug snapshot: {base.name}")
    except Exception as exc:
        log(f"Could not save debug snapshot: {exc}")


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

STATUS_EMOJI = {"available": "🟢", "sold_out": "🔴", "unavailable": "⚪️", "unknown": "❔"}


def format_seats(matching: list[str], limit: int = 30) -> str:
    grouped: dict[tuple[str, str], list[str]] = {}
    for seat_id in matching:
        section, row, number = seat_id.split("|")
        grouped.setdefault((section, row), []).append(number)
    lines = []
    for (section, row), numbers in list(grouped.items())[:limit]:
        lines.append(f"• {html.escape(section)}, row {html.escape(row)}: seats {html.escape(', '.join(numbers))}")
    return "\n".join(lines)


def link_for(perf: dict, show=None) -> str:
    return perf.get("link") or (show.url if show else PRODUCTION_URL)


def status_message(state: dict, show=None) -> str:
    show = show or NUTCRACKER
    st = show.state(state)
    perfs = st.get("performances", {})
    seat_info = st.get("seat_checks", {})
    last = st.get("last_check_human", "never")
    lines = [f"<b>{html.escape(show.name)} — what I see</b> (last check {html.escape(last)})",
             f"Watching for: {html.escape(show.describe())}"]
    if show.cast:
        lines.append(f"Only performances with: {html.escape(show.cast_raw)}")
    if show.only_dates:
        lines.append(f"Only these dates: {html.escape(show.only_dates_raw)}")
    lines.append("")
    if not perfs:
        lines.append("No performances read yet.")
    for key, perf in perfs.items():
        if not show.watched(key, perf):
            continue
        info = seat_info.get(key, {})
        extra = ""
        if info.get("readable"):
            n = len(info.get("matching", []))
            extra = f" — {n} seat{'s' if n != 1 else ''} in your rows" if n else " — none in your rows"
        lines.append(f"{STATUS_EMOJI.get(perf['status'], '❔')} {html.escape(perf['label'])}{extra}")
    lines.append("")
    lines.append("🟢 shows tickets · 🔴 sold out · ❔ unclear")
    return "\n".join(lines)


def help_text() -> str:
    lines = ["I watch for returned tickets and message you when seats in your rows come back:"]
    if LONDON_ENABLED:
        lines.append(f"• The Nutcracker, Royal Opera House: {html.escape(describe_targets())}")
    if SWAN_ENABLED:
        lines.append(f"• Swan Lake, Royal Opera House ({html.escape(SWAN.cast_raw)}): {html.escape(SWAN.describe())}")
    if PARIS_ENABLED:
        lines.append(f"• {html.escape(PARIS_SHOW)}, Théâtre du Châtelet: {html.escape(PARIS_TARGET_LABEL)}")
    lines.append("\n/status – what I see right now\n/check – check right now\n/help – this message")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# One check
# --------------------------------------------------------------------------

def run_check(context, state: dict, notify: bool = True, show=None) -> None:
    show = show or NUTCRACKER
    root = state
    state = show.state(root)
    first_run = not state.get("initialised")
    old_perfs = state.get("performances", {})
    seat_checks = state.setdefault("seat_checks", {})
    known_links = state.setdefault("links", {})

    perfs, info = read_production_page(context, show.url)

    if info["waiting_room"]:
        _warn_once(root, "waiting_room",
                   "⏳ The Royal Opera House site has a waiting room (queue) up right now, so I can't see "
                   f"ticket availability. I'll keep trying.\n{show.url}", hours=1, notify=notify)
        return
    if not perfs:
        raise RuntimeError(f"Couldn't find any performance dates on the {show.name} page")

    _clear_warning(root, "waiting_room")

    for key, perf in perfs.items():
        if perf.get("link"):
            known_links[key] = perf["link"]
        elif known_links.get(key):
            perf["link"] = known_links[key]

    # Which performances changed since last time?
    flipped = []
    for key, perf in perfs.items():
        before = (old_perfs.get(key) or {}).get("status")
        if not first_run and before != "available" and perf["status"] == "available":
            flipped.append(key)

    # Seat maps to open this round: changed ones first, then a rotation.
    watched_with_links = [k for k in perfs if show.watched(k, perfs[k]) and perfs[k].get("link")]
    to_check = [k for k in flipped if show.watched(k, perfs[k]) and perfs[k].get("link")]
    if watched_with_links:
        pointer = state.get("rotation", 0) % len(watched_with_links)
        rotation = watched_with_links[pointer:] + watched_with_links[:pointer]
        added = 0
        for key in rotation:
            if added >= SEATMAPS_PER_CYCLE:
                break
            if key not in to_check:
                to_check.append(key)
                added += 1
        state["rotation"] = (pointer + max(added, 1)) % len(watched_with_links)

    seat_results = {}
    for key in to_check:
        time.sleep(random.uniform(2, 5))
        result = check_seat_map(context, perfs[key]["link"], key, targets=show.targets, open_map=True,
                                snapshot_name=f"{show.key}_seatmap")
        result["checked_at"] = dt.datetime.now(LONDON).strftime("%d %b %H:%M")
        seat_results[key] = result
        log(f"{show.name} seat map {perfs[key]['label']}: readable={result.get('readable')} "
            f"available={result.get('available_count')} matching={len(result.get('matching', []))} "
            f"error={result.get('error')}")

    # Alerts
    title = f"{show.name}, " if show.key != "london" else ""
    for key in perfs:
        if not show.watched(key, perfs[key]):
            continue
        perf = perfs[key]
        result = seat_results.get(key)
        previous_matching = set((seat_checks.get(key) or {}).get("matching", []))

        contradictory = bool(result and result.get("readable") and key in flipped
                             and not result.get("available_count"))
        if result and result.get("readable") and not contradictory:
            current = set(result["matching"])
            new_seats = current - previous_matching
            if new_seats and not first_run and notify:
                send(
                    f"🎟🎟 <b>Seats in your rows — {html.escape(title + perf['label'])}</b>\n"
                    f"{format_seats(sorted(result['matching']))}\n\n"
                    f"Book fast: {html.escape(link_for(perf, show))}"
                )
            elif key in flipped and not current and notify:
                send(
                    f"Returns appeared for {html.escape(title + perf['label'])} "
                    f"({result.get('available_count', 0)} seats), but none in your rows.\n{html.escape(link_for(perf, show))}",
                    silent=True,
                )
        elif key in flipped and notify:
            send(
                f"🎟 <b>Tickets just appeared — {html.escape(title + perf['label'])}</b>\n"
                + ("" if not contradictory else "(the seat map looked empty, so it may be a glitch)\n") +
                f"I couldn't read the seat map, so check your rows quickly:\n{html.escape(link_for(perf, show))}"
            )

        if result and not result.get("error") and not result.get("waiting_room"):
            if result.get("readable") and not contradictory:
                seat_checks[key] = {k: result.get(k) for k in
                                    ("readable", "matching", "available_count", "sections", "source", "checked_at")}
            else:  # keep what we knew, so the same seats aren't announced twice
                seat_checks[key] = {**seat_checks.get(key, {}), "readable": False,
                                    "checked_at": result.get("checked_at")}

    state["performances"] = perfs
    state["page_sold_out_banner"] = info["page_sold_out_banner"]
    state["last_check_human"] = dt.datetime.now(LONDON).strftime("%a %d %b %H:%M")

    if first_run:
        state["initialised"] = True
        if notify:
            send(_welcome_message(perfs, seat_results, show))


def _welcome_message(perfs: dict, seat_results: dict, show=None) -> str:
    show = show or NUTCRACKER
    watched = {k: p for k, p in perfs.items() if show.watched(k, p)}
    counts: dict[str, int] = {}
    for perf in watched.values():
        counts[perf["status"]] = counts.get(perf["status"], 0) + 1
    readable = [r for r in seat_results.values() if r.get("readable")]
    lines = [
        f"👋 <b>{html.escape(show.name)} watcher is running.</b>",
        f"Watching {len(watched)} of {len(perfs)} performances: {counts.get('sold_out', 0)} sold out, "
        f"{counts.get('available', 0)} showing tickets, "
        f"{counts.get('unknown', 0) + counts.get('unavailable', 0)} unclear.",
        f"Watching for: {html.escape(show.describe())}.",
    ]
    if show.cast:
        lines.append(f"Only performances with: {html.escape(show.cast_raw)}.")
        for perf in watched.values():
            lines.append(f"{STATUS_EMOJI.get(perf['status'], '❔')} {html.escape(perf['label'])}")
    if show.only_dates:
        lines.append(f"Only these dates: {html.escape(show.only_dates_raw)}.")
    if readable:
        sections = sorted({s for r in readable for s in r.get("sections", [])})
        lines.append(f"Seat map reading: ✅ works (sections seen: {html.escape(', '.join(sections[:8]))}).")
        matching_now = {k: r for k, r in seat_results.items() if r.get("matching")}
        for key, r in matching_now.items():
            lines.append(f"\nAlready available now — {html.escape(perfs[key]['label'])}:\n{format_seats(r['matching'])}\n"
                         f"{html.escape(link_for(perfs[key], show))}")
    elif seat_results:
        lines.append("Seat map reading: ⚠️ couldn't read seat rows yet — I'll alert you whenever "
                     "<i>any</i> tickets reappear instead.")
    else:
        lines.append("Seat map reading: no booking links visible on sold-out dates — I'll alert you "
                     "when any date starts showing tickets.")
    lines.append("\nSend /status any time.")
    return "\n".join(lines)


def _warn_once(state: dict, name: str, text: str, hours: float, notify: bool) -> None:
    warnings = state.setdefault("warnings", {})
    last = warnings.get(name, 0)
    if time.time() - last >= hours * 3600:
        if notify:
            send(text, silent=True)
        warnings[name] = time.time()


def _clear_warning(state: dict, name: str) -> None:
    state.setdefault("warnings", {}).pop(name, None)


# --------------------------------------------------------------------------
# Paris: Théâtre du Châtelet (SecuTix shop + Châtelet's official resale exchange)
# --------------------------------------------------------------------------

def _flag(name: str, default: str = "1") -> bool:
    return _env(name, default).lower() not in ("0", "false", "no", "off")


LONDON_ENABLED = _flag("LONDON_ENABLED")
SWAN_ENABLED = _flag("SWAN_ENABLED")
PARIS_ENABLED = _flag("PARIS_ENABLED")
NUTCRACKER = RBOShow("london", "The Nutcracker", PRODUCTION_URL, TARGETS_RAW, ONLY_DATES_RAW)
SWAN = RBOShow("swan", "Swan Lake",
               _env("SWAN_URL", "https://www.rbo.org.uk/production/swan-lake-marius-petipa"),
               _env("SWAN_TARGET_SEATS", TARGETS_RAW), _env("SWAN_ONLY_DATES"),
               _env("SWAN_CAST", "Marianela Nuñez, Francesca Hayward"))
PARIS_SHOW = _env("PARIS_SHOW_NAME", "Hammer")
PARIS_PRODUCT_ID = _env("PARIS_PRODUCT_ID", "10229714846846")
PARIS_SHOP = _env("PARIS_SHOP_URL", "https://billetterie.chatelet.com").rstrip("/")
PARIS_RESALE = _env("PARIS_RESALE_URL", "https://billetterie-bourse.chatelet.com").rstrip("/")
PARIS_PERFORMANCES_RAW = _env(
    "PARIS_PERFORMANCES",
    "10229732342858=2027-03-06 20:00; 10229732342860=2027-03-07 15:00; "
    "10229732342861=2027-03-09 20:00; 10229732342862=2027-03-10 20:00; "
    "10229732342859=2027-03-11 20:00; 10229732342863=2027-03-12 20:00; "
    "10229732342864=2027-03-13 20:00; 10229732342865=2027-03-14 15:00",
)
# Rows are listed both as numbers and letters because the Châtelet seat map
# could use either; area names in French and English for the same reason.
PARIS_TARGETS_RAW = _env(
    "PARIS_TARGET_SEATS",
    "Orchestre: 1-6, A-F; Orchestra: 1-6, A-F; Parterre: 1-6, A-F; Corbeille: 1, A",
)
PARIS_TARGET_LABEL = _env("PARIS_TARGET_LABEL", "Orchestre (stalls) first 6 rows or Corbeille front row")
PARIS_ONLY_DATES_RAW = _env("PARIS_ONLY_DATES")
PARIS_SEATMAPS_PER_CYCLE = int(_env("PARIS_SEATMAPS_PER_CYCLE", "2"))

PARIS_TARGETS = parse_targets(PARIS_TARGETS_RAW)
PARIS_ONLY_DATES = [norm(d) for d in PARIS_ONLY_DATES_RAW.split(",") if d.strip()]

MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]
DAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_paris_performances(raw: str) -> list[dict]:
    perfs = []
    for part in raw.split(";"):
        if "=" not in part:
            continue
        perf_id, when = (x.strip() for x in part.split("=", 1))
        try:
            d = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        except ValueError:
            log(f"Ignoring Paris performance with unreadable date: {part!r}")
            continue
        month_en, month_fr = MONTHS_EN[d.month - 1], MONTHS_FR[d.month - 1]
        perfs.append({
            "id": perf_id,
            "label": f"{DAYS_EN[d.weekday()]} {d.day} {month_en}, {d:%H:%M}",
            "key": norm(f"{DAYS_EN[d.weekday()]} {d.day} {month_en} {d.year} {d:%H:%M}"),
            # Ways the shop may write this date (used when no link carries the id)
            "needles": [f"{d:%d.%m.%Y}", f"{d.day} {month_en} {d.year}", f"{d.day} {month_fr} {d.year}",
                        f"{d:%d/%m/%Y}"],
        })
    return perfs


PARIS_PERFS = parse_paris_performances(PARIS_PERFORMANCES_RAW)


def paris_date_watched(perf: dict) -> bool:
    if not PARIS_ONLY_DATES:
        return True
    return any(re.search(r"(?<!\d)" + re.escape(d) + r"(?![\w])", perf["key"]) for d in PARIS_ONLY_DATES)


def paris_shop_dates_url() -> str:
    return f"{PARIS_SHOP}/selection/event/date?productId={PARIS_PRODUCT_ID}&lang=en"


def paris_resale_dates_url() -> str:
    return f"{PARIS_RESALE}/selection/event/date?productId={PARIS_PRODUCT_ID}&lang=en"


def paris_seat_url(perf_id: str, resale: bool = False) -> str:
    base = PARIS_RESALE if resale else PARIS_SHOP
    return f"{base}/selection/event/seat?perfId={perf_id}&productId={PARIS_PRODUCT_ID}&lang=en"


# The Châtelet shop runs on SecuTix. Its seat map loads JSON from /tnwr/v1/...:
#   seatmap/config/svg        -> every area ("polygon") with its free-seat count
#   seatmap/seats/free/polygon -> each free seat in one area: area, row, number, category
# The bot opens one seat page per site (to get a normal browser session), then
# asks for that same JSON for each performance, like the seat map itself does.

SECUTIX_QS = "isSeasonTicketMode=false&advantageId=&ppid=&reservationIdx=&crossSellId=&baseOperationIdsString="
SEATS_REFRESH_MINUTES = 30      # re-read seat lists at least this often, even if counts look unchanged
SEEN_SEAT_HOURS = 6             # a seat that was free in the last 6 hours isn't announced again

FETCH_JSON_JS = r"""
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
    return {status: r.status, type: r.headers.get('content-type') || '', body: await r.text()};
  } catch (e) { return {status: 0, type: '', body: String(e)}; }
}
"""

def _cal_status(label: str):
    l = label.lower()
    if l.startswith(("available", "last", "places dispo", "derni")):
        return "available"
    if "sold out" in l or "complet" in l or l.startswith(("épuis", "epuis")):
        return "sold_out"
    return None


CAL_RE = re.compile(
    r"(?:^|\n)[ \t]*(\d{1,2})[ \t]*\n[ \t]*(available tickets|last seats|currently sold out|sold out|inactive|"
    r"places disponibles|derni[eè]res places|actuellement complet|complet|[ée]puis[ée]|inactif)",
    re.I,
)


def _perf_day(perf: dict) -> int:
    return int(perf["needles"][0][:2])


def _month_label(perf: dict) -> str:
    return perf["needles"][1].split(" ", 1)[1]          # "March 2027"


def parse_calendar(text: str) -> dict[str, str]:
    """Calendar on a SecuTix date page -> {perf id: 'available' | 'sold_out'}."""
    out: dict[str, str] = {}
    if not text:
        return out
    days: dict[int, str] = {}
    for m in CAL_RE.finditer(text):
        status = _cal_status(m.group(2))
        if status:
            days.setdefault(int(m.group(1)), status)
    lowered = text.lower()
    for perf in PARIS_PERFS:
        month = _month_label(perf).lower()
        month_fr = perf["needles"][2].split(" ", 1)[1].lower()
        if month not in lowered and month_fr not in lowered:
            continue
        status = days.get(_perf_day(perf))
        if status:
            out[perf["id"]] = status
    return out


def _target_polygon(name: str) -> bool:
    n = norm(name)
    return any(t_section in n or n in t_section for t_section, _ in PARIS_TARGETS)


class SecutixError(RuntimeError):
    pass


_SKIP_HEADERS = {"cookie", "host", "content-length", "connection", "accept-encoding"}


def _fetch_json(page, url: str, headers: dict | None = None):
    """GET seat-map JSON the way the seat map itself does (same session and headers)."""
    if headers is not None:
        hdrs = {k: v for k, v in headers.items() if k.lower() not in _SKIP_HEADERS and not k.startswith(":")}
        resp = page.context.request.get(url, headers=hdrs, timeout=30_000)
        status, ctype, body = resp.status, resp.headers.get("content-type", ""), resp.text()
    else:
        res = page.evaluate(FETCH_JSON_JS, url)
        status, ctype, body = res.get("status"), res.get("type") or "", res.get("body") or ""
    if status != 200 or "json" not in ctype.lower():
        raise SecutixError(f"HTTP {status} for {url.split('?')[0].rsplit('/', 2)[-2:]}: {body[:160]}")
    return json.loads(body)


def _open_seat_page(page, base: str, pid: str):
    """Opens a performance's seat page and returns (config json, request headers) from the
    seat map's own request for seatmap/config/svg."""
    url = f"{base}/selection/event/seat?perfId={pid}&productId={PARIS_PRODUCT_ID}&lang=en"
    with page.expect_response(lambda r: "/tnwr/v1/seatmap/config/svg?" in r.url, timeout=25_000) as info:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    resp = info.value
    if resp.status != 200:
        raise SecutixError(f"seat page config HTTP {resp.status}")
    return resp.json(), resp.request.all_headers()


_SEAT_CACHE: dict = {"shop": {}, "resale": {}}   # in memory only (kept out of state.json)
_API_BACKOFF: dict = {}                            # site -> time until which we don't ask for seat lists
API_BACKOFF_MINUTES = 60


def read_secutix(context, base: str, perfs: list[dict], cache: dict, resale: bool,
                 snapshot_name: str) -> dict:
    """Free seats in the target areas for each performance, via the seat-map JSON.

    cache keeps, per performance, the last seat list and area counts, so seat
    lists are only downloaded again when a count changes (or every 30 minutes).
    Returns {"waiting_room": bool, "calendar": {...}, "perfs": {id: {...}}, "errors": [...]}.
    """
    out = {"waiting_room": False, "calendar": {}, "perfs": {}, "errors": []}
    if not perfs:
        return out
    page = context.new_page()
    try:
        # A date page first: it has the calendar (status of every date) and gives us a session.
        page.goto(f"{base}/selection/event/date?productId={PARIS_PRODUCT_ID}&lang=en",
                  wait_until="domcontentloaded", timeout=60_000)
        if in_waiting_room(page):
            out["waiting_room"] = True
            return out
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _accept_cookies(page)
        text = page.inner_text("body")
        out["calendar"] = parse_calendar(text)
        save_snapshot(snapshot_name, page.url, text, extra={"calendar": out["calendar"]})
        if resale and out["calendar"]:
            # Only dates whose resale calendar isn't "sold out" need their seat lists
            perfs = [p for p in perfs if out["calendar"].get(p["id"]) != "sold_out"]
        if time.time() < _API_BACKOFF.get(base, 0):
            return out          # seat lists didn't work recently on this site; calendar only for now

        now = time.time()
        headers = None          # copied from the seat map's own request once we've seen one
        direct_ok = True        # can we ask for other dates' data without opening their pages?
        for perf in perfs:
            pid = perf["id"]
            entry = {"ok": False}
            try:
                cfg = None
                if headers is not None and direct_ok:
                    try:
                        cfg = _fetch_json(page, f"{base}/tnwr/v1/seatmap/config/svg?productId={PARIS_PRODUCT_ID}"
                                                f"&performanceId={pid}&{SECUTIX_QS}", headers)
                    except SecutixError as exc:
                        log(f"Direct seat-map request refused ({exc}); opening each seat page instead")
                        direct_ok = False
                opened_here = False
                if cfg is None:
                    cfg, headers = _open_seat_page(page, base, pid)
                    opened_here = True
                    if in_waiting_room(page):
                        out["waiting_room"] = True
                        return out
                polygons = cfg.get("polygons") or []
                counts = {}
                for poly in polygons:
                    counts[poly.get("id")] = {
                        "name": str(poly.get("name") or poly.get("id")),
                        "free": int(poly.get("availability") or 0),
                        "resale": int(poly.get("resaleAvailability") or 0),
                    }
                entry.update(ok=True, counts=counts,
                             total=sum(c["free"] for c in counts.values()),
                             resale_total=sum(c["resale"] for c in counts.values()))
                cal_level = next((c.get("availability") for c in cfg.get("calendarConfigurations") or []
                                  if c.get("performanceDate") == perf["needles"][0]), None)
                entry["level"] = cal_level

                old = cache.get(pid) or {}
                old_counts = old.get("counts") or {}
                stale = now - float(old.get("seats_at") or 0) > SEATS_REFRESH_MINUTES * 60
                seats, seat_errors = [], []
                for poly_id, c in counts.items():
                    if not _target_polygon(c["name"]):
                        continue
                    n = c["free"] + c["resale"]
                    if n == 0:
                        continue
                    if not stale and old_counts.get(poly_id) == c and "seats" in old:
                        seats.extend(s for s in old["seats"] if s.get("poly") == poly_id)
                        continue
                    time.sleep(random.uniform(0.5, 1.5))
                    free_url = (f"{base}/tnwr/v1/seatmap/seats/free/polygon?productId={PARIS_PRODUCT_ID}"
                                f"&performanceId={pid}&polygonId={urllib.parse.quote(str(poly_id))}"
                                f"&isExclusive=false&{SECUTIX_QS}")
                    try:
                        free = _fetch_json(page, free_url, headers)
                    except SecutixError as exc:
                        if opened_here:
                            seat_errors.append(str(exc))
                            continue
                        # Maybe the site wants this date's own seat page open first
                        try:
                            _, headers = _open_seat_page(page, base, pid)
                            opened_here = True
                            direct_ok = False
                            free = _fetch_json(page, free_url, headers)
                        except SecutixError as exc2:
                            seat_errors.append(str(exc2))
                            continue
                    seen_ids = set()
                    for feat in free.get("features") or []:
                        props = feat.get("properties") or {}
                        sid = props.get("id") or feat.get("id")
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)
                        area = props.get("area") or {}
                        area_name = (area.get("name") or {}).get("en") or (area.get("name") or {}).get("fr") or c["name"]
                        seats.append({"poly": poly_id, "section": area_name,
                                      "row": str(props.get("row") or "").strip(),
                                      "number": str(props.get("number") or "").strip(),
                                      "category": str(props.get("seatCategory") or ""),
                                      "available": True})
                if not seat_errors or seats:
                    entry["seats_at"] = now if (stale or not old.get("seats_at")) else old.get("seats_at")
                entry["seats"] = seats
                entry["seat_errors"] = seat_errors
                if seat_errors:
                    out["errors"].extend(seat_errors[:2])
            except SecutixError as exc:
                entry["error"] = str(exc)
                out["errors"].append(str(exc))
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
                out["errors"].append(entry["error"])
            out["perfs"][pid] = entry
            if not entry.get("ok") and not any(e.get("ok") for e in out["perfs"].values()) and len(out["perfs"]) >= 2:
                _API_BACKOFF[base] = time.time() + API_BACKOFF_MINUTES * 60
                log(f"Seat lists not readable on {base} - using its calendar only for the next hour")
                break
            time.sleep(random.uniform(0.5, 1.5))
        return out
    finally:
        page.close()


def _seat_key(seat: dict) -> str:
    return f"{seat['section']}|{seat['row']}|{seat['number']}"


def _seat_sort(key: str):
    section, row, number = key.split("|")
    row_key = (0, int(row), "") if row.isdigit() else (1, 0, row)
    return (section, row_key, int(re.sub(r"\D", "", number) or 0))


def format_paris_seats(keys: list[str], limit_rows: int = 12) -> str:
    grouped: dict[tuple[str, str], list[str]] = {}
    for key in sorted(keys, key=_seat_sort):
        section, row, number = key.split("|")
        grouped.setdefault((section, row), []).append(number)
    lines = [f"• {html.escape(section)}, row {html.escape(row)}: {html.escape(', '.join(numbers))}"
             for (section, row), numbers in list(grouped.items())[:limit_rows]]
    if len(grouped) > limit_rows:
        lines.append(f"• …and {len(grouped) - limit_rows} more rows")
    return "\n".join(lines)


def paris_status_message(state: dict) -> str:
    ps = state.get("paris", {})
    lines = [f"<b>{html.escape(PARIS_SHOW)}, Châtelet (Paris)</b> (last check {html.escape(ps.get('last_check_human', 'never'))})",
             f"Watching for: {html.escape(PARIS_TARGET_LABEL)}"]
    if PARIS_ONLY_DATES:
        lines.append(f"Only these dates: {html.escape(PARIS_ONLY_DATES_RAW)}")
    lines.append("")
    for perf in PARIS_PERFS:
        if not paris_date_watched(perf):
            continue
        pid = perf["id"]
        status = ps.get("status", {}).get(pid) or "unknown"
        parts = []
        mine = ps.get("matching", {}).get(pid)
        if mine is not None:
            parts.append(f"{len(mine)} in your rows" if mine else "none in your rows")
        resale_status = ps.get("resale_status", {}).get(pid)
        resale_mine = ps.get("resale_matching", {}).get(pid)
        if resale_mine:
            parts.append(f"resale: {len(resale_mine)} in your rows")
        elif resale_status == "available":
            parts.append("resale: some")
        extra = (" — " + " · ".join(parts)) if parts else ""
        lines.append(f"{STATUS_EMOJI.get(status, '❔')} {html.escape(perf['label'])}{extra}")
    lines.append("")
    lines.append("🟢 shows tickets · 🔴 sold out · ❔ unclear")
    return "\n".join(lines)


def _summary_line(perf: dict, seats: set, suffix: str = "") -> str:
    rows: dict[str, set] = {}
    for key in seats:
        section, row, _ = key.split("|")
        rows.setdefault(section, set()).add(row)
    where = "; ".join(f"{html.escape(sec)} rows {', '.join(sorted(r, key=lambda x: (0, int(x), '') if x.isdigit() else (1, 0, x)))}"
                      for sec, r in sorted(rows.items()))
    return f"• {html.escape(perf['label'])}{suffix}: {len(seats)} seats — {where}"


def _new_seats(current: set, seen: dict, now: float) -> set:
    cutoff = now - SEEN_SEAT_HOURS * 3600
    return {k for k in current if float(seen.get(k, 0)) < cutoff}


def _remember(seen: dict, current: set, now: float) -> None:
    for k in current:
        seen[k] = now
    cutoff = now - SEEN_SEAT_HOURS * 3600 * 2
    for k in [k for k, t in seen.items() if float(t) < cutoff]:
        del seen[k]


def run_paris_check(context, state: dict, notify: bool = True) -> None:
    ps = state.setdefault("paris", {})
    first_run = not ps.get("initialised")
    status: dict = ps.setdefault("status", {})
    resale_status: dict = ps.setdefault("resale_status", {})
    matching: dict = ps.setdefault("matching", {})
    resale_matching: dict = ps.setdefault("resale_matching", {})
    seen: dict = ps.setdefault("seen", {})            # perf|seat -> last time seen free
    seen_resale: dict = ps.setdefault("seen_resale", {})
    cache: dict = _SEAT_CACHE["shop"]
    resale_cache: dict = _SEAT_CACHE["resale"]
    ps.pop("cache", None)
    ps.pop("resale_cache", None)
    now = time.time()
    by_id = {p["id"]: p for p in PARIS_PERFS}
    watched = [p for p in PARIS_PERFS if paris_date_watched(p)]

    shop = read_secutix(context, PARIS_SHOP, watched, cache, resale=False, snapshot_name="paris_shop_dates")
    time.sleep(random.uniform(1, 3))
    # Resale exchange: read the calendar, and seat lists only for dates that have resale tickets
    # (or whose resale status we don't know yet).
    resale = read_secutix(context, PARIS_RESALE, watched, resale_cache, resale=True,
                          snapshot_name="paris_resale_dates")
    if shop["waiting_room"] and resale["waiting_room"]:
        _warn_once(state, "paris_waiting_room",
                   "⏳ The Châtelet ticket shop has a waiting room up right now, so I can't see "
                   f"{html.escape(PARIS_SHOW)} availability. I'll keep trying.", hours=2, notify=notify)
        return
    _clear_warning(state, "paris_waiting_room")
    if not shop["perfs"] and not shop["calendar"] and not resale["calendar"]:
        raise RuntimeError("Couldn't read the Châtelet pages")

    alerts_loud, alerts_quiet = [], []
    welcome_lines = []

    # --- main shop
    for perf in watched:
        pid = perf["id"]
        entry = shop["perfs"].get(pid) or {}
        old_status = status.get(pid)
        if entry.get("ok"):
            cache[pid] = {k: entry.get(k) for k in ("counts", "seats", "seats_at", "total", "level")}
            new_status = "available" if entry.get("total") else "sold_out"
        else:
            new_status = shop["calendar"].get(pid) or old_status
        if new_status:
            status[pid] = new_status
        if entry.get("ok") and not entry.get("seat_errors"):
            current = {_seat_key(s) for s in entry.get("seats", []) if seat_is_target(s, PARIS_TARGETS)}
            key_seen = {k: t for k, t in seen.items() if k.startswith(pid + "|")}
            fresh = _new_seats({f"{pid}|{k}" for k in current}, key_seen, now)
            fresh = {k.split("|", 1)[1] for k in fresh}
            before = len(matching.get(pid) or [])
            matching[pid] = sorted(current, key=_seat_sort)
            if first_run:
                if current:
                    welcome_lines.append(_summary_line(perf, current))
            elif fresh and before < 2:
                alerts_loud.append(
                    f"🎟🎟 <b>Paris — seats in your rows: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                    f"{format_paris_seats(list(fresh))}\n\nBook fast: {html.escape(paris_seat_url(pid))}")
            _remember(seen, {f"{pid}|{k}" for k in current}, now)
        elif not first_run and old_status == "sold_out" and new_status == "available":
            alerts_loud.append(
                f"🎟 <b>Paris — tickets just appeared: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                f"I couldn't read the seat list, so check your rows quickly:\n{html.escape(paris_seat_url(pid))}")

    # --- official resale exchange
    for perf in watched:
        pid = perf["id"]
        entry = resale["perfs"].get(pid) or {}
        old = resale_status.get(pid)
        cal = resale["calendar"].get(pid)
        if entry.get("ok"):
            resale_cache[pid] = {k: entry.get(k) for k in ("counts", "seats", "seats_at", "total", "level")}
            have = (entry.get("total") or 0) + (entry.get("resale_total") or 0)
            new = "available" if have else "sold_out"
        else:
            new = cal or old
        if new:
            resale_status[pid] = new
        if new == "sold_out" and not entry:
            resale_matching[pid] = []
        if entry.get("ok") and not entry.get("seat_errors"):
            current = {_seat_key(s) for s in entry.get("seats", []) if seat_is_target(s, PARIS_TARGETS)}
            key_seen = {k: t for k, t in seen_resale.items() if k.startswith(pid + "|")}
            fresh = {k.split("|", 1)[1] for k in _new_seats({f"{pid}|{k}" for k in current}, key_seen, now)}
            before = len(resale_matching.get(pid) or [])
            resale_matching[pid] = sorted(current, key=_seat_sort)
            if first_run:
                if current:
                    welcome_lines.append(_summary_line(perf, current, " (resale)"))
            elif fresh and before < 2:
                alerts_loud.append(
                    f"🎟🎟 <b>Paris resale — seats in your rows: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                    f"{format_paris_seats(list(fresh))}\n\nBook fast: {html.escape(paris_seat_url(pid, resale=True))}")
            elif old == "sold_out" and new == "available":
                alerts_quiet.append(f"Paris resale: tickets for {html.escape(perf['label'])}, "
                                    f"but none in your rows.\n{html.escape(paris_resale_dates_url())}")
            _remember(seen_resale, {f"{pid}|{k}" for k in current}, now)
        elif not first_run and old == "sold_out" and new == "available":
            alerts_loud.append(
                f"🎟 <b>Paris resale — tickets just appeared: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                f"On Châtelet's official resale exchange. Check if they're in your rows:\n"
                f"{html.escape(paris_resale_dates_url())}")

    if notify:
        for text in alerts_loud:
            send(text)
        for text in alerts_quiet:
            send(text, silent=True)

    ps["last_check_human"] = dt.datetime.now(LONDON).strftime("%a %d %b %H:%M")
    ps["read_ok"] = {"shop_dates": sum(1 for e in shop["perfs"].values() if e.get("ok")),
                     "resale_dates": sum(1 for e in resale["perfs"].values() if e.get("ok")),
                     "shop_calendar": len(shop["calendar"]), "resale_calendar": len(resale["calendar"])}
    if shop["errors"] or resale["errors"]:
        log("Paris read problems: " + " | ".join((shop["errors"] + resale["errors"])[:4]))
    rows_seen = {}
    for entry in shop["perfs"].values():
        for s in entry.get("seats", []):
            rows_seen.setdefault(s["section"], set()).add(s["row"])
    ps["rows_seen"] = {k: sorted(v, key=lambda r: (0, int(r), "") if r.isdigit() else (1, 0, r)) for k, v in rows_seen.items()}

    if first_run:
        ps["initialised"] = True
        if notify:
            send(_paris_welcome(ps, watched, welcome_lines))


def _paris_welcome(ps: dict, watched: list[dict], welcome_lines: list[str]) -> str:
    st = [ps.get("status", {}).get(p["id"]) for p in watched]
    rs = [ps.get("resale_status", {}).get(p["id"]) for p in watched]
    ok = ps.get("read_ok", {})
    lines = [
        f"👋 <b>Now also watching {html.escape(PARIS_SHOW)} at the Châtelet, Paris.</b>",
        f"Main shop: {st.count('available')} of {len(watched)} dates have tickets, {st.count('sold_out')} sold out.",
        f"Resale exchange: {rs.count('available')} dates with resale tickets.",
        f"Watching for: {html.escape(PARIS_TARGET_LABEL)}.",
    ]
    if ok.get("shop_dates"):
        lines.append(f"Seat reading: ✅ works ({ok.get('shop_dates')}/{len(watched)} dates).")
    else:
        lines.append("Seat reading: ⚠️ not yet — I'll alert you whenever any tickets appear instead.")
    if welcome_lines:
        lines.append("\n<b>Free right now in your rows:</b>")
        lines.extend(welcome_lines[:10])
        lines.append(f"\nBook: {html.escape(paris_shop_dates_url())}")
        lines.append("While a date already has free seats in your rows I stay quiet about it; "
                     "I message you when a date that had (almost) none gets some.")
    elif ok.get("shop_dates"):
        lines.append("\nNothing free in your rows right now — I'll message you the moment something appears.")
    lines.append("\nSend /status any time.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def new_browser(playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="en-GB",
        timezone_id="Europe/London",
        viewport={"width": 1366, "height": 900},
    )
    return browser, context


def main() -> int:
    args = set(sys.argv[1:])

    if "--test-telegram" in args:
        if not BOT_TOKEN or not CHAT_IDS:
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
            return 1
        send("✅ Test message from your ticket watcher. Telegram is set up correctly.")
        return 0

    from playwright.sync_api import sync_playwright

    if "--once" in args:
        state: dict = {"initialised": True, "paris": {"initialised": True}}
        with sync_playwright() as p:
            browser, context = new_browser(p)
            for i, url in enumerate(u for u in _env("SNAPSHOT_URLS").split() if u.startswith("http")):
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:
                        pass
                    _accept_cookies(page)
                    _expand_all_dates(page)
                    page.wait_for_timeout(2000)
                    rows = page.evaluate(EXTRACT_ROWS_JS, DATE_RE_SRC)
                    save_snapshot(f"page_{i + 1}", page.url, page.inner_text("body"), extra={"date_rows": rows})
                    print(f"Snapshot of {url}: {len(rows)} date rows")
                except Exception as exc:
                    print(f"Snapshot of {url} failed: {exc}")
                finally:
                    page.close()
            if LONDON_ENABLED:
                run_check(context, state, notify=False)
            if SWAN_ENABLED:
                state["swan"] = {"initialised": True}
                run_check(context, state, notify=False, show=SWAN)
            if PARIS_ENABLED:
                run_paris_check(context, state, notify=False)
            browser.close()
        if LONDON_ENABLED:
            print(re.sub(r"<[^>]+>", "", status_message(state)))
        if SWAN_ENABLED:
            print(re.sub(r"<[^>]+>", "", status_message(state, SWAN)))
        if PARIS_ENABLED:
            print(re.sub(r"<[^>]+>", "", paris_status_message(state)))
            ps = state.get("paris", {})
            print("Paris read:", json.dumps(ps.get("read_ok")))
            print("Paris rows with free seats:", json.dumps(ps.get("rows_seen"), ensure_ascii=False))
            for perf in PARIS_PERFS:
                for label, key in (("shop", "matching"), ("resale", "resale_matching")):
                    seats = ps.get(key, {}).get(perf["id"])
                    if seats:
                        print(f"{perf['label']} {label}: " + re.sub(r"<[^>]+>", "", format_paris_seats(seats, 20)).replace("\n", " | "))
        return 0

    if not BOT_TOKEN or not CHAT_IDS:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (see README).")
        return 1

    state = load_state()
    deadline = time.time() + RUN_FOR_MINUTES * 60 if RUN_FOR_MINUTES > 0 else None
    if LONDON_ENABLED:
        log(f"London: watching {PRODUCTION_URL} for {describe_targets()}"
            + (f" on {ONLY_DATES_RAW}" if ONLY_DATES else ""))
    if PARIS_ENABLED:
        log(f"Paris: watching {PARIS_SHOW} ({len(PARIS_PERFS)} performances) for {PARIS_TARGET_LABEL}")

    watchers = []
    if LONDON_ENABLED:
        watchers.append(("london", run_check, "the Nutcracker page (London)"))
    if SWAN_ENABLED:
        watchers.append(("swan", lambda ctx, st: run_check(ctx, st, show=SWAN), "the Swan Lake page (London)"))
    if PARIS_ENABLED:
        watchers.append(("paris", run_paris_check, f"the {PARIS_SHOW} pages (Paris)"))
    failures = {name: 0 for name, _, _ in watchers}

    with sync_playwright() as p:
        browser, context = new_browser(p)
        checks_with_this_browser = 0
        while True:
            if checks_with_this_browser >= 40:  # fresh browser now and then
                try:
                    browser.close()
                except Exception:
                    pass
                browser, context = new_browser(p)
                checks_with_this_browser = 0
            for name, check, what in watchers:
                initialised = state.get("initialised") if name == "london" else state.get(name, {}).get("initialised")
                try:
                    check(context, state)
                    if failures[name] >= 3:
                        send(f"✅ I can read {what} again.", silent=True)
                    failures[name] = 0
                    _clear_warning(state, f"failing_{name}")
                except Exception as exc:
                    failures[name] += 1
                    log(f"{name} check failed ({failures[name]} in a row): {exc}")
                    traceback.print_exc()
                    if failures[name] >= (1 if not initialised else 3):
                        _warn_once(state, f"failing_{name}",
                                   f"⚠️ I've failed to read {what} {failures[name]} times in a row "
                                   f"({html.escape(str(exc)[:200])}). I'll keep trying.", hours=3, notify=True)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser, context = new_browser(p)
                    checks_with_this_browser = 0
            checks_with_this_browser += 1
            save_state(state)

            # Wait for the next check, answering Telegram commands meanwhile.
            next_check = time.time() + CHECK_EVERY_MINUTES * 60 * random.uniform(0.85, 1.15)
            check_now = False
            while time.time() < next_check and not check_now:
                if deadline and time.time() >= deadline:
                    break
                for chat_id, command in poll_commands(state, next_check - time.time()):
                    if command in ("/status", "/start"):
                        if LONDON_ENABLED:
                            send(status_message(state), chat_ids=[chat_id])
                        if SWAN_ENABLED:
                            send(status_message(state, SWAN), chat_ids=[chat_id])
                        if PARIS_ENABLED:
                            send(paris_status_message(state), chat_ids=[chat_id])
                    elif command == "/check":
                        send("🔎 Checking now…", silent=True, chat_ids=[chat_id])
                        check_now = True
                    else:
                        send(help_text(), chat_ids=[chat_id])
                save_state(state)
            if deadline and time.time() >= deadline:
                log("Run time is up - stopping so the next run can take over.")
                break
        browser.close()
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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


def pick_link(row: dict) -> str | None:
    production_path = urllib.parse.urlparse(PRODUCTION_URL).path.rstrip("/")
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


def rows_to_performances(rows: list[dict]) -> dict[str, dict]:
    performances: dict[str, dict] = {}
    for row in rows:
        key = norm(row["date"])
        perf = {
            "label": re.sub(r"\s+", " ", row["date"]).strip().title().replace("Am", "am").replace("Pm", "pm"),
            "status": classify(row.get("text", "")),
            "text": row.get("text", "")[:300],
            "link": pick_link(row),
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


def read_production_page(context) -> tuple[dict[str, dict], dict]:
    page = context.new_page()
    info = {"waiting_room": False, "page_sold_out_banner": False}
    try:
        page.goto(PRODUCTION_URL, wait_until="domcontentloaded", timeout=60_000)
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
        return rows_to_performances(rows), info
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


def link_for(perf: dict) -> str:
    return perf.get("link") or PRODUCTION_URL


def status_message(state: dict) -> str:
    perfs = state.get("performances", {})
    seat_info = state.get("seat_checks", {})
    last = state.get("last_check_human", "never")
    lines = [f"<b>The Nutcracker — what I see</b> (last check {html.escape(last)})",
             f"Watching for: {html.escape(describe_targets())}"]
    if ONLY_DATES:
        lines.append(f"Only these dates: {html.escape(ONLY_DATES_RAW)}")
    lines.append("")
    if not perfs:
        lines.append("No performances read yet.")
    for key, perf in perfs.items():
        if not date_is_watched(key):
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
    if PARIS_ENABLED:
        lines.append(f"• {html.escape(PARIS_SHOW)}, Théâtre du Châtelet: {html.escape(PARIS_TARGET_LABEL)}")
    lines.append("\n/status – what I see right now\n/check – check right now\n/help – this message")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# One check
# --------------------------------------------------------------------------

def run_check(context, state: dict, notify: bool = True) -> None:
    first_run = not state.get("initialised")
    old_perfs = state.get("performances", {})
    seat_checks = state.setdefault("seat_checks", {})
    known_links = state.setdefault("links", {})

    perfs, info = read_production_page(context)

    if info["waiting_room"]:
        _warn_once(state, "waiting_room",
                   "⏳ The Royal Opera House site has a waiting room (queue) up right now, so I can't see "
                   f"ticket availability. I'll keep trying.\n{PRODUCTION_URL}", hours=1, notify=notify)
        return
    if not perfs:
        raise RuntimeError("Couldn't find any performance dates on the Nutcracker page")

    _clear_warning(state, "waiting_room")

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
    watched_with_links = [k for k in perfs if date_is_watched(k) and perfs[k].get("link")]
    to_check = [k for k in flipped if date_is_watched(k) and perfs[k].get("link")]
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
        result = check_seat_map(context, perfs[key]["link"], key, snapshot_name="london_seatmap")
        result["checked_at"] = dt.datetime.now(LONDON).strftime("%d %b %H:%M")
        seat_results[key] = result
        log(f"Seat map {perfs[key]['label']}: readable={result.get('readable')} "
            f"available={result.get('available_count')} matching={len(result.get('matching', []))} "
            f"error={result.get('error')}")

    # Alerts
    for key in perfs:
        if not date_is_watched(key):
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
                    f"🎟🎟 <b>Seats in your rows — {html.escape(perf['label'])}</b>\n"
                    f"{format_seats(sorted(result['matching']))}\n\n"
                    f"Book fast: {html.escape(link_for(perf))}"
                )
            elif key in flipped and not current and notify:
                send(
                    f"Returns appeared for {html.escape(perf['label'])} "
                    f"({result.get('available_count', 0)} seats), but none in your rows.\n{html.escape(link_for(perf))}",
                    silent=True,
                )
        elif key in flipped and notify:
            send(
                f"🎟 <b>Tickets just appeared — {html.escape(perf['label'])}</b>\n"
                + ("" if not contradictory else "(the seat map looked empty, so it may be a glitch)\n") +
                f"I couldn't read the seat map, so check your rows quickly:\n{html.escape(link_for(perf))}"
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
            send(_welcome_message(perfs, seat_results))


def _welcome_message(perfs: dict, seat_results: dict) -> str:
    counts: dict[str, int] = {}
    for perf in perfs.values():
        counts[perf["status"]] = counts.get(perf["status"], 0) + 1
    readable = [r for r in seat_results.values() if r.get("readable")]
    lines = [
        "👋 <b>Nutcracker watcher is running.</b>",
        f"Found {len(perfs)} performances: {counts.get('sold_out', 0)} sold out, "
        f"{counts.get('available', 0)} showing tickets, "
        f"{counts.get('unknown', 0) + counts.get('unavailable', 0)} unclear.",
        f"Watching for: {html.escape(describe_targets())}.",
    ]
    if ONLY_DATES:
        lines.append(f"Only these dates: {html.escape(ONLY_DATES_RAW)}.")
    if readable:
        sections = sorted({s for r in readable for s in r.get("sections", [])})
        lines.append(f"Seat map reading: ✅ works (sections seen: {html.escape(', '.join(sections[:8]))}).")
        matching_now = {k: r for k, r in seat_results.items() if r.get("matching")}
        for key, r in matching_now.items():
            lines.append(f"\nAlready available now — {html.escape(perfs[key]['label'])}:\n{format_seats(r['matching'])}\n"
                         f"{html.escape(link_for(perfs[key]))}")
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
PARIS_ENABLED = _flag("PARIS_ENABLED")
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


# Runs inside the page: for each performance, find the block of the page that
# belongs to it (by its id in a link/attribute, else by its date) and return its text.
EXTRACT_PERF_BLOCKS_JS = r"""
(perfs) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const mentions = (el, p) => {
    const h = el.outerHTML;
    if (h.includes(p.id)) return true;
    const t = el.innerText || '';
    return p.needles.some(n => t.includes(n));
  };
  const out = {};
  const all = Array.from(document.querySelectorAll('body *'));
  for (const p of perfs) {
    let anchor = null, via = null;
    for (const el of all) {
      if (!visible(el)) continue;
      for (const a of el.attributes) {
        if (a.value && a.value.includes(p.id)) { anchor = el; via = 'id'; break; }
      }
      if (anchor) break;
    }
    if (!anchor) {
      for (const el of all) {
        if (!visible(el)) continue;
        const t = el.innerText || '';
        if (t.length > 200 || !p.needles.some(n => t.includes(n))) continue;
        if (Array.from(el.children).some(c => p.needles.some(n => (c.innerText || '').includes(n)))) continue;
        // skip headers like "from 6 March 2027 to 14 March 2027"
        if (perfs.some(o => o.id !== p.id && o.needles.some(n => t.includes(n)))) continue;
        anchor = el; via = 'date'; break;
      }
    }
    if (!anchor) { out[p.id] = null; continue; }
    let row = anchor;
    while (row.parentElement && row.parentElement !== document.body) {
      const par = row.parentElement;
      if ((par.innerText || '').length > 900) break;
      if (perfs.some(o => o.id !== p.id && mentions(par, o))) break;
      row = par;
    }
    out[p.id] = { text: norm(row.innerText).slice(0, 700), via };
  }
  return out;
}
"""

PARIS_SOLD_RE = re.compile(
    r"sold\s*out|[ée]puis[ée]|\bcomplet\b|no longer available|plus disponible|"
    r"waiting\s*list|liste d.attente|\b0\s*(?:tickets?|billets?|places?)\b",
    re.I,
)
PARIS_AVAILABLE_RE = re.compile(
    r"\d+[.,]\d{2}\s*(?:eur|€)|€\s*\d+|\bbook\b|\bbuy\b|choose|select|add to|choisir|r[ée]server|acheter|"
    r"\bavailable\b|\bdisponible|derni[eè]res places|last seats|few seats|limited",
    re.I,
)
COUNT_RE = re.compile(r"\b(\d{1,4})\s*(?:tickets?|billets?|places?)\b", re.I)


def classify_paris_shop(text: str | None) -> str | None:
    if not text:
        return None
    if PARIS_SOLD_RE.search(text):
        return "sold_out"
    if PARIS_AVAILABLE_RE.search(text):
        return "available"
    return "unknown"


def resale_count(text: str | None):
    """Number of resale tickets on offer: int, -1 when 'some' but unclear, None when unknown."""
    if not text:
        return None
    m = COUNT_RE.search(text)
    if m:
        return int(m.group(1))
    if PARIS_SOLD_RE.search(text):
        return 0
    if PARIS_AVAILABLE_RE.search(text):
        return -1
    return None


def read_perf_blocks(context, url: str, snapshot_name: str) -> tuple[dict, dict]:
    page = context.new_page()
    info = {"waiting_room": False}
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
        blocks = page.evaluate(EXTRACT_PERF_BLOCKS_JS, PARIS_PERFS) or {}
        save_snapshot(snapshot_name, page.url, page.inner_text("body"),
                      extra={"blocks_found": blocks})
        return {k: v for k, v in blocks.items() if v}, info
    finally:
        page.close()


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
        status = ps.get("status", {}).get(perf["id"]) or "unknown"
        seat = ps.get("seat_checks", {}).get(perf["id"], {})
        resale = ps.get("resale", {}).get(perf["id"])
        extra = ""
        if seat.get("readable"):
            n = len(seat.get("matching", []))
            extra = f" — {n} seat{'s' if n != 1 else ''} in your rows" if n else " — none in your rows"
        if resale not in (None, 0):
            extra += " · resale: " + ("some" if resale == -1 else str(resale))
        lines.append(f"{STATUS_EMOJI.get(status, '❔')} {html.escape(perf['label'])}{extra}")
    lines.append("")
    lines.append("🟢 shows tickets · 🔴 sold out · ❔ unclear")
    return "\n".join(lines)


def run_paris_check(context, state: dict, notify: bool = True) -> None:
    ps = state.setdefault("paris", {})
    first_run = not ps.get("initialised")
    old_status: dict = ps.setdefault("status", {})
    old_resale: dict = ps.setdefault("resale", {})
    seat_checks: dict = ps.setdefault("seat_checks", {})
    by_id = {p["id"]: p for p in PARIS_PERFS}

    shop_blocks, shop_info = read_perf_blocks(context, paris_shop_dates_url(), "paris_shop_dates")
    time.sleep(random.uniform(1, 3))
    resale_blocks, resale_info = read_perf_blocks(context, paris_resale_dates_url(), "paris_resale_dates")

    if shop_info["waiting_room"] and resale_info["waiting_room"]:
        _warn_once(state, "paris_waiting_room",
                   "⏳ The Châtelet ticket shop has a waiting room up right now, so I can't see "
                   f"availability for {html.escape(PARIS_SHOW)}. I'll keep trying.", hours=2, notify=notify)
        return
    _clear_warning(state, "paris_waiting_room")

    new_status = {pid: classify_paris_shop((b or {}).get("text")) for pid, b in shop_blocks.items()}
    new_resale = {pid: resale_count((b or {}).get("text")) for pid, b in resale_blocks.items()}

    watched = [p["id"] for p in PARIS_PERFS if paris_date_watched(p)]
    flipped = [pid for pid in watched
               if old_status.get(pid) == "sold_out" and new_status.get(pid) == "available"]

    # Seat maps: anything that just changed, then a rotation through the rest.
    to_check = list(flipped)
    if watched:
        pointer = ps.get("rotation", 0) % len(watched)
        added = 0
        for pid in watched[pointer:] + watched[:pointer]:
            if added >= PARIS_SEATMAPS_PER_CYCLE:
                break
            if pid not in to_check:
                to_check.append(pid)
                added += 1
        ps["rotation"] = (pointer + max(added, 1)) % len(watched)

    seat_results = {}
    for pid in to_check:
        time.sleep(random.uniform(2, 4))
        current = new_status.get(pid) or old_status.get(pid)
        snap = "paris_seatmap_available" if current == "available" else "paris_seatmap_soldout"
        result = check_seat_map(context, paris_seat_url(pid), "paris_" + by_id[pid]["key"],
                                targets=PARIS_TARGETS, open_map=True, snapshot_name=snap)
        result["checked_at"] = dt.datetime.now(LONDON).strftime("%d %b %H:%M")
        seat_results[pid] = result
        log(f"Paris seat map {by_id[pid]['label']}: readable={result.get('readable')} "
            f"available={result.get('available_count')} matching={len(result.get('matching', []))} "
            f"sold_out_text={result.get('page_says_sold_out')} error={result.get('error')}")
        # If the dates page didn't show this performance, use what the seat page says.
        if new_status.get(pid) in (None, "unknown") and not result.get("error") and not result.get("waiting_room"):
            if result.get("readable") and result.get("available_count"):
                new_status[pid] = "available"
            elif result.get("page_says_sold_out"):
                new_status[pid] = "sold_out"
            if old_status.get(pid) == "sold_out" and new_status.get(pid) == "available" and pid not in flipped:
                flipped.append(pid)

    # Alerts - shop
    for pid in watched:
        perf = by_id[pid]
        result = seat_results.get(pid)
        previous_matching = set((seat_checks.get(pid) or {}).get("matching", []))
        link = paris_seat_url(pid)
        readable = bool(result and result.get("readable"))
        if readable:
            current = set(result["matching"])
            new_seats = current - previous_matching
            if new_seats and not first_run and notify:
                send(f"🎟🎟 <b>Paris — seats in your rows: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                     f"{format_seats(sorted(result['matching']))}\n\nBook fast: {html.escape(link)}")
            elif pid in flipped and not current and notify:
                send(f"Paris: tickets appeared for {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])} "
                     f"({result.get('available_count', 0)} seats), but none in your rows.\n{html.escape(link)}",
                     silent=True)
        elif pid in flipped and notify:
            send(f"🎟 <b>Paris — tickets just appeared: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                 f"I couldn't read the seat map, so check your rows quickly:\n{html.escape(link)}")
        if result and not result.get("error") and not result.get("waiting_room"):
            if readable:
                seat_checks[pid] = {k: result.get(k) for k in
                                    ("readable", "matching", "available_count", "sections", "source", "checked_at")}
            else:
                seat_checks[pid] = {**seat_checks.get(pid, {}), "readable": False,
                                    "checked_at": result.get("checked_at")}

    # Alerts - official resale exchange
    for pid in watched:
        perf = by_id[pid]
        now, before = new_resale.get(pid), old_resale.get(pid)
        if now in (None, 0):
            continue
        if before in (None, 0):
            more = True
        else:
            more = now > 0 and before > 0 and now > before
        if first_run or not more or not notify:
            continue
        count_text = "some" if now == -1 else str(now)
        resale_link = paris_resale_dates_url()
        # Try to see which seats they are.
        time.sleep(random.uniform(1, 3))
        detail = check_seat_map(context, paris_seat_url(pid, resale=True), "paris_resale_" + perf["key"],
                                targets=PARIS_TARGETS, open_map=True, snapshot_name="paris_resale_seatmap")
        if detail.get("readable") and detail.get("available_count"):
            if detail.get("matching"):
                send(f"🎟🎟 <b>Paris resale — seats in your rows: {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                     f"{format_seats(detail['matching'])}\n\nBook fast: {html.escape(resale_link)}")
            else:
                send(f"Paris resale: {count_text} ticket(s) for {html.escape(perf['label'])}, "
                     f"but none in your rows.\n{html.escape(resale_link)}", silent=True)
        else:
            send(f"🎟 <b>Paris resale — {count_text} ticket(s): {html.escape(PARIS_SHOW)}, {html.escape(perf['label'])}</b>\n"
                 f"On Châtelet's official resale exchange. Check if they're in your rows:\n{html.escape(resale_link)}")

    # Remember what we saw (keep old values for performances not visible this time)
    for pid, value in new_status.items():
        if value is not None:
            old_status[pid] = value
    for pid, value in new_resale.items():
        if value is not None:
            old_resale[pid] = value
    ps["last_check_human"] = dt.datetime.now(LONDON).strftime("%a %d %b %H:%M")
    ps["dates_found"] = {"shop": len(shop_blocks), "resale": len(resale_blocks)}

    if first_run:
        ps["initialised"] = True
        if notify:
            send(_paris_welcome(ps, seat_results))


def _paris_welcome(ps: dict, seat_results: dict) -> str:
    statuses = [ps.get("status", {}).get(p["id"]) for p in PARIS_PERFS]
    found = ps.get("dates_found", {})
    lines = [
        f"👋 <b>Now also watching {html.escape(PARIS_SHOW)} at the Châtelet, Paris.</b>",
        f"{len(PARIS_PERFS)} performances: {statuses.count('sold_out')} sold out, "
        f"{statuses.count('available')} showing tickets, "
        f"{len(PARIS_PERFS) - statuses.count('sold_out') - statuses.count('available')} not read yet.",
        f"Dates readable — shop: {found.get('shop', 0)}/{len(PARIS_PERFS)}, "
        f"resale exchange: {found.get('resale', 0)}/{len(PARIS_PERFS)}.",
        f"Watching for: {html.escape(PARIS_TARGET_LABEL)}.",
    ]
    resale_now = [(p, ps.get("resale", {}).get(p["id"])) for p in PARIS_PERFS]
    resale_now = [(p, n) for p, n in resale_now if n not in (None, 0)]
    if resale_now:
        lines.append("Resale tickets on offer right now: " + ", ".join(
            f"{html.escape(p['label'])} ({'some' if n == -1 else n})" for p, n in resale_now)
            + f"\n{html.escape(paris_resale_dates_url())}")
    readable = [r for r in seat_results.values() if r.get("readable")]
    if readable:
        sections = sorted({s for r in readable for s in r.get("sections", [])})
        lines.append(f"Seat map reading: ✅ works (areas seen: {html.escape(', '.join(sections[:8]))}).")
        for pid, r in seat_results.items():
            if r.get("matching"):
                perf = next(p for p in PARIS_PERFS if p["id"] == pid)
                lines.append(f"\nAlready available now — {html.escape(perf['label'])}:\n"
                             f"{format_seats(r['matching'])}\n{html.escape(paris_seat_url(pid))}")
    else:
        lines.append("Seat map reading: ⚠️ not yet — until it works I'll alert you whenever "
                     "<i>any</i> tickets or resale tickets appear.")
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
            if LONDON_ENABLED:
                run_check(context, state, notify=False)
            if PARIS_ENABLED:
                run_paris_check(context, state, notify=False)
            browser.close()
        if LONDON_ENABLED:
            print(re.sub(r"<[^>]+>", "", status_message(state)))
        if PARIS_ENABLED:
            print(re.sub(r"<[^>]+>", "", paris_status_message(state)))
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
                initialised = state.get("initialised") if name == "london" else state.get("paris", {}).get("initialised")
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

#!/usr/bin/env python3
"""
Presidio Golf Course tee-time watcher.

Presidio books through Club Prophet (presidio.cps.golf). That site is a
JavaScript app that fetches tee times from a private JSON API with a
short-lived token. Rather than hard-code that auth flow (which changes),
this script opens the real booking page in a headless browser, lets the page
authenticate itself, captures the tee-time API call it makes, and then
replays that same call for each date you care about.

Usage:
    python checker.py                # normal run: check + notify
    python checker.py --dry-run      # check and print, don't notify or save state
    python checker.py --discover     # dump what the site returns (debugging)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BOOKING_URL = "https://presidio.cps.golf/onlineresweb/"
TZ = ZoneInfo("America/Los_Angeles")
HERE = Path(__file__).parent
STATE_FILE = HERE / "state" / "seen.json"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TeeTime:
    day: str            # YYYY-MM-DD
    time: str           # HH:MM (24h)
    open_spots: int | None
    min_group: int | None
    holes: int | None
    course: str | None
    price: float | None

    @property
    def key(self) -> str:
        return f"{self.day} {self.time} {self.holes or ''} {self.course or ''}".strip()

    def pretty(self) -> str:
        t = datetime.strptime(self.time, "%H:%M").strftime("%-I:%M %p")
        d = datetime.strptime(self.day, "%Y-%m-%d").strftime("%a %b %-d")
        bits = [f"{d} at {t}"]
        if self.open_spots is not None:
            bits.append(f"{self.open_spots} spot{'s' if self.open_spots != 1 else ''}")
        if self.holes:
            bits.append(f"{self.holes} holes")
        if self.course and "10" in self.course:
            bits.append("starts on 10")
        return " · ".join(bits)


# --------------------------------------------------------------------------
# Config + target dates
# --------------------------------------------------------------------------

def load_config() -> dict:
    with open(HERE / "config.yaml") as f:
        return yaml.safe_load(f)


def target_dates(cfg: dict, today: date) -> dict[str, list[dict]]:
    """Map each date (YYYY-MM-DD) to the rules that apply to it."""
    window = int(cfg.get("booking_window_days", 7))
    skip = set(cfg.get("skip_dates") or [])
    out: dict[str, list[dict]] = {}
    for rule in cfg["rules"]:
        days: list[date] = []
        if rule.get("dates"):
            days = [date.fromisoformat(str(d)) for d in rule["dates"]]
        elif rule.get("weekdays"):
            want = {w[:3].title() for w in rule["weekdays"]}
            days = [today + timedelta(i) for i in range(window + 1)
                    if WEEKDAYS[(today + timedelta(i)).weekday()] in want]
        for d in days:
            if d < today or d.isoformat() in skip:
                continue
            out.setdefault(d.isoformat(), []).append(rule)
    return out


def matches(tt: TeeTime, rule: dict) -> bool:
    if not (rule.get("earliest", "00:00") <= tt.time <= rule.get("latest", "23:59")):
        return False
    need = int(rule.get("players", 1))
    if tt.open_spots is not None and tt.open_spots < need:
        return False
    if tt.min_group is not None and need < tt.min_group:
        return False   # slot requires a bigger group than you're booking
    if rule.get("holes") and tt.holes and int(rule["holes"]) != tt.holes:
        return False
    return True


# --------------------------------------------------------------------------
# Parsing the API response (defensive — field names vary by CPS version)
# --------------------------------------------------------------------------

TIME_KEYS = ("startTime", "teeTime", "teeOffTime", "time", "startDateTime", "teeTimeDateTime")
SPOT_KEYS = ("maxPlayer", "maxPlayers", "availableSpots", "playersAvailable",
             "availablePlayers", "openSlots")
HOLE_KEYS = ("holes", "holesDisplay", "numberOfHoles", "holeCount")
COURSE_KEYS = ("courseName", "course", "courseDisplayName")
PRICE_KEYS = ("shItemPrices", "shItemPrice","price", "greenFee", "displayPrice", "minPrice")


def _first(d: dict, keys) -> object:
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] not in (None, ""):
            return lower[k.lower()]
    return None


def _parse_time(v: object, fallback_day: str) -> tuple[str, str] | None:
    s = str(v)
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})", s)
    if m:
        return m.group(1), f"{int(m.group(2)):02d}:{m.group(3)}"
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", s)
    if m:
        h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").lower()
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return fallback_day, f"{h:02d}:{mi}"
    return None


def _to_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):          # e.g. [1,2,3,4] allowed group sizes
        nums = [x for x in (_to_int(i) for i in v) if x is not None]
        return max(nums) if nums else None
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


def _to_float(v) -> float | None:
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = _first(v, PRICE_KEYS)
    try:
        return float(str(v).replace("$", "").replace(",", "")) if v is not None else None
    except ValueError:
        return None


def parse_tee_times(payload: object, day: str) -> list[TeeTime]:
    """Walk any JSON shape and pull out objects that look like tee times."""
    found: dict[str, TeeTime] = {}

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            tv = _first(node, TIME_KEYS)
            parsed = _parse_time(tv, day) if tv is not None else None
            if parsed and parsed[0] == day:
                tt = TeeTime(
                    day=parsed[0],
                    time=parsed[1],
                    open_spots=_to_int(_first(node, SPOT_KEYS)),
                    min_group=_to_int(_first(node, ("minPlayer", "minPlayers"))),
                    holes=_to_int(_first(node, HOLE_KEYS)),
                    course=(lambda c: str(c) if c else None)(_first(node, COURSE_KEYS)),
                    price=_to_float(_first(node, PRICE_KEYS)),
                )
                # Keep the record with the most spots if duplicates appear.
                prev = found.get(tt.key)
                if not prev or (tt.open_spots or 0) > (prev.open_spots or 0):
                    found[tt.key] = tt
                return  # don't descend into a tee time's own sub-records (bookings, prices)
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    walk(payload)
    return sorted(found.values(), key=lambda t: (t.day, t.time))


# --------------------------------------------------------------------------
# Fetching via headless browser
# --------------------------------------------------------------------------

# The booking page's own API flow (verified against the live site):
#   1. POST /identityapi/myconnect/token/short   -> anonymous short-lived token
#   2. POST .../RegisterTransactionId           -> registers a search session
#   3. GET  .../TeeTimes?searchDate=...          -> tee times for one day
# It runs inside the loaded page so it passes the site's bot checks the same
# way a normal visitor does.
FETCH_JS = r"""
async ([dates, courseIds]) => {
  const fd = new FormData(); fd.append('client_id', 'onlinereswebshortlived');
  const tokRes = await fetch('/identityapi/myconnect/token/short', {method: 'POST', body: fd});
  if (!tokRes.ok) return {error: 'token HTTP ' + tokRes.status};
  const tok = (await tokRes.json()).access_token;
  const H = {
    'Authorization': 'Bearer ' + tok, 'client-id': 'onlineresweb',
    'x-websiteid': '04a414d5-42cc-4020-88bf-08da0ea514be', 'x-siteid': '1',
    'x-componentid': '1', 'x-moduleid': '7', 'x-productid': '1', 'x-terminalid': '3',
    'x-ismobile': 'false', 'x-timezone-offset': '420',
    'x-timezoneid': 'America/Los_Angeles', 'Content-Type': 'application/json'
  };
  const api = '/onlineres/onlineapi/api/v1/onlinereservation';
  const txn = crypto.randomUUID();
  const reg = await fetch(api + '/RegisterTransactionId',
      {method: 'POST', headers: H, body: JSON.stringify({transactionId: txn})});
  if (!reg.ok) return {error: 'register HTTP ' + reg.status};
  const out = {};
  for (const iso of dates) {
    const [y, m, d] = iso.split('-').map(Number);
    const q = new URLSearchParams({
      searchDate: new Date(y, m - 1, d).toDateString(), holes: '0', numberOfPlayer: '0',
      courseIds: courseIds, searchTimeType: '0', transactionId: txn,
      teeOffTimeMin: '0', teeOffTimeMax: '23', isChangeTeeOffTime: 'true',
      teeSheetSearchView: '5', classCode: 'R', defaultOnlineRate: 'N',
      isUseCapacityPricing: 'false', memberStoreId: '1', searchType: '1'
    });
    const r = await fetch(api + '/TeeTimes?' + q, {headers: H});
    out[iso] = r.ok ? await r.json() : {httpError: r.status};
  }
  return {results: out};
}
"""

COURSE_IDS = "3,4"   # 3 = The Presidio (1st tee), 4 = The Presidio 10th Tee


def fetch(dates: list[str], discover: bool = False) -> dict[str, list[TeeTime]]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            timezone_id="America/Los_Angeles",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/129.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.goto(BOOKING_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2_000)
        data = page.evaluate(FETCH_JS, [dates, COURSE_IDS])
        if discover:
            page.screenshot(path=str(HERE / "discover.png"), full_page=True)
        browser.close()

    if data.get("error"):
        raise RuntimeError(f"Booking site refused the request: {data['error']}")

    results: dict[str, list[TeeTime]] = {}
    for d, body in data["results"].items():
        if isinstance(body, dict) and body.get("httpError"):
            print(f"[warn] {d}: HTTP {body['httpError']}", file=sys.stderr)
            continue
        results[d] = parse_tee_times(body, d)
        if discover:
            print(f"\n{d}: {len(results[d])} open tee times")
            if not results[d]:
                print("   raw:", json.dumps(body)[:600])
            for t in results[d]:
                print("  ", t.pretty())
    return results


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(hits: list[tuple[TeeTime, dict]]) -> None:
    lines = [f"• {t.pretty()}  ({r.get('name', 'rule')})" for t, r in hits]
    title = f"⛳ {len(hits)} Presidio tee time{'s' if len(hits) > 1 else ''} opened"
    body = "\n".join(lines) + f"\n\nBook: {BOOKING_URL}"
    sent = False

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode(),
            headers={"Title": title.encode("ascii", "ignore").decode().strip(),
                     "Tags": "golf", "Click": BOOKING_URL, "Priority": "high"},
        )
        urllib.request.urlopen(req, timeout=15)
        sent = True

    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("NOTIFY_EMAIL")
    if user and pw and to:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, user, to
        msg.set_content(body)
        host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465"))) as s:
            s.login(user, pw)
            s.send_message(msg)
        sent = True

    if not sent:
        print("[warn] No notifier configured (set NTFY_TOPIC and/or SMTP_* secrets).")
    print(title + "\n" + body)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_seen() -> dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_seen(seen: dict[str, str], today: date) -> None:
    # Forget slots for past days so the file stays small.
    seen = {k: v for k, v in seen.items() if k[:10] >= today.isoformat()}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True))


def run(dry_run: bool, discover: bool) -> int:
    cfg = load_config()
    today = datetime.now(TZ).date()
    targets = target_dates(cfg, today)
    if not targets:
        print("No dates to check right now.")
        return 0
    print("Checking:", ", ".join(sorted(targets)))

    available = fetch(sorted(targets), discover=discover)
    seen = load_seen()
    now_open: set[str] = set()
    hits: list[tuple[TeeTime, dict]] = []

    for d, rules in targets.items():
        for tt in available.get(d, []):
            rule = next((r for r in rules if matches(tt, r)), None)
            if not rule:
                continue
            now_open.add(tt.key)
            if tt.key not in seen:
                hits.append((tt, rule))

    print(f"{len(now_open)} matching slot(s) open; {len(hits)} new.")
    if hits and not dry_run and not discover:
        notify(hits)
    elif hits:
        for t, r in hits:
            print("  NEW:", t.pretty(), f"({r.get('name')})")

    if not dry_run and not discover:
        # Remember what's open now. Slots that get booked again drop out, so
        # if one re-opens later you'll be alerted again.
        stamp = datetime.now(TZ).isoformat(timespec="minutes")
        checked_days = set(available)
        seen = {k: v for k, v in seen.items() if k[:10] not in checked_days or k in now_open}
        for k in now_open:
            seen.setdefault(k, stamp)
        save_seen(seen, today)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--discover", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.dry_run, a.discover))

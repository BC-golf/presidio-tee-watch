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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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
    holes: int | None
    course: str | None
    price: float | None

    @property
    def key(self) -> str:
        return f"{self.day} {self.time} {self.holes or ''}".strip()

    def pretty(self) -> str:
        t = datetime.strptime(self.time, "%H:%M").strftime("%-I:%M %p")
        d = datetime.strptime(self.day, "%Y-%m-%d").strftime("%a %b %-d")
        bits = [f"{d} at {t}"]
        if self.open_spots is not None:
            bits.append(f"{self.open_spots} spot{'s' if self.open_spots != 1 else ''}")
        if self.holes:
            bits.append(f"{self.holes} holes")
        if self.price:
            bits.append(f"${self.price:,.0f}")
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
    if rule.get("holes") and tt.holes and int(rule["holes"]) != tt.holes:
        return False
    return True


# --------------------------------------------------------------------------
# Parsing the API response (defensive — field names vary by CPS version)
# --------------------------------------------------------------------------

TIME_KEYS = ("startTime", "teeTime", "teeOffTime", "time", "startDateTime", "teeTimeDateTime")
SPOT_KEYS = ("availableParticipantNo", "availableSpots", "playersAvailable",
             "maxPlayer", "maxPlayers", "availablePlayers", "openSlots")
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
                    holes=_to_int(_first(node, HOLE_KEYS)),
                    course=(lambda c: str(c) if c else None)(_first(node, COURSE_KEYS)),
                    price=_to_float(_first(node, PRICE_KEYS)),
                )
                # Keep the record with the most spots if duplicates appear.
                prev = found.get(tt.key)
                if not prev or (tt.open_spots or 0) > (prev.open_spots or 0):
                    found[tt.key] = tt
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    walk(payload)
    return sorted(found.values(), key=lambda t: (t.day, t.time))


# --------------------------------------------------------------------------
# Fetching via headless browser
# --------------------------------------------------------------------------

DATE_FORMATS = [
    ("%a %b %d %Y", re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{4}$")),
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("%m/%d/%Y", re.compile(r"^\d{2}/\d{2}/\d{4}$")),
    ("%Y-%m-%dT%H:%M:%S", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")),
]


def swap_date_param(url: str, day: date) -> str:
    """Replace the date query param in the captured URL, keeping its format."""
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    out = []
    for k, v in q:
        if "date" in k.lower():
            for fmt, pat in DATE_FORMATS:
                if pat.match(v):
                    v = day.strftime(fmt)
                    break
        out.append((k, v))
    return urlunsplit(parts._replace(query=urlencode(out)))


def fetch(dates: list[str], discover: bool = False) -> dict[str, list[TeeTime]]:
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []
    results: dict[str, list[TeeTime]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            timezone_id="America/Los_Angeles",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/129.0 Safari/537.36"),
        )
        page = ctx.new_page()

        def on_response(resp):
            if "teetime" in resp.url.lower() and resp.request.method == "GET":
                try:
                    body = resp.json()
                except Exception:
                    return
                captured.append({"url": resp.url, "headers": resp.request.headers, "body": body})

        page.on("response", on_response)
        page.goto(BOOKING_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(4_000)

        if not captured:
            # Some versions only search after a click on a "search"/date control.
            for label in ("Search", "Tee Times", "Book a Tee Time", "Public"):
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=2_000)
                    page.wait_for_timeout(3_000)
                except Exception:
                    pass
                if captured:
                    break

        if discover:
            print(f"Final page URL: {page.url}")
            print(f"Captured {len(captured)} tee-time API call(s).")
            for c in captured[:3]:
                print("\nURL:", c["url"])
                print("BODY (truncated):", json.dumps(c["body"])[:2500])
            page.screenshot(path=str(HERE / "discover.png"), full_page=True)
            print("\nSaved screenshot to discover.png")

        if not captured:
            browser.close()
            raise RuntimeError(
                "Didn't see the tee-time API call. Run `python checker.py --discover` "
                "and check discover.png — the booking page layout may have changed."
            )

        template = captured[-1]
        # Strip headers the browser sets itself.
        headers = {k: v for k, v in template["headers"].items()
                   if not k.startswith(":") and k.lower() not in
                   ("host", "content-length", "accept-encoding", "cookie")}

        for d in dates:
            url = swap_date_param(template["url"], date.fromisoformat(d))
            r = ctx.request.get(url, headers=headers, timeout=30_000)
            if not r.ok:
                print(f"[warn] {d}: HTTP {r.status}", file=sys.stderr)
                continue
            try:
                body = r.json()
            except Exception:
                print(f"[warn] {d}: non-JSON response", file=sys.stderr)
                continue
            results[d] = parse_tee_times(body, d)
            if discover:
                print(f"\n{d}: parsed {len(results[d])} tee times")
                for t in results[d][:10]:
                    print("  ", t.pretty())

        browser.close()
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

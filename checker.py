#!/usr/bin/env python3
"""
San Francisco tee-time watcher: Presidio, TPC Harding Park and the Fleming 9.

- Presidio books through Club Prophet (presidio.cps.golf).
- Harding Park / Fleming 9 book through EZLinks (hardingpark.ezlinksgolf.com).

For each site the script opens the real booking page in a headless browser,
lets it load normally, then asks the page's own tee-time API for each date.
If one site fails, the others still get checked.

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
from dataclasses import dataclass
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

HARDING_URL = "https://hardingpark.ezlinksgolf.com/index.html#/search"
SITE_LABEL = {"presidio": "Presidio", "harding": "Harding Park", "fleming": "Fleming 9"}
SITE_BOOKING = {"presidio": BOOKING_URL, "harding": "https://hardingpark.ezlinksgolf.com/",
                "fleming": "https://hardingpark.ezlinksgolf.com/"}


def rule_sites(rule: dict) -> set[str]:
    """Courses an alert covers. Alerts from before multi-course support = Presidio."""
    c = rule.get("courses")
    if not c:
        return {"presidio"}
    return {str(x).strip().lower() for x in (c if isinstance(c, list) else [c])}


def site_of_key(key: str) -> str:
    if "Fleming" in key:
        return "fleming"
    if "Harding" in key:
        return "harding"
    return "presidio"


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
    site: str = "presidio"     # presidio | harding | fleming

    @property
    def key(self) -> str:
        return f"{self.day} {self.time} {self.holes or ''} {self.course or ''}".strip()

    def pretty(self) -> str:
        t = datetime.strptime(self.time, "%H:%M").strftime("%-I:%M %p")
        d = datetime.strptime(self.day, "%Y-%m-%d").strftime("%a %b %-d")
        bits = [SITE_LABEL.get(self.site, self.site), f"{d} at {t}"]
        if self.site != "presidio" and self.open_spots is not None:
            lo = self.min_group or self.open_spots
            bits.append(f"{lo} golfer{'s' if lo != 1 else ''}" if lo == self.open_spots
                        else f"{lo}–{self.open_spots} golfers")
        elif self.open_spots is not None:
            bits.append(f"{self.open_spots} spot{'s' if self.open_spots != 1 else ''}")
        if self.holes:
            bits.append(f"{self.holes} holes")
        if self.site == "presidio" and self.course and "10" in self.course:
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
    if tt.site not in rule_sites(rule):
        return False
    if not (rule.get("earliest", "00:00") <= tt.time <= rule.get("latest", "23:59")):
        return False
    players = rule.get("players", "any")
    if str(players).strip().lower() not in ("any", "0", ""):
        need = int(players)
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

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch_presidio(browser, dates: list[str], discover: bool) -> dict[str, list[TeeTime]]:
    ctx = browser.new_context(timezone_id="America/Los_Angeles", user_agent=UA)
    page = ctx.new_page()
    page.goto(BOOKING_URL, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(2_000)
    data = page.evaluate(FETCH_JS, [dates, COURSE_IDS])
    if discover:
        page.screenshot(path=str(HERE / "discover.png"), full_page=True)
    ctx.close()

    if data.get("error"):
        raise RuntimeError(f"Presidio booking site refused the request: {data['error']}")
    results: dict[str, list[TeeTime]] = {}
    for d, body in data["results"].items():
        if isinstance(body, dict) and body.get("httpError"):
            print(f"[warn] Presidio {d}: HTTP {body['httpError']}", file=sys.stderr)
            continue
        results[d] = parse_tee_times(body, d)
    return results


# Harding Park / Fleming 9 (EZLinks). The search endpoint takes the number of
# golfers and only returns slots that group can book, so we ask once per group
# size we care about and record the smallest/largest size each slot accepts.
HARDING_JS = r"""
async ([dates, sizes]) => {
  const init = await fetch('/api/search/init', {credentials: 'include'});
  if (!init.ok) return {error: 'init HTTP ' + init.status};
  const cfg = await init.json();
  const ids = String(cfg.AllCourseIDs || '5999,6000').split(',').map(Number);
  const out = {};
  for (const iso of dates) {
    const [y, m, d] = iso.split('-');
    out[iso] = {};
    for (const n of sizes) {
      let r = null;
      for (let attempt = 0; attempt < 4; attempt++) {   // site sometimes 403/429s briefly
        if (attempt) await new Promise(res => setTimeout(res, 1500 * attempt));
        r = await fetch('/api/search/search', {
          method: 'POST', credentials: 'include',
          headers: {'Content-Type': 'application/json; charset=utf-8'},
          body: JSON.stringify({p01: ids, p02: `${m}/${d}/${y}`, p03: '5:00 AM', p04: '7:00 PM',
                                p05: 0, p06: n, p07: false})
        });
        if (r.ok) break;
      }
      if (!r.ok) { out[iso][n] = {httpError: r.status}; continue; }
      const j = await r.json();
      out[iso][n] = (j.r06 || []).map(t => ({start: t.r15, course: t.r16, holes: t.r28, price: t.r08}));
      await new Promise(res => setTimeout(res, 400));
    }
  }
  return {results: out};
}
"""


def parse_harding(raw: dict[str, dict], day: str) -> list[TeeTime]:
    slots: dict[tuple, dict] = {}
    for n, rows in raw.items():
        if isinstance(rows, dict):        # httpError for this size
            continue
        for r in rows:
            parsed = _parse_time(r.get("start"), day)
            if not parsed or parsed[0] != day:
                continue
            course = "Fleming 9" if "fleming" in str(r.get("course", "")).lower() else "Harding Park"
            k = (parsed[1], course)
            # The site's "holes" field is unreliable for the main course; the
            # course itself tells us: Fleming is a 9-hole course, Harding is 18.
            e = slots.setdefault(k, {"sizes": set(), "holes": 9 if course == "Fleming 9" else 18, "prices": []})
            e["sizes"].add(int(n))
            if r.get("price") is not None:
                e["prices"].append(float(r["price"]))
    out = [TeeTime(day=day, time=t, open_spots=max(e["sizes"]), min_group=min(e["sizes"]),
                   holes=e["holes"], course=course, price=min(e["prices"]) if e["prices"] else None,
                   site="fleming" if course == "Fleming 9" else "harding")
           for (t, course), e in slots.items()]
    return sorted(out, key=lambda x: (x.time, x.course))


def _start_virtual_screen():
    """Start a virtual display (Xvfb) so Chrome can run as a normal, non-headless
    window on a server. Returns the process, or None if Xvfb isn't available."""
    import shutil, subprocess, time
    if os.getenv("DISPLAY") or not shutil.which("Xvfb"):
        return None
    proc = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    if proc.poll() is not None:
        return None
    os.environ["DISPLAY"] = ":99"
    return proc


def fetch_harding(p, dates: list[str], sizes: list[int], discover: bool) -> dict[str, list[TeeTime]]:
    # Harding sits behind a "verifying you are human" check that blocks headless
    # browsers. Best chance: real Google Chrome (preinstalled on GitHub's
    # servers) running as a normal window on a virtual screen.
    xvfb = _start_virtual_screen()
    headless = xvfb is None and not os.getenv("DISPLAY")
    args = ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"]
    browser, mode = None, ""
    for channel in ("chrome", "chromium", None):
        try:
            kw = {"channel": channel} if channel else {}
            browser = p.chromium.launch(headless=headless, args=args, **kw)
            mode = f"{channel or 'bundled'} {'headless' if headless else 'windowed'}"
            break
        except Exception:
            continue
    if browser is None:
        raise RuntimeError("couldn't start a browser for Harding Park")
    if discover:
        print(f"Harding browser: {mode}")
    try:
        ctx_kw = dict(timezone_id="America/Los_Angeles", locale="en-US",
                      viewport={"width": 1366, "height": 900})
        if headless:
            ctx_kw["user_agent"] = UA          # hide "HeadlessChrome"
        ctx = browser.new_context(**ctx_kw)
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()
        page.goto(HARDING_URL, wait_until="domcontentloaded", timeout=60_000)
        for _ in range(40):                       # up to ~60s for the human check
            if "just a moment" not in (page.title() or "").lower():
                break
            page.wait_for_timeout(1_500)
        page.wait_for_load_state("networkidle", timeout=30_000)
        page.wait_for_timeout(3_000)              # let the page's own first search finish
        title = page.title() or ""
        if discover:
            page.screenshot(path=str(HERE / "discover-harding.png"), full_page=True)
            print(f"Harding page title: {title!r}")
        if "just a moment" in title.lower():
            raise RuntimeError("blocked by the site's 'verifying you are human' check")
        data = page.evaluate(HARDING_JS, [dates, sizes])
    finally:
        browser.close()
        if xvfb:
            xvfb.terminate()
    if data.get("error"):
        raise RuntimeError(f"Harding booking site refused the request: {data['error']}")
    out = {}
    for d, raw in data["results"].items():
        errs = {n: v["httpError"] for n, v in raw.items() if isinstance(v, dict)}
        if errs:
            print(f"[warn] Harding {d}: some searches failed {errs}", file=sys.stderr)
        else:                             # only trust a day if every search worked
            out[d] = parse_harding(raw, d)
    return out


def fetch(targets: dict[str, list[dict]], discover: bool = False):
    """Returns ({day: [TeeTime]}, {(day, site) that were checked successfully})."""
    from playwright.sync_api import sync_playwright

    p_dates = sorted(d for d, rules in targets.items() if any("presidio" in rule_sites(r) for r in rules))
    h_dates = sorted(d for d, rules in targets.items()
                     if any(rule_sites(r) & {"harding", "fleming"} for r in rules))
    sizes: set[int] = set()
    for d in h_dates:
        for r in targets[d]:
            if rule_sites(r) & {"harding", "fleming"}:
                pl = str(r.get("players", "any")).strip().lower()
                sizes |= {1, 2, 3, 4} if pl in ("any", "0", "") else {int(pl)}

    results: dict[str, list[TeeTime]] = {}
    checked: set[tuple[str, str]] = set()
    presidio_failed = False
    with sync_playwright() as p:
        if p_dates:
            try:
                browser = p.chromium.launch(headless=True)
                try:
                    for d, tts in fetch_presidio(browser, p_dates, discover).items():
                        results.setdefault(d, []).extend(tts)
                        checked.add((d, "presidio"))
                finally:
                    browser.close()
            except Exception as e:
                presidio_failed = True
                print(f"[warn] Presidio check failed: {e}", file=sys.stderr)
        if h_dates:
            try:
                for d, tts in fetch_harding(p, h_dates, sorted(sizes), discover).items():
                    results.setdefault(d, []).extend(tts)
                    checked.update({(d, "harding"), (d, "fleming")})
            except Exception as e:
                print(f"[warn] Harding Park check failed: {e}", file=sys.stderr)

    if discover:
        for d in sorted(results):
            print(f"\n{d}: {len(results[d])} open tee times")
            for t in sorted(results[d], key=lambda x: (x.site, x.time)):
                print("  ", t.pretty())
    # Presidio has been reliable, so a Presidio failure is worth a red X in
    # GitHub. Harding failures only warn (its bot check can block at times).
    if presidio_failed:
        raise RuntimeError("Presidio could not be checked — see warnings above.")
    return results, checked


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(hits: list[tuple[TeeTime, dict]]) -> None:
    lines = [f"• {t.pretty()}  ({r.get('name', 'rule')})" for t, r in hits]
    title = f"⛳ {len(hits)} tee time{'s' if len(hits) > 1 else ''} opened"
    sites = []
    for t, _ in hits:
        if t.site not in sites:
            sites.append(t.site)
    links = {}
    for st in sites:   # Harding + Fleming share one booking page
        links.setdefault(SITE_BOOKING[st], SITE_LABEL[st] if st != "fleming" else "Harding Park")
    body = "\n".join(lines) + "\n\n" + "\n".join(f"Book {n}: {u}" for u, n in links.items())
    click = SITE_BOOKING[sites[0]]
    sent = False

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode(),
            headers={"Title": title.encode("ascii", "ignore").decode().strip(),
                     "Tags": "golf", "Click": click, "Priority": "high"},
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

    available, checked = fetch(targets, discover=discover)
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
        # Only forget slots for courses we actually checked this run, so a
        # site being down doesn't cause repeat alerts when it comes back.
        seen = {k: v for k, v in seen.items()
                if (k[:10], site_of_key(k)) not in checked or k in now_open}
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

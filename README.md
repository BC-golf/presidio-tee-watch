# Presidio Tee-Time Watcher

Checks Presidio Golf Course's booking site every 10 minutes (5am–10pm Pacific)
and alerts you when a tee time matching your rules opens up. Runs free on
GitHub Actions — nothing to keep running on your computer.

## How it works

Presidio books through Club Prophet (`presidio.cps.golf`), a JavaScript app
that pulls tee times from a private API with a short-lived token. The script
opens the real page in a headless browser, lets it authenticate, captures the
tee-time API call, and replays it for each date in your rules. Each slot only
alerts once; if it gets booked and later re-opens, you're alerted again.

## Setup (~10 minutes)

1. **Create a public GitHub repo** and upload everything in this folder
   (including the hidden `.github/` folder). Public repos get unlimited free
   Actions minutes; a private repo would exceed the free 2,000 minutes/month
   at this check frequency. Your secrets (step 3) stay hidden either way.

2. **Pick how you want alerts** — either or both:
   - **Phone push (easiest):** install the free **ntfy** app (iOS/Android),
     subscribe to a hard-to-guess topic like `presidio-brooks-x7k2q`. Anyone
     who knows the topic name can read it, so make it random.
   - **Email:** use a Gmail account with 2-step verification and create an
     **App Password** (Google Account → Security → App passwords).
   - **Text message:** use the email option with `NOTIFY_EMAIL` set to your
     carrier's email-to-SMS address (e.g. `5551234567@vtext.com` for Verizon,
     `@txt.att.net` for AT&T, `@tmomail.net` for T-Mobile).

3. **Add secrets** in the repo: Settings → Secrets and variables → Actions →
   New repository secret:

   | Secret | Value |
   |---|---|
   | `NTFY_TOPIC` | your ntfy topic name |
   | `SMTP_USER` | the Gmail address sending alerts |
   | `SMTP_PASS` | the 16-character App Password |
   | `NOTIFY_EMAIL` | where alerts go (email or SMS gateway) |

4. **Edit `config.yaml`** with the days/times you want.

5. **Test it:** Actions tab → "Check Presidio tee times" → Run workflow. Open
   the run log; you should see `Checking: …` and a count of matching slots.

## Testing locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
python checker.py --discover   # shows raw API data + parsed times, saves discover.png
python checker.py --dry-run    # full check, no alerts sent
```

## If it stops working

The most likely cause is Club Prophet changing their page. Run
`python checker.py --discover` and look at `discover.png` and the printed
API body — usually only the field names in `TIME_KEYS` / `SPOT_KEYS` near the
top of `checker.py` need a tweak.

## Notes

- GitHub's scheduler can run a few minutes late at busy times. Fine for
  cancellations; not fast enough to win a slot the second the booking window
  opens.
- Polling every 10 minutes is light, personal-scale use. Don't crank it much
  faster — it's a small course's booking system.
- GitHub pauses scheduled workflows in repos with no commits for 60 days; a
  tiny commit (e.g. tweaking `config.yaml`) restarts it.

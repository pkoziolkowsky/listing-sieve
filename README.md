# NYC Listing Sieve — Setup Guide

Watches StreetEasy + Zillow saved-search alert emails in a dedicated Gmail
inbox from GitHub's free servers (7:00 AM–10:30 PM NY time, checking every
20 seconds), filters for Peter's signals, and pushes matches to his phone
via Pushover (ntfy backup).

**The search:** Upper West Side + All Downtown, 1–3+ bedrooms, up to $3,600.

**The signals** (any one fires an alert; the alert names which hit):

| Signal | What it means |
|---|---|
| BY OWNER | "Listing by" line contains "owner" — no broker in the way |
| ODD RENT | Rent isn't a multiple of $5 (e.g. $3,631) — a weak hint that a rent is a regulated legal rent, not proof |
| STABILIZED MENTION | The listing card text mentions stabilization |
| STABILIZED BLDG | The **building** is presumptively rent-stabilized per NYC public data: 6+ units, built before 1974, not a condo/co-op. **The strong signal.** |

Every alert also carries a **building verdict** — LIKELY / POSSIBLE /
UNLIKELY stabilized, with year built and unit count — looked up live from
NYC GeoSearch + PLUTO (free, keyless city data). So a round-numbered rent
in a 1920s 40-unit walk-up still gets flagged as a strong candidate, while
a by-owner listing in a shiny new condo gets an UNLIKELY label and is
quietly skipped. Each building is looked up once and cached.

Nothing here scrapes StreetEasy or Zillow — the robot only reads alert
emails that the sites send to Peter's own dedicated mailbox, then enriches
them with public building data.

---

## Part 1 — Dedicated Gmail (10 min, do this first)

Use a NEW Gmail so the robot never touches your real email.

1. Go to gmail.com → Create account (e.g. `pk.nyc.listings@gmail.com`).
2. Turn on 2-Step Verification (required for the next step):
   myaccount.google.com → Security → 2-Step Verification → follow prompts.
3. Create an **App Password** (a special password just for the robot):
   myaccount.google.com → Security → search "App passwords" → create one
   named `sieve` → Google shows a 16-character password → **copy it now**
   (it's shown only once). This is what goes in the GitHub secret — never
   your real Gmail password.

## Part 2 — Saved searches that email this inbox

**StreetEasy** (signed up with the new Gmail):
1. streeteasy.com → Rentals → set filters: Areas = **Upper West Side** and
   **All Downtown**; Price max **$3,600**; Bedrooms **1, 2, 3+** (no studio).
2. Click **Save search** and turn on email alerts, frequency
   **Immediately** (not daily digest).

**Zillow** (same new Gmail):
1. zillow.com → For Rent → draw/select Manhattan (or set the same two
   areas roughly — a bit wider is fine, the sieve re-filters).
2. Filters: price max $3,600, beds 1+. No keyword filter — you want the
   full feed; the sieve scores every listing itself.
3. Save search → set alert frequency to the fastest option offered.

Keep this Gmail subscribed to ONLY StreetEasy + Zillow rental alerts, so
the parser isn't fed unrelated mail.

## Part 3 — GitHub repo (5 min)

1. github.com/new → name: `listing-sieve` → **Public** → Create.
2. "uploading an existing file" → drag in `sieve.py`, `README.md`, and the
   `.github` folder → Commit. (If `.github` won't drag: Add file → Create
   new file → name it `.github/workflows/sieve.yml` → paste that file's
   contents → Commit.)
3. Settings → Secrets and variables → Actions → add **five** secrets:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | biancaassistant23@gmail.com |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `PUSHOVER_USER` | same User Key as the StuyTown watcher |
| `PUSHOVER_TOKEN` | same API Token as the StuyTown watcher |
| `NTFY_TOPIC` | `stuytown-peter-37cd4475` (optional backup channel) |

## Part 4 — Turn on and test

1. Actions tab → enable workflows if asked → **Listing Sieve** →
   Run workflow → check **test** box → Run. Phone should chime in ~1 min.
2. Run once more WITHOUT the test box: it sweeps the inbox once and marks
   everything currently there as seen (so you only get alerted for new
   listings from now on).
3. Done. Sessions run themselves 7:00 AM–10:30 PM daily.

---

## What to expect

- Alerts arrive a few minutes after a listing goes live (the sites'
  own email dispatch is the only real delay; the robot reacts in ~20s).
- Each alert: price, beds, address, neighborhood, "Listed by", which
  signals hit, and a tap-through link to the listing.
- Volume depends on the market; if it's too chatty or too quiet, ask
  Claude to tighten/loosen the signal rules (they're at the top of
  sieve.py, plain to read).

## If it seems quiet

- Actions tab → recent runs → open the log: it prints every email it
  reads and how many listing cards it found. `0 card(s)` on real alert
  emails means the sites changed their email design — tell Claude, it's
  a 5-minute parser fix.
- GitHub emails you automatically if a run fails outright.

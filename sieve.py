"""
NYC Listing Sieve
-----------------
Reads StreetEasy and Zillow saved-search alert emails from a dedicated Gmail
inbox (IMAP, read-only intent), parses the listing cards, applies Peter's
signal filters, and pushes matches to his phone via Pushover (+ ntfy backup).

Signals (any one fires an alert; each alert lists which signals hit):
  OWNER          -- "Listing by" line contains "owner"
  ODD-RENT       -- monthly rent is NOT a multiple of $5 (e.g. $3,631):
                    legal rent-stabilized rents drift to odd dollar amounts
                    after RGB percentage increases -- a weak hint, not proof
  STABILIZED-TXT -- the listing card text mentions stabilization
  STABILIZED-BLDG-- the BUILDING looks presumptively rent-stabilized from
                    NYC public data: 6+ residential units AND built before
                    1974, and not a condo/co-op. This is the strong signal.

Every alert also carries a building verdict (LIKELY / POSSIBLE / UNLIKELY
stabilized, with year built + unit count) from NYC GeoSearch + PLUTO, so a
round-numbered rent in an old 40-unit walk-up still gets flagged, and a
by-owner listing in a brand-new condo gets the skeptical label it deserves.

Hard filters: price <= MAX_PRICE, no studios (beds >= 1 when stated).

Runs as a long session (up to ~5h20m) polling the inbox every 20 seconds
between 7:00 AM and 10:30 PM New York time. The GitHub Actions schedule
starts a run every hour; a concurrency group queues them so exactly one is
active and a crashed session is replaced within the hour.

Email-card parsing regexes adapted from the MIT-licensed rental-inbox
project (github.com/osaidd/rental-inbox) -- credit to its author.

No third-party dependencies: Python standard library only.
"""

import email
import email.policy
import hashlib
import html as htmlmod
import imaplib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

# ------------------------- Peter's configuration -------------------------

MAX_PRICE = 3600          # ignore anything above this rent
MIN_BEDS = 1              # ignore studios (listings with unknown beds pass)
WATCH_START = dtime(7, 0)     # NY time
WATCH_END = dtime(22, 30)     # NY time
POLL_SECONDS = 20
SESSION_MAX_SECONDS = 5 * 3600 + 20 * 60   # stay under GitHub's 6h job cap

STATE_FILE = "seen_listings.json"
BUILDING_CACHE_FILE = "building_cache.json"
HEARTBEAT_FILE = "heartbeat.txt"

NY = ZoneInfo("America/New_York")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "").strip()
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

LISTINGS_URL = "https://streeteasy.com"  # generic click-through fallback

# ------------------------- notifications -------------------------


def now_ny() -> datetime:
    return datetime.now(NY)


def _send_pushover(title: str, message: str, url: str) -> None:
    fields = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message[:1024],
        "priority": "1",
        "sound": "magic",
    }
    if url:
        fields["url"] = url
        fields["url_title"] = "Open listing"
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def _send_ntfy(title: str, message: str, url: str) -> None:
    headers = {"Title": title, "Priority": "high", "Tags": "house_with_garden"}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode(),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def notify(title: str, message: str, url: str = "") -> None:
    sent = []
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        try:
            _send_pushover(title, message, url)
            sent.append("pushover")
        except Exception as e:
            print(f"Pushover send failed: {e}")
    if NTFY_TOPIC:
        try:
            _send_ntfy(title, message, url)
            sent.append("ntfy")
        except Exception as e:
            print(f"ntfy send failed: {e}")
    print(f"[{now_ny():%H:%M:%S}] notify via {sent or 'NOTHING -- no channel configured'}: {title}")


# ------------------------- email-card parsers -------------------------
# Adapted from rental-inbox (MIT). Each returns a list of dicts:
# {address, neighborhood, price, beds, baths, broker, url, text}

SE_ADDR_RE = re.compile(r'class="[^"]*ListingCard-info--address"[^>]*>(.*?)</td>', re.S)
SE_AREA_RE = re.compile(r'class="[^"]*ListingCard-info--area"[^>]*>(.*?)</td>', re.S)
SE_LINK_RE = re.compile(r'https://links\.streeteasy\.com/[ua]/click[^"\'\s<>]+')
SE_BROKER_RE = re.compile(r'ListingCard-listingBy[^>]*>(.*?)</', re.S)
PRICE_RE = re.compile(r'\$[\d,]+')
BEDS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*Beds?|\bStudio\b', re.I)
BATHS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*Baths?', re.I)

Z_CARD_SPLIT = re.compile(r'class="mw504 w100p dmBorderGray500')
Z_ZPID_RE = re.compile(r'(\d{6,})_zpid')
Z_PRICE_RE = re.compile(r'\$([\d,]+)\s*/\s*mo')
Z_BEDS_RE = re.compile(r'<b>([\d.]+)</b>&nbsp;<abbr title="bedrooms"')
Z_BATHS_RE = re.compile(r'<b>([\d.]+)</b>&nbsp;<abbr title="bathrooms"')
Z_ADDR_RE = re.compile(r'>\s*([^<>]{4,90}?,\s*New York,\s*NY[^<]{0,10})\s*<')
Z_BROKER_RE = re.compile(r'Listing by:\s*([^<]+)')
Z_STUDIO_RE = re.compile(r'Studio', re.I)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_streeteasy(html: str) -> list:
    addrs = list(SE_ADDR_RE.finditer(html))
    areas = list(SE_AREA_RE.finditer(html))
    out = []
    for i, am in enumerate(addrs):
        start = am.start()
        end = addrs[i + 1].start() if i + 1 < len(addrs) else len(html)
        block = html[start:end]
        text = _text(block)
        address = _text(am.group(1))
        if not address:
            continue
        pm = PRICE_RE.search(text)
        if not pm:
            continue
        prev_areas = [a for a in areas if a.start() < start]
        area = _text(prev_areas[-1].group(1)) if prev_areas else ""
        neighborhood = re.sub(r"^Rental Unit in\s*", "", area, flags=re.I).strip()
        bm = BEDS_RE.search(text)
        beds = None
        if bm:
            beds = 0.0 if "studio" in bm.group(0).lower() else float(bm.group(1))
        bam = BATHS_RE.search(text)
        lm = SE_LINK_RE.search(block)
        km = SE_BROKER_RE.search(block)
        out.append({
            "source": "streeteasy",
            "address": address,
            "neighborhood": neighborhood,
            "price": int(pm.group(0).replace("$", "").replace(",", "")),
            "beds": beds,
            "baths": float(bam.group(1)) if bam else None,
            "broker": _text(km.group(1)).split(" (")[0] if km else "",
            "url": htmlmod.unescape(lm.group(0)) if lm else "",
            "text": text,
        })
    return out


def parse_zillow(html: str) -> list:
    out, seen = [], set()
    for block in Z_CARD_SPLIT.split(html)[1:]:
        pm = Z_PRICE_RE.search(block)
        am = Z_ADDR_RE.search(block)
        if not pm or not am:
            continue
        zm = Z_ZPID_RE.search(block)
        zpid = zm.group(1) if zm else ""
        if zpid and zpid in seen:
            continue
        seen.add(zpid)
        bm = Z_BEDS_RE.search(block)
        beds = float(bm.group(1)) if bm else (0.0 if Z_STUDIO_RE.search(block) else None)
        bam = Z_BATHS_RE.search(block)
        km = Z_BROKER_RE.search(block)
        out.append({
            "source": "zillow",
            "address": htmlmod.unescape(am.group(1)).split(", New York")[0].strip(),
            "neighborhood": "",
            "price": int(pm.group(1).replace(",", "")),
            "beds": beds,
            "baths": float(bam.group(1)) if bam else None,
            "broker": htmlmod.unescape(km.group(1)).strip() if km else "",
            "url": f"https://www.zillow.com/homedetails/{zpid}_zpid/" if zpid else "",
            "text": _text(block),
        })
    return out


# ------------------- building enrichment (NYC public data) -------------------
# Geocode + PLUTO helpers adapted from MIT-licensed rental-inbox. All free,
# keyless. Runs on GitHub's servers, which have open internet. Every lookup is
# cached to building_cache.json (committed back to the repo) so each building
# is fetched once, ever.

GEOSEARCH = "https://geosearch.planninglabs.nyc/v2/search"
PLUTO = "https://data.cityofnewyork.us/resource/64uk-42ks.json"
UA = "listing-sieve/1.0 (personal apartment search)"

_building_cache = {}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def _geocode_bbl(address: str):
    clean = re.sub(r"\s*#\S+", "", address).strip()
    if not clean:
        return ""
    url = f"{GEOSEARCH}?{urllib.parse.urlencode({'text': clean, 'size': 1})}"
    try:
        data = json.loads(_get(url))
        feats = data.get("features") or []
        if not feats:
            return ""
        pad = ((feats[0].get("properties") or {}).get("addendum") or {}).get("pad") or {}
        return str(pad.get("bbl") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _pluto(bbl: str):
    if not bbl:
        return None
    q = urllib.parse.urlencode({"bbl": bbl,
                                "$select": "bldgclass,numfloors,unitsres,yearbuilt"})
    try:
        rows = json.loads(_get(f"{PLUTO}?{q}"))
    except (OSError, ValueError):
        return None
    if not rows or not isinstance(rows, list):
        return None
    r = rows[0]

    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    return {"bldg_class": str(r.get("bldgclass") or ""),
            "floors": _int(r.get("numfloors")),
            "units": _int(r.get("unitsres")),
            "year_built": _int(r.get("yearbuilt"))}


def stabilization_verdict(address: str) -> dict:
    """Return {'verdict','why','year','units','bldg_class'}.

    verdict is one of LIKELY / POSSIBLE / UNLIKELY / UNKNOWN.

    Rule of thumb: a building with 6+ residential units built before 1974 is
    presumptively rent-stabilized, unless it's a condo/co-op (DOF class R*).
    Newer big buildings can still be stabilized via 421-a/J-51/ICAP tax
    benefits, which PLUTO alone doesn't show -> POSSIBLE, worth a look.
    """
    key = re.sub(r"\s*#\S+", "", address).strip().lower()
    if key in _building_cache:
        return _building_cache[key]

    out = {"verdict": "UNKNOWN", "why": "building not found", "year": None,
           "units": None, "bldg_class": ""}
    bbl = _geocode_bbl(address)
    facts = _pluto(bbl) if bbl else None
    if facts:
        yr, units, cls = facts["year_built"], facts["units"], facts["bldg_class"]
        out.update({"year": yr, "units": units, "bldg_class": cls})
        is_condo = bool(cls) and cls[0].upper() == "R"
        is_house = bool(cls) and cls[0].upper() in ("A", "B")
        if is_condo:
            out.update(verdict="UNLIKELY", why=f"condo/co-op (class {cls})")
        elif is_house:
            out.update(verdict="UNLIKELY", why=f"1-2 family house (class {cls})")
        elif units is not None and units < 6:
            out.update(verdict="UNLIKELY", why=f"only {units} units (<6)")
        elif units is not None and units >= 6 and yr and yr < 1974:
            out.update(verdict="LIKELY",
                       why=f"{units} units, built {yr} (pre-1974, 6+ units)")
        elif units is not None and units >= 6 and yr and yr >= 1974:
            out.update(verdict="POSSIBLE",
                       why=f"{units} units, built {yr} -- check for 421a/J-51 benefit")
        elif units is not None and units >= 6:
            out.update(verdict="POSSIBLE", why=f"{units} units, year unknown")

    _building_cache[key] = out
    return out


# ------------------------- signals & filtering -------------------------


def evaluate(listing: dict, verdict: dict) -> list:
    """Return the list of signal names this listing trips."""
    signals = []
    if re.search(r"\bowner\b", listing.get("broker", ""), re.I):
        signals.append("BY OWNER")
    if listing["price"] % 5 != 0:
        signals.append(f"ODD RENT (${listing['price']:,})")
    if re.search(r"stabiliz", listing.get("text", ""), re.I):
        signals.append("STABILIZED MENTION")
    if verdict.get("verdict") == "LIKELY":
        signals.append("STABILIZED BLDG")
    return signals


def passes_hard_filters(listing: dict) -> bool:
    if listing["price"] > MAX_PRICE:
        return False
    beds = listing.get("beds")
    if beds is not None and beds < MIN_BEDS:
        return False  # studios out
    return True


def listing_key(listing: dict) -> str:
    raw = f"{listing['address'].lower()}|{listing['price']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ------------------------- state -------------------------


def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    # keep the file from growing forever: cap at the most recent ~3000
    if len(seen) > 3000:
        items = sorted(seen.items(), key=lambda kv: kv[1])[-3000:]
        seen = dict(items)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=0)
    with open(BUILDING_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(_building_cache, f, indent=0)
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        f.write(f"last session ended {now_ny():%Y-%m-%d %H:%M} NY\n")


def load_building_cache() -> None:
    global _building_cache
    if os.path.exists(BUILDING_CACHE_FILE):
        try:
            with open(BUILDING_CACHE_FILE, "r", encoding="utf-8") as f:
                _building_cache = json.load(f)
        except Exception:
            _building_cache = {}


# ------------------------- inbox handling -------------------------


def connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    conn.select("INBOX")
    return conn


def html_body(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    parts.append(part.get_content())
                except Exception:
                    pass
    elif msg.get_content_type() == "text/html":
        parts.append(msg.get_content())
    return "\n".join(parts)


def process_new_mail(conn, seen: dict) -> int:
    """Fetch unseen alert emails, parse, filter, notify. Returns #alerts."""
    typ, data = conn.search(None, "(UNSEEN)")
    if typ != "OK":
        return 0
    alerts = 0
    for num in data[0].split():
        typ, msgdata = conn.fetch(num, "(RFC822)")  # fetch marks it \Seen
        if typ != "OK" or not msgdata or msgdata[0] is None:
            continue
        msg = email.message_from_bytes(msgdata[0][1], policy=email.policy.default)
        sender = (msg.get("From") or "").lower()
        subject = msg.get("Subject") or ""
        if "streeteasy" in sender:
            listings = parse_streeteasy(html_body(msg))
        elif "zillow" in sender:
            listings = parse_zillow(html_body(msg))
        else:
            continue  # welcome mail, promos, anything else
        print(f"[{now_ny():%H:%M:%S}] email from {sender[:40]!r} "
              f"({subject[:50]!r}): {len(listings)} card(s)")
        for lst in listings:
            if not passes_hard_filters(lst):
                continue
            key = listing_key(lst)
            if key in seen:
                continue
            seen[key] = now_ny().strftime("%Y-%m-%d %H:%M")
            verdict = stabilization_verdict(lst["address"])
            signals = evaluate(lst, verdict)
            if not signals:
                continue
            beds = lst.get("beds")
            bedtxt = "studio" if beds == 0 else (f"{beds:g}BR" if beds is not None else "?BR")
            hood = lst.get("neighborhood") or lst["source"]
            title = f"\U0001F3E0 {' + '.join(s.split(' (')[0] for s in signals)}"
            stab = f"{verdict['verdict']} stabilized"
            if verdict.get("why"):
                stab += f" ({verdict['why']})"
            body = (
                f"${lst['price']:,}/mo -- {bedtxt} -- {lst['address']} ({hood})\n"
                f"Listed by: {lst.get('broker') or 'n/a'}\n"
                f"Signals: {', '.join(signals)}\n"
                f"Building: {stab}\n"
                f"{lst.get('url') or ''}"
            )
            notify(title, body, lst.get("url", ""))
            alerts += 1
    return alerts


# ------------------------- main modes -------------------------


def watch_session() -> None:
    seen = load_seen()
    load_building_cache()
    started = time.monotonic()
    alerts = 0
    conn = None
    polls = 0
    while True:
        t = now_ny().time()
        if t >= WATCH_END or t < dtime(5, 0):
            print("Outside watch window -- ending session.")
            break
        if time.monotonic() - started > SESSION_MAX_SECONDS:
            print("Session time cap reached -- ending (next queued run takes over).")
            break
        try:
            if conn is None:
                conn = connect()
                print(f"[{now_ny():%H:%M:%S}] connected to inbox.")
            alerts += process_new_mail(conn, seen)
        except Exception as e:
            print(f"[{now_ny():%H:%M:%S}] inbox error, reconnecting: {e}")
            try:
                if conn is not None:
                    conn.logout()
            except Exception:
                pass
            conn = None
            time.sleep(30)
            continue
        polls += 1
        if polls % 90 == 0:  # roughly every 30 min
            print(f"[{now_ny():%H:%M:%S}] alive -- {polls} polls, {alerts} alert(s) so far.")
        time.sleep(POLL_SECONDS)
    try:
        if conn is not None:
            conn.logout()
    except Exception:
        pass
    save_seen(seen)
    print(f"Session done: {alerts} alert(s).")


def single_sweep() -> None:
    seen = load_seen()
    load_building_cache()
    conn = connect()
    alerts = process_new_mail(conn, seen)
    conn.logout()
    save_seen(seen)
    print(f"Single sweep done: {alerts} alert(s).")


def main() -> None:
    if os.environ.get("TEST_NOTIFY", "").lower() == "true":
        notify(
            "Test: Listing Sieve is working",
            "This is a test alert. Real ones show price, address, and which "
            "signals hit (BY OWNER / ODD RENT / STABILIZED).",
            "https://streeteasy.com",
        )
        return
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("ERROR: GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets not set.", file=sys.stderr)
        sys.exit(1)
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    if manual:
        single_sweep()
    else:
        watch_session()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)

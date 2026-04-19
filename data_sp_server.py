"""
DATA-SP Local Server
====================
Run this on your laptop before opening the HTML app.
It fetches BLS data and serves it to the browser.

SETUP (one-time):
  pip install requests flask flask-cors

RUN:
  python data_sp_server.py

Then open data_sp_engine_live.html in your browser.
Keep this terminal window open while using the app.

STOP:
  Press Ctrl+C in this terminal window.

ENDPOINTS:
  /fetch/cpi    -- CPI headline + core YoY surprise scoring
  /fetch/nfp    -- Non-farm payrolls surprise scoring
  /fetch/fomc   -- FOMC statement language scoring (hawkish/dovish)
  /health       -- Server status check
"""

from flask import Flask, jsonify
from flask_cors import CORS
import requests
import hashlib
import json
import os
import math
import re
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
CORS(app)

BLS_API_KEY   = "f2186f8eed434c3aad7624ee7e9b33fc"
REVISION_FILE = "prior_values.json"

BLS_SERIES = {
    "CPI_HEADLINE": "CUSR0000SA0",
    "CPI_CORE":     "CUSR0000SA0L1E",
    "NFP_TOTAL":    "CES0000000001",
}

# ─────────────────────────────────────────────
#  FOMC SCORING DICTIONARIES
#  positive weight = hawkish (rate hike pressure)
#  negative weight = dovish  (rate cut pressure)
# ─────────────────────────────────────────────

FOMC_HAWKISH = {
    "inflation remains elevated": 3.0,
    "inflation has remained elevated": 3.0,
    "inflation is elevated": 3.0,
    "elevated inflation": 2.5,
    "inflation well above": 2.5,
    "inflation above 2 percent": 2.0,
    "inflation above its 2 percent": 2.0,
    "upside risks to inflation": 2.5,
    "price stability": 1.0,
    "return inflation to 2": 2.0,
    "restore price stability": 2.0,
    "raise the target range": 3.0,
    "increase the target range": 3.0,
    "further increases": 2.5,
    "ongoing increases": 2.5,
    "additional policy firming": 2.5,
    "sufficiently restrictive": 2.5,
    "remain restrictive": 2.0,
    "higher for longer": 2.0,
    "not appropriate to reduce": 2.0,
    "not yet appropriate": 2.0,
    "strong labor market": 1.5,
    "labor market remains tight": 2.0,
    "tight labor market": 1.5,
}

FOMC_DOVISH = {
    "reduce the target range": -3.0,
    "decrease the target range": -3.0,
    "lower the target range": -3.0,
    "cut rates": -3.0,
    "rate cut": -2.5,
    "policy easing": -2.5,
    "begin to reduce": -2.5,
    "appropriate to reduce": -2.5,
    "less restrictive": -2.0,
    "move toward neutral": -2.0,
    "inflation has eased": -2.5,
    "inflation has declined": -2.5,
    "inflation coming down": -2.0,
    "inflation is coming down": -2.0,
    "progress on inflation": -2.0,
    "further progress": -1.5,
    "closer to 2 percent": -1.5,
    "disinflation": -2.0,
    "downside risks": -2.0,
    "risks to employment": -1.5,
    "labor market has cooled": -2.0,
    "labor market is cooling": -1.5,
    "unemployment has risen": -1.5,
    "uncertainty": -1.0,
}

FOMC_GUIDANCE_PHRASES = [
    # explicit rate action language
    "not appropriate to reduce",
    "appropriate to reduce",
    "begin to reduce",
    "further increases",
    "ongoing increases",
    "higher for longer",
    "reduce the target range",
    "raise the target range",
    "rate cut",
    "no cuts",
    # common Fed forward guidance phrases
    "sufficiently restrictive",
    "remain restrictive",
    "restrictive for some time",
    "not yet appropriate",
    "prepared to adjust",
    "data dependent",
    "meeting by meeting",
    "gradual",
    "patient",
    "proceed carefully",
    "moving toward",
    "well positioned",
    "any additional",
    "extent of any",
    "how long",
]


# ─────────────────────────────────────────────
#  SHARED UTILITIES
# ─────────────────────────────────────────────

def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))

def load_prior():
    if os.path.exists(REVISION_FILE):
        with open(REVISION_FILE) as f:
            return json.load(f)
    return {}

def save_prior(data):
    with open(REVISION_FILE, "w") as f:
        json.dump(data, f, indent=2)

def revision_level(delta):
    if delta < 0.001: return 0
    if delta < 0.05:  return 1
    if delta <= 0.10: return 2
    return 3

def now_timestamps():
    now_utc = datetime.now(timezone.utc)
    return {
        "posted_at_est": (now_utc + timedelta(hours=-4)).strftime("%Y-%m-%dT%H:%M:%S"),
        "posted_at_sgt": (now_utc + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ─────────────────────────────────────────────
#  BLS HELPERS
# ─────────────────────────────────────────────

def fetch_bls(series_ids):
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    now = datetime.now(timezone.utc)
    start_year = str(now.year - 2)
    end_year   = str(now.year)
    payload = {
        "seriesid":        series_ids,
        "startyear":       start_year,
        "endyear":         end_year,
        "registrationkey": BLS_API_KEY,
    }
    r = requests.post(url, json=payload,
                      headers={"Content-type": "application/json"},
                      timeout=30)
    r.raise_for_status()
    return r.json()

def yoy(series, i, j):
    a = float(series[i]["value"])
    b = float(series[j]["value"])
    return round(((a - b) / b) * 100, 2)


# ─────────────────────────────────────────────
#  FOMC HELPERS
# ─────────────────────────────────────────────

def score_fomc_text(text):
    """Score FOMC statement text. Returns raw score, normalised, direction, top phrases."""
    text_lower = text.lower()
    raw_score  = 0.0
    matched    = []

    for phrase, weight in {**FOMC_HAWKISH, **FOMC_DOVISH}.items():
        count = text_lower.count(phrase)
        if count > 0:
            raw_score += weight * count
            matched.append({"phrase": phrase, "weight": weight, "count": count})

    # tanh normalises to -1..+1, saturating smoothly
    normalised = round(math.tanh(raw_score / 10.0), 3)

    if normalised > 0.15:
        direction = "HAWKISH"
    elif normalised < -0.15:
        direction = "DOVISH"
    else:
        direction = "NEUTRAL"

    return {
        "raw_score":  round(raw_score, 2),
        "normalised": normalised,
        "direction":  direction,
        "matched":    sorted(matched, key=lambda x: abs(x["weight"]), reverse=True)[:10],
    }


def compute_tone_delta(current, prior):
    """How much did tone shift? Returns 0..1. Large shift = more signal."""
    delta = abs(current - prior)
    return round(min(sigmoid(delta * 6 - 1.5), 1.0), 3)


def _strip_html(raw_html):
    """Strip HTML tags and decode entities. Extracts content body first."""
    import html as html_module

    # try to extract just the main content area — Fed pages have
    # a lot of nav/header boilerplate before the actual statement
    content_match = re.search(
        r'<div[^>]*class="[^"]*col-xs-12[^"]*"[^>]*>(.*?)</div\s*>\s*</div',
        raw_html, flags=re.DOTALL
    )
    if content_match and len(content_match.group(1)) > 500:
        raw_html = content_match.group(1)

    clean = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', raw_html, flags=re.DOTALL)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = html_module.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def _fetch_statement_url(stmt_url, headers):
    """Fetch and extract text from a Fed statement URL."""
    sr = requests.get(stmt_url, headers=headers, timeout=20)
    sr.raise_for_status()
    return _strip_html(sr.text)


def _get_latest_date_from_rss(headers):
    """
    PRIMARY METHOD — Fed RSS feed.
    Priority order:
    1. FOMC statement (same-day release after meeting)
    2. FOMC minutes (released ~3 weeks later)
    Excludes: discount rate meetings, board minutes, speeches.
    Returns latest statement date string YYYYMMDD or None.
    """
    rss_url = "https://www.federalreserve.gov/feeds/press_monetary.xml"
    try:
        r = requests.get(rss_url, headers=headers, timeout=15)
        r.raise_for_status()

        # Parse items with title + link
        items = re.findall(
            r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?</item>',
            r.text, flags=re.DOTALL
        )

        statements = []  # same-day FOMC statements (highest priority)
        minutes    = []  # FOMC minutes releases (lower priority)

        for title, link in items:
            title_clean = title.lower().strip()
            title_clean = re.sub(r'<[^>]+>', '', title_clean)  # strip any HTML

            # Hard excludes
            if 'discount rate' in title_clean:
                continue
            if 'minutes of the board' in title_clean:
                continue
            if 'speech' in title_clean or 'testimony' in title_clean:
                continue

            date_match = re.search(r'monetary(\d{8})a', link)
            if not date_match:
                continue
            date_str = date_match.group(1)

            # Same-day FOMC statement — title says "issues FOMC statement"
            if ('issues fomc statement' in title_clean or
                    'fomc statement' in title_clean and 'minutes' not in title_clean):
                statements.append(date_str)

            # FOMC minutes — title says "Minutes of the Federal Open Market"
            elif ('minutes of the federal open market' in title_clean or
                  'minutes of the fomc' in title_clean):
                minutes.append(date_str)

        # Prefer statements over minutes
        if statements:
            statements.sort(reverse=True)
            return statements[0]
        if minutes:
            minutes.sort(reverse=True)
            return minutes[0]

    except Exception as e:
        print(f"  RSS fetch failed: {e}")
    return None


def _get_latest_date_from_calendar(headers):
    """
    FALLBACK METHOD — Fed calendar HTML page.
    Used if RSS fails. More fragile but reliable historically.
    Returns latest statement date string YYYYMMDD or None.
    """
    calendar_url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    try:
        r = requests.get(calendar_url, headers=headers, timeout=20)
        r.raise_for_status()
        pattern = r'/newsevents/pressreleases/monetary(\d{8})a\.htm'
        matches = re.findall(pattern, r.text)
        if matches:
            matches.sort(reverse=True)
            return matches[0]
    except Exception as e:
        print(f"  Calendar fetch failed: {e}")
    return None


def fetch_fomc_statement():
    """
    Fetch the latest FOMC statement from federalreserve.gov.
    Uses RSS feed as primary source · falls back to calendar HTML if RSS fails.
    Returns dict with text, statement_url, statement_date, fetch_method.
    """
    headers = {"User-Agent": "Mozilla/5.0 (DATA-SP macro signal engine; research)"}

    # Step 1 — try RSS first (most stable)
    latest_date  = _get_latest_date_from_rss(headers)
    fetch_method = "rss"

    # Step 2 — fall back to calendar HTML if RSS failed
    if not latest_date:
        print("  RSS failed · trying calendar HTML fallback...")
        latest_date  = _get_latest_date_from_calendar(headers)
        fetch_method = "calendar_html"

    if not latest_date:
        raise ValueError(
            "Could not find FOMC statement URL via RSS or calendar page. "
            "Check federalreserve.gov manually."
        )

    stmt_url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{latest_date}a.htm"
    text     = _fetch_statement_url(stmt_url, headers)

    if len(text) < 200:
        raise ValueError(
            f"Extracted text too short ({len(text)} chars) — "
            f"statement page may have changed structure. URL: {stmt_url}"
        )

    return {
        "text":           text,
        "statement_url":  stmt_url,
        "statement_date": latest_date,
        "fetch_method":   fetch_method,
    }


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route("/fetch/cpi")
def fetch_cpi():
    try:
        data = fetch_bls([BLS_SERIES["CPI_HEADLINE"], BLS_SERIES["CPI_CORE"]])

        if data.get("status") != "REQUEST_SUCCEEDED":
            return jsonify({"error": "BLS API error: " + str(data.get("message", "unknown"))}), 500

        h_raw = next(s for s in data["Results"]["series"] if s["seriesID"] == BLS_SERIES["CPI_HEADLINE"])["data"]
        c_raw = next(s for s in data["Results"]["series"] if s["seriesID"] == BLS_SERIES["CPI_CORE"])["data"]

        def sort_series(s):
            return sorted(s, key=lambda x: (x["year"], x["period"]), reverse=True)

        h = sort_series(h_raw)
        c = sort_series(c_raw)

        if len(h) < 14:
            return jsonify({"error": f"BLS returned only {len(h)} months. Need 14 for YoY."}), 500

        actual_yoy  = yoy(h, 0, 12)
        prior_yoy   = yoy(h, 1, 13)
        core_actual = yoy(c, 0, 12)

        prior_store = load_prior()
        stored      = prior_store.get("cpi_prior_yoy")
        rev_delta   = round(abs(prior_yoy - stored), 3) if stored is not None else 0.0
        rev_level   = revision_level(rev_delta) if stored is not None else 0

        prior_store["cpi_prior_yoy"] = prior_yoy
        save_prior(prior_store)

        bls_url = f"https://api.bls.gov/publicAPI/v2/timeseries/data/{BLS_SERIES['CPI_HEADLINE']}"
        sha256  = hashlib.sha256(bls_url.encode()).hexdigest()
        ts      = now_timestamps()

        return jsonify({
            "ok":            True,
            "actual_yoy":    actual_yoy,
            "core_actual":   core_actual,
            "prior_yoy":     prior_yoy,
            "rev_level":     rev_level,
            "rev_delta":     rev_delta,
            "sha256":        sha256,
            "source":        "api.bls.gov",
            "series_h":      BLS_SERIES["CPI_HEADLINE"],
            "series_c":      BLS_SERIES["CPI_CORE"],
            "posted_at_est": ts["posted_at_est"],
            "posted_at_sgt": ts["posted_at_sgt"],
        })

    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Cannot reach api.bls.gov — check your internet connection"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fetch/nfp")
def fetch_nfp():
    try:
        data = fetch_bls([BLS_SERIES["NFP_TOTAL"]])
        if data.get("status") != "REQUEST_SUCCEEDED":
            return jsonify({"error": "BLS API error"}), 500

        series      = data["Results"]["series"][0]["data"]
        actual      = int(float(series[0]["value"]))

        prior_store = load_prior()
        stored_prev = prior_store.get("nfp_prev")
        rev_delta   = round(abs(actual - stored_prev) / 1000, 1) if stored_prev is not None else 0.0
        rev_level   = 0 if rev_delta < 10 else (1 if rev_delta < 30 else (2 if rev_delta <= 60 else 3))

        prior_store["nfp_prev"] = actual
        save_prior(prior_store)

        bls_url = f"https://api.bls.gov/publicAPI/v2/timeseries/data/{BLS_SERIES['NFP_TOTAL']}"
        sha256  = hashlib.sha256(bls_url.encode()).hexdigest()
        ts      = now_timestamps()

        return jsonify({
            "ok":            True,
            "actual":        actual,
            "rev_level":     rev_level,
            "rev_delta":     rev_delta,
            "sha256":        sha256,
            "source":        "api.bls.gov",
            "posted_at_est": ts["posted_at_est"],
            "posted_at_sgt": ts["posted_at_sgt"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fetch/fomc")
def fetch_fomc():
    """
    Fetch latest FOMC statement and score for hawkish/dovish tone.

    C-parameters:
      C_language        0.40 weight — absolute tone (0=dovish, 0.5=neutral, 1=hawkish)
      C_tone_delta      0.40 weight — how much tone SHIFTED vs prior statement
      C_forward_guidance 0.20 weight — explicit rate path language present

    C_composite = 0.40*C_language + 0.40*C_tone_delta + 0.20*C_forward_guidance

    High C_composite (>0.80) = strong directional signal for prediction market bots.
    Low C_composite (<0.40)  = repeat of prior statement, low signal value.
    """
    try:
        # fetch and score
        result    = fetch_fomc_statement()
        text      = result["text"]
        stmt_url  = result["statement_url"]
        stmt_date = result["statement_date"]

        scoring   = score_fomc_text(text)
        norm      = scoring["normalised"]   # -1.0 to +1.0

        # C_language: map -1..+1 to 0..1
        C_language = round((norm + 1.0) / 2.0, 3)

        # C_tone_delta: shift vs prior
        prior_store = load_prior()
        prior_norm  = prior_store.get("fomc_prior_normalised")

        if prior_norm is not None:
            C_tone_delta = compute_tone_delta(norm, prior_norm)
        else:
            C_tone_delta = 0.500   # first run, no prior available

        # C_forward_guidance: explicit rate path language
        text_lower       = text.lower()
        guidance_hits    = sum(1 for p in FOMC_GUIDANCE_PHRASES if p in text_lower)
        C_forward_guidance = round(min(guidance_hits / 3.0, 1.0), 3)

        # composite
        C_composite = round(
            0.40 * C_language +
            0.40 * C_tone_delta +
            0.20 * C_forward_guidance,
            3
        )

        # save for next meeting comparison
        prior_store["fomc_prior_normalised"] = norm
        prior_store["fomc_prior_date"]       = stmt_date
        save_prior(prior_store)

        sha256 = hashlib.sha256(stmt_url.encode()).hexdigest()
        ts     = now_timestamps()

        return jsonify({
            "ok":                True,
            "statement_date":    stmt_date,
            "statement_url":     stmt_url,
            "fetch_method":      result.get("fetch_method", "unknown"),
            "direction":         scoring["direction"],
            "raw_score":         scoring["raw_score"],
            "normalised_score":  norm,
            "top_phrases":       scoring["matched"],
            "C_language":        C_language,
            "C_tone_delta":      C_tone_delta,
            "C_forward_guidance": C_forward_guidance,
            "C_composite":       C_composite,
            "prior_normalised":  prior_norm,
            "sha256":            sha256,
            "source":            "federalreserve.gov",
            "posted_at_est":     ts["posted_at_est"],
            "posted_at_sgt":     ts["posted_at_sgt"],
        })

    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Cannot reach federalreserve.gov — check your internet connection"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/fomc-text")
def debug_fomc_text():
    """Debug endpoint — shows extracted FOMC text and phrase detection."""
    try:
        result = fetch_fomc_statement()
        text   = result["text"]
        phrases_to_check = [
            "prepared to adjust", "sufficiently restrictive",
            "data dependent", "meeting by meeting",
            "extent of any", "how long", "gradual", "patient",
            "remain restrictive", "not yet appropriate",
        ]
        found = {p: p in text.lower() for p in phrases_to_check}
        return jsonify({
            "text_length":    len(text),
            "text_preview":   text[:2000],
            "phrases_found":  found,
            "statement_date": result["statement_date"],
            "fetch_method":   result.get("fetch_method", "unknown"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def check_fed_calendar():
    """
    Fetch the Fed FOMC calendar and return upcoming meeting dates.
    Prevents planning against wrong dates.
    Returns list of upcoming meetings with dates and status.
    """
    calendar_url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    headers = {"User-Agent": "Mozilla/5.0 (DATA-SP macro signal engine; research)"}

    try:
        r = requests.get(calendar_url, headers=headers, timeout=15)
        r.raise_for_status()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        months = {
            "January": 1, "February": 2, "March": 3, "April": 4,
            "May": 5, "June": 6, "July": 7, "August": 8,
            "September": 9, "October": 10, "November": 11, "December": 12
        }

        meetings = []

        # Look for year headings to assign correct year to each meeting
        # Fed page has "2026 FOMC Meetings" then lists months
        # Strategy: find all year + month + date combos in the page text
        text = r.text

        # Find year sections — look for 4-digit years near "FOMC"
        year_sections = re.findall(r'(\d{4})\s+FOMC', text)
        current_year  = now.year

        # Process each month in the calendar text
        for month_name, month_num in months.items():
            # Find date ranges for this month e.g. "27-28" or "17-18*"
            pattern = rf'{month_name}\s+(\d{{1,2}})[–\-](\d{{1,2}})\*?'
            found   = re.findall(pattern, text)

            for start_day, end_day in found:
                # Try current year first then next year
                for year in [current_year, current_year + 1]:
                    try:
                        meeting_end = datetime(
                            year, month_num, int(end_day),
                            14, 0, 0, tzinfo=timezone.utc
                        )
                        days_away = (meeting_end - now).days

                        # Minutes released ~3 weeks after meeting
                        minutes_day = min(int(end_day) + 21, 28)
                        # Handle month overflow simply
                        minutes_month = month_num
                        minutes_year  = year
                        if minutes_day > 28:
                            minutes_day   = minutes_day - 28
                            minutes_month = month_num + 1
                            if minutes_month > 12:
                                minutes_month = 1
                                minutes_year  = year + 1

                        minutes_date = datetime(
                            minutes_year, minutes_month, minutes_day,
                            tzinfo=timezone.utc
                        )

                        meetings.append({
                            "meeting":            f"{month_name} {start_day}-{end_day} {year}",
                            "statement_date":     meeting_end.strftime("%Y-%m-%d"),
                            "statement_time_est": "14:00 EST",
                            "minutes_approx":     minutes_date.strftime("%Y-%m-%d"),
                            "days_away":          days_away,
                        })
                        break
                    except ValueError:
                        continue

        # sort and return only future meetings
        meetings.sort(key=lambda x: x["days_away"])
        upcoming = [m for m in meetings if m["days_away"] >= 0][:4]

        return {
            "ok":               True,
            "source":           "federalreserve.gov/monetarypolicy/fomccalendars.htm",
            "checked":          now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "upcoming_meetings": upcoming,
            "reminder":         "Always verify dates here before planning signals",
        }

    except Exception as e:
        return {
            "ok":    False,
            "error": str(e),
            "source": calendar_url,
        }


@app.route("/health")
def health():
    return jsonify({
        "status":      "running",
        "bls_key_set": BLS_API_KEY != "YOUR_BLS_API_KEY",
        "endpoints":   ["/fetch/cpi", "/fetch/nfp", "/fetch/fomc",
                        "/check/fed-calendar", "/debug/fomc-text", "/health"],
    })


@app.route("/debug/fed-calendar-raw")
def debug_fed_calendar_raw():
    """Debug — shows raw text around April/May in Fed calendar page."""
    calendar_url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    headers = {"User-Agent": "Mozilla/5.0 (DATA-SP macro signal engine; research)"}
    try:
        r = requests.get(calendar_url, headers=headers, timeout=15)
        r.raise_for_status()
        text = r.text
        # find April section
        idx = text.find("April")
        snippet = text[max(0,idx-50):idx+300] if idx >= 0 else "April not found"
        # also try regex patterns
        patterns_tested = {
            "dash":     re.findall(r'April\s+(\d{1,2})-(\d{1,2})', text),
            "endash":   re.findall(r'April\s+(\d{1,2})–(\d{1,2})', text),
            "any":      re.findall(r'April.{0,20}(\d{1,2}).{0,5}(\d{1,2})', text),
        }
        return jsonify({
            "april_snippet": snippet,
            "patterns_tested": patterns_tested,
            "page_length": len(text),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/check/fed-calendar")
def check_fed_calendar_endpoint():
    """
    Check the Fed FOMC calendar for upcoming meeting dates.
    Use this to verify event dates before planning signals.
    Primary source: federalreserve.gov
    """
    return jsonify(check_fed_calendar())


if __name__ == "__main__":
    print("\n" + "="*52)
    print("  DATA-SP Local Server")
    print("="*52)
    if BLS_API_KEY == "YOUR_BLS_API_KEY":
        print("  BLS API key not set.")
        print("  Register free at bls.gov/developers")
    else:
        print("  BLS API key configured")
    print("\n  Endpoints:")
    print("    http://localhost:5050/fetch/cpi")
    print("    http://localhost:5050/fetch/nfp")
    print("    http://localhost:5050/fetch/fomc")
    print("    http://localhost:5050/health")
    print("\n  Press Ctrl+C to stop\n")
    app.run(port=5050, debug=False)

"""Formatting, language and context-lookup helpers shared by the composer and reply engine.

Everything here is pure and deterministic: same inputs -> same outputs.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Optional

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Singular nouns used when the category is referenced in prose.
CATEGORY_NOUN = {
    "dentists": "clinic",
    "salons": "salon",
    "restaurants": "restaurant",
    "gyms": "gym",
    "pharmacies": "pharmacy",
}
CUSTOMER_NOUN = {
    "dentists": "patients",
    "salons": "clients",
    "restaurants": "diners",
    "gyms": "members",
    "pharmacies": "customers",
}


# --------------------------------------------------------------------------- dict access

def g(obj: Any, *path: str, default: Any = None) -> Any:
    """Safe nested get: g(m, "identity", "name")."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def is_placeholder(trigger: dict) -> bool:
    return bool(g(trigger, "payload", "placeholder"))


# --------------------------------------------------------------------------- numbers / money

def pct(x: Any) -> str:
    """0.18 -> '18%', -0.5 -> '50%' (sign handled by caller wording)."""
    try:
        return f"{abs(round(float(x) * 100))}%"
    except (TypeError, ValueError):
        return ""


def pct1(x: Any) -> str:
    """0.021 -> '2.1%'."""
    try:
        v = round(float(x) * 100, 1)
    except (TypeError, ValueError):
        return ""
    return f"{v:g}%"


def inr(amount: Any) -> str:
    try:
        n = int(round(float(amount)))
    except (TypeError, ValueError):
        return f"₹{amount}"
    return f"₹{n:,}"


def num(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def price_in(title: str) -> Optional[int]:
    """Extract the first ₹ price from an offer title: 'Haircut @ ₹99' -> 99."""
    m = re.search(r"₹\s?([\d,]+)", title or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def humanize(slug: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning', 'high_risk_adults' -> 'high-risk adults'."""
    s = str(slug or "").strip()
    s = re.sub(r"^(\d+)_", r"\1-", s)
    s = s.replace("high_risk", "high-risk").replace("_", " ")
    return s


# --------------------------------------------------------------------------- dates

def parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.fromisoformat(v[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_date(value: Any) -> str:
    """'2026-12-15' -> '15 Dec 2026'."""
    dt = parse_dt(value)
    if not dt:
        return str(value or "")
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def fmt_day(value: Any) -> str:
    """'2026-05-02T19:00:00+05:30' -> 'Sat 2 May, 7pm' (local time as given)."""
    dt = parse_dt(value)
    if not dt:
        return str(value or "")
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    minute = f":{dt.minute:02d}" if dt.minute else ""
    return f"{WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]}, {hour}{minute}{ampm}"


def fmt_time(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    minute = f":{dt.minute:02d}" if dt.minute else ""
    return f"{hour}{minute}{ampm}"


def days_between(a: Any, b: Any) -> Optional[int]:
    da, db = parse_dt(a), parse_dt(b)
    if not da or not db:
        return None
    return (db.date() - da.date()).days


def month_of(now: Any) -> Optional[int]:
    dt = parse_dt(now)
    return dt.month if dt else None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- language

HINGLISH_MARKERS = {
    "hai", "hain", "nahi", "nahin", "kya", "karo", "kar", "karna", "karein", "haan", "han", "ji",
    "mujhe", "mera", "meri", "aap", "aapka", "aapki", "chahiye", "bhai", "abhi", "baad", "mein",
    "theek", "thik", "accha", "acha", "achha", "kaise", "kitna", "kyun", "matlab", "bolo", "batao",
    "bhejo", "bhej", "doon", "dijiye", "sakte", "hoga", "wala", "wali", "judna", "judrna", "shukriya",
    "dhanyavad", "namaste", "chalega", "jaldi", "kal", "samjha", "samajh", "nahi.", "haan.",
}


def is_hinglish(text: str) -> bool:
    if not text:
        return False
    if re.search(r"[ऀ-ॿ]", text):  # Devanagari
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & HINGLISH_MARKERS) >= 1


REGIONAL_GREETING = {"ta": "Vanakkam", "te": "Namaskaram", "kn": "Namaskara", "mr": "Namaskar"}
_PREF_TO_CODE = {"ta": "ta", "tamil": "ta", "te": "te", "telugu": "te", "kn": "kn", "kannada": "kn", "mr": "mr", "marathi": "mr"}


def merchant_regional_greeting(merchant: dict) -> str:
    """Greeting word for merchants whose primary regional language is not Hindi (e.g. ['en','ta','hi'] -> Vanakkam)."""
    langs = [str(x).lower() for x in (g(merchant, "identity", "languages") or [])]
    regional = [l for l in langs if l not in ("en", "english")]
    return REGIONAL_GREETING.get(regional[0], "") if regional and regional[0] not in ("hi", "hindi") else ""


def customer_regional_greeting(customer: Optional[dict]) -> str:
    """'ta-en mix' -> Vanakkam; 'te-en mix' -> Namaskaram; hi / en -> ''."""
    pref = str(g(customer, "identity", "language_pref") or "").lower()
    code = _PREF_TO_CODE.get(pref.split("-")[0].split()[0] if pref else "", "")
    return REGIONAL_GREETING.get(code, "")


def merchant_prefers_hinglish(merchant: dict) -> bool:
    """Code-mix when Hindi is the merchant's primary non-English language."""
    langs = [str(x).lower() for x in (g(merchant, "identity", "languages") or [])]
    regional = [l for l in langs if l not in ("en", "english")]
    return bool(regional) and regional[0] in ("hi", "hindi")


def customer_prefers_hinglish(customer: Optional[dict]) -> bool:
    pref = str(g(customer, "identity", "language_pref") or "").lower()
    return pref.startswith("hi")


# --------------------------------------------------------------------------- names

def owner_first(merchant: dict) -> str:
    return str(g(merchant, "identity", "owner_first_name") or "").strip()


def merchant_salutation(merchant: dict, slug: str) -> str:
    owner = owner_first(merchant)
    name = str(g(merchant, "identity", "name") or "").strip()
    if not owner:
        return f"{name} team" if name else "Hi"
    bare = re.sub(r"^dr\.?\s*", "", owner, flags=re.I).strip()
    if slug == "dentists":
        return f"Dr. {bare}"
    return bare


def merchant_name(merchant: dict) -> str:
    return str(g(merchant, "identity", "name") or "your business")


def customer_address(customer: Optional[dict]) -> tuple[str, str]:
    """Returns (greeting_name, subject_name).

    'Karthik (parent: Sumitra)' -> ('Sumitra', 'Karthik'); 'Mr. Sharma' -> ('', 'Sharma ji');
    '(walk-in, no profile)' -> ('', '').
    """
    raw = str(g(customer, "identity", "name") or "").strip()
    if not raw or raw.startswith("("):
        return "", ""
    m = re.match(r"^(.*?)\s*\(parent:\s*(.*?)\)\s*$", raw)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(r"^(mr|mrs|ms|shri|smt)\.?\s+(.*)$", raw, flags=re.I)
    if m:
        return "", f"{m.group(2).strip()} ji"
    return raw, raw


# --------------------------------------------------------------------------- merchant facts

def active_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in (merchant.get("offers") or [])
            if isinstance(o, dict) and o.get("status") == "active" and o.get("title")]


def expired_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in (merchant.get("offers") or [])
            if isinstance(o, dict) and o.get("status") in ("expired", "paused") and o.get("title")]


def catalog_offer(category: dict, prefer: tuple[str, ...] = ("service_at_price",)) -> Optional[str]:
    for o in category.get("offer_catalog") or []:
        if isinstance(o, dict) and o.get("type") in prefer and o.get("title"):
            return o["title"]
    for o in category.get("offer_catalog") or []:
        if isinstance(o, dict) and o.get("title"):
            return o["title"]
    return None


def offer_matching(merchant: dict, category: dict, keywords: list[str]) -> Optional[str]:
    """Active merchant offer matching any keyword, else None."""
    for title in active_offers(merchant):
        low = title.lower()
        if any(k in low for k in keywords):
            return title
    return None


def signal_value(merchant: dict, prefix: str) -> Optional[str]:
    for s in merchant.get("signals") or []:
        s = str(s)
        if s == prefix:
            return ""
        if s.startswith(prefix + ":"):
            return s.split(":", 1)[1]
    return None


def has_signal(merchant: dict, prefix: str) -> bool:
    return signal_value(merchant, prefix) is not None


def review_theme(merchant: dict, sentiment: str) -> Optional[dict]:
    themes = [t for t in (merchant.get("review_themes") or []) if isinstance(t, dict) and t.get("sentiment") == sentiment]
    themes.sort(key=lambda t: -(t.get("occurrences_30d") or 0))
    return themes[0] if themes else None


def ctr_vs_peer(merchant: dict, category: dict) -> Optional[str]:
    ctr = g(merchant, "performance", "ctr")
    peer = g(category, "peer_stats", "avg_ctr")
    if ctr is None or peer is None:
        return None
    rel = "below" if ctr < peer else "above"
    return f"your CTR is {pct1(ctr)}, {rel} the {pct1(peer)} peer average"


# --------------------------------------------------------------------------- category facts

def digest_item(category: dict, item_id: Any = None, kinds: tuple[str, ...] = ()) -> Optional[dict]:
    items = [d for d in (category.get("digest") or []) if isinstance(d, dict)]
    if item_id:
        for d in items:
            if d.get("id") == item_id:
                return d
    for k in kinds:
        for d in items:
            if d.get("kind") == k:
                return d
    return None


def month_in_range(month: int, month_range: str) -> bool:
    """'Nov-Feb' / 'Apr-Jun' / 'Jan' / 'Feb 14'."""
    parts = re.findall(r"[A-Za-z]{3}", month_range or "")
    idx = [MONTHS.index(p.title()) + 1 for p in parts if p.title() in MONTHS]
    if not idx:
        return False
    if len(idx) == 1:
        return month == idx[0]
    start, end = idx[0], idx[1]
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def seasonal_beat(category: dict, now: Any = None, keyword: Optional[str] = None) -> Optional[dict]:
    beats = [b for b in (category.get("seasonal_beats") or []) if isinstance(b, dict)]
    if keyword:
        for b in beats:
            if keyword in str(b.get("note", "")).lower():
                return b
    m = month_of(now)
    if m:
        for b in beats:
            if month_in_range(m, str(b.get("month_range", ""))):
                return b
    return None


def top_trend(category: dict, specific: bool = False) -> Optional[dict]:
    """Highest-growth search trend; `specific` skips generic queries (offers, costs, 'gym near me')."""
    trends = [t for t in (category.get("trend_signals") or []) if isinstance(t, dict) and t.get("delta_yoy") is not None]
    if specific:
        trends = [t for t in trends if not any(w in str(t.get("query", "")).lower() for w in ("offer", "gym near me", "cost"))] or trends
    trends.sort(key=lambda t: -float(t["delta_yoy"]))
    return trends[0] if trends else None


def first_sentence(text: Any) -> str:
    s = str(text or "").strip()
    m = re.match(r"(.+?(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bvs)(?<!\bp)(?<!\bNo)[.!?])(\s|$)", s)
    return (m.group(1) if m else s).strip()


def strip_taboos(text: str, category: dict) -> str:
    """Last-line defence: remove category taboo phrases if any slipped into a body."""
    for taboo in g(category, "voice", "vocab_taboo", default=[]) or []:
        phrase = re.sub(r"\s*\(.*?\)", "", str(taboo)).strip()
        if phrase and len(phrase) > 3:
            text = re.sub(re.escape(phrase), "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()

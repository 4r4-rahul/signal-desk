#!/usr/bin/env python3
"""
SIGNAL ENGINE — turn a provider signal into a consistent, mechanical trade plan.

Input : a signal string, e.g.
    python3 signal_engine.py "sulker: IWM 298P 0DTE @0.64"
    python3 signal_engine.py "mike: SPX 7510C 1.6 lotto"
    python3 signal_engine.py "SPY 750C 0DTE @1.20"     (provider unknown -> lower confidence)

Output: a CONFIDENCE score (rules-based, grounded in historical segment stats — NOT a
prediction of this trade) + a full trade plan: contracts, stop, scale-out, runners, exits.

Config via env:  ACCOUNT (default 15000)   RISK_PCT (default 1.0)
"""
import sqlite3, re, sys, os, json, statistics as st
from pathlib import Path
ROOT = Path(__file__).parent
ACCOUNT = float(os.environ.get("ACCOUNT", 15000))
RISK_PCT = float(os.environ.get("RISK_PCT", 1.0))
THROTTLE = 1.0   # drawdown throttle (1.0 normal … 0.0 halt), set by the server from equity state
SIZING_STOP_FRAC = 0.35    # RISK-ACCOUNTING stop (your real -30% own-stop + slippage buffer) = the 1R basis
EXIT_STOP_FRAC = 0.30      # DISPLAYED hard-stop the trader acts on (matches _trust.json exit rule)
STOP_FRAC = SIZING_STOP_FRAC   # back-compat alias; discipline.py & equity.py import SIZING_STOP_FRAC
TP1, TP2 = 0.40, 0.75      # scale target / runner target
TRAIL = 0.25               # runner trails 25% off peak

# Provider registry: trust score (from our analysis) + author-name matches (for live routing).
# trust here is only a FALLBACK if channels/_trust.json is missing; kept in sync with the
# data-fit ordering (jmt/prince up, mike/optionking down) so a fallback can't silently invert.
PROVIDERS = {
    "sulker":     {"trust": 47, "match": ["sulker"]},
    "mike":       {"trust": 33, "match": ["spx mike", "mike"]},
    "optionking": {"trust": 40, "match": ["option king", "optionking"]},
    "jmt":         {"trust": 58, "match": ["jmt", "cgbg", "jmoney", "j money"]},
    "prince":      {"trust": 52, "match": ["twp", "prince"]},
    "prince_small": {"trust": 56, "match": ["prince small", "small account", "prince-small"]},
}
TRUST = {k: v["trust"] for k, v in PROVIDERS.items()}

# Data-driven priors (offline-fit, read-only in the live path). Missing file/key -> hardcode fallback.
import json as _json


def _load_trust():
    try:
        return _json.loads((ROOT / "channels" / "_trust.json").read_text())
    except Exception:
        return {}


_TRUST = _load_trust()


def provider_behavior(p):
    return (_TRUST.get("behavior") or {}).get(p, {})


def provider_exit(p):
    """Per-provider stop/scale/trail (ML: stop style differs by provider — follow managed books,
    hard-stop the fire-hose ones). Falls back to the global exit_rule."""
    return (_TRUST.get("exit_by_provider") or {}).get(p or "", {})


def provider_size_mult(p):
    """Per-provider risk multiplier (data-driven: robust/followable providers size up,
    integrity-capped/negative-EV size down). Unknown/unrecognized author -> 0.25 sandbox."""
    m = (_TRUST.get("providers") or {}).get(p or "", {}).get("size_mult")
    return float(m) if m is not None else 0.25


_EDGE_FILE = ROOT / "channels" / "_provider_edge.json"


def provider_edge_mult(p):
    """Calibration-gated size tilt from the LIVE realized-R record (written by the server from the DB).
    Shrinks proven bleeders, unlocks tilt-up only when a provider earns it over a real sample. Defaults
    to 1.0 (no change) when there's no data. This is 'data buys the size, feelings don't' in code."""
    try:
        edge = json.loads(_EDGE_FILE.read_text())
        return float(edge.get(p or "", {}).get("mult", 1.0))
    except Exception:
        return 1.0


def provider_disabled(p):
    """User eliminated this provider (proven money-loser / cancelled subscription) — drop their signals."""
    return bool((_TRUST.get("providers") or {}).get(p or "", {}).get("disabled"))


def provider_manual_override(p):
    """A PAPER provider the user may still take at their own discretion, capped at manual_max lots.
    Returns the max lots (e.g. 1) or 0 if no override."""
    pj = (_TRUST.get("providers") or {}).get(p or "", {})
    return int(pj.get("manual_max", 1)) if pj.get("manual_override") else 0


def detect_provider(author):
    a = (author or "").lower()
    for label, cfg in PROVIDERS.items():
        if any(m in a for m in cfg["match"]):
            return label
    return None


def trust_of(p):
    tj = (_TRUST.get("providers") or {}).get(p or "", {})
    if tj.get("trust") is not None:                       # data-derived trust (fit offline, shrunk)
        return tj["trust"]
    return PROVIDERS.get(p, {}).get("trust", 30)


# Ticker does NOT predict outcome (ML: noise). Kept only as a small LIQUIDITY prior
# (liquid index/ETF = tighter spreads = lower guaranteed slippage cost), capped small.
TICKER_ADJ = {"SPX": 5, "SPY": 5, "QQQ": 5, "IWM": 5, "DIA": 4, "NDX": 4,
              "NVDA": 3, "TSLA": 3, "MSFT": 3, "META": 3, "AAPL": 3, "GOOGL": 3, "AMD": 3}
line_re = re.compile(r'\b([A-Z]{2,4})?\s*(\d{2,5}(?:\.\d)?)\s*([cp])\b', re.I)
pct_re = re.compile(r'(-?\d+)\s*%')
MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "JULY", "AUG", "SEP", "SEPT",
          "OCT", "NOV", "DEC"}
# words that look like a ticker before a number but aren't — modifiers/months/action verbs
NOT_TICKERS = MONTHS | {
    "EOD", "EXP", "DTE", "ODTE", "OTDE", "ODTC", "ATH", "ATL", "PM", "AM", "ITM", "OTM",
    "UP", "DOWN", "DID", "AT", "TP", "SL", "BE", "PT", "USD", "PER", "AND", "THE", "FOR", "ON",
    "HIGH", "RISK", "RISKY", "LOTTO", "LOTTOS", "HERO", "BROUGHT", "BOUGHT", "BUYING", "BUY",
    "JUST", "LIKE", "INTO", "TOOK", "SWINGING", "SWING", "GRABBED", "GRAB", "ENTERED", "ENTER",
    "ENTRY", "ADDING", "ADDED", "ADD", "BACK", "NEW", "SMALL", "STARTER", "TRYING", "GOT",
    "SIZE", "LOW", "PLAY", "DAY", "TRADE", "REENTER", "RE", "AVG", "OF", "CONTRACTS", "CONTRACT",
    "CONS", "CON", "WITH", "BOTH", "EACH", "NEAR", "ROLL", "ROLLING", "MORE", "IN", "OUT",
    "SOLD", "SELLING", "SELL", "CUTTING", "CUT", "SCALING", "SCALED", "SECURE", "TAKING", "TAKE",
    "HOLDING", "HOLD", "LEAVING", "LEAVE", "RUNNER", "RUNNERS", "PLAYING", "OFF", "POSITION",
    "NO", "STOP", "LOSS", "SWINGING", "GONNA", "WILL", "IF", "HERE", "NOW", "SO", "FAR"}
ALIAS = {"TESLA": "TSLA", "GOOGLE": "GOOGL", "GOOG": "GOOGL", "APPLE": "AAPL", "META": "META",
         "NETFLIX": "NFLX", "MICROSOFT": "MSFT", "AMAZON": "AMZN", "NVIDIA": "NVDA", "COIN": "COIN"}
# known tradeable tickers — helps find the real ticker + weak allow-list (parser is NOT gated on this)
KNOWN_TK = set(TICKER_ADJ) | set(ALIAS.values()) | {
    "SPX", "SPXW", "NDX", "RUT", "VIX", "DIA", "QQQ", "IWM", "C", "F", "TGT",
    "AMD", "AMZN", "NFLX", "COIN", "MSTR", "PLTR", "SMCI", "AVGO", "MU", "BABA", "UBER", "SNAP",
    "SOFI", "HOOD", "DELL", "CRM", "ORCL", "BA", "DIS", "GOOG", "UNH", "LLY", "COST", "WMT",
    "JPM", "BAC", "XLF", "GLD", "SLV", "USO", "TLT", "BMNR", "TEM", "IONQ", "ALAB", "CRWV",
    "RDDT", "OKLO", "NVTS", "CLSK", "BULL", "HIVE", "UUUU", "WBD", "LAES", "BTBT", "PFE", "JNJ",
    "OXY", "NKE", "CSCO", "XOM", "QCOM", "OKTA", "RBLX", "RKLB", "GEO", "SQ", "LMND", "AFRM",
    "CELH", "GRAB", "SMR", "UPST", "EBAY", "APP", "RIOT", "MARA", "MRVL", "NBIS", "MSTU", "SBET",
    "OSCR", "SOUN", "SNDK", "ZETA", "GME", "IBIT", "QBTS", "TSM", "NOW", "ARM", "PYPL", "SHOP"}
_SKIP_TK = NOT_TICKERS


# Tickers that are ALSO common English words — matching them lowercase mid-sentence is how
# Prince's "Filled now" became a ServiceNow card. These only count when written UPPERCASE (or $-prefixed).
AMBIG_TK = {"NOW", "ARM", "COIN", "HOOD", "BULL", "COST", "APP", "GRAB", "HIVE", "BILL", "ALL", "OPEN", "RUN", "PLAY"}


def _find_ticker(text):
    """Find the real ticker anywhere in the message (handles $C / multi-line 'ticker then strike')."""
    for t in re.findall(r'\$([A-Za-z]{1,5})', text):      # $-prefixed first (catches single-letter $C)
        if t.upper() in KNOWN_TK:
            return ALIAS.get(t.upper(), t.upper())
    up = text.upper()
    for t in KNOWN_TK:                                     # bare known multi-letter symbol as a word
        if len(t) >= 2 and re.search(rf'\b{re.escape(t)}\b', up):
            if t in AMBIG_TK and not re.search(rf'\b{re.escape(t)}\b', text):
                continue                                   # 'now'/'arm'/'coin' as lowercase words ≠ tickers
            return t
    return None


def _resolve_ticker(text, mstart, provider, strike):
    """Ticker resolution across all provider styles: cashtag -> nearest ticker-looking token
    before the strike -> known-symbol scan -> provider/implicit defaults."""
    for t in re.findall(r'\$([A-Za-z]{1,5})\b', text):    # 1) cashtag (Prince, $C)
        u = t.upper()
        if u not in _SKIP_TK or u in KNOWN_TK:            # a cashtag is EXPLICIT — $now really is ServiceNow
            return ALIAS.get(u, u)
    pre = text[:mstart]                                   # 2) nearest ticker-ish token before strike
    for _i, tok in enumerate(reversed(re.findall(r'[A-Za-z]{1,5}', pre))):
        u = tok.upper()
        # ambiguous common-word tickers (now/arm/coin/hood…) in lowercase only count in TICKER POSITION
        # (within 2 tokens of the strike, e.g. mike's 'hood 80c') — never as distant sentence words
        _ambig_block = (not tok.isupper()) and _i > 1
        if u in _SKIP_TK:
            continue
        if u in ALIAS:
            if ALIAS[u] in AMBIG_TK and _ambig_block:     # distant lowercase 'coin flip' ≠ COIN
                continue
            return ALIAS[u]
        if tok.isupper() and len(tok) >= 2:               # providers write tickers in CAPS
            return u
        if u in KNOWN_TK:                                 # known symbol in any case (mike's lowercase)
            if u in AMBIG_TK and _ambig_block:            # 'arm the stop'/'filled now' ≠ ARM/NOW tickers
                continue
            return u
        if len(tok) == 1 and tok in ("C", "F"):
            return u
    for tok in reversed(re.findall(r'[A-Za-z]{3,6}', pre)):   # 2b) typo-tolerant ticker (msttr -> MSTR)
        u = tok.upper()
        if u in _SKIP_TK or u in KNOWN_TK:
            continue
        cand = _closest_known(u)
        if cand:
            return cand
    kt = _find_ticker(text)                               # 3) known-symbol scan anywhere
    if kt:
        return kt
    si = int(float(strike)) if re.match(r'^\d', strike or "") else 0   # 4) defaults
    if provider == "jmt":
        return "SPY"                                      # JMT trades SPY only, rarely writes it
    if si >= 4000:
        return "SPX"                                      # Mike's implicit SPX (bare 4-digit strike)
    return None                                           # no ticker -> don't fabricate


def _parse_expiry(text):
    low = text.lower()
    if re.search(r'\bd0te\b|\b0\s*dte\b|today expiry|expir\w*\s+today|exp\w*\s+today', low):
        return 0, "0DTE"
    m = re.search(r'\b(\d)\s*dte\b', low)
    if m:
        return int(m.group(1)), f"{m.group(1)}DTE"
    m = re.search(r'\b(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b', low)
    if m:
        return None, f"{int(m.group(1))} {m.group(2)[:3].title()}"
    m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})\b', low)
    if m:
        return None, f"{m.group(1)[:3].title()} {int(m.group(2))}"
    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?\b', text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 12 or b <= 12:
            mo, dd = (b, a) if a > 12 else (a, b)
            return None, f"{mo:02d}/{dd:02d}"
    return None, None


def _parse_premium(text, pos):
    for scope in (text[pos:pos + 42], text):
        # decimal premium: optional integer part + dot + 1-2 fractional, digits IMMEDIATELY after the dot
        # (no space) — otherwise a sentence period like "today. 1.35" is misread as ".1" = 0.10.
        m = re.search(r'@?\s*((?:\d{1,3})?\.\d{1,2})\b', scope)
        if m:
            v = float(m.group(1))
            if 0.03 <= v <= 60:
                return v
        m = re.search(r'@\s*(\d{1,2})(?!\d)', scope)                 # integer premium after @
        if m and 0.03 <= float(m.group(1)) <= 60:
            return float(m.group(1))
    # cents notation: "85c per contract", "85 cents", "$0.85" already caught above.
    # The (?<!\d) guard stops it firing inside a strike like "385c" (the 85 is preceded by 3).
    # Require a cents/per-contract cue so a bare call strike ("$5c") is never read as $0.05.
    for scope in (text[pos:pos + 42], text):
        cm = re.search(r'(?<![\d.])(\d{1,3})\s*(?:¢|cents?\b'
                       r'|c\s*(?:per\s+|/|a\s+)?(?:contract|ct|share|ea)\b'
                       r'|c\s+(?:each|apiece)\b)', scope, re.I)
        if cm:
            v = float(cm.group(1)) / 100.0
            if 0.03 <= v <= 9.99:
                return v
    return None


_STRIKE_RE = re.compile(r'(?<![\d.])(\d{1,5}(?:\.\d{1,2})?)\s*([cp])\b', re.I)
_WORD_LEG_RE = re.compile(r'(?<![\d.])\$?(\d{1,5}(?:\.\d{1,2})?)\s*(call|put)s?\b', re.I)
# "TICKER STRIKE at/@ PREMIUM" with NO c/p and no call/put word (e.g. "NVDA 200 at 0.10") -> default CALL
_BARE_LEG_RE = re.compile(r'(?<![\d.])(\d{2,5}(?:\.\d{1,2})?)\s*(?:@|\bat\b)\s*\$?\d{0,3}\.\d{1,2}', re.I)
# Mike shorthand: "TICKER STRIKE PREMIUM" space-separated, NO c/p (e.g. "META 675 2.5", "NVDA 212.5 .14",
# "meta 672 1.7") -> default CALL. Strike is the big number; premium is the small decimal that follows.
_BARE_SPACE_RE = re.compile(r'(?<![\d.$])(\d{2,5}(?:\.\d{1,2})?)\s+\$?(\.\d{1,2}|\d{1,2}\.\d{1,2})(?![\d.])')
# odd word order: "call/put ... on/at STRIKE"  and  "on/at STRIKE ... call/put"  (e.g. "tsla call .39 on 417")
_CALLPUT_ON_RE = re.compile(r'\b(call|put)s?\b[^\n]{0,20}?\b(?:on|at|@|strike)\s*\$?(\d{2,5}(?:\.\d{1,2})?)\b', re.I)
_ON_CALLPUT_RE = re.compile(r'\b(?:on|at|@|strike)\s*\$?(\d{2,5}(?:\.\d{1,2})?)\b[^\n]{0,20}?\b(call|put)s?\b', re.I)


def _sniff_premium(text, strike):
    """Last-resort premium finder for odd word order — the smallest plausible decimal that isn't the strike."""
    sv = float(strike) if strike and re.match(r'^\d', str(strike)) else None
    for mm in re.finditer(r'(?<![\d./:$%])\$?(\.\d{1,2}|\d{1,3}\.\d{1,2})\b', text):
        v = float(mm.group(1))
        if sv is not None and abs(v - sv) < 1e-9:
            continue
        if 0 < v <= 60:
            return v
    return None


def _lev_le1(a, b):
    """True if a and b are within edit distance 1 (one insert/delete/substitute) — for ticker typos."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if la > lb:
        a, b = b, a                                          # a = shorter
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
        else:
            diff += 1
            if diff > 1:
                return False
        j += 1
    return True


def _closest_known(u):
    """A KNOWN_TK within one typo of u (msttr -> MSTR). Length-gated to avoid loose 2-letter matches."""
    if len(u) < 3:
        return None
    for t in KNOWN_TK:
        if len(t) >= 3 and _lev_le1(u, t):
            return t
    return None


def _strip_noise(s):
    s = re.split(r'\[EMBED\]', s, 1)[0]
    s = re.sub(r'\(edited\)', ' ', s, flags=re.I)
    s = re.sub(r'\n(?:mon|tues|wednes|thurs|fri|satur|sun)day,[^\n]*', ' ', s, flags=re.I)
    return s


# ---------- historical segment stats (evidence behind the score) ----------
def realized(line):
    low = line.lower()
    if 'break even' in low or 'b/e' in low: return 0.0
    if 'expired worthless' in low or ('zero hero' in low and ('fail' in low or '-100' in low)): return -100.0
    sm = re.search(r'(?:sold|exit)[^%]*\((-?\d+)\s*%', low)
    if sm: return float(sm.group(1))
    pm = pct_re.search(re.split(r'contract high|high\s*-*>', low)[0])
    if pm: return float(pm.group(1))
    if 'scratch' in low or 'flat' in low: return 0.0
    if 'stopped' in low: return -50.0
    if 'cut' in low or 'loss' in low: return -40.0
    return None


def load_segments():
    seg = {}
    for ch in ('mike', 'sulker'):
        db = ROOT / "channels" / ch / f"{ch}.db"
        if not db.exists(): continue
        rows = sqlite3.connect(str(db)).execute(
            "SELECT text FROM raw_messages WHERE text IS NOT NULL").fetchall()
        for (t,) in rows:
            if 'recap' not in t.lower()[:40]: continue
            for line in t.split('\n'):
                m = line_re.search(line)
                if not m: continue
                r = realized(line)
                if r is None: continue
                tk = (m.group(1) or 'SPX').upper()
                seg.setdefault(tk, []).append(max(min(r, 2000), -100))
    return {k: v for k, v in seg.items() if len(v) >= 15}


# ---------- parse a signal ----------
def parse_signal(s, provider=None):
    if not s:
        return None
    mp = re.match(r'\s*([a-z_]+)\s*[:\-]', s, re.I)              # optional "sulker:" CLI prefix
    if mp and mp.group(1).lower() in TRUST:
        provider = provider or mp.group(1).lower(); s = s[mp.end():]
    text = _strip_noise(s)
    low = text.lower()
    # reject non-entries: recaps/reviews/watchlists/outlooks, and update/sell lines
    if re.search(r'\b(recap|review|watchlist|watch list|outlook|holdings?)\b', low[:60]):
        return None
    if re.match(r'\s*(sold|all ?out|out\b|trimm?ed|cutting|closing|secure|scal(ed|ing)|de-?risk)\b', low):
        return None
    m, typ, assumed, strike = None, None, False, None
    for mm in _STRIKE_RE.finditer(text):                        # strike + C/P suffix (e.g. 750C, $73c)
        typ = mm.group(2).upper(); m = mm; strike = mm.group(1); break
    if not m:                                                   # "$300 ... Calls": $-strike + call/put word
        dm = re.search(r'\$(\d{2,5}(?:\.\d{1,2})?)', text)      #   ($-prefixed strike beats a date's "10")
        wm = re.search(r'\b(call|put)s?\b', text, re.I)
        if dm and wm:
            typ = "C" if wm.group(1).lower().startswith("c") else "P"; m = dm; strike = dm.group(1)
    if not m:                                                   # strike + word "call"/"put" (digit before)
        mm = _WORD_LEG_RE.search(text)
        if mm:
            typ = "C" if mm.group(2).lower().startswith("c") else "P"; m = mm; strike = mm.group(1)
    if not m:                                                   # Mike's word order: "call/put … on STRIKE"
        mm = _CALLPUT_ON_RE.search(text) or _ON_CALLPUT_RE.search(text)
        if mm:
            g1, g2 = mm.group(1), mm.group(2)
            word = g1 if g1.lower() in ("call", "put") else g2
            strike = g2 if g1.lower() in ("call", "put") else g1
            typ = "C" if word.lower().startswith("c") else "P"; m = mm
    if not m:                                                   # "STRIKE at/@ PREMIUM", no C/P -> CALL
        mm = _BARE_LEG_RE.search(text)
        if mm:
            m = mm; typ = "C"; assumed = True; strike = mm.group(1)
    if not m:                                                   # Mike shorthand "STRIKE PREMIUM" (space) -> CALL
        mm = _BARE_SPACE_RE.search(text)
        if mm:
            m = mm; typ = "C"; assumed = True; strike = mm.group(1)
    if not m:
        return None
    tk = _resolve_ticker(text, m.start(), provider, strike)
    if not tk:
        return None
    dte, expiry = _parse_expiry(text)
    prem = _parse_premium(text, m.end())
    if prem is None:                                            # odd word order / space-shorthand — sniff it
        prem = _sniff_premium(text, strike)
    lotto = bool(re.search(r'lotto|zero ?hero|\bhero\b|yolo|degen|flip coin|risky', low))
    stated_stop = None                                  # the PROVIDER's own stated stop, if any
    slm = re.search(r'\bs\s*/?\s*l\s*:?\s*(?:at|to)?\s*\$?(\d+(?:\.\d{1,2})?)\s*(%?)', low)
    if slm:
        v = float(slm.group(1)); is_pct = slm.group(2) == '%'
        # a big number (>= ~50) is an UNDERLYING price level; a small decimal is a premium stop
        stated_stop = {"value": v, "kind": "pct" if is_pct else ("underlying" if v >= 50 else "premium")}
    return dict(provider=provider, ticker=ALIAS.get(tk, tk), strike=strike, type=typ,
                dte=dte, expiry=expiry, premium=prem, lotto=lotto, assumed_call=assumed,
                stated_stop=stated_stop)


# ---------- confidence ----------
def confidence(sig, seg):
    prov = sig['provider']
    score = trust_of(prov)                            # data-derived if available, else hardcode
    reasons = [f"provider {prov or 'UNKNOWN'} (+{trust_of(prov)})"]
    # per-provider x ticker tilt (data-fit, min-n gated, ±10) -> else global TICKER_ADJ fallback
    pt = (_TRUST.get("provider_ticker") or {}).get(f"{prov}|{sig['ticker']}")
    if pt and pt.get("adj") is not None:
        adj = max(-10, min(10, pt["adj"]))
        score += adj; reasons.append(f"{prov} on {sig['ticker']} ({adj:+d}, n={pt.get('n','?')})")
    else:
        adj = TICKER_ADJ.get(sig['ticker'], 0)
        score += adj; reasons.append(f"ticker {sig['ticker']} ({adj:+d})")
    if sig['type'] == 'C': score += 3; reasons.append("call (+3)")
    p = sig['premium']
    pstats = (_TRUST.get("providers") or {}).get(prov or "", {})
    med, mad = pstats.get("premium_median"), pstats.get("premium_mad")
    if p is not None and med and mad:                 # provider-RELATIVE premium (don't punish native style)
        z = (p - med) / mad
        if z < -1.5: score -= 6; reasons.append(f"unusually cheap for {prov} (z{z:.1f}, -6)")
        elif z > 2.0: score -= 4; reasons.append(f"chasing extended premium (z{z:+.1f}, -4)")
        else: score += 4; reasons.append(f"premium normal for {prov} (z{z:+.1f}, +4)")
    elif p is not None:                               # unknown provider -> absolute buckets
        if p < 0.50: score -= 15; reasons.append(f"cheap ${p:.2f} slippage trap (-15)")
        elif p <= 2.0: score += 8; reasons.append(f"liquid ${p:.2f} (+8)")
        elif p > 3.0: score -= 5; reasons.append(f"pricey ${p:.2f} (-5)")
    if sig['lotto']: score -= 12; reasons.append("lotto (-12)")
    score = max(0, min(100, score))
    tier = "HIGH ✅" if score >= 75 else "MEDIUM 🟡" if score >= 60 else "LOW 🟠" if score >= 45 else "SKIP ❌"
    ev = seg.get(sig['ticker'])
    eff = None
    if ev:
        w = sum(1 for x in ev if x > 1) / len(ev) * 100
        eff = f"{sig['ticker']} history: {w:.0f}% win, median {st.median(ev):+.0f}% (n={len(ev)}, self-reported)"
    return score, tier, reasons, eff


LOW_TRUST = ("mike", "sulker")   # providers with PROVEN-negative realized R (large n) — strict single-reason gate.
# optionking removed 2026-07-14: his block rested on n=3 (−0.94R, one −4.25R gap); a BS reconstruction over
# n=1,279 cross-validated against tick data put him at ≈break-even (44% win, −0.09R), NOT a Mike-class bleeder.
# He now gets the BALANCED gate: his far-OTM 0–1DTE lottos still hard-block (2 junk flags: far-OTM Δ + θ-cliff),
# but his 2–30DTE weekly/swing (Δ≥0.30, tight spread) surface as PAPER for scalp-tracking. Stays zero-size.


def junk_setup(opt, sig, provider=None, chase_pct=0):
    """BALANCED quality gate. HARD-BLOCK the real garbage — far-OTM lottery tickets, wide-spread
    slippage traps, chased R:R, and theta-cliffs on anything NOT cleanly scalpable. WARN (but allow) a
    clean ATM tight-spread θ-cliff you can actually scalp (Δ0.30-0.70, spread<10%) — like Mike's ATM
    META 0DTE, which nets green on fast scalps but must never be held. Low-trust providers get a stricter
    bar. Returns {block, warn, reasons}. Built from what bled the account (Mike: 74% θ-cliffs, 21% win)."""
    empty = {"block": False, "warn": False, "reasons": []}
    if not opt or opt.get("error") or opt.get("mid") is None:
        return empty
    mid, delta, theta, spread = opt.get("mid"), opt.get("delta"), opt.get("theta"), opt.get("spread_pct")
    burn = (abs(theta) / mid * 100) if (theta is not None and mid) else None
    ad = abs(delta) if delta is not None else None
    low = provider in LOW_TRUST
    th_burn, th_delta, th_spread = (40, 0.15, 15) if low else (60, 0.10, 25)
    garbage, warns = [], []
    if ad is not None and ad < th_delta:
        garbage.append(f"far-OTM Δ{ad:.2f} — a lottery ticket, not a trade")
    if spread is not None and spread >= th_spread:
        garbage.append(f"wide spread {spread:.0f}% — you lose on entry+exit")
    if mid is not None and mid < 0.50 and (spread is not None and spread >= 10):
        garbage.append(f"cheap ${mid} + wide spread — slippage eats it")
    if chase_pct and chase_pct >= 25:
        garbage.append(f"chasing +{chase_pct:.0f}% — R:R already gone")
    if burn is not None and burn >= th_burn:
        scalpable = (ad is not None and 0.30 <= ad <= 0.70) and (spread is not None and spread < 10)
        if scalpable:
            warns.append(f"θ-cliff {burn:.0f}%/day — SCALP ONLY, never hold it")
        else:
            garbage.append(f"θ-cliff {burn:.0f}%/day — decays to zero fast")
    if (low and len(garbage) >= 1) or (len(garbage) >= 2):
        return {"block": True, "warn": False, "reasons": garbage}
    if garbage or warns:
        return {"block": False, "warn": True, "reasons": garbage + warns}
    return empty


def cost_adjust(opt, sig, now_min=None):
    """Fold the live MECHANICAL costs the base score misses — chasing the provider's stated entry,
    theta decay, and 0DTE time-of-day. (Spread + delta are already scored in market.option_score.)
    These are causal costs that bleed expectancy REGARDLESS of direction: this ranks how expensive the
    setup is to express, not whether it wins. Returns (adj, reasons); adj bounded so it can't dominate."""
    if not opt or opt.get("error") or opt.get("mid") is None:
        return 0, []
    adj, reasons = 0, []
    mid, prem, th = opt.get("mid"), sig.get("premium"), opt.get("theta")
    # 1) chasing the provider's stated entry — paying up is a guaranteed R:R erosion
    if prem and mid:
        chase = (mid / prem - 1) * 100
        if chase >= 20:
            adj -= 8; reasons.append(f"chasing +{chase:.0f}% over entry (-8)")
        elif chase >= 12:
            adj -= 4; reasons.append(f"chasing +{chase:.0f}% over entry (-4)")
        elif chase <= 3:
            adj += 2; reasons.append("in the buy zone (+2)")
    # 2) theta burn — guaranteed daily decay as a % of premium (0DTE cliffs)
    if th is not None and mid:
        burn = abs(th) / mid * 100
        if burn >= 50:
            adj -= 6; reasons.append(f"θ-cliff {burn:.0f}%/day (-6)")
        elif burn >= 30:
            adj -= 3; reasons.append(f"heavy θ {burn:.0f}%/day (-3)")
    # 3) time-of-day — 0DTE midday chop bleeds theta with little movement; power hour has range (mild)
    if now_min is not None and (sig.get("dte") == 0 or sig.get("expiry") == "0DTE"):
        if 120 <= now_min <= 300:
            adj -= 2; reasons.append("0DTE midday chop (-2)")
        elif now_min >= 360:
            adj += 1; reasons.append("power hour (+1)")
    return max(-16, min(4, adj)), reasons


# ---------- trade plan ----------
def plan(sig):
    p = sig['premium']
    if p is None:
        return "  ⚠ No entry premium in signal — enter it once you see the fill, then rerun.\n" \
               f"  Formula: contracts = floor({ACCOUNT:.0f} × {RISK_PCT}% ÷ (premium × 45))"
    contracts = int((ACCOUNT * RISK_PCT / 100) / (p * 100 * STOP_FRAC))
    if contracts < 1:
        return f"  ❌ Too expensive for {RISK_PCT}% risk on ${ACCOUNT:.0f} — 1 contract risks " \
               f"${p*100*STOP_FRAC:.0f} (> ${ACCOUNT*RISK_PCT/100:.0f} budget). Trade a cheaper strike or skip."
    risk = contracts * p * 100 * STOP_FRAC
    stop = round(p * (1 - EXIT_STOP_FRAC), 2)
    t1, t2 = round(p * (1 + TP1), 2), round(p * (1 + TP2), 2)
    sold = round(contracts * 2 / 3)
    runner = contracts - sold
    lines = [
        f"  Contracts   : {contracts}  (entry ~${p:.2f}, limit within 10% — no fill in 60s → SKIP)",
        f"  Capital used: ${contracts*p*100:.0f}   |   Risk at stop: ${risk:.0f} ({risk/ACCOUNT*100:.1f}% of acct)",
        f"  STOP-LOSS   : ${stop:.2f}  (−45%). Hard stop, no averaging down.",
    ]
    if contracts >= 3:
        lines += [
            f"  SCALE OUT   : sell {sold} contracts at ${t1:.2f} (+40%)  → bank the base hit",
            f"  RUNNER      : keep {runner}; move its stop to BREAKEVEN (${p:.2f})",
            f"  RUNNER EXIT : trail 25% off peak, or take at ${t2:.2f} (+75%); HARD EXIT by end of day",
        ]
    elif contracts == 2:
        lines += [f"  SCALE OUT   : sell 1 at ${t1:.2f} (+40%); trail the 2nd (BE stop), exit by EOD"]
    else:
        lines += [f"  EXIT        : full 1 contract at ${t1:.2f}–${round(p*1.5,2):.2f} (+40–50%). Can't scale a 1-lot."]
    return "\n".join(lines)


MAX_NOTIONAL_PCT = 0.08     # single copy's capital <= 8% of account (survive a -100% on any one copy)
MIN_PREMIUM = 0.20          # below this, slippage makes copying untradeable
STANDARD_STOP_PCT = 30      # ONE standard premium stop for ALL trades (bracket-order friendly)


R_MAX = 1.0     # quarter-Kelly ceiling: 1R = 1% of account is the most any single copy risks


def conviction_factor(setup_delta):
    """THIS-signal setup quality, provider-neutral -> [0.5, 1.0]. setup_delta = score - provider_trust
    (ticker liquidity + call + premium-z + lotto + tape). Linear = transparent, hard to overfit."""
    return round(max(0.5, min(1.0, 0.5 + setup_delta / 24.0)), 3)


def size_pct(score, lotto, mult=1.0, premium=None, base_trust=None):
    """Risk% = R_MAX × provider_mult × conviction, then lotto/cheap haircuts.
    CLEAN DECOMPOSITION: provider trust lives ONLY in mult; conviction is setup-only (score-base_trust).
    Legacy call (base_trust=None) falls back to the old coarse ladder for safety."""
    if base_trust is None:
        conv = 0.75 if score >= 65 else 0.5 if score >= 50 else 0.25
    else:
        # ML: confidence does NOT earn more EV within a provider (flat calibration). So conviction's
        # effect on SIZE is compressed to a small execution-quality nudge (~0.85-1.0), NOT a win-bet.
        conv = 0.85 + 0.30 * (conviction_factor(score - base_trust) - 0.5)
    r = R_MAX * mult * conv                             # provider trust only via mult
    if lotto:
        r = min(r, 0.10)                                # lottos: token size regardless of provider
    if premium is not None and premium < 0.50:
        r *= 0.5                                         # cheap-option slippage haircut
    return round(r, 3)


def dynamic_stop(sig, q, ind, rv=None, now_et_min=None):
    """Live VOLATILITY-adjusted premium stop (Phase A). NOT a predictor — sets the stop just outside
    the option's own expected intra-horizon noise, using live Greeks + tape. Falls back to the flat
    STANDARD_STOP_PCT whenever live data is missing. Transparent: returns the terms behind it."""
    import math
    prov = sig.get("provider")
    px = provider_exit(prov)
    base = px.get("stop_pct", STANDARD_STOP_PCT) / 100.0
    gb = provider_behavior(prov).get("give_back_pct", 17) / 100.0
    P = (q or {}).get("mid")
    d = abs((q or {}).get("delta") or 0)
    iv = (q or {}).get("iv")
    S = (ind or {}).get("price")
    prem = sig.get("premium") or P or 0
    if not q or q.get("error") or iv is None or not d or not P or not S:   # no live Greeks -> flat
        return {"stop_pct": STANDARD_STOP_PCT, "stop": round(prem * (1 - STANDARD_STOP_PCT / 100.0), 2),
                "fallback": True, "reliable": True}
    if d < 0.20 or sig.get("lotto"):                       # far-OTM/lotto: binary bet, vol model meaningless
        f = min(max(base, 0.20), 0.45)
        return {"stop_pct": round(f * 100), "stop": round(prem * (1 - f), 2), "fallback": False, "reliable": False}
    sigma = max(iv, rv or iv)
    dte = sig.get("dte")
    T = 10 / 60 if dte == 0 else 60 / 60                    # reaction horizon (hrs): tight watch 0DTE, loose multi-day
    um = sigma * math.sqrt(T / 6.5) / math.sqrt(252)       # 1σ underlying move over the horizon
    Omega = d * S / P                                      # option leverage (premium %move per 1% underlying)
    EM = Omega * um                                        # 1σ premium move (fraction)
    tod = 1 + 0.4 * ((now_et_min or 0) / 390) if dte == 0 else 1.0   # 0DTE gamma widens the band late-day
    f = 0.5 * base + 0.5 * (1.5 * EM * tod)                 # blend behavioral anchor with live vol (anti-overfit)
    f = max(f, gb + 0.05)                                  # never tighter than give-back -> don't clip winners
    upper = 0.35 if px.get("stop_style") == "own_hard" else 0.45
    f = min(max(f, 0.20), upper)
    return {"stop_pct": round(f * 100), "stop": round(prem * (1 - f), 2), "fallback": False, "reliable": True,
            "terms": {"Omega": round(Omega, 1), "EM_pct": round(EM * 100, 1), "sigma": round(sigma, 3), "base_pct": round(base * 100)}}


def exit_plan(conviction):
    """Confidence-scaled exits (C1 — the real edge). Low conviction: bank quick, trail tight,
    stop tight. High conviction: let the runner breathe. conviction None -> legacy defaults."""
    if conviction is None:
        return TP1, TP2, TRAIL, EXIT_STOP_FRAC
    f = max(0.0, min(1.0, (conviction - 0.5) / 0.5))     # 0 at conv .5 -> 1 at conv 1.0
    tp1 = 0.25 + 0.10 * f                                 # +25%..+35%  scale target
    trail = 0.15 + 0.10 * f                               # 15%..25% off peak
    stop = 0.25 + 0.05 * f                                # -25%..-30% hard stop
    return round(tp1, 3), round(tp1 + 0.35, 3), round(trail, 3), round(stop, 3)


def plan_data(sig, risk_pct=None, conviction=None, q=None, ind=None, rv=None, now_min=None, force_contracts=None):
    p = sig['premium']
    if p is None:
        return {"ok": False, "reason": "no premium in signal"}
    if p < MIN_PREMIUM:
        return {"ok": False, "reason": f"premium ${p:.2f} below tradeable floor — slippage kills copy"}
    rp = risk_pct if risk_pct is not None else RISK_PCT
    if rp <= 0 and not force_contracts:
        return {"ok": False, "reason": "paper / zero-size provider — tracking only, no live risk"}
    ds = dynamic_stop(sig, q, ind, rv, now_min)          # live vol-adjusted stop (flat 30% if no live data)
    stop_pct = ds["stop_pct"] / 100.0
    # RISK PER CONTRACT = what you actually lose in the worst realistic case: the stop distance for a
    # normal trade (the stop holds), but the FULL premium for a lotto / far-OTM low-delta contract that
    # can gap straight through the stop. This is the calculated part — you set the account-% to risk, and
    # contracts follow from premium + real risk. (No live Greeks ≠ gap risk — a normal stop still holds.)
    _dl = abs((q or {}).get("delta") or 0)
    gap_risk = bool(sig.get("lotto")) or (0 < _dl < 0.20)
    risk_frac = max(0.15, min(1.0 if gap_risk else stop_pct, 1.0))
    risk_contracts = int((ACCOUNT * rp / 100) / (p * 100 * risk_frac))
    notional_cap = int((ACCOUNT * MAX_NOTIONAL_PCT) / (p * 100))   # -100%-survival cap
    contracts = force_contracts if force_contracts else min(risk_contracts, notional_cap)
    # small-account floor: a LIVE provider (rp>0) always gets at least 1 lot if it survives the
    # notional cap — the multiplier then decides who gets MORE than one, not who gets zero.
    floored = False
    if contracts < 1 and notional_cap >= 1:
        contracts, floored = 1, True
    if contracts < 1:
        return {"ok": False, "reason": f"too expensive — 1 lot exceeds {MAX_NOTIONAL_PCT*100:.0f}% notional cap"}
    capped = risk_contracts > contracts
    risk = contracts * p * 100 * risk_frac
    sold = round(contracts * 2 / 3)
    # per-provider exit config (ML-fit) takes precedence over the conviction-based defaults
    px = provider_exit(sig.get("provider"))
    if px:                                               # scale/trail per-provider; STOP is now dynamic
        tp1_pct = px.get("scale_pct", 35) / 100.0
        trail_pct = px.get("trail_pct", 25) / 100.0
        tp2_pct = tp1_pct + 0.35
        follow_exits = px.get("follow_exits", False)
    else:
        tp1_pct, tp2_pct, trail_pct, _ = exit_plan(conviction)
        follow_exits = False
    tp3_pct = tp2_pct + 0.40                             # T3 — the runner / moonshot target
    return {"ok": True, "contracts": contracts, "capital": round(contracts * p * 100),
            "risk": round(risk), "risk_pct": round(risk / ACCOUNT * 100, 2), "sized_at": rp,
            "risk_R": round(risk / (ACCOUNT / 100), 2), "notional_capped": capped, "min_position": floored,
            "risk_frac": round(risk_frac, 2),
            "risk_basis": ("full premium (lotto/gap)" if gap_risk else f"to −{ds['stop_pct']}% stop"),
            "entry": round(p, 2), "stop": ds["stop"],
            "tp1": round(p * (1 + tp1_pct), 2), "tp2": round(p * (1 + tp2_pct), 2), "tp3": round(p * (1 + tp3_pct), 2),
            "tp1_pct": round(tp1_pct * 100), "tp2_pct": round(tp2_pct * 100), "tp3_pct": round(tp3_pct * 100),
            "trail_pct": round(trail_pct * 100), "stop_pct": ds["stop_pct"],
            "stop_dynamic": not ds["fallback"], "stop_reliable": ds["reliable"], "stop_terms": ds.get("terms"),
            "follow_exits": follow_exits,
            "sold": sold, "runner": contracts - sold, "account": ACCOUNT}


def order_ticket(opt, prov_entry, stop_pct=30, tp1_pct=0.35, tp2_pct=0.70, sold_frac=0.667, tp3_pct=1.10):
    """The exact order to key into the broker, priced off the LIVE market (not the provider's stale
    number). Buying a debit option: recommend a LIMIT just above mid (fills on a fast tape without
    paying the full ask), then a premium STOP and two TARGETS off that fill, plus reward:risk.
    Goal: don't overpay the spread, don't chase, and know your exit before you click — win more, lose less."""
    if not opt or opt.get("error"):
        return None
    bid, ask, mid = opt.get("bid"), opt.get("ask"), opt.get("mid")
    if mid is None:
        mid = prov_entry
    if not mid or mid <= 0:
        return None
    spread = (ask - bid) if (bid is not None and ask is not None and ask >= bid) else None
    spread_pct = round(spread / mid * 100, 1) if (spread and mid) else opt.get("spread_pct")
    tick = 0.05 if mid >= 3 else 0.01                       # standard option tick sizes
    rnd = lambda x: round(round(x / tick) * tick, 2)
    half = (spread / 2) if spread else 0.0
    limit = mid + 0.30 * half                               # nudge just over mid to fill on movers
    if ask is not None:
        limit = min(limit, ask)                             # never bid above the ask
    limit = max(rnd(limit), 0.01)
    sp = (stop_pct or 30) / 100.0
    stop = rnd(limit * (1 - sp))
    t1 = rnd(limit * (1 + (tp1_pct or 0.35)))
    t2 = rnd(limit * (1 + (tp2_pct or 0.70)))
    t3 = rnd(limit * (1 + (tp3_pct or 1.10)))
    risk = limit - stop                                    # $ risked per contract to the stop
    rr1 = round((t1 - limit) / risk, 2) if risk > 0 else None   # reward:risk to the scale target
    rr2 = round((t2 - limit) / risk, 2) if risk > 0 else None   # reward:risk to the mid/runner target
    rr3 = round((t3 - limit) / risk, 2) if risk > 0 else None   # reward:risk to the moonshot target
    chasing = round((ask / prov_entry - 1) * 100) if (prov_entry and ask and ask > prov_entry) else None
    return {"bid": bid, "ask": ask, "mid": mid, "limit": limit, "spread_pct": spread_pct,
            "stop": stop, "stop_pct": round(sp * 100), "t1": t1, "t1_pct": round((tp1_pct or 0.35) * 100),
            "t2": t2, "t2_pct": round((tp2_pct or 0.70) * 100), "t3": t3, "t3_pct": round((tp3_pct or 1.10) * 100),
            "rr1": rr1, "rr2": rr2, "rr3": rr3,
            "prov_entry": prov_entry, "chasing_pct": chasing if (chasing and chasing >= 12) else None,
            "wide": bool(spread_pct and spread_pct > 15)}


def setup_grade(ticket, tier_key, lotto, chase_pct=0, delta=None, zero_dte=False):
    """PURE EXECUTION quality A–D — how cheaply and cleanly you can express this bet with CONTROLLED
    risk, NOT how likely it is to win (direction is a coin flip). Built only from causal, mechanical
    costs: liquidity (slippage in AND out), buy-zone (not chasing), risk-clarity (a definable-risk
    contract, not a far-OTM lotto that gaps through its stop). 0DTE is penalized for theta risk.

    Deliberately does NOT reward R:R: high R:R on an option ticket just means a far-OTM strike — the
    exact setups that round-trip — so the OLD grade rewarded the worst trades (it graded A worse than D
    on realized R). Provider trust is a minor nudge here; it already drives the base score + sizing.

    The size tilt is TAMED (A 1.25× … D 0.5×, was 1.5×…0.3×): the grade hasn't yet EARNED trust on the
    calibration panel, so it can't over-size an unproven bet. Widen it back once buckets separate."""
    t = ticket or {}
    sp, mid = t.get("spread_pct"), t.get("mid")
    ch = chase_pct or 0
    # liquidity — the biggest execution cost (slippage both ways). 0–35
    liq = 6 if (mid is not None and mid < 0.15) else 18 if sp is None else \
        35 if sp <= 6 else 25 if sp <= 10 else 12 if sp <= 18 else 4
    # buy-zone — paying near the provider's price, not extended. 0–30
    buy = 30 if ch <= 5 else 22 if ch <= 12 else 12 if ch <= 20 else 4
    # risk-clarity — definable-risk contract, not a far-OTM lotto that gaps its stop. 0–25
    ad = abs(delta) if delta is not None else None
    risk = 2 if lotto else 14 if ad is None else \
        4 if ad < 0.15 else 12 if ad < 0.30 else 25 if ad <= 0.70 else 16
    # provider quality — minor nudge only (already in the base score). 0–10
    prov = {"HIGH": 10, "MEDIUM": 7, "LOW": 4}.get(tier_key, 2)
    sc = max(0, min(100, liq + buy + risk + prov - (6 if zero_dte else 0)))
    g = "A" if sc >= 80 else "B" if sc >= 64 else "C" if sc >= 46 else "D"
    mult = {"A": 1.25, "B": 1.0, "C": 0.75, "D": 0.5}[g]    # tamed tilt (hard risk caps still apply)
    return {"g": g, "score": sc, "mult": mult, "suggest_lots": {"A": 2, "B": 1, "C": 1, "D": 1}[g]}


_ENTRY_VERB = re.compile(r'\b(took|taking|buy(?:ing)?|bought|brought|add(?:ing|ed)?|grabb?ed|'
                         r'swinging|entered|entering|entry|re-?enter|got in|i(?:\'| a)m in)\b', re.I)
# a FIRM fill confirmation — the provider states they are actually in at a price
_FILL_VERB = re.compile(r'\b(?:in|got in|i(?:\'| a)?m in)\s*(?:@|at)\s*\$?\d'
                        r'|\b(?:filled|entered|took it)\s*(?:@|at)?\s*\$?\d'
                        r'|(?:@|\bat)\s*\$?\d.{0,12}\bfill(?:ed)?\b', re.I)
# WATCH intent — flagged/eyeing a setup but NOT in yet ("Watch $X", "might be a play", "may pivot", "Watching")
_WATCH_RE = re.compile(r'\b(watch(?:ing|list)?|on watch|keep(?:ing)? an eye|eye ?ing|on (?:my )?radar'
                       r'|might be a play|could be a play|may(?:be)? a play|watch for'
                       r'|may(?: need)? (?:to )?pivot|might pivot|thinking about|considering)\b', re.I)


def is_watch(text):
    """A setup the provider is WATCHING (not filled). Watch language present, no firm entry/fill verb."""
    t = text or ""
    return bool(_WATCH_RE.search(t)) and not _ENTRY_VERB.search(t) and not _FILL_VERB.search(t)


def analyze(text, seg, provider=None):
    """Full structured payload for the dashboard/server. A CARD is only created for a real ENTRY —
    i.e. it has an entry PRICE or explicit entry intent. Bare ticker+strike mentions (commentary,
    watchlists, 'SPY 750 call looking strong') are NOT signals and get no card."""
    sig = parse_signal(text, provider=provider)
    if not sig or not sig.get("strike"):
        return None
    if sig.get("premium") is None and not _ENTRY_VERB.search(text or ""):
        return None                                          # no price + no entry verb -> commentary, not a signal
    if provider:
        sig["provider"] = provider
    score, tier, reasons, ev = confidence(sig, seg)
    key = "HIGH" if score >= 75 else "MEDIUM" if score >= 60 else "LOW" if score >= 45 else "SKIP"
    mult = provider_size_mult(sig.get("provider"))       # data-driven per-provider risk multiplier
    override = provider_manual_override(sig.get("provider"))   # paper provider you may still take, capped
    conv = conviction_factor(score - trust_of(sig.get("provider")))
    rp = round(size_pct(score, sig["lotto"], mult, sig.get("premium"),
                        base_trust=trust_of(sig.get("provider"))) * THROTTLE
               * provider_edge_mult(sig.get("provider")), 3)   # calibration-gated tilt (bleeders shrink)
    plan = plan_data(sig, rp, conviction=conv)
    if mult == 0 and override and not plan.get("ok") and sig.get("premium"):
        plan = plan_data(sig, conviction=conv, force_contracts=override)   # 1-lot discretionary plan
        plan["manual"] = True
    watch = is_watch(text)                               # provider is EYEING this, not filled yet
    sig["watch"] = watch
    return {"sig": sig, "score": score, "tier": tier, "tier_key": key,
            "reasons": reasons, "evidence": ev, "behavior": provider_behavior(sig.get("provider")),
            "size_mult": mult, "paper": mult == 0, "manual_override": override, "conviction": conv,
            "plan": plan, "watch": watch}


def main():
    s = " ".join(sys.argv[1:]) or "sulker: IWM 298P 0DTE @0.64"
    seg = load_segments()
    sig = parse_signal(s)
    if not sig:
        print("Could not parse a signal from:", s); return
    score, tier, reasons, ev = confidence(sig, seg)
    print("=" * 60)
    print(f"SIGNAL: {sig['ticker']} {sig['strike']}{sig['type']}"
          f"{'  '+str(sig['dte'])+'DTE' if sig['dte'] is not None else ''}"
          f"{'  @$'+format(sig['premium'],'.2f') if sig['premium'] else ''}"
          f"{'  [LOTTO]' if sig['lotto'] else ''}   provider={sig['provider'] or 'unknown'}")
    print("=" * 60)
    print(f"CONFIDENCE: {score}/100  →  {tier}")
    print("  " + " · ".join(reasons))
    if ev: print("  evidence: " + ev)
    print("-" * 60)
    print("TRADE PLAN  (account ${:.0f}, {}% risk):".format(ACCOUNT, RISK_PCT))
    print(plan(sig))
    print("-" * 60)
    print("  Log it:  python3 tracker.py {} take/close/skip ...".format(sig['provider'] or '<ch>'))


if __name__ == "__main__":
    main()

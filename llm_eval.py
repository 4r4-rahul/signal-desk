#!/usr/bin/env python3
"""
GOLDEN-SET EVALUATOR for the desk's parsing layer.

Without this, "the parser got better" is a feeling. With it, it's a number that moves.

WHAT THE GOLDEN SET IS MADE OF (and how honest each label is)
  card_corroborated  A message the desk carded, where the LIVE OPTION QUOTE for the contract we
                     resolved came back within 3x of the premium the provider stated. That is
                     ground truth from a source that is not the parser: the wrong strike or the
                     wrong expiry does not quote near the right price. Strongest label we have.
  card               A message the desk carded with no usable live quote (after hours, no IBKR).
                     Weaker: it is a REGRESSION anchor (a change that moves it must be
                     deliberate), not independent proof the parse was right.
  noise              A message the desk saw and did NOT card — management follow-ups, chatter,
                     links, recaps. Label = "no new contract". These are what measure
                     FABRICATION, and they are the majority of real traffic.
  hand               Hand-labelled hard cases, including the SMCI pair this whole layer exists
                     for. Small, and deliberately adversarial.

THE TWO ERROR CLASSES, AND WHY THEY ARE NOT WEIGHTED THE SAME
  FABRICATION  the parser names a contract the provider never named (the SMCI 31C). This puts a
               fake trade on a live desk in front of someone who is about to key it into
               tastytrade. Weight 10.
  MISS         the parser fails to produce a real entry. The safety net pings, a human glances,
               worst case a trade is skipped. Weight 1.
  A parser that asks instead of inventing scores well here. That is the intent.

USAGE
  python3 llm_eval.py build                 # (re)build eval/golden_parses.jsonl from desk logs
  python3 llm_eval.py context-stats         # measure the conversational window justification
  python3 llm_eval.py run                   # regex only — free, no API calls
  python3 llm_eval.py run --llm [--limit N] # hybrid: regex + LLM on triggered cases only
  python3 llm_eval.py cost                  # projected calls/day + $/day from real volume

Read-only against the live desk. Never posts to the running server.
"""
import argparse, ast, json, os, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
GOLD = ROOT / "eval" / "golden_parses.jsonl"
INBOUND = ROOT / "logs" / "inbound.log"
CHANNELS = ROOT / "channels"

W_FAB, W_MISS = 10, 1                     # cost weights: a fabrication is ~10 misses

# --------------------------------------------------------------------- log parsing

_LINE = re.compile(r"^(?P<ts>\S+)\s+\[(?P<prov>[^|]*)\|(?P<chan>[^\]]*)\]\s+"
                   r"(?P<outcome>\S+)\s+(?P<text>.*)$")


def _ts(s):
    try:
        if "T" in s:
            from datetime import datetime
            return datetime.fromisoformat(s).timestamp()
        h, m, sec = s.split(":")
        return float(h) * 3600 + float(m) * 60 + float(sec)      # legacy HH:MM:SS lines
    except Exception:
        return 0.0


def read_inbound():
    """Every inbound message the server actually saw, in order, with what happened to it."""
    rows = []
    for ln in INBOUND.read_text(errors="replace").splitlines():
        m = _LINE.match(ln)
        if not m:
            continue
        try:
            text = ast.literal_eval(m.group("text"))
        except Exception:
            continue
        if not isinstance(text, str):
            continue
        rows.append({"ts": _ts(m.group("ts")), "provider": m.group("prov").strip(),
                     "channel": m.group("chan").strip(), "outcome": m.group("outcome"),
                     "text": text})
    return rows


def read_cards():
    """Archived cards -> {normalized raw text: label}. This is what the desk decided, plus the
    live quote it decided it against."""
    out = {}
    for f in sorted(CHANNELS.glob("*/live_signals.jsonl")):
        for ln in f.read_text(errors="replace").splitlines():
            try:
                c = json.loads(ln)
            except Exception:
                continue
            sig = c.get("sig") or {}
            if not sig.get("ticker") or not sig.get("strike"):
                continue
            mid = (c.get("opt") or {}).get("mid")
            prem = sig.get("premium")
            corroborated = bool(mid and prem and (mid <= prem * 3) and (mid * 3 >= prem)
                                and not c.get("price_mismatch"))
            out[_norm(c.get("raw"))] = {
                "provider": c.get("provider"), "ticker": sig["ticker"],
                "strike": _f(sig["strike"]), "right": sig.get("type"),
                "expiry": sig.get("expiry"), "premium": _f(prem),
                "intent": "WATCH" if c.get("watch") else "ENTRY",
                "_source": "card_corroborated" if corroborated else "card",
                "_mid": mid,
            }
    return out


def _norm(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()[:110]


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- hand-labelled cases
# The adversarial core. Every one of these is a real observed failure or a near-miss shape.

HAND = [
    {
        "id": "hand-smci-1", "provider": "prince_small",
        "context": [],
        "text": "Watching SMCI 25 calls\nJuly 31\nMay even go this week expiry\n"
                "First price target would be that 25.66 area off the 1H @everyone",
        "label": {"ticker": "SMCI", "strike": 25.0, "right": "C", "expiry": "Jul 31",
                  "premium": None, "intent": "WATCH"},
        "note": "25.66 is an UNDERLYING target, not a premium and not a strike.",
    },
    {
        "id": "hand-smci-2", "provider": "prince_small",
        "context": ["Watching SMCI 25 calls\nJuly 31\nMay even go this week expiry\n"
                    "First price target would be that 25.66 area off the 1H @everyone"],
        "text": "In SMCI july 31\n1.67 per contract @everyone",
        "label": {"ticker": "SMCI", "strike": 25.0, "right": "C", "expiry": "Jul 31",
                  "premium": 1.67, "intent": "ENTRY"},
        "note": "THE case. No strike in this message. 31 is the day of the month. "
                "Recoverable only from the prior message.",
    },
    {
        "id": "hand-smci-2-nocontext", "provider": "prince_small",
        "context": [],
        "text": "In SMCI july 31\n1.67 per contract @everyone",
        "label": {"ticker": "SMCI", "strike": None, "right": None, "expiry": "Jul 31",
                  "premium": 1.67, "intent": "ENTRY"},
        "note": "Same message with the context REMOVED. The only correct answer is now "
                "'I don't know the strike'. Any strike here is a fabrication.",
    },
    {
        "id": "hand-date-strike", "provider": "prince",
        "context": [],
        "text": "In $AMD $73c July 8 @1.81\n\ns/l 1.29 @everyone",
        "label": {"ticker": "AMD", "strike": 73.0, "right": "C", "expiry": "Jul 8",
                  "premium": 1.81, "intent": "ENTRY"},
        "note": "Both a date and a strike present — must not swap them.",
    },
    {
        "id": "hand-target-not-premium", "provider": "prince_small",
        "context": [],
        "text": "Adding $CELH $33c 1.25 entry (SET A LIMIT ORDER THE SPREAD WIDE( 1.00 stop limit "
                "July 17 expiry PRICE TARGET $36.02 @everyone",
        "label": {"ticker": "CELH", "strike": 33.0, "right": "C", "expiry": "Jul 17",
                  "premium": 1.25, "intent": "ENTRY"},
        "note": "36.02 is an underlying target; 1.00 is a stop. Neither is the premium.",
    },
    {
        "id": "hand-mgmt-no-contract", "provider": "prince",
        "context": ["In $AMD $73c July 8 @1.81"],
        "text": "out half here 2.40, moving stop to break even",
        "label": {"ticker": None, "strike": None, "right": None, "expiry": None,
                  "premium": None, "intent": "MANAGEMENT"},
        "note": "Management of an existing position. Producing a NEW contract here is a "
                "fabrication even though the context names one.",
    },
    {
        "id": "hand-commentary", "provider": "prince",
        "context": ["Watching SMCI 25 calls July 31"],
        "text": "market looks heavy into the close, be careful out there",
        "label": {"ticker": None, "strike": None, "right": None, "expiry": None,
                  "premium": None, "intent": "COMMENTARY"},
        "note": "Context contains a contract. Chatter must not inherit it.",
    },
    {
        "id": "hand-watch-price", "provider": "prince_small",
        "context": [],
        "text": "$NVDA 200c july 25 looks nice here, 2.10 entry if it breaks 198",
        "label": {"ticker": "NVDA", "strike": 200.0, "right": "C", "expiry": "Jul 25",
                  "premium": 2.10, "intent": "WATCH"},
        "note": "'entry' as a NOUN + 'if it breaks' = a planned price while watching, not a fill.",
    },
    {
        "id": "hand-put", "provider": "sulker",
        "context": [],
        "text": "IWM 298P 0DTE @0.64",
        "label": {"ticker": "IWM", "strike": 298.0, "right": "P", "expiry": "0DTE",
                  "premium": 0.64, "intent": "ENTRY"},
    },
    {
        "id": "hand-bare-mike", "provider": "mike",
        "context": [],
        "text": "6815P 1.40",
        "label": {"ticker": "SPX", "strike": 6815.0, "right": "P", "expiry": None,
                  "premium": 1.40, "intent": "ENTRY"},
        "note": "Mike's bare 4-digit shorthand = implicit SPX.",
    },
]


# --------------------------------------------------------------------- build

# ------------------------------------------------------------------ label corrections
# A logged outcome is a snapshot of what the parser did THAT DAY. Where the parser has since
# improved, the old outcome is a stale label and scoring against it punishes the fix. Each entry
# below was read by hand and carries its reason. Keyed by the start of the normalized text.
#   "contract"    -> a real trade IS here; the old REJECT/MGMT was the parser being wrong then
#   "unlabelable" -> excluded; I could not determine the truth, so I refuse to assert one
CORRECTIONS = [
    ("watching hims 37 calls", "contract",
     "real WATCH on HIMS 37C; the desk misrouted it to the management classifier"),
    ("tsla 417.5 .35", "contract", "mike shorthand entry; parser now reads it, log predates that"),
    ("meta 675 2.5", "contract", "mike shorthand entry; parser now reads it"),
    ("nvda 0dte 197.5c 0.47s", "contract", "real entry; the '.47s' cents shorthand now parses"),
    ("@everyone nvda 200 at 0.10", "contract", "real lotto entry; 'STRIKE at PREMIUM' now parses"),
    # --- anaphoric fills: the prior message names ONE contract, this one supplies the fill.
    # These are the SMCI shape. The desk dropped them, so the log says "no contract" — but the
    # trade is unambiguously identifiable by reading two messages, which is the whole point.
    ("googl pulling back now", "contract",
     "prior message is 'Watch $GOOGL $385c July 10'; this one is 'IN AT 0.93' — a real fill"),
    ("getting in pltr here @2.03", "contract",
     "SECOND real instance of the SMCI bug, straight out of inbound.log: 10:14 'Watching $PLTR "
     "$134 calls ... entry around 2.05' CARDed as a watch, 10:29 'Getting in PLTR here @2.03' "
     "REJECTed, 10:33 'Up 10% on $PLTR calls' MGMT. The desk carded the watch and then threw "
     "away the actual fill. _apply_entry_fill never ran because manage.classify() did not "
     "recognise 'Getting in ... here @2.03' as ENTRY_FILL"),
    ("in 1 contract at 1.91", "contract",
     "prior message is 'Watching NVDA 205 calls expiring today. 1.35 entry' — a real fill"),
    ("bid filled on mu call @ 2.2", "contract",
     "prior message is literally 'mu 1000c 2.75 taking one' — the provider really did write "
     "1000c. Reporting what they said is correct; the market-corroboration gate is what "
     "rejects an implausible contract, not the parser"),
    # --- genuinely ambiguous: the ONLY correct answer is to ask
    ("added 1.05", "must_ask",
     "context holds TWO live positions (NBIS 235C and BABA 110C). 'added 1.05' names neither. "
     "Confidently picking one attaches a fill to possibly the wrong position"),
    ("spcx 160c", "unlabelable",
     "'spcx' is not a known symbol. The typo-tolerant resolver maps it to SPX, but SPX 160C at "
     "$0.55 is absurd (SPX ~6800), so SPX is probably wrong too. I cannot say what was meant, so "
     "this is excluded rather than scored either way. NOTE: the price-mismatch guard rejects it."),
    ("spcx 150c", "unlabelable", "same as spcx 160c"),
]


def _correction(norm_text):
    for prefix, kind, why in CORRECTIONS:
        if norm_text.startswith(prefix):
            return kind, why
    return None, None


NO_CONTRACT = {"REJECT", "MGMT", "DISABLED"}          # desk routed it as "no NEW contract here"
HAS_CONTRACT = {"CARD", "REPOST→SKIP", "EDIT→REPRICE", "FILL→ENTERED"}


def build():
    """Label from the desk's OWN LOGGED ROUTING DECISION, not from whether an archive row
    happened to survive. logs/inbound.log records RECV plus the outcome for every message; the
    archive (channels/*/live_signals.jsonl) is incomplete (rotated), so using archive-presence
    as the label marks real carded entries as noise. Ask the log what happened; ask the archive
    only for the field VALUES."""
    rows = read_inbound()
    cards = read_cards()
    by_prov = defaultdict(list)
    for r in rows:
        by_prov[r["provider"]].append(r)

    out, seen = [], set()
    for prov, msgs in by_prov.items():
        msgs.sort(key=lambda m: m["ts"])
        recvs = [m for m in msgs if m["outcome"] == "RECV"]
        # outcome for a message = the non-RECV line with the same text nearest in time
        outcome_of = defaultdict(list)
        for m in msgs:
            if m["outcome"] != "RECV":
                outcome_of[_norm(m["text"])].append(m["outcome"])
        for i, m in enumerate(recvs):
            key = _norm(m["text"])
            if not key or key in seen:
                continue
            seen.add(key)
            outs = set(outcome_of.get(key) or [])
            ctx = [x["text"] for x in recvs[max(0, i - 6):i]][-6:]
            lab = cards.get(key)
            fix, why = _correction(key)
            if fix == "unlabelable":
                continue
            if fix == "must_ask":
                out.append({"id": f"{prov}-{i}", "provider": prov, "context": ctx,
                            "text": m["text"], "source": "must_ask", "note": why,
                            "truncated": len(m["text"]) >= 119,
                            "label": {"ticker": None, "strike": None, "right": None,
                                      "expiry": None, "premium": None, "intent": "ENTRY",
                                      "_contract_present": True, "_must_ask": True}})
                continue
            if fix == "contract" and not lab:
                out.append({"id": f"{prov}-{i}", "provider": prov, "context": ctx,
                            "text": m["text"], "source": "corrected", "note": why,
                            "truncated": len(m["text"]) >= 119,
                            "label": {"ticker": None, "strike": None, "right": None,
                                      "expiry": None, "premium": None, "intent": "ENTRY",
                                      "_contract_present": True}})
                continue
            if lab:                                   # archived card -> real field values
                label = {k: lab[k] for k in
                         ("ticker", "strike", "right", "expiry", "premium", "intent")}
                source = lab["_source"]
            elif outs & HAS_CONTRACT:
                # the desk carded it, but the archive row is gone. We know a contract IS here;
                # we do NOT know its fields. Scored for MISS only — never for fabrication.
                label = {"ticker": None, "strike": None, "right": None, "expiry": None,
                         "premium": None, "intent": "ENTRY", "_contract_present": True}
                source = "carded_unarchived"
            elif outs & NO_CONTRACT:
                label = {"ticker": None, "strike": None, "right": None, "expiry": None,
                         "premium": None, "intent": None}
                source = "noise"
            else:
                # RECV with NO outcome line. ingest() returns early — without logging — on the
                # dedup / duplicate / repost paths, which are reposts of REAL entries. Labelling
                # these "noise" manufactures fake fabrications (it did exactly that: 'META 675C
                # @ 2.4' scored as an invention). We cannot label them, so we don't.
                continue
            out.append({"id": f"{prov}-{i}", "provider": prov, "context": ctx,
                        "text": m["text"], "label": label, "source": source,
                        # _inlog truncates at 120 chars, so anything after that is invisible to
                        # the parser here but WAS visible when the label was made. Fields are
                        # only scored on untruncated text.
                        "truncated": len(m["text"]) >= 119})

    for h in HAND:
        out.append({"id": h["id"], "provider": h["provider"], "context": h["context"],
                    "text": h["text"], "label": h["label"], "source": "hand",
                    "note": h.get("note")})

    GOLD.parent.mkdir(parents=True, exist_ok=True)
    with GOLD.open("w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    c = Counter(r["source"] for r in out)
    print(f"wrote {len(out)} cases -> {GOLD}")
    for k, v in c.most_common():
        print(f"  {k:20s} {v}")
    print("\nNOTE: inbound.log truncates message text at 120 chars, so long messages are")
    print("evaluated on their first 120 chars only. Hand cases carry full text.")
    return out


def load():
    if not GOLD.exists():
        sys.exit(f"no golden set at {GOLD} — run: python3 llm_eval.py build")
    return [json.loads(l) for l in GOLD.read_text().splitlines() if l.strip()]


# --------------------------------------------------------------------- context stats

def context_stats():
    """Empirical justification for CTX_MESSAGES / CTX_MINUTES."""
    rows = read_inbound()
    by = defaultdict(list)
    for r in rows:
        if r["outcome"] == "RECV":
            by[r["provider"]].append(r["ts"])
    gaps = []
    for prov, ts in by.items():
        ts.sort()
        gaps += [b - a for a, b in zip(ts, ts[1:]) if 0 <= b - a < 86400]
    gaps.sort()
    if not gaps:
        return print("no gap data")
    def pct(p):
        return gaps[min(len(gaps) - 1, int(len(gaps) * p))] / 60.0
    print(f"consecutive same-provider message gaps (n={len(gaps)}):")
    for p in (0.5, 0.75, 0.9, 0.95, 0.99):
        print(f"  p{int(p*100):<3} {pct(p):8.1f} min")
    print(f"\n-> a {90}-minute window covers {sum(1 for g in gaps if g <= 5400)/len(gaps)*100:.1f}% "
          f"of consecutive same-provider gaps.")
    print("-> the referent of an anaphoric entry is the PREVIOUS message, so 6 messages is ~3x "
          "headroom;\n   widening it mostly imports OTHER trades' strikes into context, which is "
          "extra material to fabricate from.")


# --------------------------------------------------------------------- scoring

FIELDS = ("ticker", "strike", "right", "expiry", "premium")


def _contract(d):
    """The identity of a trade: ticker + strike + right. Everything else is detail."""
    if not d or not d.get("ticker") or d.get("strike") is None or not d.get("right"):
        return None
    return (str(d["ticker"]).upper(), round(float(d["strike"]), 2), str(d["right"]).upper())


def _exp_eq(a, b):
    """Expiry compared loosely — 'Jul 31' / '7/31' / '2026-07-31' are the same day."""
    if a is None or b is None:
        return a == b
    def key(x):
        x = str(x).strip().lower()
        if "dte" in x:
            return re.sub(r"\D", "", x) + "dte"
        mon = {m: i for i, m in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
        m = re.search(r"([a-z]{3})[a-z]*\.?\s*(\d{1,2})|(\d{1,2})\s*([a-z]{3})", x)
        if m:
            mm = mon.get(m.group(1) or m.group(4), 0)
            dd = int(m.group(2) or m.group(3))
            return f"{mm:02d}-{dd:02d}"
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", x)
        if m:
            return f"{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(\d{1,2})[/-](\d{1,2})", x)
        if m:
            return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        return x
    return key(a) == key(b)


# How much each label source is allowed to assert.
EXACT = {"card_corroborated", "hand"}    # market-verified or human-verified -> identity scored
WEAK = {"card"}                          # archived, unverified -> a diff proves nothing either way
PRESENCE = {"carded_unarchived", "corrected"}   # "must_ask" handled separately in score()         # we know a contract exists, not which one


def score(cases, preds):
    """preds[i] is the parser's output dict (or None) for cases[i].

    A label is only allowed to accuse the parser of fabricating when the label itself is
    trustworthy. An archived card the market never corroborated is not evidence — the desk's
    own history contains parser bugs (an AAPL card whose 'strike' was the price target), and
    scoring against those would train the harness to defend the old bug."""
    per_field = {f: {"ok": 0, "n": 0} for f in FIELDS}
    fab, miss, correct, asked, unverified, drop_ok = [], [], [], [], [], 0
    by_source = defaultdict(lambda: {"fab": 0, "miss": 0, "ok": 0, "ask": 0, "diff": 0, "n": 0})

    for case, pred in zip(cases, preds):
        lab, src = case["label"], case["source"]
        by_source[src]["n"] += 1
        want, got = _contract(lab), _contract(pred)
        present = want is not None or lab.get("_contract_present")
        asked_here = bool(pred and pred.get("_asked"))

        if lab.get("_must_ask"):
            # The referent is genuinely ambiguous. ASKING is the only correct answer:
            # a confident contract may attach a fill to the wrong position, and silence
            # loses a real event. This label class exists to score the design goal directly.
            if asked_here:
                correct.append(case); by_source[src]["ok"] += 1
            elif got is not None:
                fab.append({"case": case, "got": got, "want": "ASK"}); by_source[src]["fab"] += 1
            else:
                miss.append(case); by_source[src]["miss"] += 1
        elif not present and got is None:
            drop_ok += 1; by_source[src]["ok"] += 1
        elif present and got is None:
            if asked_here:
                asked.append(case); by_source[src]["ask"] += 1
            else:
                miss.append(case); by_source[src]["miss"] += 1
        elif not present and got is not None:
            # produced a contract where the desk logged "no new contract" -> fabrication
            fab.append({"case": case, "got": got}); by_source[src]["fab"] += 1
        elif want is None:                                   # PRESENCE label: any contract is ok
            correct.append(case); by_source[src]["ok"] += 1
        elif got == want:
            correct.append(case); by_source[src]["ok"] += 1
        elif src in EXACT:
            fab.append({"case": case, "got": got, "want": want}); by_source[src]["fab"] += 1
        else:                                                # WEAK: can't tell who is right
            unverified.append({"case": case, "got": got, "want": want})
            by_source[src]["diff"] += 1

        if src in EXACT and not case.get("truncated"):       # trustworthy label AND full text
            for f in FIELDS:
                if lab.get(f) is None and (not pred or pred.get(f) is None):
                    continue
                per_field[f]["n"] += 1
                a, b = lab.get(f), (pred or {}).get(f)
                if f == "expiry":
                    ok = _exp_eq(a, b)
                elif f in ("strike", "premium"):
                    ok = a is not None and b is not None and abs(float(a) - float(b)) < 0.011
                else:
                    ok = (str(a).upper() if a else None) == (str(b).upper() if b else None)
                per_field[f]["ok"] += bool(ok)

    return {"n": len(cases), "fabrications": fab, "misses": miss, "correct": len(correct),
            "asked": len(asked), "true_negatives": drop_ok, "unverified": unverified,
            "per_field": per_field, "weighted_cost": W_FAB * len(fab) + W_MISS * len(miss),
            "by_source": dict(by_source)}


def report(name, r):
    n = r["n"]
    print(f"\n{'='*74}\n{name}   (n={n})\n{'='*74}")
    print(f"  contract right              {r['correct']:5d}")
    print(f"  correctly produced nothing  {r['true_negatives']:5d}")
    print(f"  ASKED instead of guessing   {r['asked']:5d}   (a CONFIRM-ME card — safe)")
    print(f"  unverified diff             {len(r['unverified']):5d}   (weak label; proves nothing)")
    print(f"  MISS  (real entry dropped)  {len(r['misses']):5d}   weight {W_MISS}")
    print(f"  FABRICATION (invented)      {len(r['fabrications']):5d}   weight {W_FAB}   <-- the one that costs money")
    print(f"  WEIGHTED COST               {r['weighted_cost']:5d}   (lower is better)")
    print(f"\n  per-field accuracy (verified labels only — card_corroborated + hand):")
    for f, d in r["per_field"].items():
        pct = 100.0 * d["ok"] / d["n"] if d["n"] else 0.0
        print(f"    {f:9s} {d['ok']:4d}/{d['n']:<4d} {pct:6.1f}%")
    print(f"\n  by label source:")
    for s, d in sorted(r["by_source"].items()):
        print(f"    {s:20s} n={d['n']:<5d} ok={d['ok']:<5d} ask={d['ask']:<4d} "
              f"miss={d['miss']:<4d} FAB={d['fab']}")
    if r["fabrications"]:
        print(f"\n  fabrications (up to 10):")
        for f in r["fabrications"][:10]:
            c = f["case"]
            print(f"    [{c['source']}] {c['text'][:70]!r}")
            print(f"        got {f['got']}  want {f.get('want')}")


# --------------------------------------------------------------------- the parsers

def regex_predict(case):
    import signal_engine as E
    sig = E.parse_signal(case["text"], provider=case["provider"])
    # analyze() adds the commentary gate the live desk applies before carding
    if sig and sig.get("premium") is None and not E._ENTRY_VERB.search(case["text"]):
        sig = None
    if not sig or not sig.get("strike"):
        return {"_asked": False}
    return {"ticker": sig.get("ticker"), "strike": _f(sig.get("strike")),
            "right": sig.get("type"), "expiry": sig.get("expiry"),
            "premium": _f(sig.get("premium")),
            "intent": "WATCH" if E.is_watch(case["text"]) else "ENTRY", "_asked": False}


def hybrid_predict(case, stats):
    """Regex fast path; LLM ONLY when a trigger fires. Mirrors the live ingest decision."""
    import signal_engine as E
    import llm_parse as L

    text, prov = case["text"], case["provider"]
    sig = E.parse_signal(text, provider=prov)
    carded = bool(sig and sig.get("strike") and
                  (sig.get("premium") is not None or E._ENTRY_VERB.search(text)))
    base = regex_predict(case)

    maybe = _maybe_entry_like(text)
    why = L.triggers(text, regex_sig=sig if carded else None, carded=carded,
                     market_mid=None, is_watch=E.is_watch(text), maybe_entry=maybe)
    if not why:
        stats["no_trigger"] += 1
        return base
    stats["triggered"] += 1
    if not stats.get("live"):
        return base                                        # dry run: count triggers only

    window = [{"ts": time.time(), "text": t} for t in (case.get("context") or [])]
    parse = L.call(text, provider=prov, window=window, why=why)
    if parse is None:
        stats["llm_failed"] += 1
        return base                                        # FAIL OPEN -> today's behaviour
    stats["llm_ok"] += 1
    d = L.adjudicate(parse, sig if carded else None)
    stats[f"action_{d['action']}"] += 1

    if d["action"] == "DROP":
        return base if carded else {"_asked": False}
    if d["action"] == "ANNOTATE":
        # the regex card stands and gets a disagreement badge — the human breaks the tie
        return {**base, "_asked": True}
    s = d["sig"]
    pred = {"ticker": s.get("ticker"), "strike": _f(s.get("strike")), "right": s.get("type"),
            "expiry": s.get("expiry"), "premium": _f(s.get("premium")),
            "intent": "WATCH" if s.get("watch") else "ENTRY"}
    if d["action"] == "CONFIRM":
        # A CONFIRM-ME card is NOT an assertion that this is the trade — it is a question.
        # Score it as "asked", which is neither a fabrication nor a miss.
        return {**pred, "_asked": True}
    return {**pred, "_asked": False}


def _maybe_entry_like(text):
    """Mirror of server._maybe_missed_entry (kept local so the harness needs no server import)."""
    low = (text or "").lower()
    if re.search(r"\b(out|off|sold|selling|trim|cut|close|closing|stop|recap|review|"
                 r"watchlist|outlook)\b", low):
        return False
    if "%" in text:
        return False
    return bool(re.search(r"(?<![\d.])(?:\.\d{1,2}|\d{1,4}\.\d{1,2})\b", text)
                and re.search(r"(?<![\d.])\d{2,5}\b", text)
                and re.search(r"[A-Za-z]{2,5}", text))


# --------------------------------------------------------------------- cost

def cost():
    rows = read_inbound()
    days = len({time.strftime("%Y-%m-%d", time.gmtime(r["ts"])) for r in rows if r["ts"] > 1e6})
    days = max(days, 1)
    recv = [r for r in rows if r["outcome"] == "RECV"]
    cases = load()
    stats = Counter()
    stats["live"] = 0
    for c in cases:
        hybrid_predict(c, stats)
    trig = stats["triggered"]
    total = stats["triggered"] + stats["no_trigger"]
    rate = trig / total if total else 0
    per_day = len(recv) / days
    calls_day = per_day * rate
    # measured prompt shape: ~450 in / ~180 out tokens per call at CTX_MESSAGES=6
    tin, tout = 450, 180
    dollars = calls_day * (tin / 1e6 * 1.00 + tout / 1e6 * 5.00)
    print(f"inbound messages          {len(recv)} over {days} log-day(s)  ->  {per_day:.0f}/day")
    print(f"trigger rate              {trig}/{total} = {rate*100:.1f}% of messages")
    print(f"projected LLM calls/day   {calls_day:.0f}   (hard cap {400})")
    print(f"model                     claude-haiku-4-5  ($1.00 / $5.00 per MTok)")
    print(f"projected cost/day        ${dollars:.3f}   (~${dollars*21:.2f}/month of trading days)")
    print(f"latency                   OFF the ingest path entirely; ~1-3s on a worker thread.")
    print(f"                          The card the desk shows at ingest time is unchanged.")


# =====================================================================================
# ROUTING EVALUATION
# =====================================================================================
# The parser eval above asks "did we read the fields right?". This asks the earlier and more
# expensive question: "should this message have touched the board AT ALL?" — because the SMCI
# 32.16C did not come from misreading a strike, it came from routing a P&L update to CARD.
#
# TWO GRANULARITIES, ON PURPOSE:
#   ROUTE level  (CARD / MGMT / REJECT / ASK)  — scoreable against the desk's own logged decision,
#       so it covers all 706 real messages. Weak where the desk itself was wrong, which is exactly
#       why the hand corrections below exist.
#   CLASS level  (ENTRY / WATCH / FILL / ...)  — only scored against HAND labels, because the log
#       records what the desk did, not what the message meant. Asserting a class from a REJECT
#       would be manufacturing ground truth.
#
# THE TWO ERROR CLASSES, weighted as in the parser eval:
#   FABRICATION  routed to CARD when the true class opens nothing.        weight 10
#   MISS         did not route to CARD when the true class opens one.     weight 1
#   ASK is neither. A system that asks when unsure is behaving correctly and must not be punished
#   for it — that is the entire design goal, so the scorer has to be able to express it.

ROUTE_CORPUS = ROOT / "eval" / "routing_corpus.jsonl"            # owned by the parallel agent
ROUTE_INTERIM = ROOT / "eval" / "routing_corpus.interim.jsonl"   # ours, used only until that lands

_MGMT_TO_CLASS = {"TRIM": "TRIM", "SCALE_OUT": "TRIM", "FULL_EXIT": "EXIT", "CUT": "EXIT",
                  "MOVE_STOP": "STOP_MOVE", "PROFIT_UPDATE": "PNL_UPDATE", "ENTRY_FILL": "FILL"}

# ---- HAND-LABELLED ADVERSARIAL CORE ------------------------------------------------------
# Every entry is a REAL message from logs/inbound.log (or the desk's own history), labelled by
# hand. These are the only labels allowed to accuse the router of fabricating.
ROUTE_HAND = [
    # --- THE FABRICATION. Carded as an SMCI 32.16C. 32.16 is a price target; that strike does
    #     not exist. The message reports a position that was already open.
    {"id": "r-smci-303", "provider": "prince_small", "context": [],
     "text": "303% on $SMCI calls. Best overnight trade this month  My price target for today "
             "is $32.16 @everyone",
     "cls": "PNL_UPDATE", "route": "MGMT",
     "note": "THE fabrication. Desk outcome was CARD -> SMCI 32.16C, a strike that does not exist."},
    {"id": "r-pltr-124", "provider": "prince", "context": [],
     "text": "124% ON $PLTR CALLS @everyone", "cls": "PNL_UPDATE", "route": "MGMT",
     "note": "Same shape, no price target -> the desk got this one right. Guards the fix."},
    {"id": "r-pltr-213", "provider": "prince", "context": [],
     "text": "213% on PLTR calls!!! Insane swing trade @everyone", "cls": "PNL_UPDATE",
     "route": "MGMT"},

    # --- THE MISS. Watch carded, fill thrown away.
    {"id": "r-pltr-watch", "provider": "prince", "context": [],
     "text": "Watching $PLTR $134 calls\nLooking for an entry around 2.05\nJuly 10 @everyone \n"
             "Price target $134",
     "cls": "WATCH", "route": "CARD", "contract": {"ticker": "PLTR", "strike": 134.0, "right": "C"}},
    {"id": "r-pltr-fill", "provider": "prince",
     "context": ["Watching $PLTR $134 calls\nLooking for an entry around 2.05\nJuly 10 @everyone \n"
                 "Price target $134"],
     "text": "Getting in PLTR here @2.03 @everyone", "cls": "FILL", "route": "CARD",
     "resolves_to": {"ticker": "PLTR", "strike": 134.0, "right": "C"},
     "book": [{"ticker": "PLTR", "strike": 134.0, "right": "C", "state": "WATCHING"}],
     "note": "THE miss. Desk outcome REJECT. The strike exists only in the previous message."},
    {"id": "r-googl-fill", "provider": "prince",
     "context": ["Watch $GOOGL $385c July 10\n85c per contract @everyone"],
     "text": "GOOGL pulling back now, 1H, 4H and 1D look way better than AAPL\nIN AT 0.93 "
             "@everyone \nLooking at my hourly chart the price",
     "cls": "FILL", "route": "CARD",
     "resolves_to": {"ticker": "GOOGL", "strike": 385.0, "right": "C"},
     "book": [{"ticker": "GOOGL", "strike": 385.0, "right": "C", "state": "WATCHING"}],
     "note": "Second real miss. Desk outcome REJECT."},
    {"id": "r-googl-bare-fill", "provider": "prince",
     "context": ["I'm gonna take a shot at these GOOGL $370c here\n3.40 entry\n2.90 stop limit\n"
                 "$359.59 first price target July 24 expiry"],
     "text": "in @3.40 july 31 expiry", "cls": "FILL", "route": "CARD",
     "resolves_to": {"ticker": "GOOGL", "strike": 370.0, "right": "C"},
     "book": [{"ticker": "GOOGL", "strike": 370.0, "right": "C", "state": "WATCHING"}],
     "note": "The bare-fill shape from the brief. 31 is a day of the month, not a strike."},
    {"id": "r-smci-fill", "provider": "prince_small",
     "context": ["Watching SMCI 25 calls\nJuly 31\nMay even go this week expiry\n"
                 "First price target would be that 25.66 area off the 1H @everyone"],
     "text": "In SMCI july 31\n1.67 per contract @everyone", "cls": "FILL", "route": "CARD",
     "resolves_to": {"ticker": "SMCI", "strike": 25.0, "right": "C"},
     "book": [{"ticker": "SMCI", "strike": 25.0, "right": "C", "state": "WATCHING"}]},

    # --- THE AMBIGUOUS ONE. Two live positions, the message names neither.
    {"id": "r-added-105", "provider": "mike",
     "context": ["NBIS 235C @ 1.8 @Options Day Trade Alerts  starter",
                 "BABA 110C @ 0.95 @Options Day Trade Alerts"],
     "text": "added 1.05 @Options Day Trade Alerts", "cls": "MUST_ASK", "route": "ASK",
     "book": [{"ticker": "NBIS", "strike": 235.0, "right": "C", "state": "OPEN"},
              {"ticker": "BABA", "strike": 110.0, "right": "C", "state": "OPEN"}],
     "note": "Two open positions. Picking one attaches a fill to possibly the wrong trade."},
    {"id": "r-added-105-single", "provider": "mike",
     "context": ["NBIS 235C @ 1.8 @Options Day Trade Alerts  starter"],
     "text": "added 1.05 @Options Day Trade Alerts", "cls": "ADD", "route": "MGMT",
     "resolves_to": {"ticker": "NBIS", "strike": 235.0, "right": "C"},
     "book": [{"ticker": "NBIS", "strike": 235.0, "right": "C", "state": "OPEN"}],
     "note": "SAME message, ONE open position -> now resolvable. Tests that the book, not the "
             "model, is what decides."},

    # --- clean entries the prefilter should own outright (no LLM call, no risk)
    {"id": "r-clean-qqq", "provider": "optionking", "context": [],
     "text": "@everyone SPY 750C at 0.80 0DTE SL 30%.", "cls": "ENTRY", "route": "CARD",
     "contract": {"ticker": "SPY", "strike": 750.0, "right": "C"}},
    {"id": "r-clean-iwm", "provider": "sulker", "context": [],
     "text": "IWM 298P 0DTE @0.64", "cls": "ENTRY", "route": "CARD",
     "contract": {"ticker": "IWM", "strike": 298.0, "right": "P"}},
    {"id": "r-clean-nbis", "provider": "mike", "context": [],
     "text": "NBIS 235C @ 1.8 @Options Day Trade Alerts  starter", "cls": "ENTRY", "route": "CARD",
     "contract": {"ticker": "NBIS", "strike": 235.0, "right": "C"}},

    # --- management
    {"id": "r-trim-half", "provider": "prince",
     "context": ["In $AMD $73c July 8 @1.81"],
     "text": "out half here 2.40, moving stop to break even", "cls": "TRIM", "route": "MGMT",
     "book": [{"ticker": "AMD", "strike": 73.0, "right": "C", "state": "OPEN"}]},
    {"id": "r-trim-nbis", "provider": "mike",
     "context": ["NBIS 235C @ 1.8 @Options Day Trade Alerts  starter"],
     "text": "NBIS 130% 1/2 out @Options Day Trade Alerts", "cls": "TRIM", "route": "MGMT",
     "book": [{"ticker": "NBIS", "strike": 235.0, "right": "C", "state": "OPEN"}]},
    {"id": "r-exit-googl", "provider": "prince_small",
     "context": ["Watch $GOOGL $385c July 10\n85c per contract @everyone"],
     "text": "Sold GOOGL calls as .31 @everyone will see how the next hour plays out before "
             "anything else", "cls": "EXIT", "route": "MGMT",
     "book": [{"ticker": "GOOGL", "strike": 385.0, "right": "C", "state": "OPEN"}]},
    {"id": "r-pnl-up40", "provider": "prince", "context": [],
     "text": "Up 40% on PLTR calls @everyone", "cls": "PNL_UPDATE", "route": "MGMT"},
    {"id": "r-pnl-down", "provider": "prince", "context": [],
     "text": "Down 20% on GOOGL. Still holding for now. I was down on PLTR yesterday and it blew, "
             "continuing into today. So I’ll let t", "cls": "PNL_UPDATE", "route": "MGMT"},

    # --- commentary / noise. Context deliberately contains a contract: chatter must not inherit it.
    {"id": "r-commentary", "provider": "prince",
     "context": ["Watching SMCI 25 calls July 31"],
     "text": "market looks heavy into the close, be careful out there", "cls": "COMMENTARY",
     "route": "REJECT"},
    {"id": "r-commentary-pivot", "provider": "prince",
     "context": ["Watch $GOOGL $385c July 10\n85c per contract @everyone"],
     "text": "GOOGL already blowing, in that case may need to pivot.", "cls": "COMMENTARY",
     "route": "REJECT",
     "note": "The desk CARDED the longer version of this because a strike appeared later in the "
             "sentence. This truncated form opens nothing."},
    {"id": "r-noise-url", "provider": "mike", "context": [],
     "text": "https://discord.com/channels/123/456", "cls": "NOISE", "route": "REJECT"},
    {"id": "r-recap", "provider": "sulker", "context": [],
     "text": "Sulker - Recap 9 July 2026\n\n\nIWM 296P @ 0.60 → 1.02  +70%\n,\n"
             "PLTR 125P (Low Size) @ 1.25 → 0.90  -28%", "cls": "COMMENTARY", "route": "REJECT",
     "note": "A recap names several contracts at prices. Carding any of them would be a "
             "fabrication of a trade that already closed."},
]

# Weak-label corrections for messages whose LOGGED outcome was the desk being wrong.
ROUTE_CORRECTIONS = [
    ("303% on $smci calls", "MGMT", "PNL_UPDATE", "carded as SMCI 32.16C — a strike that does not exist"),
    ("getting in pltr here @2.03", "CARD", "FILL", "real fill of the carded PLTR 134C watch; desk REJECTed it"),
    ("googl pulling back now", "CARD", "FILL", "real fill (IN AT 0.93) of the GOOGL 385C watch; desk REJECTed it"),
    ("added 1.05", "ASK", "MUST_ASK", "two open positions; neither is named"),
]


def _route_correction(norm_text):
    for prefix, route, cls, why in ROUTE_CORRECTIONS:
        if norm_text.startswith(prefix):
            return route, cls, why
    return None, None, None


def route_build():
    """Build the INTERIM routing corpus from real traffic.

    INTERIM, and labelled as such in every row: eval/routing_corpus.jsonl is owned by a parallel
    agent. When that file exists route_load() prefers it and this one is ignored. We build our own
    so the router is never blocked on another agent, not because these labels are better."""
    rows = read_inbound()
    by_prov = defaultdict(list)
    for r in rows:
        by_prov[r["provider"]].append(r)
    try:
        import manage as M
    except Exception:
        M = None

    out, seen = [], set()
    for prov, msgs in by_prov.items():
        if prov in ("SYSTEM", "?", "-"):
            continue
        msgs.sort(key=lambda m: m["ts"])
        recvs = [m for m in msgs if m["outcome"] == "RECV"]
        outcome_of = defaultdict(list)
        for m in msgs:
            if m["outcome"] != "RECV":
                outcome_of[_norm(m["text"])].append(m["outcome"])
        for i, m in enumerate(recvs):
            key = _norm(m["text"])
            if not key or key in seen:
                continue
            seen.add(key)
            outs = set(outcome_of.get(key) or [])
            ctx = [x["text"] for x in recvs[max(0, i - 6):i]][-6:]

            fix_route, fix_cls, why = _route_correction(key)
            if fix_route:
                out.append({"id": f"{prov}-{i}", "provider": prov, "context": ctx,
                            "text": m["text"], "route": fix_route, "cls": fix_cls,
                            "label_source": "corrected", "note": why,
                            "truncated": len(m["text"]) >= 119, "interim": True})
                continue

            if outs & HAS_CONTRACT:
                route, cls = "CARD", None
                src = "desk_card"
            elif "MGMT" in outs:
                route, src = "MGMT", "desk_mgmt"
                cls = _MGMT_TO_CLASS.get((M.classify(m["text"]) or {}).get("type")) if M else None
            elif outs & {"REJECT", "DISABLED"}:
                route, cls, src = "REJECT", None, "desk_reject"
            else:
                continue                      # RECV with no outcome (dedup/repost paths) — unlabelable
            out.append({"id": f"{prov}-{i}", "provider": prov, "context": ctx, "text": m["text"],
                        "route": route, "cls": cls, "label_source": src,
                        "truncated": len(m["text"]) >= 119, "interim": True})

    for h in ROUTE_HAND:
        out.append({**h, "label_source": "hand", "interim": True})

    ROUTE_INTERIM.parent.mkdir(parents=True, exist_ok=True)
    with ROUTE_INTERIM.open("w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    c = Counter(r["label_source"] for r in out)
    print(f"wrote {len(out)} routing cases -> {ROUTE_INTERIM}   (INTERIM — see header)")
    for k, v in c.most_common():
        print(f"  {k:16s} {v}")
    print(f"  route distribution: {dict(Counter(r['route'] for r in out))}")
    return out


def _normalise_case(c):
    """Bring both corpus dialects onto one shape.

    The parallel agent's schema is the authority: {type, should_card, links_to, expect,
    prior_context:[{text}], hard}. Ours (interim) used {route, cls, context:[str]}. The TARGET
    ROUTE is DERIVED from `type` + `should_card` rather than stored, because those two together
    say what the desk must do and a stored route would be a third thing to keep in sync."""
    import llm_route as R
    cls = c.get("type") or c.get("cls")
    ctx = c.get("prior_context")
    if ctx and isinstance(ctx[0], dict):
        ctx = [x.get("text") or "" for x in ctx]
    ctx = ctx if ctx is not None else (c.get("context") or [])
    if c.get("route"):
        route = c["route"]
    elif cls == "MUST_ASK":
        route = R.ROUTE_ASK
    elif c.get("should_card"):
        route = R.ROUTE_CARD
    elif cls == "FILL":
        route = R.ROUTE_FILL
    else:
        route = R._CLASS_ROUTE.get(cls, R.ROUTE_DROP)
    return {**c, "cls": cls, "context": ctx, "route": route,
            "hard": bool(c.get("hard")),
            "label_source": c.get("label_source") or ("hand" if "type" in c else "?"),
            "truncated": bool(c.get("text_truncated_in_log") or c.get("truncated"))}


def route_load():
    """Prefer the parallel agent's corpus the moment it lands; fall back to ours, loudly."""
    for p, tag in ((ROUTE_CORPUS, "corpus"), (ROUTE_INTERIM, "INTERIM corpus")):
        if p.exists():
            rows = [_normalise_case(json.loads(l)) for l in p.read_text().splitlines() if l.strip()]
            hard = sum(1 for r in rows if r["hard"])
            print(f"[{tag}] {p.name}: {len(rows)} cases ({hard} flagged hard)")
            if tag != "corpus":
                print("      ^ eval/routing_corpus.jsonl not present yet; these are our own "
                      "interim labels.")
            return rows, tag
    sys.exit("no routing corpus — run: python3 llm_eval.py route-build")


# ---- prediction ---------------------------------------------------------------------------

def _desk_route(case):
    """What the DETERMINISTIC layer does today, reproduced from server.ingest()'s own order of
    checks. This is the baseline every LLM number below has to beat.

    Not reproduced: ingest()'s dedup sweep (it needs live SIGNALS state). So REPOST and EDIT cases
    score as whatever their text parses to, which OVERSTATES this baseline's over-carding. Called
    out in the report rather than silently excluded."""
    import signal_engine as E
    import manage as M
    text = case["text"]
    res = E.parse_signal(text, provider=case["provider"])
    carded = bool(res and res.get("strike") and
                  (res.get("premium") is not None or E._ENTRY_VERB.search(text)))
    c = M.classify(text)
    if carded and c["type"] not in ("TRIM", "SCALE_OUT", "FULL_EXIT", "CUT"):
        return "CARD", res, c                     # fresh entry wins over the mgmt classifier
    if c["type"] == "ENTRY_FILL":
        return "FILL", res, c                     # -> server._apply_entry_fill
    if c["type"] in ("TRIM", "SCALE_OUT", "FULL_EXIT", "CUT", "MOVE_STOP", "PROFIT_UPDATE"):
        return "MGMT", res, c
    return ("CARD", res, c) if carded else ("REJECT", res, c)


_BY_ID = {}


def _seed_book(case, BOOK, stats=None):
    """Reconstruct the Book state that would exist when this message arrives.

    Two seeding paths, and the split is reported because they are not equally strong:
      `parse`    the antecedent parses deterministically -> the live desk really would have
                 carded it, so the Book really would hold it. This is the honest path.
      `recovery` the antecedent does NOT parse, but the corpus says it should have carded. In
                 production the router/llm_parse promote exactly these (that is failure F4 — the
                 PLTR and SMCI watches), and a promoted card calls ROUTE.observe(), so the Book
                 gets it. Seeding it here models that, and is counted separately so the anaphora
                 number cannot quietly lean on it.
    """
    import signal_engine as E
    prov = case["provider"]
    BOOK._d.pop(prov, None)
    for b in case.get("book") or []:                      # interim/hand cases carry it explicitly
        BOOK.put(prov, b["ticker"], b["strike"], b["right"], b.get("state", "WATCHING"),
                 premium=b.get("premium"), src="test")
    if case.get("book"):
        return
    for entry in (case.get("prior_context") or case.get("context") or []):
        t = entry.get("text") if isinstance(entry, dict) else entry
        pid = entry.get("id") if isinstance(entry, dict) else None
        s = E.parse_signal(t or "", provider=prov)
        if s and s.get("strike") and s.get("ticker"):
            BOOK.put(prov, s["ticker"], s["strike"], s.get("type") or "C",
                     "WATCHING" if E.is_watch(t) else "OPEN",
                     expiry=s.get("expiry"), premium=s.get("premium"), src="test")
            if stats is not None:
                stats["seed_parse"] += 1
            continue
        ante = _BY_ID.get(pid)
        if ante and ante.get("should_card"):
            ex = ante.get("expect") or {}
            if ex.get("ticker") and ex.get("strike") is not None and ex.get("right"):
                BOOK.put(prov, ex["ticker"], ex["strike"], ex["right"],
                         "WATCHING" if ante.get("cls") == "WATCH" else "OPEN",
                         expiry=ex.get("expiry"), premium=ex.get("premium"), src="test")
                if stats is not None:
                    stats["seed_recovery"] += 1


def _needs_llm(case):
    """Does this case reach L2? Mirrors route_predict's gating so the eval can batch the network
    calls without changing the decision path."""
    import llm_route as R
    desk, res, _ = _desk_route(case)
    pre = R.prefilter(case["text"], provider=case["provider"], regex_sig=res,
                      carded=(desk in R.CARD_ROUTES if hasattr(R, "CARD_ROUTES") else desk == "CARD"))
    if not pre:
        return True
    want = R._CLASS_ROUTE.get(pre["cls"], "REJECT")
    card = {"CARD", "FILL"}
    return desk in card and want not in card


ROUTE_CACHE = ROOT / "eval" / ".route_cache.json"     # gitignored scratch; safe to delete


def route_classify_batch(cases, workers=6, use_cache=True):
    """Fire the L2 calls concurrently. HARNESS ONLY — production never does this: ingest() enqueues
    and one worker thread drains, because the desk sees ~35 messages a day. Here it turns a 12
    minute eval into a 2 minute one, which is the difference between a metric you re-run and a
    metric you quote once. classify() is pure (no desk state), so this is safe to parallelise.

    Results are cached on disk by (model, provider, text, context). Re-scoring after a change to
    the SCORER — which is most iterations — then costs nothing, so fixing a metric bug never means
    choosing between paying again and quoting a stale number. Delete eval/.route_cache.json (or
    pass --no-cache) to force fresh calls after changing the PROMPT or the model."""
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    import llm_route as R
    cache = {}
    if use_cache:
        try:
            cache = json.loads(ROUTE_CACHE.read_text())
        except Exception:
            cache = {}

    def ckey(c):
        blob = json.dumps([R.MODEL, c["provider"], c["text"], c.get("context") or []],
                          sort_keys=True)
        return hashlib.md5(blob.encode()).hexdigest()

    idxs = [i for i, c in enumerate(cases) if _needs_llm(c)]
    out, todo = {}, []
    for i in idxs:
        k = ckey(cases[i])
        if k in cache:
            out[i] = cache[k]
        else:
            todo.append(i)
    if cache:
        print(f"    cache: {len(out)} hit / {len(todo)} to call", flush=True)

    def one(i):
        c = cases[i]
        window = [{"ts": time.time(), "text": t} for t in (c.get("context") or [])]
        return i, R.classify(c["text"], provider=c["provider"], window=window)

    done = 0
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, r in ex.map(one, todo):
                out[i] = r
                if r is not None:
                    cache[ckey(cases[i])] = r
                done += 1
                if done % 25 == 0:
                    print(f"    ... {done}/{len(todo)} classified", flush=True)
        try:
            ROUTE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            ROUTE_CACHE.write_text(json.dumps(cache))
        except Exception:
            pass
    return out


def route_predict(case, stats, live=False, precomputed=None, have_pre=False):
    """Full three-layer routing for one case.
    Returns (route, cls, resolved_contract, asked, how)."""
    import llm_route as R
    desk, res, c = _desk_route(case)
    stats["desk_" + desk] += 1

    pre = R.prefilter(case["text"], provider=case["provider"], regex_sig=res,
                      carded=(desk in R.CARD_ROUTES))
    if pre:
        stats["prefiltered"] += 1
        stats["pre_" + pre["why"]] += 1
        want = R._CLASS_ROUTE.get(pre["cls"], "REJECT")
        if not (desk in R.CARD_ROUTES and want not in R.CARD_ROUTES):
            return want, pre["cls"], None, False, "prefilter"
        stats["pre_escalate"] += 1                # disagrees with a card -> still ask the model

    if not live:
        stats["would_call"] += 1
        return desk, None, None, False, "desk"

    _seed_book(case, R.BOOK, stats)
    if have_pre:
        route = precomputed
    else:
        window = [{"ts": time.time(), "text": t} for t in (case.get("context") or [])]
        route = R.classify(case["text"], provider=case["provider"], window=window)
    if route is None:
        stats["llm_failed"] += 1
        return desk, None, None, False, "failopen"
    stats["llm_ok"] += 1
    stats["cls_" + str(route.get("message_class"))] += 1
    for k in ("in", "out"):
        stats["tok_" + k] += (route.get("_usage") or {}).get(k, 0)
    for k in ("cache_read", "cache_write"):
        stats["tok_" + k] += (route.get("_usage") or {}).get(k, 0)

    bv = None
    if (route.get("refers_to_earlier_position") or not route.get("names_new_contract")
            or route.get("ambiguous_referent") or route.get("message_class") == "MUST_ASK"):
        bv = R.BOOK.resolve(case["provider"], case["text"], route.get("message_class"),
                            hint_ticker=route.get("ticker"))
    v = R.adjudicate(route, desk, book_verdict=bv, carded_status=None)
    stats["act_" + v["action"]] += 1

    # Context resolution is measured INDEPENDENTLY of the action taken. Whether the desk already
    # routed a fill to CARD (server._apply_entry_fill handles it) or the router had to promote it,
    # the question "did the referent resolve to the right prior setup?" has the same answer, and
    # scoring it only on the promote path would flatter the number by only counting the cases
    # where we acted.
    resolved = None
    if bv and bv.get("status") == "resolved":
        c = bv["contract"]
        resolved = {"ticker": c["ticker"], "strike": c["strike"], "type": c["right"]}
        stats["ctx_resolved"] += 1
    elif bv and bv.get("status") == "ambiguous":
        stats["ctx_ambiguous"] += 1
    if v["action"] == "PROMOTE":
        resolved = v.get("sig") or resolved

    final = desk
    if v["action"] == "DEMOTE":
        final = v["want"]
    elif v["action"] == "PROMOTE":
        final = "CARD"
    elif v["action"] == "ASK":
        final = "ASK"
    return final, route.get("message_class"), resolved, v["action"] == "ASK", v["action"]


# ---- scoring ------------------------------------------------------------------------------

ROUTES = ["CARD", "FILL", "MGMT", "REJECT", "ASK"]
CARD_ROUTES = {"CARD", "FILL"}          # routes that put or move something on the board


def route_score(cases, preds):
    """preds[i] = (route, cls, resolved_contract, asked, how).

    Error classes follow eval/message_taxonomy.md §8 "Scoring guidance", which weights them very
    differently — so they are counted separately rather than folded into one accuracy number:

      FABRICATION (critical, w=10)  a card minted from a class that opens nothing
                                    (PNL_UPDATE / RECAP / COMMENTARY / NON_PROVIDER / NOISE /
                                    CHAT / QUESTION / WARNING). This is failure F2.
      FALSE CONFIDENCE (high)       a confident card/fill where the corpus says MUST_ASK.
      OVER-CARD (medium)            carded a REPOST/EDIT or a management message. Wasteful and
                                    confusing, but not a contract out of nothing.
      MISS (w=1)                    should_card was true and we produced nothing (F4), or a FILL
                                    with a linkable antecedent was dropped (F3).
      ASK where the corpus is confident — explicitly NOT an error. Taxonomy §8: "costs the
      operator five seconds". Counted so it cannot hide, never charged.
    """
    import llm_route as R
    conf = defaultdict(int)                       # (true_route, pred_route) -> n
    cls_conf = defaultdict(int)
    fab, miss, overcard, false_conf = [], [], [], []
    asked_ok, asked_extra = 0, []
    ctx_ok, ctx_n, ctx_wrong = 0, 0, []
    ask_total = 0
    hard = {"n": 0, "ok": 0}
    pre_n = pre_ok = 0
    pre_bad = []

    for case, (route, cls, resolved, asked, how) in zip(cases, preds):
        want, tcls = case["route"], case.get("cls")
        conf[(want, route)] += 1
        if tcls:
            cls_conf[(tcls, cls or "-")] += 1
        if case["hard"]:
            hard["n"] += 1
            hard["ok"] += (route == want)
        if how == "prefilter":
            # The prefilter answers WITHOUT the model, so its precision is the one number that
            # decides whether skipping the call is safe. Scored separately and reported in full.
            pre_n += 1
            if route == want:
                pre_ok += 1
            else:
                pre_bad.append({"case": case, "want": want, "got": route, "cls": cls})

        want_card, got_card = want in CARD_ROUTES, route in CARD_ROUTES
        if want == "ASK":
            ask_total += 1
        if route == "ASK":
            if want == "ASK":
                asked_ok += 1
            else:
                asked_extra.append({"case": case, "want": want})    # safe, cheap, not an error
        elif want == "ASK" and got_card:
            # MUST be checked BEFORE the generic over-card branch. want=="ASK" makes want_card
            # False, so an earlier version fell into over-card first and FALSE CONFIDENCE always
            # printed 0 — silently zeroing a metric the taxonomy calls first-class.
            false_conf.append({"case": case, "want": want, "got": route, "cls": cls})
        elif got_card and not want_card:
            if tcls in R.NEVER_CARD:
                fab.append({"case": case, "want": want, "got": route, "cls": tcls})
            else:
                overcard.append({"case": case, "want": want, "got": route, "cls": tcls})
        elif want_card and not got_card:
            miss.append({"case": case, "want": want, "got": route, "cls": tcls})

        # --- context resolution. Only scored where the corpus says the message LINKS to an
        #     antecedent AND the contract is not recoverable from this message alone — i.e.
        #     exactly the anaphora the Book exists for.
        exp = case.get("expect") or {}
        if case.get("links_to") and exp.get("strike") is not None and \
                not R._has_fresh_contract_syntax(case["text"]):
            ctx_n += 1
            got_ok = (resolved and str(resolved.get("ticker", "")).upper() == str(exp["ticker"]).upper()
                      and abs(float(resolved.get("strike") or -1) - float(exp["strike"])) < 1e-6
                      and str(resolved.get("type") or resolved.get("right") or "").upper()
                      == str(exp["right"]).upper())
            if got_ok:
                ctx_ok += 1
            else:
                # Split by failure MODE, because they are not equally dangerous: resolving to the
                # WRONG contract corrupts two positions at once (taxonomy §8: high severity);
                # resolving to nothing degrades to an ASK, which costs five seconds.
                ctx_wrong.append({"case": case, "got": resolved, "want": exp,
                                  "mode": "WRONG_CONTRACT" if resolved else "unresolved->ask"})

    per_class = {}
    for r in ROUTES:
        tp = conf[(r, r)]
        fp = sum(conf[(w, r)] for w in ROUTES if w != r)
        fn = sum(conf[(r, g)] for g in ROUTES if g != r)
        per_class[r] = {"tp": tp, "fp": fp, "fn": fn,
                        "prec": tp / (tp + fp) if tp + fp else 0.0,
                        "rec": tp / (tp + fn) if tp + fn else 0.0}
    cls_ok = sum(v for k, v in cls_conf.items() if k[0] == k[1])
    cls_tot = sum(cls_conf.values())
    return {"n": len(cases), "conf": conf, "per_class": per_class, "cls_conf": cls_conf,
            "cls_ok": cls_ok, "cls_tot": cls_tot,
            "fab": fab, "miss": miss, "overcard": overcard, "false_conf": false_conf,
            "asked_ok": asked_ok, "ask_total": ask_total, "asked_extra": asked_extra,
            "ctx_ok": ctx_ok, "ctx_n": ctx_n, "ctx_wrong": ctx_wrong, "hard": hard,
            "pre_n": pre_n, "pre_ok": pre_ok, "pre_bad": pre_bad,
            "weighted_cost": W_FAB * len(fab) + W_MISS * len(miss)}


def route_report(name, r):
    print(f"\n{'='*78}\n{name}   (n={r['n']})\n{'='*78}")
    print(f"  {'route':8s} {'prec':>7s} {'recall':>7s}   {'tp':>4s} {'fp':>4s} {'fn':>4s}")
    for k in ROUTES:
        d = r["per_class"][k]
        print(f"  {k:8s} {d['prec']*100:6.1f}% {d['rec']*100:6.1f}%   "
              f"{d['tp']:4d} {d['fp']:4d} {d['fn']:4d}")
    print(f"\n  confusion (rows = truth, cols = predicted):")
    print("           " + "".join(f"{g:>9s}" for g in ROUTES))
    for w in ROUTES:
        print(f"    {w:7s}" + "".join(f"{r['conf'][(w, g)]:9d}" for g in ROUTES))

    n = max(1, r["n"])
    print(f"\n  --- errors, weighted as in taxonomy §8 ---")
    print(f"  FABRICATION  card minted from a class that opens nothing  {len(r['fab']):4d}"
          f"   w={W_FAB}   <- the one that costs money")
    print(f"  FALSE CONFIDENCE  confident card where truth is MUST_ASK  {len(r['false_conf']):4d}"
          f"   (high)")
    print(f"  OVER-CARD    carded a repost/edit/management message      {len(r['overcard']):4d}"
          f"   (medium)")
    print(f"  MISS         real entry/watch/fill dropped                {len(r['miss']):4d}"
          f"   w={W_MISS}")
    print(f"  WEIGHTED COST (fab*{W_FAB} + miss*{W_MISS})                          "
          f"{r['weighted_cost']:4d}   (lower is better)")
    print(f"\n  MUST_ASK recall  {r['asked_ok']}/{r['ask_total']} = "
          f"{100.0*r['asked_ok']/max(1,r['ask_total']):.1f}%   "
          f"(taxonomy §8: a first-class metric — an engine that never asks is not doing well)")
    print(f"  extra asks       {len(r['asked_extra'])} "
          f"({100.0*len(r['asked_extra'])/n:.1f}% of traffic) — NOT charged; costs the operator "
          f"five seconds each")
    if r["hard"]["n"]:
        print(f"  route accuracy on `hard` cases: {r['hard']['ok']}/{r['hard']['n']} = "
              f"{100.0*r['hard']['ok']/r['hard']['n']:.1f}%")
    if r["cls_tot"]:
        print(f"\n  CLASS accuracy (20-way, all cases): {r['cls_ok']}/{r['cls_tot']} = "
              f"{100.0*r['cls_ok']/r['cls_tot']:.1f}%")
        bad = {k: v for k, v in r["cls_conf"].items() if k[0] != k[1]}
        for (w, g), c in sorted(bad.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    truth {w:13s} -> predicted {g:13s}  x{c}")
    if r["pre_n"]:
        print(f"\n  L0 PREFILTER (answered with NO model call): {r['pre_ok']}/{r['pre_n']} = "
              f"{100.0*r['pre_ok']/r['pre_n']:.1f}% agree with the corpus")
        for b in r["pre_bad"][:8]:
            print(f"    DISAGREE want {b['want']:7s} got {b['got']:7s} ({b['cls']}) "
                  f"{b['case']['text'][:55]!r}")
    if r["ctx_n"]:
        wrong = [w for w in r["ctx_wrong"] if w["mode"] == "WRONG_CONTRACT"]
        print(f"\n  CONTEXT RESOLUTION (anaphoric message -> right antecedent contract): "
              f"{r['ctx_ok']}/{r['ctx_n']} = {100.0*r['ctx_ok']/r['ctx_n']:.1f}%")
        print(f"    resolved to the WRONG contract  {len(wrong):3d}   <- the dangerous failure "
              f"(corrupts two positions)")
        print(f"    could not resolve -> asked      {len(r['ctx_wrong'])-len(wrong):3d}   "
              f"<- degrades to a question, costs five seconds")
        for w in r["ctx_wrong"][:8]:
            print(f"    [{w['mode']:16s}] {w['case']['id']:16s} {w['case']['text'][:40]!r}")
            print(f"       want {w['want'].get('ticker')} {w['want'].get('strike')}"
                  f"{w['want'].get('right')}   got {w['got']}")
    for tag, rows in (("FABRICATIONS", r["fab"]), ("FALSE CONFIDENCE", r["false_conf"]),
                      ("OVER-CARDS", r["overcard"]), ("MISSES", r["miss"])):
        if rows:
            print(f"\n  {tag} ({len(rows)}, up to 10):")
            for f in rows[:10]:
                c = f["case"]
                print(f"    [{str(f.get('cls')):12s}] want {f.get('want','ASK'):7s} "
                      f"got {f['got']:7s} {c['text'][:56]!r}")


def route_run(live=False, limit=0, only=""):
    global _BY_ID
    cases, tag = route_load()
    _BY_ID = {c["id"]: c for c in cases}          # antecedent lookup for _seed_book
    if only:
        cases = [c for c in cases if only in (c.get("label_source") or "")
                 or only == c.get("id") or only == c.get("cls")
                 or (only == "hard" and c.get("hard"))]
    if limit:
        cases = cases[:limit]
    stats = Counter()
    cache = route_classify_batch(cases) if live else {}
    preds = [route_predict(c, stats, live=live, precomputed=cache.get(i), have_pre=live)
             for i, c in enumerate(cases)]
    route_report(("ROUTER: prefilter + regex + LLM" if live else
                  "ROUTER: prefilter + regex ONLY (no API calls — this is today's desk + L0)"),
                 route_score(cases, preds))
    total = stats["prefiltered"] + stats["would_call"] + stats["llm_ok"] + stats["llm_failed"]
    if total:
        print(f"\n  L0 prefilter hit rate: {stats['prefiltered']}/{total} = "
              f"{100.0*stats['prefiltered']/total:.1f}%  "
              f"(escalated back to the model: {stats['pre_escalate']})")
    print(f"  stats: {dict(stats)}")
    if live and stats["llm_ok"]:
        n = stats["llm_ok"]
        ti, to = stats["tok_in"] / n, stats["tok_out"] / n
        print(f"\n  MEASURED tokens/call: {ti:.0f} in / {to:.0f} out   "
              f"cache_read={stats['tok_cache_read']} cache_write={stats['tok_cache_write']}")
        print(f"  -> cache counters are 0 as predicted: the prompt is ~{ti:.0f} tokens, below "
              f"Haiku 4.5's 4096-token minimum cacheable prefix.")
        print(f"  MEASURED $/call: ${ti/1e6*1.00 + to/1e6*5.00:.5f}")
    return stats


def route_cost(stats=None):
    rows = read_inbound()
    days = max(1, len({time.strftime("%Y-%m-%d", time.gmtime(r["ts"])) for r in rows if r["ts"] > 1e6}))
    recv = [r for r in rows if r["outcome"] == "RECV"]
    per_day = len(recv) / days
    cases, _ = route_load()
    st = stats or Counter()
    if not st:
        for c in cases:
            route_predict(c, st, live=False)
    called = st["would_call"] + st["llm_ok"] + st["llm_failed"]     # dry run vs live run
    total = st["prefiltered"] + called
    call_rate = called / total if total else 1.0
    ti = (st["tok_in"] / st["llm_ok"]) if st.get("llm_ok") else 620
    to = (st["tok_out"] / st["llm_ok"]) if st.get("llm_ok") else 120
    src = "MEASURED" if st.get("llm_ok") else "estimated (run `route-live` for measured)"
    calls = per_day * call_rate
    dollars = calls * (ti / 1e6 * 1.00 + to / 1e6 * 5.00)
    print(f"\ninbound messages          {len(recv)} over {days} log-days -> {per_day:.1f}/day")
    print(f"L0 prefilter absorbs      {st['prefiltered']}/{total} = {(1-call_rate)*100:.1f}% "
          f"of messages (no API call, no model blast radius)")
    print(f"LLM calls/day             {calls:.1f}")
    print(f"model                     {'claude-haiku-4-5'}  ($1.00 / $5.00 per MTok)")
    print(f"tokens/call ({src})  {ti:.0f} in / {to:.0f} out")
    print(f"COST/DAY                  ${dollars:.4f}      (~${dollars*21:.2f}/month of trading days)")
    print(f"prompt caching            NOT used — prompt is ~{ti:.0f} tok, Haiku 4.5's minimum "
          f"cacheable prefix is 4096. A cache_control marker here is silently ignored.")


def route_latency(n=400):
    """The number the whole design exists to protect: how much wall-clock does routing add to
    ingest()? on_message() must do a regex prefilter, a context-window read and a queue put, and
    nothing else — the network is on a worker thread.

    THE WORKER IS STUBBED HERE, deliberately. An earlier version of this harness left the real
    worker running and measured 4.05 ms/message; that number was the harness fighting its own
    worker for the GIL while it made 65 real API calls in under a second. The desk sees ~35
    messages a DAY, so that contention does not exist in production and reporting it would
    overstate the cost by ~50x. What ingest() actually pays is measured below."""
    import llm_route as R
    import llm_parse as LP
    import signal_engine as E
    cases, _ = route_load()
    cases = (cases * ((n // max(1, len(cases))) + 1))[:n]
    sigs = [E.parse_signal(c["text"], provider=c["provider"]) for c in cases]

    real_call_tool = LP.call_tool
    LP.call_tool = lambda *a, **k: None            # worker drains instantly, never touches network
    R._SEEN.clear()
    R.STATS.clear()
    for c in cases:                                # populate the context ring, as ingest() does
        R.note(c["provider"], c["text"])

    t0 = time.perf_counter()
    for c, s in zip(cases, sigs):
        R.prefilter(c["text"], provider=c["provider"], regex_sig=s)
    t1 = time.perf_counter()
    per_pre = (t1 - t0) / len(cases) * 1000

    # Drain _JOBS by hand rather than racing the worker: with the worker stubbed, the main thread
    # wins the GIL and the queue simply fills, so 264/400 calls would take the cheap queue_full
    # early-return and flatter the average. Emptying it here makes EVERY message pay the real
    # enqueue cost.
    t0 = time.perf_counter()
    for i, (c, s) in enumerate(zip(cases, sigs)):
        R.on_message(f"{c['text']} #{i}", c["provider"], "REJECT", regex_sig=s)  # unique -> no dedup
        R.drain(limit=8, apply=False)              # exactly what ingest() does
        try:
            R._JOBS.get_nowait(); R._JOBS.task_done()
        except Exception:
            pass
    t1 = time.perf_counter()
    per_full = (t1 - t0) / len(cases) * 1000

    t0 = time.perf_counter()
    for _ in range(n):
        R.drain(limit=8, apply=False)
    t1 = time.perf_counter()
    per_drain = (t1 - t0) / n * 1000
    LP.call_tool = real_call_tool

    print(f"\nADDED INGEST LATENCY  (n={len(cases)} real messages, worker stubbed — see docstring)")
    print(f"  L0 prefilter only            {per_pre:8.4f} ms/message")
    print(f"  on_message() + drain()       {per_full:8.4f} ms/message   <- what ingest() pays")
    print(f"  drain() alone, queue empty   {per_drain:8.4f} ms/call")
    print(f"  ingest() baseline today      ~349 ms")
    print(f"  -> routing adds {per_full/349*100:.3f}% of the existing ingest budget "
          f"({per_full:.2f} ms of ~349 ms). The API call happens on a worker thread and is never "
          f"on this path.")
    print(f"  route stats: {dict(R.STATS)}")


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "run", "cost", "context-stats",
                                    "route-build", "route", "route-live", "route-cost",
                                    "route-latency"])
    ap.add_argument("--llm", action="store_true", help="actually call the API on triggered cases")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="only cases whose source matches")
    a = ap.parse_args()

    if a.cmd == "build":
        return build()
    if a.cmd == "context-stats":
        return context_stats()
    if a.cmd == "cost":
        return cost()
    if a.cmd == "route-build":
        return route_build()
    if a.cmd == "route":
        return route_run(live=False, limit=a.limit, only=a.only)
    if a.cmd == "route-live":
        st = route_run(live=True, limit=a.limit, only=a.only)
        return route_cost(st)
    if a.cmd == "route-cost":
        return route_cost()
    if a.cmd == "route-latency":
        return route_latency()

    cases = load()
    if a.only:
        cases = [c for c in cases if a.only in c["source"]]
    if a.limit:
        cases = cases[:a.limit]

    report("REGEX ONLY (today's desk)", score(cases, [regex_predict(c) for c in cases]))

    stats = Counter()
    stats["live"] = 1 if a.llm else 0
    preds = [hybrid_predict(c, stats) for c in cases]
    label = "HYBRID regex + LLM" if a.llm else "HYBRID (trigger counting only, no API calls)"
    report(label, score(cases, preds))
    print(f"\n  trigger/lLM stats: {dict(stats)}")
    if a.llm:
        import llm_parse as L
        print(f"  llm status: {L.status()}")


if __name__ == "__main__":
    main()

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


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "run", "cost", "context-stats"])
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

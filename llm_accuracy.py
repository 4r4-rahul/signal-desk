#!/usr/bin/env python3
"""
llm_accuracy.py — how accurately does each reader turn a provider's real messages into a CARD?

GROUND TRUTH is eval/gold_messages.json: hand-labelled from the message text alone — what a human would
key into a broker. The first version of this script scored against the cards the desk had already stored,
which was WRONG: many stored cards are fossils from before parser fixes (a card says NVDA 212 because the
old regex grabbed "Price target $212"; the message plainly says 210). Scoring against a buggy card
measures conformity to the bug, and it graded the model's correct answer as a fabrication. Hence labels.

TWO READERS ARE SCORED SIDE BY SIDE on the identical messages, because "is the LLM accurate?" is only
actionable next to the regex it would replace or backstop:
  · regex — signal_engine.parse_signal   (free, deterministic, runs on every message)
  · llm   — llm_route.classify           (costs a call, runs when the regex declines or is unsure)

ERRORS ARE NOT SYMMETRIC, so they are never summed into one accuracy number:
  · MISSING — declined to state a field. Cheap: the desk holds the signal for resolution, nothing trades.
  · WRONG   — asserted a value that contradicts the message. Expensive: this is what puts real money on
              the wrong contract. A reader that says nothing beats a reader that says 212.
Gold strike:null means the message states no strike; asserting one there is WRONG, not a hit.

Read-only. Touches no desk state, never calls ingest().

Run:  python3 llm_accuracy.py           both readers
      python3 llm_accuracy.py regex     regex only (free, no API calls)
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

GOLD = Path(__file__).parent / "eval" / "gold_messages.json"
PROVIDERS = ("prince", "prince_small", "optionking", "jmt")
FIELDS = ("ticker", "strike", "right", "premium")


def _num(x):
    try:
        return float(str(x).replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _norm(field, v):
    if v is None or v == "":
        return None
    if field in ("ticker",):
        return str(v).upper().lstrip("$")
    if field == "right":
        return str(v).upper()[:1]
    return _num(v)


def _read_regex(text, provider):
    import signal_engine as SE
    s = SE.parse_signal(text, provider=provider) or {}
    cls = None
    if s.get("ticker"):
        cls = "WATCH" if s.get("watch") else "ENTRY"
    return {"message_class": cls, "ticker": s.get("ticker"), "strike": s.get("strike"),
            "right": s.get("type"), "premium": s.get("premium")}


def _read_llm(text, provider):
    import llm_route as R
    r = R.classify(text, provider=provider) or {}
    return {"message_class": r.get("message_class"), "ticker": r.get("ticker"),
            "strike": r.get("strike"), "right": r.get("right"), "premium": r.get("premium")}


def score(reader_name, reader):
    gold = json.loads(GOLD.read_text())["labels"]
    tot = defaultdict(lambda: defaultdict(int))
    misses = []
    for g in gold:
        prov = g["provider"]
        try:
            r = reader(g["text"], prov)
        except Exception as e:
            r = {"error": str(e)}
        t = tot[prov]
        t["n"] += 1
        cls = r.get("message_class")
        # class is scored as "did it keep the message alive as a contract" vs "did it correctly drop it"
        want_contract = g.get("ticker") is not None
        got_contract = cls in ("ENTRY", "WATCH", "FILL")
        t["cls_ok"] += 1 if got_contract == want_contract else 0
        for f in FIELDS:
            want = _norm(f, g.get(f))
            got = _norm(f, r.get(f))
            alts = [_num(a) for a in (g.get("premium_alt") or [])] if f == "premium" else []
            if f == "ticker" and g.get("ticker_optional"):
                t[f + "_ok"] += 1        # naming the position is fine here; only strike/premium must stay empty
                continue
            if want is None:
                # message states no such field: silence is correct, an assertion is WRONG
                if got is None:
                    t[f + "_ok"] += 1
                else:
                    t[f + "_wrong"] += 1
                    misses.append((prov, f, "WRONG", g["text"][:52], got, want))
                continue
            hit = (got == want) or (f == "premium" and got is not None and
                                    any(abs(got - a) <= 0.011 for a in alts + [want]))
            if hit:
                t[f + "_ok"] += 1
            elif got is None:
                t[f + "_missing"] += 1
                misses.append((prov, f, "missing", g["text"][:52], got, want))
            else:
                t[f + "_wrong"] += 1
                misses.append((prov, f, "WRONG", g["text"][:52], got, want))
    print(f"\n{'='*92}\n{reader_name.upper()}  — scored on {sum(t['n'] for t in tot.values())} hand-labelled messages\n{'='*92}")
    hdr = f"{'provider':13s} {'n':>3s} {'class':>7s}"
    for f in FIELDS:
        hdr += f" {f[:7]:>17s}"
    print(hdr)
    print(f"{'':13s} {'':>3s} {'':>7s}" + "      ok/miss/WRONG" * 4)
    agg = defaultdict(int)
    for prov in PROVIDERS:
        t = tot.get(prov)
        if not t:
            continue
        line = f"{prov:13s} {t['n']:3d} {100.0*t['cls_ok']/t['n']:6.0f}%"
        for f in FIELDS:
            line += f"  {t[f+'_ok']:6d}/{t[f+'_missing']:3d}/{t[f+'_wrong']:<4d}"
        print(line)
        agg["n"] += t["n"]; agg["cls_ok"] += t["cls_ok"]
        for f in FIELDS:
            for k in ("_ok", "_missing", "_wrong"):
                agg[f + k] += t[f + k]
    line = f"{'ALL':13s} {agg['n']:3d} {100.0*agg['cls_ok']/agg['n']:6.0f}%"
    for f in FIELDS:
        line += f"  {agg[f+'_ok']:6d}/{agg[f+'_missing']:3d}/{agg[f+'_wrong']:<4d}"
    print(line)
    fw = sum(agg[f + "_wrong"] for f in FIELDS)
    fm = sum(agg[f + "_missing"] for f in FIELDS)
    fo = sum(agg[f + "_ok"] for f in FIELDS)
    print(f"\n  fields: {fo} correct, {fm} missing (cheap — desk holds for resolution), "
          f"{fw} WRONG (expensive — wrong contract)")
    if misses:
        print(f"\n  every error, {reader_name}:")
        for prov, f, kind, txt, got, want in misses:
            print(f"    {kind:7s} {prov:13s} {f:8s} got {str(got):>9s}  want {str(want):>9s}   {txt!r}")
    return agg


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("both", "regex"):
        score("regex", _read_regex)
    if which in ("both", "llm"):
        import llm_route as R
        if not R.available():
            print("\nLLM unavailable (no key/SDK) — regex results above stand alone.")
        else:
            score("llm", _read_llm)

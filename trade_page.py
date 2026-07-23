#!/usr/bin/env python3
"""
trade_page.py — the full story of ONE trade, as a standalone page.

The scalp log has room for a row: "QQQ ▼ -1R". That is a verdict with no explanation. This is the page
behind it — what was risked, what was actually bought, how the ladder was SUPPOSED to work, which legs
filled, when and why it closed, and where the plan broke. Everything is read from the record (outcomes,
path ticks, the playbook ledger); nothing is recomputed, so this page cannot tell a different story from
the card that spawned it.

The question it exists to answer is the one a row can never answer: "why did the ladder thesis fail?"
"""
import json, sqlite3
from datetime import datetime, timezone


def _n(v, spec=".2f", dash="—", pre="", suf=""):
    """Format a number, or return a dash. Every numeric field on this page can legitimately be NULL —
    a trade that never resolved has no exit, an unparsed signal has no strike, an old row predates the
    cost columns. Ten separate f-string guards is ten places to forget one, which is exactly what
    happened: the page 500'd with "unsupported format string passed to NoneType" on the first trade
    whose fields differed from the one I tested. One formatter, used everywhere, cannot be forgotten."""
    if v is None:
        return dash
    try:
        return f"{pre}{v:{spec}}{suf}"
    except (TypeError, ValueError):
        return dash


def _fmt_t(iso, tz_offset_h=-4):
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        return (d + timedelta(hours=tz_offset_h)).strftime("%b %d  %H:%M:%S")
    except Exception:
        return str(iso)[:19]


def _load(sid, db_path):
    con = sqlite3.connect(str(db_path))
    try:
        sig = con.execute(
            "SELECT ticker,strike,type,expiry,dte,premium,stop_pct,t1_pct,t2_pct,t3_pct,"
            "       limit_px,delta,iv,spread_pct,score,provider,raw,ts_utc "
            "FROM signals WHERE signal_id=?", (sid,)).fetchone()
        out = con.execute(
            "SELECT entry_px,exit_px,return_pct,r_multiple,peak_pct,trough_pct,hold_secs,result,"
            "       opened_utc,closed_utc,entry_fill_px,cost_pct,score_basis "
            "FROM outcomes WHERE signal_id=? AND kind='SHADOW'", (sid,)).fetchone()
        path = con.execute(
            "SELECT ts_utc,opt_mid,opt_pct,bid,ask,spot,rsi,vwap FROM path "
            "WHERE signal_id=? AND phase='shadow' AND opt_pct IS NOT NULL ORDER BY ts_utc", (sid,)).fetchall()
        return sig, out, path
    finally:
        con.close()


def _ladder_story(t1, t2, t3, stop, peak, trough, plan):
    """Leg by leg: was it reached, and if not, by how much did it miss. This is the answer to
    'why did the ladder thesis fail' — a target that was never touched failed for a different reason
    than one that was touched and given back."""
    rows = []
    for name, tgt in (("T1", t1), ("T2", t2), ("T3", t3)):
        if tgt is None:
            continue
        reached = (peak or -999) >= tgt
        gap = (tgt - (peak or 0))
        rows.append({"leg": name, "target_pct": tgt, "reached": reached,
                     "detail": (f"peak {_n(peak,'+.0f')}% cleared it" if reached
                                else f"peak {_n(peak,'+.0f')}% — missed by {_n(gap,'.0f')}pp")})
    rows.append({"leg": "STOP", "target_pct": -abs(stop or 0),
                 "reached": (trough or 0) <= -abs(stop or 0),
                 "detail": (f"trough {_n(trough,'+.0f')}% hit it" if (trough or 0) <= -abs(stop or 0)
                            else f"trough {_n(trough,'+.0f')}% held above it")})
    return rows


CSS = """
:root{--bg:#0b0e14;--panel:#141922;--panel2:#111621;--line:#1f2733;--fg:#e6edf3;--muted:#8b98a9;
      --dim:#5d6b7d;--g:#22c55e;--r:#ef4444;--a:#f59e0b}
@media(prefers-color-scheme:light){:root{--bg:#f6f8fa;--panel:#fff;--panel2:#fbfcfd;--line:#e2e7ee;
      --fg:#1a1f28;--muted:#5a6675;--dim:#8b98a9}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:940px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:21px;margin:0 0 2px;letter-spacing:-.2px}
.sub{color:var(--muted);font-size:12.5px}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
      border-radius:14px;padding:15px 17px;margin-top:14px}
.h{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);font-weight:700;
   margin-bottom:9px;border-bottom:1px solid var(--line);padding-bottom:5px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:11px}
.k{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.4px}
.v{font-size:15px;font-weight:650;margin-top:1px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:10px;text-transform:uppercase;
   letter-spacing:.5px;padding:4px 6px;border-bottom:1px solid var(--line)}
td{padding:5px 6px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}.d{color:var(--dim)}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700}
.bar{height:7px;border-radius:4px;background:var(--line);position:relative;overflow:hidden;margin-top:5px}
.bar>i{position:absolute;top:0;bottom:0;border-radius:4px}
a{color:#7aa2f7}
"""


def _from_card(card):
    """Build the same tuple _load returns, but from a LIVE CARD. The DB is synced on a heavy lap, so a
    card created minutes ago exists in the card store and not yet in `signals` — 10 of chip's 19 cards
    were in exactly that state. Reading only the DB made those trades unopenable and showed "Trade not
    found" for a trade that plainly exists on screen. The card is the same data, earlier."""
    sg = (card or {}).get("sig") or {}
    o = (card or {}).get("opt") or {}
    tk = (card or {}).get("ticket") or {}
    pl = (card or {}).get("plan") or {}
    try:
        strike = float(sg.get("strike")) if sg.get("strike") is not None else None
    except (TypeError, ValueError):
        strike = None
    return (sg.get("ticker"), strike, sg.get("type"), sg.get("expiry"), sg.get("dte"),
            sg.get("premium"), tk.get("stop_pct") or pl.get("stop_pct"), tk.get("t1_pct"),
            tk.get("t2_pct"), tk.get("t3_pct"), tk.get("limit"), o.get("delta"), o.get("iv"),
            o.get("spread_pct"), (card or {}).get("score"), (card or {}).get("provider"),
            (card or {}).get("raw"), (card or {}).get("iso"))


def _shadow_out(card):
    """The outcome tuple from a live card's shadow block, for trades the DB has not scored yet."""
    sh = (card or {}).get("shadow") or {}
    if not sh:
        return None
    return (sh.get("ref"), None, sh.get("now"), None, sh.get("peak"), sh.get("trough"), None,
            sh.get("result"), sh.get("start_utc"), sh.get("ended_utc"), None, None, None)


def render(sid, db_path, book_row=None, life=None, card=None):
    sig, out, path = _load(sid, db_path)
    if not sig and card:                       # not in the DB yet -> read the live card instead
        sig, out = _from_card(card), (_shadow_out(card) or out)
    if not sig:
        return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><div class=wrap><h1>Trade not found</h1><p class=sub>{sid}</p></div>"
    (tk, strike, typ, expiry, dte, premium, stop_pct, t1, t2, t3, limit_px,
     delta, iv, spread, score, provider, raw, ts) = sig
    entry = ret = r = peak = trough = result = None
    opened = closed = None
    if out:
        (entry, exit_px, ret, r, peak, trough, hold, result, opened, closed,
         fill_px, cost_pct, basis) = out
    b = book_row or {}
    qty = b.get("qty")
    pnl = b.get("pnl")
    capital = round((entry or 0) * 100 * (qty or 0), 2) if entry and qty else None
    risk = b.get("risk_usd")

    pcls = "g" if (pnl or 0) > 0 else "r"
    legs = _ladder_story(t1, t2, t3, stop_pct, peak, trough, b.get("exec_plan"))

    # the money section — what was actually committed, and what came back
    money = f"""<div class=card><div class=h>money</div><div class=kv>
      <div><div class=k>contracts</div><div class=v>{qty if qty else '—'}</div></div>
      <div><div class=k>entry fill</div><div class="v mono">{_n(entry,'.2f',pre='$')}</div></div>
      <div><div class=k>capital deployed</div><div class="v mono">{'$'+format(capital,',.0f') if capital else '—'}</div></div>
      <div><div class=k>at risk (1%)</div><div class="v mono">{'$'+format(risk,',.0f') if risk else '—'}</div></div>
      <div><div class=k>realised</div><div class="v mono {pcls}">{('$'+format(pnl,'+,.2f')) if pnl is not None else '—'}</div></div>
      <div><div class=k>return</div><div class="v mono {pcls}">{f'{ret:+.1f}%' if ret is not None else '—'}</div></div>
      <div><div class=k>R multiple</div><div class="v mono {pcls}">{f'{r:+.2f}R' if r is not None else '—'}</div></div>
      <div><div class=k>sizing rule</div><div class="v" style="font-size:12px;font-weight:500">{b.get('sizing','—')}</div></div>
    </div></div>"""

    # the ladder — the actual answer to "why did the thesis fail"
    lrows = "".join(
        f"<tr><td><b>{l['leg']}</b></td>"
        f"<td class=mono>{_n(l['target_pct'],'+.0f')}%</td>"
        f"<td class='{'g' if (l['reached'] and l['leg']!='STOP') else ('r' if l['reached'] else 'd')}'>"
        f"{'REACHED' if l['reached'] else 'not reached'}</td>"
        f"<td class='d mono'>{l['detail']}</td></tr>" for l in legs)
    span = (peak or 0) - (trough or 0)
    ppos = (((peak or 0) - (trough or 0)) / span * 100) if span else 0
    ladder = f"""<div class=card><div class=h>the ladder — what was planned vs what happened</div>
      <div class=sub style="margin-bottom:8px">plan: <b>{b.get('exec_plan','—')}</b>
        &nbsp;·&nbsp; shape <b>{b.get('exec_shape','—')}</b>
        {' <span class=a>· degraded</span>' if b.get('ladder_degraded') else ''}</div>
      <table><tr><th>leg</th><th>target</th><th>outcome</th><th>evidence</th></tr>{lrows}</table>
      <div style="margin-top:11px">
        <div class=k>excursion — how far it went both ways</div>
        <div class=bar><i style="left:0;width:{max(0,min(100,abs(trough or 0)/max(span,1)*100)):.0f}%;background:var(--r)"></i>
          <i style="right:0;width:{max(0,min(100,abs(peak or 0)/max(span,1)*100)):.0f}%;background:var(--g)"></i></div>
        <div class="sub mono" style="display:flex;justify-content:space-between;margin-top:3px">
          <span class=r>trough {_n(trough,'+.0f')}%</span><span class=g>peak {_n(peak,'+.0f')}%</span></div>
      </div></div>"""

    # timeline
    tl = f"""<div class=card><div class=h>timeline</div><div class=kv>
      <div><div class=k>opened</div><div class="v mono" style="font-size:13px">{_fmt_t(opened)}</div></div>
      <div><div class=k>closed</div><div class="v mono" style="font-size:13px">{_fmt_t(closed)}</div></div>
      <div><div class=k>exit reason</div><div class=v style="font-size:13px">{(life or {}).get('end_reason') or result or '—'}</div></div>
      <div><div class=k>ticks recorded</div><div class="v mono">{len(path)}</div></div>
    </div></div>"""

    # the contract
    contract = f"""<div class=card><div class=h>contract</div><div class=kv>
      <div><div class=k>leg</div><div class=v>{tk} {_n(strike,'.0f')}{typ or ''}</div></div>
      <div><div class=k>expiry</div><div class="v mono">{expiry or '—'}</div></div>
      <div><div class=k>delta / IV</div><div class="v mono">{f'{delta:.2f}' if delta else '—'} / {f'{iv:.1%}' if iv else '—'}</div></div>
      <div><div class=k>spread at entry</div><div class="v mono">{f'{spread:.1f}%' if spread else '—'}</div></div>
      <div><div class=k>stop / T1 / T2 / T3</div><div class="v mono" style="font-size:13px">{_n(stop_pct,'.0f')} · {_n(t1,'.0f')} · {_n(t2,'.0f')} · {_n(t3,'.0f')}%</div></div>
      <div><div class=k>tape at entry</div><div class="v mono">{score or '—'}</div></div>
    </div></div>"""

    checks = ""
    for stage, blk in (("before entry", None), ("after close", (b.get("checks") or {}))):
        if not blk or not blk.get("items"):
            continue
        rows = "".join(
            f"<tr><td class='{'g' if c['state']=='ok' else ('r' if c['state']=='fail' else 'a')}'>"
            f"{'✓' if c['state']=='ok' else ('✕' if c['state']=='fail' else '!')}</td>"
            f"<td>{c['label']}</td><td class='d mono'>{c['detail']}</td></tr>" for c in blk["items"])
        checks += f"<div class=card><div class=h>{stage} — checklist</div><table>{rows}</table></div>"

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{tk} {_n(strike,'.0f')}{typ or ''} — trade detail</title><style>{CSS}</style></head><body><div class=wrap>
<h1>{tk} {_n(strike,'.0f')}{typ or ''} <span class="pill" style="background:{'rgba(34,197,94,.16)' if (pnl or 0)>0 else 'rgba(239,68,68,.16)'};color:var(--{'g' if (pnl or 0)>0 else 'r'})">{result or '—'}</span></h1>
<div class=sub>{provider} · {sid} · <span class=mono>{_fmt_t(ts)}</span></div>
<div class=sub style="margin-top:6px;font-style:italic;color:var(--dim)">{(raw or '')[:180]}</div>
{money}{ladder}{tl}{contract}{checks}
<div class=sub style="margin-top:18px;color:var(--dim)">shadow-only paper trade · costs are real (ask in, bid out, $1.30/contract) · <a href="/">back to the desk</a></div>
</div></body></html>"""

#!/usr/bin/env python3
"""
Deep-learning + vector prediction on POOLED trustworthy providers (Mike + Sulker,
the only two with documented real outcomes). Features:
  - numeric: premium, lotto, EOD, call/put, day-of-week, month
  - market regime: SPY daily return & range, VIX level & change (from data/*.json)
  - provider + ticker one-hots
  - "deep vectors": TF-IDF -> TruncatedSVD (LSA) semantic vectors of each trade's text
Models: Dummy / Logistic / GradientBoosting / MLP neural net, 5-fold CV.
Then a CALIBRATION test: do high-predicted-probability trades actually win more?
Run: .venv/bin/python deep_predict.py
"""
import sqlite3, re, json, math, statistics as st
from pathlib import Path
from datetime import date
import numpy as np
from sklearn.model_selection import StratifiedKFold, cross_val_score, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.dummy import DummyClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).parent
SPY = json.load(open(ROOT / "data/spy_daily.json"))
VIX = json.load(open(ROOT / "data/vix_daily.json"))
line_re = re.compile(r'\b([A-Z]{2,4})?\s*(\d{2,5})\s*([cp])\b', re.I)
mfe_re  = re.compile(r'high[^(]*\(\s*\d{2,5}\s*%')
pct_re  = re.compile(r'(-?\d+)\s*%')
prem_re = re.compile(r'(\d{0,2}\.\d{1,2})')


def realized(line):
    low = line.lower()
    if 'break even' in low or 'b/e' in low: return 0.0
    if 'expired worthless' in low or 'zero hero' in low and ('fail' in low or '-100' in low): return -100.0
    if 'fail zero' in low or 'fail target' in low: return -100.0
    sm = re.search(r'(?:sold|exit)[^%]*\((-?\d+)\s*%', low)
    if sm: return float(sm.group(1))
    cleaned = re.split(r'contract high|high\s*-*>', low)[0]
    pm = pct_re.search(cleaned)
    if pm: return float(pm.group(1))
    if 'expired' in low or '-100' in low: return -100.0
    if 'scratch' in low or 'flat' in low: return 0.0
    if 'stopped' in low: return -50.0
    if 'cut' in low or 'loss' in low: return -40.0
    return None


def parse_channel(ch):
    db = str(ROOT / "channels" / ch / f"{ch}.db")
    rows = [(ts, t) for ts, t in sqlite3.connect(db).execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp")]
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:70]
        if k in seen: continue
        seen.add(k); U.append((ts, t))
    recaps = [(ts, t) for ts, t in U if 'recap' in t.lower()[:40]]
    out = []
    for ts, t in recaps:
        for line in t.split('\n'):
            m = line_re.search(line)
            if not m: continue
            r = realized(line)
            if r is None: continue
            pm = prem_re.search(re.split(r'contract high', line.lower())[0])
            out.append(dict(date=ts[:10], provider=ch,
                            ticker=(m.group(1) or 'SPX').upper(), type=m.group(3).upper(),
                            premium=float(pm.group(1)) if pm else None,
                            lotto=int(any(w in line.lower() for w in ['lotto', 'hero', 'starter'])),
                            eod=int('eod' in line.lower()),
                            ret=max(min(r, 2000), -100), text=line.strip()[:160]))
    return out


def regime():
    dts = sorted(SPY)
    reg = {}
    for i, d in enumerate(dts):
        o = SPY[d]
        pc = SPY[dts[i-1]]['c'] if i else o['c']
        vix = VIX.get(d)
        vixp = None
        j = i
        while j > 0 and dts[j-1] not in VIX:
            j -= 1
        vixp = VIX.get(dts[j-1]) if j else vix
        reg[d] = dict(spy_ret=(o['c']/pc-1)*100, spy_rng=(o['h']-o['l'])/o['o']*100,
                      vix=vix if vix else 18.0, vix_chg=((vix/vixp-1)*100 if vix and vixp else 0.0))
    return reg


def main():
    trades = parse_channel("mike") + parse_channel("sulker")
    REG = regime()
    med_prem = st.median([t['premium'] for t in trades if t['premium']])
    TOPTK = ['SPX', 'SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'MSFT', 'META']

    rows, y, texts = [], [], []
    for t in trades:
        d = t['date']; rg = REG.get(d, dict(spy_ret=0, spy_rng=1.5, vix=18, vix_chg=0))
        try: dow = date.fromisoformat(d).weekday(); mon = date.fromisoformat(d).month
        except Exception: dow, mon = 2, 6
        feats = [
            math.log((t['premium'] or med_prem) + 0.05), t['lotto'], t['eod'],
            1 if t['type'] == 'C' else 0, dow, mon,
            rg['spy_ret'], rg['spy_rng'], rg['vix'], rg['vix_chg'],
            1 if t['provider'] == 'sulker' else 0,
        ] + [1 if t['ticker'] == tk else 0 for tk in TOPTK]
        rows.append(feats); y.append(1 if t['ret'] > 1 else 0); texts.append(t['text'])
    Xnum = np.array(rows, float)
    y = np.array(y)
    fnames = ['log_prem', 'lotto', 'eod', 'is_call', 'dow', 'month',
              'spy_ret', 'spy_range', 'vix', 'vix_chg', 'is_sulker'] + ['tk_' + k for k in TOPTK]

    # deep vectors: TF-IDF -> SVD (LSA)
    tf = TfidfVectorizer(max_features=400, ngram_range=(1, 2), min_df=3)
    Xtf = tf.fit_transform(texts)
    svd = TruncatedSVD(n_components=20, random_state=0)
    Xvec = svd.fit_transform(Xtf)
    X = np.hstack([Xnum, Xvec])

    n = len(y); base = y.mean()
    print(f"=========  DEEP PREDICTION — pooled Mike+Sulker  =========")
    print(f"  {n} trades with documented outcomes. Base win rate {base*100:.1f}% "
          f"(dumb model = {max(base,1-base)*100:.0f}% acc). Features: {X.shape[1]} "
          f"({Xnum.shape[1]} numeric + 20 text-vector).\n")

    cv = StratifiedKFold(5, shuffle=True, random_state=1)
    models = {
        'Baseline (majority)': DummyClassifier(strategy='most_frequent'),
        'Logistic Regression': make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=2000)),
        'Gradient Boosting':   GradientBoostingClassifier(n_estimators=150, max_depth=3, random_state=0),
        'Neural Net (MLP)':    make_pipeline(StandardScaler(),
                                MLPClassifier(hidden_layer_sizes=(64, 32, 16), alpha=1e-2,
                                              max_iter=4000, random_state=0)),
    }
    print(f"  {'MODEL':<22}{'CV ACCURACY':>14}{'CV AUC':>16}{'OVERFIT GAP':>13}")
    print("  " + "-" * 63)
    best = None
    for name, m in models.items():
        acc = cross_val_score(m, X, y, cv=cv, scoring='accuracy')
        try:
            auc = cross_val_score(m, X, y, cv=cv, scoring='roc_auc'); a = f"{auc.mean():.3f}±{auc.std():.2f}"
        except Exception:
            a = "n/a"
        m.fit(X, y); gap = m.score(X, y) - acc.mean()
        print(f"  {name:<22}{acc.mean()*100:>12.1f}%{a:>16}{gap*100:>11.0f}%")
        if name == 'Gradient Boosting': best = m

    print("\n  AUC ~0.50 = coin flip (no edge). Big overfit gap = memorizing noise.")

    # does market regime matter? GBM importances grouped
    imp = best.feature_importances_
    allnames = fnames + [f'vec{i}' for i in range(20)]
    print("\n  Top features (gradient boosting importance):")
    for nm, v in sorted(zip(allnames, imp), key=lambda z: -z[1])[:8]:
        print(f"    {nm:<12} {v:.3f}")

    # CALIBRATION: do high-predicted-prob trades actually win more?
    proba = cross_val_predict(make_pipeline(StandardScaler(),
                LogisticRegression(C=0.3, max_iter=2000)), X, y, cv=cv, method='predict_proba')[:, 1]
    order = np.argsort(proba)
    print("\n  CALIBRATION — sort trades by predicted win-prob, split into 5 buckets:")
    print(f"    {'bucket':<10}{'pred win%':>11}{'ACTUAL win%':>13}")
    for b in range(5):
        idx = order[b*n//5:(b+1)*n//5]
        print(f"    {['lowest','low','mid','high','highest'][b]:<10}"
              f"{proba[idx].mean()*100:>10.0f}%{y[idx].mean()*100:>12.0f}%")
    print("    -> If ACTUAL win% is flat across buckets, the model can't tell winners from losers.")

    # THE REAL TEST: train on the PAST, predict the FUTURE (chronological, no peeking)
    dates = np.array([t['date'] for t in trades])
    order_t = np.argsort(dates)
    cut = int(0.7 * n)
    tr, te = order_t[:cut], order_t[cut:]
    print(f"\n  ===  FORWARD TEST: train on first 70% (through {dates[order_t[cut-1]]}), "
          f"predict last 30%  ===")
    print("    (A) WITH recap-text vectors  vs  (B) ENTRY-ONLY features (no leakage):")
    Xentry = Xnum   # only what's known at entry time — NO recap text
    for name, m in [('Gradient Boosting', GradientBoostingClassifier(n_estimators=150, max_depth=3, random_state=0)),
                    ('Logistic', make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=2000)))]:
        m.fit(X[tr], y[tr]); a_full = roc_auc_score(y[te], m.predict_proba(X[te])[:, 1])
        m2 = GradientBoostingClassifier(n_estimators=150, max_depth=3, random_state=0) if 'Grad' in name \
             else make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=2000))
        m2.fit(Xentry[tr], y[tr]); a_entry = roc_auc_score(y[te], m2.predict_proba(Xentry[te])[:, 1])
        print(f"    {name:<20} (A) with-text AUC {a_full:.3f}   (B) ENTRY-ONLY AUC {a_entry:.3f}")
    # forward calibration (GBM)
    gm = GradientBoostingClassifier(n_estimators=150, max_depth=3, random_state=0).fit(X[tr], y[tr])
    pf = gm.predict_proba(X[te])[:, 1]; of = np.argsort(pf); m2 = len(te)
    print("    forward calibration (future trades, by predicted bucket):")
    for b in range(4):
        idx = of[b*m2//4:(b+1)*m2//4]
        print(f"      {['lowest','low','high','highest'][b]:<9} pred {pf[idx].mean()*100:>3.0f}%  "
              f"ACTUAL {y[te][idx].mean()*100:>3.0f}%")
    print("    -> This is what matters. If FORWARD AUC ~0.5 / flat calibration, the in-sample")
    print("       signal was period-memorization and WON'T help you trade live.")


if __name__ == "__main__":
    main()

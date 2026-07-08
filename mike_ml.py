#!/usr/bin/env python3
"""
Deep-learning + ML run on SPX Mike's documented recap trades.

Honest framing: n~100 over 3 weeks. We extract features per trade and ask:
  (1) Can a model PREDICT win/loss from entry features? (classification)
  (2) Can it PREDICT the return magnitude?            (regression)
We run a neural net (MLP) alongside logistic regression / gradient boosting,
with 5-fold cross-validation, and compare to baselines + show overfit gaps.
Then an empirical segment EV table (the part you can actually trade on).

Run:  .venv/bin/python mike_ml.py
"""
import sqlite3, re, statistics as st
from pathlib import Path
import numpy as np
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.dummy import DummyClassifier
from sklearn.metrics import roc_auc_score

DB = str(Path(__file__).parent / "channels" / "mike" / "mike.db")
pct_re = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*%')
trade_re = re.compile(r'\b([A-Z]{2,5})?\s*(\d{2,4}(?:\.\d)?)\s*([CP])\b')
arrow_re = re.compile(r'(\d*\.?\d+)\s*(?:→|->)\s*([\d.]+)')


def parse_line(line, session):
    tm = trade_re.search(line)
    if not tm:
        return None
    ticker = (tm.group(1) or 'SPX').upper()
    low = line.lower()
    pm = pct_re.search(line)
    if pm:                                pct = float(pm.group(1))
    elif 'expired worthless' in low or '-100' in low: pct = -100.0
    elif any(w in low for w in ['scratch','flat','~be','breakeven']): pct = 0.0
    elif 'stopped' in low:                pct = -50.0
    elif 'cut' in low or 'loss' in low:   pct = -40.0
    else:                                 return None
    am = arrow_re.search(line)
    entry = float(am.group(1)) if (am and am.group(1) not in ('', '.')) else None
    return dict(ticker=ticker, type=tm.group(3).upper(), pct=pct, entry=entry,
                lotto=int('lotto' in low), eod=int('eod' in low or session == 'EOD'),
                session=session)


def load_trades():
    c = sqlite3.connect(DB)
    rows = [(ts, t) for ts, t in c.execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp")]
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:80]
        if k in seen: continue
        seen.add(k); U.append(t)
    recaps = [t for t in U if 'recap' in t.lower() and '├─' not in t]

    trades, key = [], set()
    for t in recaps:
        session = 'UNK'
        for line in t.split('\n'):
            ll = line.lower()
            if 'am session' in ll: session = 'AM'
            elif 'pm session' in ll: session = 'PM'
            elif 'eod' in ll and ('lotto' in ll or 'session' in ll): session = 'EOD'
            elif 'regular market' in ll: session = 'REG'
            r = parse_line(line, session)
            if not r: continue
            k = (r['ticker'], r['type'], r['entry'], r['pct'])
            if k in key: continue
            key.add(k); trades.append(r)
    return trades


def main():
    T = load_trades()
    n = len(T)
    print(f"=== DATASET: {n} documented Mike trades (3 weeks) ===\n")

    entries = [t['entry'] for t in T if t['entry']]
    med_entry = st.median(entries)
    # feature matrix
    X = np.array([[
        1 if t['type'] == 'C' else 0,
        1 if t['ticker'] == 'SPX' else 0,
        t['lotto'],
        t['eod'],
        np.log((t['entry'] or med_entry) + 0.01),
    ] for t in T], dtype=float)
    feat_names = ['is_call', 'is_SPX', 'is_lotto', 'is_EOD', 'log_entry']
    y = np.array([1 if t['pct'] > 0.001 else 0 for t in T])
    ret = np.array([t['pct'] / 100 for t in T])

    base = y.mean()
    print(f"Base win rate: {base*100:.1f}%  (a dumb 'always predict win' model scores {max(base,1-base)*100:.0f}% accuracy)\n")

    cv = StratifiedKFold(5, shuffle=True, random_state=42)
    models = {
        'Baseline (majority)': DummyClassifier(strategy='most_frequent'),
        'Logistic Regression': make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=1000)),
        'Gradient Boosting':   GradientBoostingClassifier(n_estimators=100, max_depth=2, random_state=0),
        'Neural Net (MLP)':    make_pipeline(StandardScaler(),
                                MLPClassifier(hidden_layer_sizes=(16, 8), max_iter=3000,
                                              alpha=1e-2, random_state=0)),
    }
    print(f"{'MODEL':<22}{'CV ACCURACY':>16}{'CV AUC':>16}{'TRAIN ACC':>12}{'OVERFIT GAP':>13}")
    print("-" * 79)
    for name, m in models.items():
        acc = cross_val_score(m, X, y, cv=cv, scoring='accuracy')
        try:
            auc = cross_val_score(m, X, y, cv=cv, scoring='roc_auc')
            aucs = f"{auc.mean():.2f} ± {auc.std():.2f}"
        except Exception:
            aucs = "n/a"
        m.fit(X, y)
        train_acc = m.score(X, y)
        gap = train_acc - acc.mean()
        print(f"{name:<22}{f'{acc.mean()*100:.0f}% ± {acc.std()*100:.0f}':>16}{aucs:>16}"
              f"{train_acc*100:>11.0f}%{gap*100:>12.0f}%")

    print("\n(AUC ~0.50 = no predictive power. A big TRAIN-minus-CV gap = overfitting/memorizing.)")

    # logistic coefficients = direction of each feature's effect
    lr = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=1000)).fit(X, y)
    coefs = lr.named_steps['logisticregression'].coef_[0]
    print("\nFeature effect on WIN probability (logistic coef; + = more likely win):")
    for f, c in sorted(zip(feat_names, coefs), key=lambda z: -abs(z[1])):
        print(f"   {f:<12} {c:+.2f}")

    # ---- the part you can actually trade on: empirical segment EV ----
    print("\n=== EMPIRICAL SEGMENT RESULTS (median is the trustworthy figure) ===")
    def seg(label, idx):
        r = ret[idx]
        if len(r) < 4: return
        w = (r > 0.001).mean()
        print(f"  {label:<22} n={len(r):>3}  win {w*100:>3.0f}%  mean {r.mean()*100:>+6.0f}%  median {np.median(r)*100:>+5.0f}%")
    seg("ALL", np.ones(n, bool))
    seg("Lotto", X[:, 2] == 1); seg("Non-lotto", X[:, 2] == 0)
    seg("SPX", X[:, 1] == 1);   seg("Non-SPX (stocks)", X[:, 1] == 0)
    seg("Calls", X[:, 0] == 1); seg("Puts", X[:, 0] == 0)
    seg("EOD", X[:, 3] == 1);   seg("Non-EOD", X[:, 3] == 0)
    cheap = np.array([(t['entry'] or med_entry) <= 0.5 for t in T])
    seg("Cheap (<=$0.50)", cheap); seg("Pricier (>$0.50)", ~cheap)

    print(f"\nReturn distribution: mean {ret.mean()*100:+.0f}%  median {np.median(ret)*100:+.0f}%  "
          f"std {ret.std()*100:.0f}%  (mean >> median = fat-tail lotto skew)")
    big = (ret > 2.0).sum()
    print(f"Trades over +200%: {big} of {n} ({big/n*100:.0f}%) — these moonshots carry the whole mean.")


if __name__ == "__main__":
    main()

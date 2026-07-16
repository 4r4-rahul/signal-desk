#!/usr/bin/env python3
"""
#4 — the ML READINESS harness on the LIVE desk warehouse (signal_desk.db / ml_dataset).

HONEST framing: with n small this is NOT a deployable predictor — it is a readiness check that
strengthens as data grows. Everything here is OUT-OF-SAMPLE (leave-one-out / k-fold CV, never
in-sample), compared to a dummy baseline, with the overfit gap shown. A hard GATE decides whether the
model has earned the right to inform the live score — it hasn't until CV clears a bar AND n is big
enough. Direction is ~a coin flip, so we test the honest targets: win/loss (expect ~0.5 AUC) and the
tradeable one, realized R (expectancy).

    .venv/bin/python ml_desk.py

Feed it by trading + logging: every closed trade adds a labelled row. Re-run anytime to see readiness.
"""
import sqlite3
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import LeaveOneOut, KFold, cross_val_predict
from sklearn.metrics import roc_auc_score, r2_score, mean_absolute_error

DB_PATH = Path(__file__).parent / "signal_desk.db"

# execution-focused features (the honest edge is cost/execution, not direction). Derived where needed.
MIN_N_READY = 100          # below this, no model earns the live score — full stop
MIN_AUC_READY = 0.58       # out-of-sample AUC must clear this (meaningfully > 0.50) to be "ready"


def _load():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM ml_dataset WHERE r_multiple IS NOT NULL")]
    con.close()
    feats, ys, yr = [], [], []
    for r in rows:
        mid = r.get("mid") or 0
        prem = r.get("premium") or 0
        theta = r.get("theta")
        delta = r.get("delta")
        burn = (abs(theta) / mid * 100) if (theta is not None and mid) else None
        chase = ((mid / prem - 1) * 100) if (mid and prem) else None
        feats.append({
            "score": r.get("score"),
            "grade_score": r.get("grade_score"),
            "premium": prem or None,
            "spread_pct": r.get("spread_pct"),
            "abs_delta": abs(delta) if delta is not None else None,
            "theta_burn": burn,
            "chase": chase,
            "iv": r.get("iv"),
            "lotto": 1.0 if r.get("lotto") else 0.0,
        })
        yr.append(r.get("r_multiple"))
        ys.append(1 if (r.get("label_win") if r.get("label_win") is not None
                        else (r.get("r_multiple") or 0) > 0) else 0)
    return feats, np.array(ys), np.array(yr, float)


def _matrix(feats):
    """Feature matrix with median imputation (small n — keep it simple and robust)."""
    keys = list(feats[0].keys())
    cols = {k: np.array([f[k] if f[k] is not None else np.nan for f in feats], float) for k in keys}
    for k in keys:                                    # median-impute missing
        med = np.nanmedian(cols[k]) if not np.all(np.isnan(cols[k])) else 0.0
        cols[k] = np.where(np.isnan(cols[k]), med, cols[k])
    X = np.column_stack([cols[k] for k in keys])
    return X, keys


def run():
    feats, y, yr = _load()
    n = len(y)
    print(f"\n  ML READINESS — desk warehouse\n  {'='*54}")
    print(f"  labelled closed trades : {n}   (wins {int(y.sum())}, losses {int((1-y).sum())})")
    if n < 12:
        print(f"\n  ⛔ NOT READY — n={n}. Log more closed trades; this needs ~{MIN_N_READY}+ to mean anything.\n")
        return
    X, keys = _matrix(feats)

    # ---- (1) DIRECTION: win/loss, leave-one-out out-of-sample AUC vs dummy ----
    auc = auc_dummy = None
    if 3 <= int(y.sum()) <= n - 3:                    # need both classes present enough
        clf = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=1000, class_weight="balanced"))
        try:
            proba = cross_val_predict(clf, X, y, cv=LeaveOneOut(), method="predict_proba")[:, 1]
            auc = roc_auc_score(y, proba)
            dproba = cross_val_predict(make_pipeline(DummyClassifier(strategy="stratified")),
                                       X, y, cv=KFold(5, shuffle=True, random_state=0), method="predict_proba")[:, 1]
            auc_dummy = roc_auc_score(y, dproba)
        except Exception as e:
            print("  (direction CV skipped:", e, ")")
        # in-sample AUC to expose the overfit gap
        clf.fit(X, y)
        insample = roc_auc_score(y, clf.predict_proba(X)[:, 1])
    else:
        insample = None

    # ---- (2) EXPECTANCY: realized R, 5-fold out-of-sample R² vs mean baseline ----
    kf = KFold(min(5, n), shuffle=True, random_state=0)
    reg = make_pipeline(StandardScaler(), Ridge(alpha=5.0))
    rpred = cross_val_predict(reg, X, yr, cv=kf)
    r2 = r2_score(yr, rpred)
    mae = mean_absolute_error(yr, rpred)
    dumb = cross_val_predict(DummyRegressor(strategy="mean"), X, yr, cv=kf)
    mae_dumb = mean_absolute_error(yr, dumb)

    print(f"\n  (1) DIRECTION  win/loss — out-of-sample (LOO-CV)")
    if auc is not None:
        gap = f"  ·  in-sample {insample:.2f} (overfit gap {insample-auc:+.2f})" if insample else ""
        print(f"      AUC {auc:.2f}   vs dummy {auc_dummy:.2f}   (0.50 = coin flip){gap}")
    else:
        print(f"      too few of one class to test honestly")
    print(f"\n  (2) EXPECTANCY  realized R — out-of-sample ({min(5,n)}-fold)")
    print(f"      R² {r2:+.2f}  (negative = worse than guessing the mean)")
    print(f"      MAE {mae:.2f}R   vs mean-baseline {mae_dumb:.2f}R   (lower is better)")

    # ---- feature signal (only meaningful if the model beats baseline; shown as directional hint) ----
    clf2 = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=1000, class_weight="balanced"))
    clf2.fit(X, y)
    coef = clf2.named_steps["logisticregression"].coef_[0]
    order = np.argsort(-np.abs(coef))
    print(f"\n  feature lean (direction, standardized — HINT only until CV clears the bar):")
    for i in order[:6]:
        print(f"      {keys[i]:<12} {coef[i]:+.2f}  ({'wins' if coef[i]>0 else 'loses'} as it rises)")

    # ---- the GATE: has the model earned the right to touch the live score? ----
    ready = (n >= MIN_N_READY) and (auc is not None and auc >= MIN_AUC_READY) and (auc - (auc_dummy or 0.5) >= 0.05)
    print(f"\n  {'='*54}")
    if ready:
        print(f"  ✅ READY — n={n}, OOS AUC {auc:.2f}. The model may now INFORM the score (start at low weight).")
    else:
        why = []
        if n < MIN_N_READY:
            why.append(f"n={n} < {MIN_N_READY}")
        if auc is None or auc < MIN_AUC_READY:
            why.append(f"OOS AUC {auc if auc else '—'} < {MIN_AUC_READY} (direction still ~coin flip)")
        print(f"  ⛔ NOT READY — {' ; '.join(why)}.")
        print(f"     Keep FEEDING it (trade + log). Do NOT wire an unproven model into the score — that's")
        print(f"     the crystal-ball trap. The honest edge stays execution + risk + exits until this clears.")
    print()
    return {"n": n, "auc": auc, "auc_dummy": auc_dummy, "r2": r2, "ready": ready}


if __name__ == "__main__":
    run()

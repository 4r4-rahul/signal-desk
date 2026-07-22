# SPY mechanics review — chip shadow desk

**Scope:** SPY only. Every number below was measured in this repo on 2026-07-22. A parallel review covers QQQ;
this file touches nothing outside SPY.

**Labels:** `MEASURED` = computed from real data in this repo, with the sample stated.
`THEORY` = derived analytically from measured inputs. `GUESS` = judgement, no supporting sample.

---

## 0. Verdict first

**No configuration of *mechanics* makes SPY intraday scalping positive-expectancy on this account.
Mechanics can only shrink a certain loss. The remaining gap must be closed by directional skill the
system has not demonstrated.**

But the gap is much smaller than the desk currently believes, and the desk is currently paying for it in
the wrong place. Four findings, in order of size:

1. `MEASURED` The SPY 1DTE ATM friction floor is **−0.051R ± 0.015R**, not the −0.142R the desk has been
   working from. The −0.142R null is a population number across all providers, tickers and DTEs. SPY 1DTE ATM
   is the cheapest instrument on the desk: it quotes **penny-wide** and its round trip costs **1.32 % of
   premium** including commission. The hurdle is real but it is one third the size assumed.
2. `MEASURED` **0DTE is catastrophic for SPY under a premium-% stop and 1DTE is not** — −0.226R vs −0.051R at
   identical geometry, a 4.4× difference. This is the single largest lever in the system and the desk has
   already pulled it (GEOM g2). Do not un-pull it. Section 2.
3. `MEASURED` **The hold curve is a 0DTE artifact.** At 1DTE the curve is flat: −0.046R at 5 min → −0.059R at
   60 min. Shortening MAX_HOLD to 5 min buys **+0.005R ± 0.003R** and *raises* the directional-accuracy hurdle
   from 54.9 % to 56.9 %. The "5 m is materially better" prior does not hold for SPY at 1DTE. Section 3.
4. `MEASURED` **The three-tier scale-out is the second most expensive mechanic on SPY, after 0DTE** — it costs
   **0.0135R ± 0.0050 per trade with no alpha at all** (paired test, z = +2.7), about a quarter of the whole
   SPY friction floor, by capping half the position at +20 % while the loss side still runs to −26 %. On SPY
   the exit discipline costs more than the broker does. Section 2c.

The break-even directional accuracy on SPY's next 20 minutes, measured by injecting a calibrated oracle into
the replay, is **54.9 % ± 0.6 %** today, **≈ 54.2 %** under the recommended configuration, and **~72 %** at
0DTE. Section 5a.

---

## 1. What I measured, and how far to trust it

### 1a. Data

| source | sample |
|---|---|
| `signal_desk.db` `path` table, SPY rows | 11,083 stored option ticks; **7,140** in RTH with a two-sided quote; **1,072** at \|Δ\| 0.25–0.85. Dates 2026-07-09 … 07-22. Real IBKR bid/ask/Δ/IV/θ/spot. |
| IBKR 1-min TRADES bars, SPY, RTH (`client_id 14`) | **17,160 bars / 44 sessions**, 2026-05-18 … 2026-07-21 |
| `channels/chip/paper_journal.csv` | chip's 1 live SPY emit (SPY 745P @ $2.16) — matches the model's 1DTE ATM level exactly |

### 1b. Replay model and its validation

Real SPY 1-min paths, Black-Scholes-repriced ATM options, costed exactly like `db._shadow_scaleout`
(entry at ask, exit at bid, $1.30/contract once), exits via a verbatim port of `_shadow_scaleout`.
The model contains **zero alpha by construction** — it prices real paths, so any conditional edge it reports
comes from the tape, not the model.

Calibration checks, all `MEASURED`:

- Time convention: solving BS for effective T on 1,492 real 0DTE SPY ticks recovers **wall-clock minutes to
  16:00 ET**, ratio 1.03–1.12 across the day. So T = calendar minutes / 525,600, with a 1.09× correction for
  0DTE.
- With that T and IBKR's own IV, BS reproduces the real mid to **−0.4 % at 1DTE** and **−5.7 % at 0DTE**
  (n = 311 / 723 ATM ticks). The 0DTE residual is what the 1.09× multiplier absorbs.
- ATM IV term structure, SPY: **<60 min 0.249 · 60–180 min 0.175 · 180–390 min 0.167 · 1DTE 0.130**.
  Replay uses 0.175 (0DTE) and 0.130 (1DTE).
- Quoted spread model `$0.010 + 0.003·P` reproduces the measured 1.10 % at 0DTE ATM ($1.04) and 0.78 % at
  1DTE ATM ($2.17).

**Sensitivity on the one weak input (the spread model).** 1DTE null at hold 20 / stop 26:

| spread model | 0DTE | 1DTE | 2DTE |
|---|---|---|---|
| flat $0.01 (optimistic) | −0.215 | −0.040 | −0.023 |
| `0.010+0.003P` (fitted) | −0.226 | **−0.051** | −0.034 |
| `0.008+0.006P` (conservative) | −0.229 | −0.059 | −0.042 |

**Path validation — does the model decay at the right rate?** The obvious objection to a calendar-time theta
clock is that market makers mark short-dated options on a *trading*-time clock, which would make the replay
under-charge theta during RTH and over-credit gamma. Tested directly: for every real SPY option tick pair
~20 minutes apart in RTH (from `path`, spot present, two-sided quotes), compare the **real** premium change to
what the replay's pricer would have produced from the same two spots.

| | pairs | real Δ% over 20 min | model Δ% | **model − real** (median) |
|---|---|---|---|---|
| 0DTE | 1,662 | +0.34 % (median) | +3.12 % | **+0.28 pp** (model slightly optimistic) |
| **1DTE** | **673** | **+0.37 %** | **+0.26 %** | **−0.31 pp** (model slightly *pessimistic*) |
| 2DTE | 265 | −5.13 % | −4.14 % | −0.51 pp |

`MEASURED` The clock objection does not hold at 1DTE — the model tracks the real 20-minute drift to within
0.31 pp of premium, on the conservative side. It over-credits 0DTE slightly, which means the 0DTE numbers below
are, if anything, **too kind to 0DTE**. Note also the empirical headline: **the real median 20-minute change in
a SPY 1DTE ATM premium is +0.37 %** — theta and gamma net to approximately zero during RTH. That is the
measured basis for "at 1DTE, holding costs almost nothing."

Folding both sources together: **the SPY 1DTE friction floor is −0.051R ± 0.015R**, and every conclusion below
survives the whole band.

### 1c. Statistics discipline

Two corrections applied to every conditional result in §4, and they matter enormously:

- **Drift neutralisation.** Over the 44-session sample SPY drifted; raw puts beat raw calls by 0.048R
  (z = 3.7 naive). That is the sample's realised direction, not an edge. All conditional numbers are
  side-demeaned.
- **Session-block bootstrap.** Overlapping minute entries inside one session are not independent; the naive
  SE on n = 33,282 is a fiction. All SEs are 400–500 resamples over the 43–44 *sessions*. This typically
  inflates the SE **4–8×** and it is why almost everything in §4 that "looked significant" is not.

---

## 2. Is the 20/26 premium geometry right for SPY?

### 2a. What T1 and the stop actually require of the underlying

`chip_strike.py` already carries the key algebra: under a %-of-premium stop,
`dS_T1 / dS_stop = t1_pct / stop_pct` — Δ and premium cancel. What does **not** cancel is the absolute size of
`dS_T1 = t1% · P / Δ` relative to what SPY delivers in the hold window. That is the geometry question.

`MEASURED` — SPY 1-min forward moves, 44 sessions, spot ≈ 750:

| hold | median \|ΔS\| | median MFE | p75 MFE | **σ(ΔS)** |
|---|---|---|---|---|
| 5 m | 0.280 pt | 0.260 | 0.500 | 0.601 |
| 10 m | 0.390 | 0.370 | 0.730 | 0.824 |
| 20 m | 0.520 | 0.520 | 0.990 | **1.138 pt (15.2 bp)** |
| 30 m | 0.660 | 0.620 | 1.210 | 1.368 |
| 60 m | 0.960 | 0.850 | 1.660 | 1.916 |

Daily RTH range: p10 **0.58 %** · median **0.90 %** · p90 **1.72 %**.

`THEORY` (from measured premiums/Δ) — what the current 20/26 ladder demands:

| contract | T | ATM prem | Δ | **dS for T1** | dS for stop | T1 in bp | T1 / σ₂₀ |
|---|---|---|---|---|---|---|---|
| 0DTE @ 09:45 | 375 m | $1.40 | 0.50 | 0.558 pt | 0.726 | 7.4 | 0.49 |
| 0DTE @ 12:00 | 240 m | $1.12 | 0.50 | 0.447 | 0.581 | 6.0 | 0.39 |
| 0DTE @ 14:00 | 120 m | $0.86 | 0.50 | 0.343 | 0.446 | 4.6 | 0.30 |
| **1DTE @ 12:00** | 1680 m | **$2.20** | 0.50 | **0.877 pt** | **1.140 pt** | **11.7** | **0.77** |
| 2DTE @ 12:00 | 3120 m | $2.72 | 0.50 | 1.084 | 1.409 | 14.5 | 0.95 |

`MEASURED` — empirical probability SPY's MFE reaches `dS_T1` inside the hold:

| contract | 5-min hold | 20-min hold |
|---|---|---|
| 0DTE @ 12:00 | 29.2 % | 55.3 % |
| 0DTE @ 14:00 | 39.3 % | 64.1 % |
| **1DTE @ 12:00** | **10.0 %** | **29.7 %** |
| 2DTE @ 12:00 | 6.2 % | 22.1 % |

### 2b. Reading

The 26 % stop at 1DTE is **1.140 pt = 1.00 σ of the 20-minute move**. That is an unusually well-calibrated
number — it is exactly where a 20-minute stop belongs. **Keep it.**

T1 at 20 % is 0.77 σ, touched 29.7 % of the time in 20 minutes, 10.0 % in 5 minutes. Two consequences:

- The desk's ~36 % T1-before-stop base rate is consistent with these numbers; nothing is broken.
- **At a 5-minute hold the whole 20/42/68 ladder is decorative** — 90 % of trades exit at the mark without
  touching any rung. A 5-minute "scalp" at 1DTE is not a scalp; it is a coin flip on the mid at minute 5,
  minus friction. This is the strongest argument against shortening the hold.

### 2c. Is a nearer or farther T1 better?

`MEASURED` — geometry grid, 1DTE, hold 20, null entries, n = 30,788 each (naive SEs shown; same-sample
comparisons so the ranking is more reliable than the levels):

| stop | t1 = 0.5·stop | 0.77·stop (**current**) | 1.0·stop | 1.5·stop |
|---|---|---|---|---|
| 20 | −0.0993 | −0.0867 | −0.0766 | −0.0656 |
| **26** | −0.0656 | **−0.0526** | −0.0474 | −0.0382 |
| 35 | −0.0347 | −0.0274 | −0.0225 | −0.0172 |

Farther targets are monotonically better under a null. The mechanism is the scale-out itself: taking ½ off at
T1 and moving the stop to breakeven converts a symmetric bet into one where you cap half your upside and then
get churned out of the remainder at breakeven. Against a driftless process that is negative carry.

To size this properly I re-ran it **paired** — identical entry minutes, only the ladder changed — so the
comparison is not exposed to between-sample noise. 1DTE / stop 26 / hold 20, n = 33,282 each, drift-neutralised,
session-block bootstrap on the *paired difference*:

| ladder | R | **paired Δ vs current** | z |
|---|---|---|---|
| **20/42/68 (current)** | −0.0511 ± 0.0031 | — | — |
| 26/55/88 (t1 = 1.0 × stop) | −0.0446 ± 0.0033 | **+0.0065 ± 0.0015** | **+4.3** |
| 33/69/112 (t1 = 1.27 × stop) | −0.0379 ± 0.0040 | **+0.0133 ± 0.0031** | **+4.3** |
| **no scale-out, all out at +26 %** | −0.0376 ± 0.0055 | **+0.0135 ± 0.0050** | **+2.7** |

`MEASURED` **The three-tier scale-out costs 0.0135R ± 0.0050 per trade with no alpha at all — about a quarter
of the entire SPY friction floor.** And the benefit saturates exactly where the ladder stops firing: pushing T1
out to 1.27 × stop recovers the *same* 0.0133R as removing the scale-out altogether. In other words the whole
gain comes from **the scale-out not happening**, not from where the rungs sit.

That makes the honest recommendation the simple one: on SPY, **drop the scale-out and run a 1 : 1 bracket** —
all out at +26 %, stop −26 %, 20-minute time stop. Deliberately parking T1 at 1.27 × stop so that it almost
never triggers is the same trade dressed up as a ladder; if the ladder is not going to fire, do not print it on
the card.

> **A caution on the size of this effect.** An earlier oracle-based estimate in an intermediate run put this at
> 0.024R and a 2.0 pp accuracy shift. That run drew one random side per minute and the difference was inside
> its Monte-Carlo noise. The paired numbers above supersede it. The correct magnitude is **+0.0135R ± 0.0050**,
> which shifts the break-even accuracy by about **0.6 pp**, not 2 pp.

**Verdict on §2: the *stop* is not the bug — the *target and the ladder around it* are.** The 26 % stop sits at
1.00 σ₂₀ and belongs there. T1 at 0.77 σ is too near, and the scale-out built on it is expensive (§2c). No
entry filter can fix a geometry that gives away 0.024R before the market moves, and equally, fixing the
geometry cannot by itself make the strategy positive (§6).

### 2d. The structural fix worth making

`THEORY` A fixed `stop_pct` is only right for one (DTE, time-of-day, IV) triple. The same 26 % is
1.00 σ₂₀ at 1DTE and **0.39 σ₂₀** at 0DTE 14:00 — the same label naming two completely different bets, which
is precisely the failure that pushed the desk to GEOM g2. Pinning DTE fixes the symptom. The general fix is to
derive the stop at emit time in **underlying** terms:

```
sigma_hold_pts = sigma_1min_pts * sqrt(hold_min)         # sigma_1min from the live 1-min bars
stop_pct = clamp(100 * K_STOP * sigma_hold_pts * |delta| / premium, 18, 40)   # K_STOP = 1.0
t1_pct   = stop_pct                                       # 1:1
t2_pct   = 2.1 * t1_pct ;  t3_pct = 3.4 * t1_pct          # keep the existing ladder ratios
```

At today's SPY (σ₁ₘ ≈ 0.255 pt, 1DTE ATM $2.20, Δ 0.50) this returns **stop_pct 26** — it reproduces the
current constant, which is the point: it is the same number when conditions match, and it *moves* when they
don't (a 20 % IV day would widen it to ~34, a dead tape narrow it to ~20). `GUESS` on the clamp bounds; they
are there only to stop a bad σ estimate producing an absurd stop.

---

## 3. DTE — resolving the 0DTE / 1DTE trade-off

The trade-off as posed was: 0DTE stops sit ~0.5 σ out (good, they resolve) but pay 2.9–3.6 % spread vs 0.55 %
at 1DTE (bad). **Both halves need correcting.**

### 3a. The spread half of the argument is wrong for ATM SPY

`MEASURED` — SPY option quotes, RTH, two-sided, by \|Δ\| band:

| DTE | \|Δ\| band | median premium | median spread % | **median spread $** | RT cost % incl. $1.30/ct |
|---|---|---|---|---|---|
| 0 | 0.25–0.40 | $0.62 | 1.60 % | $0.010 | **3.68 %** |
| 0 | **0.40–0.65** | $1.04 | **1.10 %** | **$0.010** | **2.43 %** |
| 0 | 0.65–0.85 | $1.50 | 1.36 % | $0.015 | 2.38 % |
| 1 | 0.25–0.40 | $1.13 | 0.99 % | $0.011 | 2.22 % |
| 1 | **0.40–0.65** | $2.17 | **0.78 %** | **$0.017** | **1.32 %** |
| 1 | 0.65–0.85 | $4.13 | 1.05 % | $0.043 | 1.36 % |

Absolute ATM spread distribution: **$0.01 on 63.7 % of ticks, $0.02 on 27.4 %, ≥$0.03 on 8.9 %.**
Percentiles, 1DTE ATM: p50 0.78 % · p90 1.18 % · p95 **1.30 %** · p99 1.72 % · max 2.83 %.
0DTE ATM: p50 1.10 % · p90 1.68 % · p95 2.22 % · p99 3.51 %.

**SPY options are penny-wide at the money, at every DTE.** 0DTE's *absolute* spread ($0.010) is **smaller**
than 1DTE's ($0.017). The 2.9–3.6 % figure in the brief is a premium-denominator effect measured on cheap
sub-ATM contracts, not a liquidity property of 0DTE. Per unit of delta exposure, 0DTE spread + commission is
actually the *cheaper* of the two (4.6 % of a 1 σ₂₀ move vs 5.2 %).

### 3b. So what actually kills 0DTE is theta, and it is enormous

`THEORY` ATM extrinsic decays as √T, so decay over a hold `h` is ≈ `0.5·h/T` of premium:

| | 20-min hold | as R against a 26 % stop |
|---|---|---|
| 1DTE (T ≈ 1680 m) | 0.60 % of premium | **0.023 R** |
| 0DTE @ 12:00 (T = 240 m) | 4.17 % | **0.160 R** |
| 0DTE @ 14:00 (T = 120 m) | 8.33 % | **0.321 R** |

### 3c. Measured, end to end

`MEASURED` — replay null (zero alpha), 44 sessions, integer-sized on a $25,000 / 1 %-risk book:

| config | n | R | ± | win % | ATM $ | qty | $ risk | $ friction | **$ / trade** |
|---|---|---|---|---|---|---|---|---|---|
| 0DTE stop 26 hold 5 | 30,624 | −0.146 | 0.002 | 42.6 | 1.10 | 8 | 228 | 21.04 | **−33.40** |
| 0DTE stop 26 hold 20 | 30,624 | −0.226 | 0.007 | 45.9 | 1.10 | 8 | 228 | 21.04 | **−51.57** |
| 0DTE stop 40 hold 20 | 30,624 | −0.128 | 0.008 | 45.2 | 1.10 | 5 | 220 | 13.15 | −28.09 |
| **1DTE stop 26 hold 5** | 33,282 | −0.046 | 0.001 | 43.4 | 2.22 | 4 | 231 | 11.87 | −10.62 |
| **1DTE stop 26 hold 20** | 33,282 | **−0.051** | 0.003 | 47.0 | 2.22 | 4 | 231 | 11.87 | **−11.83** |
| 1DTE stop 26 hold 60 | 33,282 | −0.059 | 0.006 | 50.0 | 2.22 | 4 | 231 | 11.87 | −13.74 |
| 1DTE stop 40 hold 20 | 33,282 | −0.018 | 0.002 | 46.1 | 2.22 | 2 | 178 | 5.93 | −3.14 |
| 2DTE stop 26 hold 20 | 32,508 | −0.034 | 0.002 | 46.5 | 2.88 | 3 | 225 | 9.49 | −7.56 |

(The 44-session sample includes Fridays, where `dte_pref 1` snaps to Monday and carries a full weekend of
calendar decay at a flat 0.130 IV. Real weekend IV is discounted, so the 1DTE line above is if anything
slightly **pessimistic**.)

**Conclusion:** `dte_pref 1` is correct and is worth **+0.175R per trade** over 0DTE — by far the largest
effect anywhere in this review. 2DTE measures better still (−0.034 vs −0.051) but the gap is inside the
spread-model band (§1b), so treat it as an A/B candidate, not a change.

### 3d. Do not read the "wider stop is better" column as an edge

`THEORY` Rows like *1DTE stop 40 → −$3.14/trade* look like free money. They are not. Friction is per
**contract**; at a fixed 1 % risk budget a wider stop simply buys fewer contracts. The invariant is

```
friction per contract / delta exposure per contract  =  $2.97 / (100 · 0.50 · 1.138 pt)  =  5.2 %
```

— independent of stop width and of position size. Widening the stop reduces the dollar bleed only by
**betting less**, and it reduces any future alpha by exactly the same factor. It is a leverage dial, not an
expectancy fix.

The one place this looks violated is the *stop 40* row in the §5a oracle table, whose break-even accuracy is a
suspiciously good 0.522. That is not the stop. The desk defines the ladder as a multiple of the stop, so a
40 % stop silently ships a 31 % (1.19 σ) T1 — it is the §2c **ladder** effect wearing the stop's clothes. Hold
the ladder fixed and the hurdle does not move with stop width. **Read a wider *target* as an edge; never a
wider stop.**

---

## 4. Entry — what should gate a SPY scalp?

Every conditioner below is measured on the same 33,282-entry 1DTE / hold-20 / stop-26 replay, **side-demeaned**
and with **session-block bootstrap** SEs. Null = −0.0511 ± 0.0032.

| conditioner | n | R | vs null | z |
|---|---|---|---|---|
| 09:30–10:00 | 2,494 | −0.033 | +0.018 ± 0.009 | +2.1 |
| 10:30–11:30 | 5,160 | −0.046 | +0.005 ± 0.005 | +0.9 |
| 11:30–13:00 | 7,740 | −0.050 | +0.001 ± 0.005 | +0.2 |
| 13:00–14:30 | 7,740 | −0.058 | −0.007 ± 0.006 | −1.2 |
| 14:30–15:30 | 5,160 | −0.060 | −0.009 ± 0.005 | −1.6 |
| tape score 56–62 | 6,005 | −0.037 | +0.014 ± 0.024 | +0.6 |
| tape score 62–68 (**the current bar**) | 4,820 | −0.049 | +0.002 ± 0.028 | +0.1 |
| **tape score 68–74** | 2,192 | **−0.160** | **−0.108 ± 0.039** | **−2.8** |
| tape score ≥ 74 ("strong") | 901 | +0.008 | +0.059 ± 0.073 | +0.8 |
| VWAP −40…−20 bp against | 5,388 | −0.067 | −0.016 ± 0.028 | −0.6 |
| VWAP +20…+40 bp in favour | 5,388 | −0.046 | +0.005 ± 0.027 | +0.2 |
| **VWAP cross within 3 min (chip's trigger)** | 2,082 | −0.053 | **−0.002 ± 0.008** | **−0.2** |
| cross + trading the reclaim side | 1,041 | −0.067 | −0.016 ± 0.025 | −0.6 |
| opening range — any of 5 buckets | 3.8–9.6 k | — | \|Δ\| ≤ 0.013 | \|z\| ≤ 0.3 |
| vs prior-day close — any of 5 buckets | 3.1–8.1 k | — | \|Δ\| ≤ 0.005 | \|z\| ≤ 0.5 |
| **trailing 5-min range < 0.04 %** | 3,392 | −0.064 | **−0.013 ± 0.004** | **−3.0** |
| trailing 30-min range < 0.10 % | 3,198 | −0.064 | −0.013 ± 0.004 | −3.0 |
| 5-min counter-move ≤ −12 bp ("buy the dip") | 1,714 | +0.019 | **+0.070 ± 0.028** | **+2.5** |
| 5-min continuation ≥ +12 bp ("chase") | 1,714 | −0.126 | −0.070 ± 0.037 | −1.9 |

### 4a. Reading

- **Time of day carries nothing for SPY.** Every bucket is inside ±0.009R of the null and the largest
  \|z\| is 2.1 — and it has the *opposite* sign to the desk's own 09:30–10:00 = −0.267R finding. That earlier
  number is almost certainly a chip-entry-selection effect (what chip *did* at the open), not a property of
  the SPY open. `MEASURED` Do not build a time-of-day gate off it.
- **The 62 bar buys literally nothing** (+0.002 ± 0.028) and the band immediately above it, 68–74, is the
  worst pocket in the whole table (−0.108 ± 0.039, z = −2.8). This corroborates the desk's anti-predictiveness
  finding on SPY-only data with clustered SEs. The ≥74 "strong" band is noise, not a rescue (n = 901, ±0.073).
- **The VWAP-cross trigger contributes −0.002R ± 0.008R.** It is not harmful; it is *nothing*. The desk's
  structural argument (cross ⇒ the 0.35 VWAP term collapses ⇒ score ≥ 62 becomes unreachable) is confirmed by
  the fact that the two gates together select 1,041 of 33,282 minutes, i.e. 3 %, for zero measurable benefit.
- **The only robust positive-sign signal — buy-the-5-min-dip, +0.070R ± 0.028R — does not replicate.**
  Split-half on the same 44 sessions: **first half +0.080R, second half −0.059R** (null in both halves
  −0.051R). Sign flip across halves of a two-month sample. `MEASURED` **Discard it.** Everything above about
  chasing being bad is the mirror of the same unstable statistic and should be discarded with it.
- **The one filter that survives everything is negative: do not trade a dead tape.** SPY 5-min range < 0.04 %
  (≈ 0.30 pt) costs −0.013R ± 0.004R (z = −3.0), and it is the same story at 30 min. This is mechanically
  sensible — a tape that does not move cannot pay the spread — and it is the only entry rule in this review I
  would actually ship. It applies to ~10 % of minutes, so its population value is ≈ **+0.002R**; its real value
  is that it stops chip firing into conditions where it *cannot* win.

### 4b. On SPY-vs-ES basis

`GUESS` Do not build it. The SPY/ES basis is arbitraged sub-millisecond by co-located participants; by the time
it reaches a 5-second Python loop through an IBKR socket it carries no information. Nothing in the data
contradicts this, but nothing tests it either — I am flagging it so it does not get built on a hunch.

### 4c. The honest summary of §4

**Across ~14 conditioners on 33,282 SPY entries over 44 sessions, nothing produces stable positive alpha
above the friction floor.** One conditioner cleared it in-sample and failed a split-half. That is the expected
result when a feature set of standard 1-min technicals is applied to the most efficiently priced instrument in
the world.

---

## 5. Recommended SPY configuration

| constant | current | **recommended** | label | justification |
|---|---|---|---|---|
| `dte_pref` | 1 | **1** (keep) | MEASURED | −0.051R vs −0.226R at 0DTE. Largest lever in the system. |
| `dte_max` | 2 | **2** (keep) | MEASURED | 2DTE measures −0.034R but the edge is inside the spread-model band; allow the snap, don't prefer it. |
| `stop_pct` | 26 | **26** (keep) | MEASURED | 26 % of a $2.20 ATM 1DTE at Δ 0.50 = 1.14 pt = **1.00 σ₂₀**. Correctly placed. |
| `t1_pct` | 20 | **26** | MEASURED | 1 : 1 bracket. Paired **+0.0065R ± 0.0015** (z = 4.3) on its own; **+0.0135R ± 0.0050** once the scale-out is also dropped. Nearer T1 loses to breakeven-stop churn (§2c). |
| **scale-out** | ½ @ t1, ¼ @ t2, ¼ @ t3 | **all out at t1 on SPY** | MEASURED | +0.0135R ± 0.0050 (z = 2.7). Keep the ladder on the card as information; stop letting it place orders on SPY. |
| `t2_pct` / `t3_pct` | 42 / 68 | **n/a on SPY** | MEASURED | With a single exit there is nothing for them to do. If the desk insists on keeping the three tiers, use 26/55/88 and take the smaller +0.0065R. |
| `spread_max` | 3 | **1.5** | MEASURED | 1DTE ATM p95 = 1.30 %, p99 = 1.72 %. A 3 % cap never binds; 1.5 % makes it a real circuit-breaker for auction/halt/stale quotes. |
| `strike_inc` | 1.0 | **1.0** (keep) | MEASURED | SPY lists $1 strikes; ATM is always available. |
| `MAX_HOLD_S` | 1200 | **1200** (keep) | MEASURED | Break-even hit rate is minimised at 20–30 min (54.9 %) and rises to 56.9 % at 5 min. §5a. |
| `MIN_TAPE_MODERATE` | 62 | **disable as a gate** | MEASURED | +0.002R ± 0.028R. The band above it is −0.108R. Keep the score as a *displayed* number; stop letting it decide. |
| VWAP-cross trigger | required | **make it optional/advisory** | MEASURED | −0.002R ± 0.008R. Costs 97 % of candidate minutes for no measurable benefit. |
| **new:** `MIN_RANGE5_PCT` | — | **0.05** | MEASURED | Refuse entry when SPY's trailing 5-min range < 0.05 % (~0.37 pt). The dead-tape decile is −0.013R ± 0.004R (z = −3.0). |
| `chip_strike.DELTA_MIN` (SPY) | 0.25 | **0.40** | MEASURED | Round-trip cost 3.68 % at \|Δ\| 0.25–0.40 vs 1.32 % at 0.40–0.65 — 1.6× more per unit of delta exposure. ITM is *not* penalised (1.36 %), so the "lowest-leverage affordable survivor" rule stays exactly as is. |
| `COOLDOWN_S` | 900 | **900** (keep) | GUESS | No measurement bears on it. Leave it. |

### 5a. Break-even directional accuracy — MEASURED, not theorised

I injected an oracle into the replay: at each entry the side is the *correct* one with probability p, wrong
otherwise, where "correct" is the sign of SPY's move over the hold window. Sweeping p locates the break-even.

| config (ladder 0.77/1.6/2.6 × stop) | p = 0.50 | 0.52 | 0.54 | 0.56 | 0.60 | **break-even p** |
|---|---|---|---|---|---|---|
| 1DTE stop 26 hold 5 | −0.044 | −0.034 | −0.025 | −0.006 | +0.021 | 0.569 |
| 1DTE stop 26 hold 10 | −0.047 | — | −0.011 | +0.001 | — | 0.560 |
| **1DTE stop 26 hold 20** | −0.058 | −0.027 | −0.007 | +0.010 | +0.049 | **0.549 ± 0.006** |
| 1DTE stop 26 hold 30 | −0.054 | — | −0.005 | +0.007 | — | 0.549 |
| 1DTE stop 26 hold 45 | −0.066 | — | −0.023 | +0.012 | — | 0.553 |
| 1DTE stop 26 hold 60 | −0.051 | — | −0.017 | +0.007 | — | 0.552 |
| 1DTE stop 40 hold 20 | −0.013 | — | +0.011 | +0.028 | — | 0.522 |
| 2DTE stop 26 hold 20 | −0.037 | — | +0.003 | — | — | 0.540 |
| 1DTE stop 26 hold 20, 1 : 1 ladder | −0.037 | — | +0.000 | +0.021 | — | 0.540 † |
| **0DTE stop 26 hold 20** | −0.216 | −0.194 | −0.180 | −0.140 | −0.103 | **≈ 0.72** |

† The oracle draws one random side per minute, so the *difference* between two ladder rows carries Monte-Carlo
noise of order ±0.008R — larger than the effect being measured. The paired test in §2c is the authority on
ladder effects and puts the shift at ≈ 0.6 pp (0.549 → ≈ 0.543), not 0.9 pp. The hold and DTE rows in this
table are unaffected: those effects are 5–20× larger than the noise.

Four things fall out:

1. **The hurdle is minimised at a 20–30 minute hold.** MAX_HOLD_S 1200 is already sitting at the optimum, and
   the curve is genuinely flat from 20 to 60 minutes (0.549 → 0.552). Shortening to 5 minutes *raises* the
   required accuracy by 2.0 pp — the same directional call has less time to express itself against a fixed
   per-trade cost. This is the direct refutation of the "shorter is always better" prior, for SPY at 1DTE.
2. **0DTE is not reachable at all.** Even a 60 %-accurate oracle loses 0.103R per trade. No entry model this
   desk can build closes a gap that size.
3. **The stop-40 row is not the free lunch it looks like.** Its break-even p of 0.522 is *not* a stop-width
   effect — it is the ladder effect from §2c reappearing, because the ladder is defined as 0.77 × stop, so a
   40 % stop silently buys a 31 % (1.19 σ) T1. Holding the ladder fixed and changing only the stop leaves the
   hurdle unchanged, as §3d predicts. Do not read a wider stop as an edge; read a wider **target** as one.
4. **After DTE, the largest single mechanical improvement available is the exit ladder — not the stop, the
   hold, or any entry gate** (§2c, paired: +0.0135R ± 0.0050, ≈ 0.6 pp off the hurdle).

### 5b. Expected effect of the recommended changes

| change | effect on E[R] | error bar |
|---|---|---|
| **t1 20 → 26 with a single exit (drop the scale-out on SPY)** | **+0.0135** (paired) | ± 0.0050 |
| dead-tape filter (`rng5 ≥ 0.05 %`) | +0.002 population, +0.013 on the excluded decile | ± 0.004 |
| DELTA_MIN 0.25 → 0.40 (SPY) | +0.000 to +0.020, depends how often the OTM rung was being picked | wide; not separately measured |
| spread_max 3 → 1.5 | ≈ 0 in the mean; removes a fat left tail | not measurable at this n |
| drop the 62 bar + cross requirement | **+0.000 expectancy, ×30 sample rate** | ± 0.028 |
| **net** | **≈ +0.016R ± 0.007R** | |

That takes SPY from **−0.051R → −0.035R**, and the break-even directional accuracy from **54.9 % → ≈ 54.2 %**.
It does not reach zero and nothing in this review can. The honest framing of the whole change set is: *it stops
chip paying for mechanics that do not work, and it raises the sample rate ~30× so that whether chip has any
entry edge at all becomes answerable in weeks instead of years.*

Note what is **not** on this list: any entry signal. Every measured gain here is a cost reduction. That is the
finding, not an omission.

### 5c. What I would NOT change, and why

- **`stop_pct` 26.** It is 1.00 σ₂₀ at 1DTE ATM. That is where a 20-minute stop belongs. Every "improvement"
  from widening it is a size reduction in disguise (§3d).
- **`MAX_HOLD_S` 1200.** Measured optimum (§5a). The instinct to cut it to 5 minutes is the single most
  expensive change available in this file, and it is expensive in a way the R-curve hides.
- **`dte_pref` 1.** Already the biggest correct decision in the system.
- **`chip_strike`'s objective** (lowest-leverage affordable survivor). Measured cost per unit of delta
  exposure is flat from ATM to Δ 0.85 and 1.6× worse below Δ 0.40 — so the rule points the right way. Only
  the floor moves.
- **`COOLDOWN_S` 900.** No data. Do not tune what you cannot measure.
- **The tape score itself.** Remove it as a *gate*; do not invert it. The 68–74 pocket is one significant
  band out of six tested — inverting it would be fitting exactly the noise this review is trying to avoid.
- **Anything requiring more than ~5 signals/day to validate.** At chip's rate (1 SPY emit in 3 sessions
  observed), a 0.05R effect needs order 10³ trades to see. Every recommendation above is therefore either a
  cost reduction (deterministic, no sample needed) or a gate removal (raises the sample rate).

---

## 6. Verdict — is positive expectancy reachable on $25k?

**No, not from mechanics. Yes, arithmetically, but only with directional accuracy the system has not shown.**

The arithmetic, all `MEASURED` inputs:

```
Instrument   SPY 1DTE ATM, ~$2.20, delta 0.50, penny-to-nickel wide
Risk budget  $25,000 x 1% = $250  ->  4 contracts at a 26% stop  ->  $231 actual risk
Friction     4 x ($0.017 x 100 spread + $1.30 comm+fees) = $11.87  =  5.1% of risk
Theta        1DTE over 20 min = 0.60% of premium = $5.3 ... offset by gamma at realised ~ implied
             (measured net: the 20-min null equals the spread+commission line almost exactly)

Null expectancy   -0.051R  =  -$11.83 per trade          (current configuration)
At 2 SPY trades/day:  -$23.6/day   -$5,949/yr   -23.8% of the account
At 5 SPY trades/day:  -$59.0/day  -$14,873/yr   -59.5% of the account
Same, if 0DTE were used:  -$51.6/trade  ->  -$25,900 to -$64,900/yr   (-104% to -260%)
Recommended config:       -0.035R  =  -$8.09/trade  ->  -$4,100 to -$10,200/yr  (-16% to -41%)

Break-even requires calling SPY's next 20 minutes correctly
    54.9% +/- 0.6%   at the current configuration
    54.2%            under the recommended configuration
```

**~54 % is the whole question.** It is not an absurd number — it is about 4 % of a standard deviation of
drift — which is why this is worth one more honest experiment rather than an immediate shutdown. But:

- 14 standard conditioners over 44 sessions and 33,282 SPY entries produced **nothing** that clears it with a
  stable sign.
- The desk's own live measurement says chip's selected minutes are **worse** than random (−0.219R vs −0.142R,
  z = −3.58), i.e. current accuracy is materially **below** 50 %.
- The instrument is SPY. If a 1-minute-technicals feature set could call it at 54 %, that fact would not have
  survived to 2026.

So the defensible position is:

1. **Ship the cost and gate changes in §5.** They are deterministic, they cannot make things worse, and they
   raise the sample rate ~30× — which is the only way this question gets answered at all.
2. **Do not size SPY up.** At −0.05R and 2–5 trades/day this bleeds 24–60 % of the account per year with no
   entry edge. `chip_paper.py`'s $25k sandbox is exactly the right place for it and it should stay there.
3. **Set an explicit falsification gate before go-live:** SPY goes live only if, over ≥ 150 shadow entries
   under the §5 configuration, the measured R exceeds **+0.02R** with a session-block-bootstrap SE that
   excludes zero. At the observed rate that is 8–12 weeks. If it does not clear, **the correct action is to
   stop trading SPY intraday at this size** — and that is a result, not a failure. The desk's edge is in
   grading *other people's* signals, and the measurement infrastructure built for chip is what makes that
   grading trustworthy. Chip does not have to be profitable for that to have been worth building.

---

## Appendix — reproduction

Working scripts (not committed; scratchpad only):
`fetch.py` (IBKR SPY 1-min, client_id 14) · `replay.py` (BS repricing + verbatim `_shadow_scaleout` port) ·
`an2.py` (drift-neutralised, session-bootstrapped conditioners) · `rev.py` (reversion + split-half) ·
`dollars2.py` (integer-sized dollar table) · `spr.py` (spread sensitivity) · `hurdle.py` / `hurdle3.py`
(oracle sweeps) · `ladder.py` (paired ladder test) · `geom.py` (move requirements).
All live in the session scratchpad under `spy_sme/`. No `.py` file in the repo was modified and nothing was
committed; the live server on :8787 was never restarted. IBKR was read-only on `client_id 14`.

Known limitations, stated so they are not discovered later:

1. **Barrier sampling.** The replay checks barriers on 1-minute closes, matching the desk's ~45–60 s poll
   cadence. A real broker-side stop would fill on intrabar extremes and be worse; a 5-second poll would be
   between the two. All numbers here are therefore mildly optimistic on the stop side, equally across configs.
2. **Constant IV per DTE.** No IV path, so a vol-crush or vol-spike entry is not modelled. This matters most
   around scheduled events (FOMC, CPI), which are not separately handled.
3. **44 sessions, one vol regime.** SPY median daily range in the sample was 0.90 %. A 2 %-range regime would
   move every σ-referenced number here proportionally — which is the argument for the σ-derived stop in §2d
   rather than a hard-coded 26.
4. **The `path` option sample is 20 SPY contracts over 10 trading days.** It is enough to pin spreads and the
   IV term structure (thousands of ticks) but not to say anything about SPY option behaviour across regimes.

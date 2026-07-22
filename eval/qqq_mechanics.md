# QQQ intraday scalp mechanics — review, measurement, and recommended configuration

SME review of `chip_desk.DEFAULT_MECHANICS["QQQ"]` and the machinery around it.
Scope: QQQ only. A parallel SME owns SPY. **No `.py` file was modified. Nothing was committed. No
signal was posted to the live desk. The live server was not restarted.**

Every claim below is labelled **MEASURED** (computed here from real data), **THEORY** (arithmetic /
model reasoning, no new data), or **GUESS** (judgement, stated so you can discount it).

---

## 0. Verdict, up front

**A positive-expectancy QQQ configuration is not proven to exist at $25k paying $1.30/contract, and it
is not excluded either. The best configuration I can build lands at +0.025R ± 0.021 per trade — the
point estimate is positive, the 95% interval straddles zero, and the system cannot tell the difference
inside 2 years of trading.**

The arithmetic that produces that verdict:

| | E[R]/trade | $/trade (1 ct, ~$187 risk) | $/yr at 5 QQQ trades/day |
|---|---|---|---|
| Current config, current entry gate | **−0.037** | −$4.88 | −$6,150 (−24.6% of $25k) |
| Proposed mechanics, entry gate unchanged | **−0.021** | −$3.95 | −$4,980 (−19.9%) |
| Proposed mechanics + proposed entry gate | **+0.025 ± 0.021** | +$4.61 ± $3.95 | +$5,810 ± $4,980 |

Three findings drive everything else:

1. **Mechanics are worth ~+0.015R and no more.** Every geometry change I can justify — wider T1, a
   trailing exit instead of the scale-out, one more strike ITM, a slightly wider stop — sums to
   **+0.0154R ± 0.0028** (MEASURED, paired, z = 5.5), and it is the *same* +0.015R under every entry
   gate I tested. That is real, free, and nowhere near enough. It moves you from −0.037R to −0.021R.
2. **The whole deficit is entry.** QQQ's round trip costs **0.747% of premium = 0.0267R** at the
   current geometry. Break-even therefore requires **0.91 bp of directional drift in QQQ over the
   hold** — any hold, because the cost is fixed and the drift is not. Nothing in `chip`'s current gate
   supplies it: chip's tape gate delivers **−0.07 bp ± 1.07** at 20 minutes.
3. **One entry signal in this study clears the bar and survives a train/test split** — the mega-cap
   divergence fade (§7.3), at **+1.56 bp ± 0.64** over 20 minutes. Net of the 0.91 bp cost that is
   **+0.65 bp ± 0.64 of edge**, i.e. one standard error from zero. It is a hypothesis worth running in
   shadow. It is not a reason to put money on it.

**Recommendation: ship the mechanics changes, swap the entry gate, keep chip shadow-only.** The
mechanics changes are well-powered and cost nothing to make. The entry change is the only thing that
can matter and it is unproven.

---

## 1. What I measured, and how to read it

### Data (all MEASURED, pulled fresh)

| Source | Content |
|---|---|
| IBKR 1-min RTH bars, client_id 16–18 | QQQ + SPY, **37 sessions**, 2026-05-29 → 2026-07-22 (14,376 / 14,377 minutes) |
| IBKR 1-min RTH bars | NVDA, AAPL, MSFT, same window (**36 sessions** common to all five symbols) |
| IBKR live option chain, 2026-07-22 **15:14 ET** | QQQ (spot 707.39) and SPY (spot 748.17), 0DTE + 1DTE, ATM ± 3, both rights — bid/ask/IV/greeks |
| IBKR `OPTION_IMPLIED_VOLATILITY` + `HISTORICAL_VOLATILITY`, daily, 3 M | per-session IV level for the replay |
| `data/QQQ_daily.json`, `data/SPY_daily.json` | 1-year overnight/intraday variance decomposition |

Scratchpad (scripts + cached data, nothing in the repo):
`/private/tmp/claude-501/-Users-rahul-Workspace-Copy-trading/53046cf7-c6bd-4419-be21-b10754d0fca6/scratchpad/qqq_sme/`

### The replay model (THEORY, but validated)

Real 1-min underlying paths. Options repriced each minute by Black–Scholes on a **measured variance
clock**: the 15-min intraday variance profile estimated over the same 37 sessions, plus a measured
overnight variance term, with the level calibrated so the 2026-07-22 chain is reproduced. Entry at the
ask, exit at the bid, spread modelled as `max(tick, 0.4% × premium)` with the **measured** tick
(QQQ $0.02, SPY $0.01 — median quoted spread over ATM ± 1, both rights, both expiries), plus
`db.RT_COST_PER_CT` = $1.30. Exits run through `db._shadow_scaleout` verbatim.

**Calibration check (MEASURED).** Model vs observed 1DTE ATM call at the snapshot:
QQQ $4.45 / Δ0.518 vs observed **$4.42 / Δ0.515**; SPY $2.35 / Δ0.513 vs observed **$2.41 / Δ0.520**.

**Fair-game check (MEASURED).** With costs zeroed and a pure stop + time-stop exit — a stopping rule on
a fairly-priced option must return zero — the engine gives **+0.0017R ± 0.0016** at 5 min and
**+0.0031R ± 0.0073** at 20 min. The model does not manufacture edge in either direction.

**The model is conservative.** My null hold curve runs −0.026R (5 m) → −0.058R (60 m) where the desk's
live-quote replay measured −0.050 → −0.219R. Same sign, same monotone shape, roughly half the
magnitude — my model holds IV fixed through the hold and does not simulate quote fade or a moving
market. **Read every negative number here as a floor on the bleed, and every positive one as
optimistic.**

Standard errors throughout are **day-block bootstraps** (resample sessions with replacement, 2,000–4,000
draws). Comparisons between configurations use a **paired** day-block bootstrap over the identical
entry set, which is far better powered than comparing two independent means.

---

## 2. The arithmetic floor: what a QQQ scalp costs before anything happens

MEASURED, from the 2026-07-22 15:14 ET chain:

```
QQQ 707C, exp 20260723 (1DTE):  bid 4.41 / ask 4.43   mid $4.42   Δ 0.515   IV 0.282
    spread      $0.02  = 0.452% of premium
    commission  $1.30  = 0.294% of premium     (db.RT_COST_PER_CT, tastytrade)
    ----------------------------------------
    round trip           0.747% of premium  =  0.0267R at a 28% stop
```

Convert that to what QQQ has to *do*:

```
required premium move   = 0.747% x $4.42            = $0.0330
required underlying move = $0.0330 / Δ 0.515        = $0.064  = 0.91 bp of QQQ
```

**Break-even needs +0.91 bp of directional drift in QQQ over the hold.** This number is independent of
the hold length, of t1, of t2, of t3, and of the stop — it is spread plus commission divided by delta.
It is the single most important number in this document. Everything in §7 is measured against it.

For scale (MEASURED, 37 sessions): QQQ's mean |Δ| over 5 min is 9.4 bp, over 20 min 17.9 bp. So
break-even asks you to extract **5.1% of a typical 20-minute move** as directional drift. That is not
an absurd bar. It is also not one that anything in the current system clears.

---

## 3. Is 22/28 the right premium geometry for QQQ?

### 3.1 What the barriers actually ask of the underlying

MEASURED, from the live chain and the 37-session move distribution:

| | QQQ 1DTE ATM | SPY 1DTE ATM |
|---|---|---|
| premium / delta / spot | $4.42 / 0.515 / 707.39 | $2.41 / 0.520 / 748.17 |
| T1 (`t1_pct`) needs | **0.267%** of spot ($1.89) | 0.124% ($0.93) |
| stop (`stop_pct`) needs | **0.340%** of spot ($2.40) | 0.161% ($1.21) |
| median \|Δ\| over 20 min | 0.121% | 0.071% |
| **T1 in units of a typical 20-min move** | **2.21** | **1.74** |

So at the current geometry a QQQ scalp is asking for a move **2.2× larger than the median 20-minute
move**, inside a 20-minute window. That is why the T1-first rate is what it is. It is not a bug — it is
what `t1_pct = 22` means when the option costs 0.63% of spot.

### 3.2 But T1-first is the wrong objective

This is the part I would most like the desk to internalise.

MEASURED (1DTE ATM, hold 20 m, 37 sessions, both sides, every minute):

| configuration | T1-first | E[R] |
|---|---|---|
| QQQ at 22/28 (current) | 14.95% | **−0.0359** |
| QQQ at 20/26 (SPY's geometry) | 17.57% | −0.0413 |
| SPY at 20/26 | 20.08% | −0.0565 |

**Narrowing T1 raises the T1-first rate and lowers expectancy.** Paired, same entries, MEASURED:

| change (vs t1 = 22, stop 28) | null entries | with an entry gate |
|---|---|---|
| t1 → 17 (0.6 × stop) | **−0.0043R ± 0.0014** (z −3.2) | −0.0078 ± 0.0020 (z −3.9) |
| t1 → 28 (1.0 × stop) | **+0.0034R ± 0.0012** (z +2.9) | +0.0052 ± 0.0018 (z +2.9) |
| t1 → 39 (1.4 × stop) | **+0.0061R ± 0.0024** (z +2.6) | +0.0088 ± 0.0037 (z +2.4) |

Monotone in both columns, consistent across gates. The current `t1_pct/stop_pct` = 22/28 = **0.79** is
too tight: banking half the position at 0.79 R truncates the right tail that has to pay for the stops.

THEORY for why: under any exit rule on a fairly-priced option, expectancy equals minus cost — *unless*
the rule is concave in the payoff, in which case it is worse. The scale-out is concave (half out at
+22%, quarter at +46%, runner capped at +74%) and the truncation cost scales with how convex the
instrument is. Confirmed by the zero-cost run below.

### 3.3 The scale-out itself, priced

MEASURED, **costs zeroed** so this is purely the exit rule's own EV, QQQ:

| exit rule | 1DTE, 5 m | 1DTE, 20 m | 0DTE, 5 m | 0DTE, 20 m |
|---|---|---|---|---|
| `_shadow_scaleout` (current) | −0.0010 | −0.0110 | **−0.0558** | **−0.1378** |
| same, uncapped runner (no t3) | −0.0010 | −0.0104 | −0.0502 | −0.1273 |
| all-out at T1 | −0.0033 | −0.0184 | −0.0851 | −0.1648 |
| stop + time stop only | +0.0017 | +0.0031 | +0.0044 | −0.0602 |
| trailing stop (22 pts off the peak) | +0.0017 | −0.0010 | +0.0064 | −0.0441 |

On 1DTE the scale-out costs about a penny — **−0.0110R at 20 min**. On 0DTE it costs
**−0.1378R**, which is most of why 0DTE fails (§5). Paired, with real costs:

| | null entries | with gate |
|---|---|---|
| stop-only vs scale-out, 20 m | **+0.0099R ± 0.0050** | **+0.0135R ± 0.0070** |
| trailing stop vs scale-out, 20 m | +0.0066 ± 0.0042 | +0.0112 ± 0.0059 |
| uncapped runner vs scale-out | +0.0003 ± 0.0005 | +0.0004 ± 0.0007 |

**`t3_pct` is a measured no-op.** At a 20–30 minute hold the runner essentially never reaches +74%; the
uncapped variant is +0.0003R. Leave it in, or take it out; it does not matter, and it is not worth an
edit.

### 3.4 Stop width

Widening the stop always improves R, because R = blended/stop and the cost is a fixed fraction of
premium. MEASURED, paired, null: stop 35 vs 28 = **+0.0110R ± 0.0008** (z 13.7); stop 22 vs 28 =
−0.0185R ± 0.0017.

**Do not read that as an edge.** It is denominator arithmetic. In dollars it only helps by reducing the
contract count, and at $25k QQQ is *already* at one contract:

```
risk/contract = stop% x premium x 100
  ATM  $4.82 @ 28% -> $135   ->  floor($250/$135) = 1 contract
  ITM2 $5.85 @ 32% -> $187   ->  1 contract
  ATM  $4.82 @ 52% -> $251   ->  0 contracts  <- the cap starts refusing here
```

So the honest recommendation is a *modest* widening — 28 → **32** — which buys a little R, keeps one
contract, and gives a trailing stop somewhere to live. Pushing past ~42% on an ITM2 rung starts
refusing the trade.

### 3.5 Regime dependence (context for the "quiet session" finding)

MEASURED, QQQ 1DTE ATM, hold 20 m, sessions sorted by hi-lo range and quartiled:

| | mean session range | T1-first | E[R] |
|---|---|---|---|
| quietest quartile (9 sessions) | 1.02% | **4.8%** | −0.0460 |
| Q2 | 1.39% | 9.0% | −0.0434 |
| Q3 | 1.80% | 14.7% | −0.0353 |
| widest quartile | 3.16% | 25.6% | −0.0213 |

Quietest single session in the sample: 2026-05-29, range 0.86%, T1-first 9.0%. This reproduces the
desk's "0.86% range → 8.0%" observation.

Note the second column moves 5× while E[R] moves 2×, and in the *opposite* direction to what a
"T1-first is good" reading would predict — wide-range days are better not because T1 is reached more
often but because the fixed cost is a smaller share of a larger move. **GUESS, not measured:** a
range-based trade filter is tempting here and I would not build one. Session range is not knowable at
entry, and its proxies (opening range, trailing RV) were among the weakest signals in §7.

### 3.6 Verdict on the geometry

`t1_pct = 22` against `stop_pct = 28` is **too tight for QQQ**, and it is tight for a reason that will
not go away: QQQ's ATM 1DTE premium is 0.63% of spot, so any percent-of-premium target maps to a large
underlying move. Recommended: **`stop_pct` 28 → 32, `t1_pct` 22 → 32 (= 1.0 × stop), `t2_pct` 46 → 60,
`t3_pct` 74 → 95**, and replace the scale-out with a trailing stop (§9). Worth **+0.015R**, MEASURED.

---

## 4. Why QQQ's T1-first rate (27%) is worse than SPY's (36%)

My replay reproduces the gap: at a 20-minute hold, QQQ 14.95% vs SPY 20.08% — a ratio of 0.74 against
the desk's measured 27/36 = 0.75. (The absolute levels differ because the desk's figure corresponds to
a longer effective window; at a 60-minute hold my engine gives QQQ 31.3% / SPY 37.6%.) The *ratio* is
what matters and it replicates.

Counterfactual decomposition, MEASURED, 1DTE ATM, hold 20 m — QQQ rerun with SPY's parameters one at a
time:

| | T1-first | Δ vs QQQ as-is |
|---|---|---|
| QQQ as-is (t1 22 / stop 28, overnight ratio 1.004) | 14.95% | — |
| QQQ with **SPY's geometry** (20/26) | 17.36% | **+2.41 pp** |
| QQQ with **SPY's overnight variance ratio** (0.637) | 17.13% | **+2.18 pp** |
| QQQ with both | 19.61% | +4.66 pp |
| SPY as-is | 20.08% | +5.13 pp |

The two mechanisms account for **91% of the gap**. They are:

**(a) `t1_pct` is 10% wider on QQQ (22 vs 20).** THEORY, and it is exact: the underlying move a
percent-of-premium target requires is `t1_pct × P / Δ`, and `P/(Δ·S)` is very nearly the same constant
for any ATM option regardless of the underlying's volatility (it is ≈ 0.8·σ·√T). So the barrier
distance *in the underlying's own sigma units* is proportional to `t1_pct` and nothing else. QQQ was
given a 10% wider target and duly reaches it ~10% less often. This part is robust and would appear in
any sample.

**(b) QQQ's 1DTE premium carries proportionally more overnight variance, which an intraday scalp pays
for and can never collect.** MEASURED:

| overnight variance / session variance | QQQ | SPY |
|---|---|---|
| 37-session sample (1-min bars, session open vs prior close) | **1.004** | 0.637 |
| 1-year daily o/c decomposition | 0.673 | 0.601 |
| overnight share of *total* daily variance, 1 year | 40.2% | 37.5% |

A 1DTE option bought mid-session prices `(remaining today) + (overnight) + (tomorrow)` of variance. For
QQQ at 12:30 that is 2.35 session-units of which **42.7% is the overnight gap alone**; for SPY, 32.1%.
The scalper holds ~2% of that variance for 20 minutes but pays a premium sized by all of it, which
pushes the percent-of-premium barriers further out in realised-move terms.

**Honest caveat on (b):** the size of this effect is sample-dependent. Using the 37-session ratios it is
worth +2.18 pp; using the stabler 1-year ratios (QQQ 0.673 vs SPY 0.601) the QQQ-minus-SPY overnight
difference is only worth **≈ +0.4 pp**. The *sign* is robust — QQQ carries more overnight variance than
SPY in both windows, which is exactly what you would expect from an index whose top three names report
earnings after the close — but the *magnitude* is not. Label (a) MEASURED-robust and (b)
MEASURED-but-sample-dependent.

**And the punchline the decomposition points at is not "fix the T1-first rate".** SPY reaches T1 more
often and has *strictly worse* expectancy at every hold (§8). Giving QQQ SPY's geometry raises its
T1-first rate to 17.6% and lowers its E[R] from −0.0359 to −0.0413. **T1-first is a diagnostic of
barrier distance, not a measure of edge. Stop optimising it.**

---

## 5. DTE: 0DTE vs 1DTE

**Stay 1DTE. This is the most decisive result in the study.**

MEASURED, paired (identical entry minutes, identical exit rule), QQQ:

```
0DTE vs 1DTE, hold 20 m, null entries:   -0.1829R  se 0.0086   z = -21.3
0DTE vs 1DTE, hold 20 m, with gate:      -0.1821R  se 0.0180   z = -10.1
```

And unpaired, across the 0DTE pricing assumptions:

| 0DTE priced at | hold 5 m | hold 10 m | hold 20 m |
|---|---|---|---|
| 1.76× the historical intraday variance clock (**measured at 15:14 ET**) | −0.1058 | −0.1501 | −0.2151 |
| 1.30× | −0.1129 | −0.1535 | −0.2078 |
| 1.00× — **fair, the most generous case for 0DTE** | −0.1193 | −0.1566 | −0.2005 |
| **1DTE reference** | **−0.0260** | **−0.0292** | **−0.0359** |

0DTE loses by ~0.10–0.18R **even when I hand it a fair price**. Two mechanisms, both MEASURED:

**(a) Cost, and it is the bigger one.** QQQ 0DTE ATM prices at $1.85–2.45 in the replay (matching the
desk's ~$1.80 observation) against $4.83 at 1DTE. The $0.02 tick and the $1.30 commission are *fixed
dollars*:

```
0DTE @ $1.85:  spread 1.08% + commission 0.70%  = 1.78% of premium = 0.064R at a 28% stop
1DTE @ $4.83:  spread 0.41% + commission 0.27%  = 0.68% of premium = 0.024R
```

Cheaper contracts are more expensive to trade. Per unit of risk you pay **2.6×** as much on 0DTE.

**(b) The scale-out truncation, which is far worse on a convex instrument.** From §3.3, at zero cost:
the scale-out costs 1DTE −0.011R at 20 min and 0DTE **−0.138R**. A 0DTE ATM's payoff has a much fatter
right tail; capping the runner at +74% throws away the trades that were supposed to pay for the stops.

**On the desk's √T reasoning.** The observation in the code — that a fixed percent-of-premium stop sits
~0.53σ out at 0DTE vs ~1.2σ at 1DTE — is *correct* and I reproduce it (0DTE T1-first at 20 m is 32–40%
vs 15% at 1DTE, exactly because the barriers are close in sigma terms). It is simply not the binding
constraint. The binding constraint is that a fixed-dollar cost against a $1.85 contract is a 1.78%
haircut. **Keep the conclusion, replace the reason.**

**Position-size consequence.** 0DTE at a 28% stop risks $52/contract → the $250 cap buys 4 contracts →
$5.20 of commission per round trip. 1DTE ITM2 at 32% risks $187 → 1 contract → $1.30. The cheap
instrument quadruples your commission bill for the same risk. That, not the stop distance, is the
$25k-account argument.

`dte_pref = 1`, `dte_max = 2`: **keep exactly as is.**

---

## 6. Hold

The desk's monotone hold curve is real, I reproduce it, and **it is being read backwards.**

MEASURED, paired, QQQ 1DTE ATM, current geometry:

| | **null entries** (no edge) | **with the proposed gate** |
|---|---|---|
| hold 5 m vs 20 m | **+0.0104R ± 0.0017** (z +6.1) | **−0.0208R ± 0.0128** |
| hold 10 m vs 20 m | +0.0072 ± 0.0013 (z +5.5) | −0.0123 ± 0.0087 |
| hold 30 m vs 20 m | −0.0072 ± 0.0011 (z −6.5) | +0.0035 ± 0.0076 |
| hold 45 m vs 20 m | −0.0148 ± 0.0025 (z −5.9) | −0.0037 ± 0.0140 |

**The hold curve flips sign the moment an entry edge exists.** THEORY, and it is just arithmetic: the
round trip costs a fixed 0.91 bp of drift-equivalent regardless of hold, while drift accumulates *with*
hold. With zero drift the optimal hold is zero minutes — i.e. "don't trade" — and any measurement will
show shorter is better all the way down. With drift, the optimum sits where marginal drift equals
marginal bleed.

So the desk's finding — 5 m −0.050R, 60 m −0.219R, win rate flat at 45–47% across the whole curve — is
**not a mechanics discovery. It is a measurement of the absence of entry edge**, expressed in the
mechanics' units. Shortening the hold from 60 to 20 minutes was correct as damage control. Shortening
it further to 5 minutes would bank a bit more of the same damage control and would simultaneously
guarantee that no entry signal can ever pay for itself, because 5 minutes only delivers a third as much
drift as 20 for the identical cost.

**Recommendation: `MAX_HOLD_S` 1200 → 1500 (25 min).** Between the 20 m and 30 m plateau under the
proposed gate; costs +0.007R if the gate turns out to be worthless, gains ~+0.015R if it is real.

**Does the hold clear the round trip?** At 20 minutes, with the proposed gate: measured drift +1.56 bp
± 0.64 against a 0.91 bp break-even. Net **+0.65 bp ± 0.64**. It clears it by one standard error. At 5
minutes: +0.53 ± 0.22 against 0.91 — it does **not** clear it, and that is well-determined. **The
short-hold configuration is the one that provably cannot work.**

---

## 7. Entry — what should actually gate a QQQ scalp

All figures MEASURED: signed forward return of QQQ in the direction the signal points, in basis points,
day-clustered bootstrap SE, 36 sessions. **The bar is +0.91 bp.**

### 7.1 chip's current gate carries no direction at all

`market.tape_strength` re-implemented on my 1-min QQQ bars (same EMA9/21, RSI14, VWAP, Bollinger inputs
`_chip_indicators` builds), taking the stronger of the two sides:

| gate | fires on | drift @ 5 m | drift @ 20 m |
|---|---|---|---|
| score ≥ 62 (the `MIN_TAPE_MODERATE` bar) | **64.3% of minutes** | −0.20 ± 0.33 bp | −0.07 ± 1.07 bp |
| score ≥ 74 ("strong momentum" trigger) | 17.5% of minutes | +0.33 ± 0.75 | +1.23 ± 2.29 |

And through the full option replay, QQQ 1DTE ATM hold 20 m:

```
null gate    E[R] -0.0365 +- 0.0025   (n = 25,128)
chip gate    E[R] -0.0366 +- 0.0247   (n =  8,112)
```

Three things follow.

**(a) The 62 bar is not a filter.** It admits two minutes in three. Its firing distribution across the
session is essentially flat (8–11% per half-hour bucket) and the trailing-20-minute volatility it fires
into is 5.21 bp vs 5.11 bp unconditional — a ratio of 1.020. It selects nothing.

**(b) I cannot reproduce "chip's entries are worse than random" as a directional effect.** The desk
measured chip's TAKE minutes at −0.219R against a −0.142R null, z = −3.58. On the underlying tape, over
36 sessions, chip's gate is **−0.07 bp ± 1.07** at 20 minutes — indistinguishable from zero — and in my
option replay it lands on top of the null. Whatever produces the desk's −0.077R gap is therefore
**not** the direction the gate picks. **THEORY / suggested follow-up:** the likely culprit is
entry-price conditioning — chip fires when the tape is moving, which is when the ask you actually lift
is furthest from the mid and when a stored quote is most likely stale. That would show up as a −0.077R
penalty in a replay that prices entries off live quotes (the desk's) and as nothing at all in a replay
that prices them off a model (mine). Worth re-running the desk's comparison with the entry price held
fixed between the two arms; if the gap collapses, it is an execution problem, not a signal problem.

**(c) The anti-predictive score bands are a time-of-day artifact.** See 7.4.

### 7.2 The gates that do not work

Drift at H = 20 min, break-even +0.91 bp:

| gate | drift @ 20 m | verdict |
|---|---|---|
| VWAP continuation (long above VWAP) | −0.25 ± 0.78 | nothing |
| VWAP reversion (fade the side) | +0.25 ± 0.78 | nothing |
| fade >0.15% VWAP extension | +0.26 ± 0.94 | nothing |
| EMA9 > EMA21 continuation | +0.28 ± 0.77 | nothing |
| 5-min momentum continuation | +0.05 ± 0.35 | nothing |
| 15-min mean reversion | +0.20 ± 0.56 | nothing |
| opening-range (30 m) breakout | +0.73 ± 0.96 | inside noise |
| opening-range fade | −0.73 ± 0.96 | inside noise |
| prior-day H/L breakout | **+1.91 ± 1.00** | clears the bar, z = 1.9, *not* significant |
| QQQ > SPY relative-strength continuation | +0.69 ± 0.87 | inside noise |
| relative volume > 1.2 × (+ EMA continuation) | +0.65 ± 2.88 | hopeless power |

At H = 5 min the only signal with z > 2 is **5-minute mean reversion: +0.44 bp ± 0.15 (z = 2.9)**. It is
the best-determined effect in the whole study and it is **less than half** the +0.93 bp break-even bar
at that horizon. That is worth stating plainly: *QQQ's most reliable short-horizon microstructure
effect is not large enough to pay a retail option round trip.*

Note also that 5-minute mean reversion has the **opposite sign to chip's momentum trigger**. The one
real short-horizon effect in QQQ runs against the direction the current gate takes.

**Specific recommendation on the VWAP cross.** `_score_ex_vwap` correctly diagnoses that the cross
trigger is structurally unreachable, and correctly de-biases the score to make it reachable. But VWAP
continuation and VWAP reversion both measure **±0.25 bp ± 0.78** — a VWAP cross in QQQ carries no
information in either direction over 5–20 minutes. Making the trigger reachable therefore just adds
trades at −0.037R each. **Remove the cross trigger rather than repairing it.**

### 7.3 The one gate that survives — mega-cap divergence

QQQ's top three names are ~27% of the index and they trade with their own idiosyncratic flow. Build a
cap-weighted composite (NVDA 0.10, AAPL 0.09, MSFT 0.08 — approximate index weights, GUESS on the
precise numbers, the result is insensitive to them) and compute its trailing-30-minute return minus
QQQ's. When the mega-caps have **led** QQQ, **fade them** — take the QQQ side that closes the gap by QQQ
moving away from the mega-caps.

MEASURED:

| horizon | drift | z | break-even |
|---|---|---|---|
| 5 min | +0.53 ± 0.22 bp | +2.5 | 0.91 bp — **fails** |
| 10 min | +0.99 ± 0.38 bp | +2.6 | 0.91 bp — at the bar |
| **20 min** | **+1.56 ± 0.64 bp** | **+2.5** | 0.91 bp — **clears by +0.65 ± 0.64** |
| 20 min, only when \|gap\| > 5 bp | **+1.74 ± 0.76 bp** | +2.3 | selecting on magnitude helps |

**Train/test split, first 18 sessions vs last 18** (the only out-of-sample check available at this
sample size):

| | train | test | all |
|---|---|---|---|
| fade mega-cap 30-min lead, H = 5 | +0.65 ± 0.30 | +0.42 ± 0.30 | +0.53 ± 0.22 |
| H = 10 | +1.21 ± 0.54 | +0.78 ± 0.54 | +0.99 ± 0.38 |
| **H = 20** | **+1.79 ± 0.83** | **+1.34 ± 0.96** | **+1.56 ± 0.64** |

Same sign, same order of magnitude, in both halves. Of the ~15 gates I scanned this is the only one
that does that. Note the lookback matters and is not fitted: the 30-minute divergence works, the
10-minute one does not (+0.02 ± 0.22 at H = 5), and the since-open one is weaker (+1.32 ± 0.85) — which
is at least a coherent story about how long an index-vs-component dislocation takes to close, rather
than a lucky parameter.

Through the option replay, QQQ 1DTE ATM, current geometry:

| gate | hold 5 m | hold 10 m | hold 20 m |
|---|---|---|---|
| null | −0.0261 ± 0.0009 | −0.0293 ± 0.0013 | −0.0365 ± 0.0025 |
| chip (score ≥ 62) | −0.0319 ± 0.0073 | −0.0329 ± 0.0132 | −0.0366 ± 0.0247 |
| mega-cap fade | −0.0136 ± 0.0061 | −0.0051 ± 0.0110 | **+0.0072 ± 0.0186** |
| mega-cap fade, \|gap\| > 5 bp | −0.0144 ± 0.0067 | −0.0034 ± 0.0127 | **+0.0125 ± 0.0217** |
| mega fade **and** 5-min fade agree | +0.0125 ± 0.0138 | +0.0316 ± 0.0243 | **+0.0412 ± 0.0363** |

The "both agree" variant has the best point estimate and the worst power (n = 1,299 ticker-side-minutes
over 36 sessions). **GUESS:** I would run it as a size multiplier rather than a gate — take the
`|gap| > 5 bp` mega-fade as the entry condition and up-weight when the 5-minute fade agrees — but I have
no data that separates those two designs and I would not defend the choice.

**Throughput warning (MEASURED).** Under the existing `COOLDOWN_S = 900`, the mega-cap gate fires
**24.0 QQQ trades/session** and the `|gap| > 5 bp` variant 23.0 — five times the system's ~4–6
signals/day capacity. The "both agree" variant gives 9.9/session. The gate therefore needs a per-session
cap. Since the `|gap| > 5 bp` restriction *improved* the drift, **rank the day's candidates by |gap| and
take the top 4–5.** That is consistent with what the data says and does not require inventing a new
threshold. It also means the deployed population is not the measured population — flag it and expect a
degradation of unknown size.

### 7.4 Time of day

MEASURED, EMA-continuation signed drift over 20 min, and unconditional move size:

| window | EMA-continuation drift (20 m) | mean \|20 m move\| |
|---|---|---|
| **09:30–10:00** | **−9.55 ± 4.35 bp** | 33.1 bp |
| 10:00–10:30 | −3.61 ± 2.80 bp | 26.2 bp |
| 10:30–11:30 | +2.11 ± 1.86 bp | 20.7 bp |
| **11:30–13:00** | **+2.01 ± 1.17 bp** | 16.0 bp |
| 13:00–14:30 | −0.55 ± 1.51 bp | 13.2 bp |
| 14:30–16:00 | +0.49 ± 1.18 bp | 14.3 bp |

This is an **independent replication** of the desk's time-of-day profile (09:30–10:00 −0.267R,
11:30–13:00 +0.129R, 14:30–16:00 −0.106R), on different data, in different units, with no shared code
path. Momentum-following in QQQ's first thirty minutes is the single most reliably destructive thing in
this study — and it is not a small effect: −9.55 bp against a +0.91 bp bar is ten break-evens in the
wrong direction.

It also explains the desk's "the tape score is anti-predictive above 56" finding without needing the
score to be anti-predictive at all: the score is *highest* when the tape is moving hardest, the tape
moves hardest in the first 30 minutes (10.5% + 8.8% of the session's variance in the first two 15-minute
buckets — QQQ's opening half-hour carries 1.5–1.7× the session's average volatility), and that is
exactly the window where momentum-continuation drift is −9.6 bp. High score ≈ opening momentum ≈ the
worst window. The score does not need to be inverted; the window needs to be excluded.

**Recommendation: hard blackout 09:30–10:00.** MEASURED effect on the mega gate specifically: nil (that
gate needs a 30-minute lookback so it cannot fire before 10:00 anyway). MEASURED effect on the current
gate: it removes the desk's worst-measured window. Cost: 0 trades under the recommended configuration.
This is free.

### 7.5 What I did not find

- **Relative volume**: nothing (+0.65 ± 2.88 bp), and it is unpowered to a degree that makes further
  work on it pointless at this sample size.
- **QQQ vs SPY basis** as a continuation signal: +0.69 ± 0.87 bp, inside noise. (The *mega-cap* version
  works and the *SPY* version does not — consistent with the effect being about index-vs-component
  dislocation rather than about beta.)
- **QQQ vs NDX/NQ basis**: not tested. NQ futures are not in the desk's data path and adding a futures
  feed for a signal I cannot pre-validate is not a recommendation I am willing to make.
- **Opening range and prior-day levels**: prior-day H/L breakout at +1.91 ± 1.00 bp is the second-best
  point estimate in the study and I am deliberately **not** recommending it. z = 1.9 over 36 sessions,
  no train/test check survived, and it fires rarely. It belongs on a watch list, not in the code.

---

## 8. Does QQQ earn its place in the slate?

**Yes, and the conventional argument runs backwards.**

MEASURED, identical model, identical period, identical exit rule, each ticker at its own shipped
geometry, 1DTE ATM, null entries:

| hold | QQQ (22/28) | SPY (20/26) |
|---|---|---|
| 5 m | **−0.0260** | −0.0381 |
| 10 m | **−0.0292** | −0.0441 |
| 20 m | **−0.0359** | −0.0565 |
| 30 m | **−0.0426** | −0.0676 |
| 60 m | **−0.0577** | −0.0846 |

QQQ is cheaper to trade at every hold, by 30–40%. The reason is the fixed-dollar cost structure:

```
QQQ ATM 1DTE $4.42:  spread $0.02 = 0.452%  +  commission 0.294%  = 0.747% = 0.0267R @ 28% stop
SPY ATM 1DTE $2.41:  spread $0.01 = 0.415%  +  commission 0.539%  = 0.954% = 0.0367R @ 26% stop
```

Both are penny-quoted and both spreads are ~0.4% of premium. The differentiator is the **$1.30
commission amortised over a 1.84× larger premium**, compounded by QQQ's slightly wider stop denominator.

Stated instrument-neutrally: break-even drift as a share of a typical 20-minute move is **QQQ 5.1%
(0.91/17.9 bp)** vs **SPY 5.6% (0.59/10.5 bp)**. QQQ needs a marginally smaller slice of its own noise.

The premise in the charter — QQQ is 1.7× SPY's volatility and 1.5× its spread in percentage terms — is
half right. QQQ *is* ~1.9× SPY's volatility in this sample (37-session intraday session sigma: QQQ
1.200%, SPY 0.721%; ATM 1DTE premium as % of spot: QQQ 0.625%, SPY 0.321%). But QQQ is **not** 1.5×
SPY's spread in percentage terms — measured at ATM ± 1 on 2026-07-22, QQQ quotes $0.02 on a $4.42
contract (0.45%) and SPY quotes $0.01 on a $2.41 contract (0.42%). Essentially identical. The
percentage-spread penalty QQQ is supposed to carry does not appear in the data at the strikes chip
actually trades.

So: **QQQ earns its slot on the merits.** At $25k paying $1.30/contract, the marginal instrument in the
slate is SPY, not QQQ — SPY's cheaper contract means more commission per unit of risk. If anything
needs justifying it is why chip trades SPY at this account size.

(That is a QQQ-side observation about a shared cost structure, not a recommendation about SPY. The SPY
SME owns that call and has data I do not.)

---

## 9. Recommended QQQ configuration

Every constant, with the label and the evidence.

### 9.1 `chip_desk.DEFAULT_MECHANICS["QQQ"]`

| field | current | **recommended** | label | why |
|---|---|---|---|---|
| `stop_pct` | 28 | **32** | MEASURED | stop 35 vs 28 = +0.0110R ± 0.0008 (null); 32 keeps 1 contract at $25k (risk/ct $187 on the ITM2 rung) and gives a trailing stop room. Do not exceed ~42% on ITM2 or the 1% cap starts refusing. |
| `t1_pct` | 22 | **32** (= 1.0 × stop) | MEASURED | t1 = 1.0 × stop vs 0.79 × stop = +0.0034R ± 0.0012 (null) / +0.0052 ± 0.0018 (gate). Monotone; 1.4 × stop is better still but I am not extrapolating past the tested grid. |
| `t2_pct` | 46 | **60** | THEORY | held at ~1.9 × t1, the current ratio. Not independently measured — t2 barely binds at a 25-minute hold. |
| `t3_pct` | 74 | **95** | THEORY | scaled with t1. **Measured no-op**: uncapped runner vs current = +0.0003R ± 0.0005. Change it or don't. |
| `spread_max` | 3 | **3 — unchanged** | MEASURED | QQQ ATM 1DTE quotes 0.45%; the cap never binds. Keep it as the fail-safe against a degraded quote, which is its actual job. |
| `strike_inc` | 1.0 | **1.0 — unchanged** | MEASURED | QQQ front-month strikes are $1 apart; confirmed on the live chain. |
| `dte_pref` / `dte_max` | 1 / 2 | **1 / 2 — unchanged** | MEASURED | 0DTE vs 1DTE = −0.1829R ± 0.0086, z = −21. |
| `regime_bias` | "trend" | **"range"** | MEASURED (weak) | cosmetic in the current code path, but "trend" mislabels an instrument whose only significant short-horizon effect is mean-reverting (5-min reversion +0.44 ± 0.15 bp; 5-min continuation −0.44). If anything downstream ever reads this field, it should not read "trend". |

### 9.2 Module-level constants

| constant | current | **recommended** | label | why |
|---|---|---|---|---|
| `MAX_HOLD_S` | 1200 (20 m) | **1500 (25 m)** | MEASURED | 20 m and 30 m are statistically tied under the proposed gate (+0.0035R ± 0.0076); 5 m is decisively worse *with* a gate (−0.0208 ± 0.0128) and decisively better *without* one (+0.0104 ± 0.0017). 25 m is the hedge. See §6. |
| `MIN_TAPE_MODERATE` | 62 | **retire it as an entry gate** | MEASURED | fires on 64.3% of minutes and carries −0.07 bp ± 1.07 of drift at 20 m. Keep the score as a *display* field; do not let it decide anything. |
| `COOLDOWN_S` | 900 | **900 — unchanged**, plus a hard cap of **5 QQQ entries/session** | MEASURED | the proposed gate fires 24 ×/session at the current cooldown — 5× the system's stated 4–6/day capacity. Rank the day's candidates by \|divergence\| and take the top 5; the \|gap\| > 5 bp variant measured *better* (+1.74 vs +1.56 bp), so magnitude-ranking is supported rather than invented. |
| — | — | **new: session blackout 09:30–10:00 ET** | MEASURED | EMA-continuation drift in that window is −9.55 ± 4.35 bp. Costs zero trades under the proposed gate (which needs a 30-minute lookback). §7.4. |

### 9.3 Exit rule

Replace `_shadow_scaleout` for chip's QQQ book with a **trailing stop**: hard stop at −32% until +32% is
touched, then trail 32 points below the peak, plus the 25-minute time stop.

MEASURED, paired, hold 20 m: trail vs scale-out = **+0.0066R ± 0.0042** (null), **+0.0112R ± 0.0059**
(gate). A pure stop + time stop measured slightly better still (+0.0099 / +0.0135) but gives up all
profit-taking discipline, which I do not think is the right trade for a system a human also reads.

**Caveat, stated because it matters:** `db._shadow_scaleout` is shared by every provider. Changing it
globally would silently re-baseline every provider's `provider_edge`. This must be a **chip-QQQ-scoped**
exit rule, emitted through `chip_plan` the way `max_hold_min` already is, or the comparison across
providers breaks.

### 9.4 Entry gate

Replace the tape gate with:

```
composite = cap-weighted 30-min return of (NVDA 0.10, AAPL 0.09, MSFT 0.08)
gap       = composite - QQQ 30-min return
require   |gap| > 5 bp
side      = PUT  if gap > 0   (mega-caps led -> fade them)
            CALL if gap < 0
window    10:00-15:30 ET
rank      by |gap|, take the top 5 of the session, 15-min cooldown
```

MEASURED: +1.74 ± 0.76 bp of 20-minute drift against a 0.91 bp break-even; train +1.96 ± 0.97 / test
+1.52 ± 1.16 on an 18/18 session split.

**Data requirement:** three extra 1-min bar streams (NVDA, AAPL, MSFT). `market.get_bars` already does
exactly this; the marginal IBKR line cost is 3 streams against `reap_subs`' cap of 80.

### 9.5 What `chip_strike` should do — unchanged

`chip_strike.select`'s **lowest-leverage-affordable** rule is correct and I would not touch it.
MEASURED, paired, hold 20 m:

| | null | with gate |
|---|---|---|
| ITM2 vs ATM | **+0.0063R ± 0.0005** (z 12.1) | **+0.0038 ± 0.0013** (z 3.1) |
| OTM2 vs ATM | −0.0088 ± 0.0007 (z −13.3) | −0.0071 ± 0.0013 (z −5.3) |

Monotone across the ladder, and for exactly the reason the module docstring gives: the barrier race is
flat in delta (delta and premium cancel), so what is left is relative spread and the fixed commission,
both of which are smaller fractions of an expensive contract. The module's own claim that the tie-break
is worth "~+0.02R" is if anything an overstatement — I measure +0.006R — but the *sign and the
reasoning* are right.

One thing to watch: at `stop_pct = 32` on an ITM2 rung, risk/contract is $187 against a $250 cap. The
`select()` feasibility filter still returns 1 contract. At `stop_pct = 40` it would return 1 contract at
$234; at 45% it returns 0. **The recommended 32 leaves headroom; do not stack a wider stop on top of it
without re-checking `size_for`.**

---

## 10. Expected effect, with the error bar

### The mechanics package alone

`stop_pct` 28→32, `t1_pct` 22→32, `t2/t3` scaled, hold 20→25 m, ITM2 rung, trailing exit. Paired,
identical entries, day-clustered bootstrap:

| entry gate used | Δ E[R] | 95% CI |
|---|---|---|
| null (every minute) | **+0.0154** ± 0.0028 | [+0.0105, +0.0216] |
| chip's current gate | **+0.0151** ± 0.0083 | [+0.0017, +0.0335] |
| mega-cap fade | **+0.0174** ± 0.0050 | [+0.0081, +0.0276] |
| mega-cap fade, \|gap\| > 5 bp | **+0.0154** ± 0.0061 | [+0.0043, +0.0283] |
| both-agree | +0.0147 ± 0.0142 | [−0.0118, +0.0434] |

**+0.015R ± 0.003, invariant to the entry gate.** This is the most solid result in the document.

In dollars it is smaller than the R figure suggests, because the proposed configuration deploys more
risk per contract ($187 vs $134) and R is normalised to risk: the honest dollar comparison is
**−$4.88 → −$3.95 per trade = +$0.93/trade ≈ +$1,170/yr** at 5 QQQ trades/day. In R terms the
configuration loses 42% less; in dollars, which is what the account feels, 19% less. It does not make
QQQ profitable.

### The full package

| configuration | E[R] | day-block 95% CI | $/trade | $/yr @ 5/day |
|---|---|---|---|---|
| current mechanics, current gate | −0.0366 ± 0.0247 | [−0.083, +0.013] | −$4.91 | −$6,190 |
| **proposed mechanics, current gate** | −0.0215 ± 0.0279 | [−0.074, +0.037] | −$4.03 | −$5,080 |
| **proposed mechanics + mega gate** | **+0.0246 ± 0.0211** | [−0.014, +0.068] | **+$4.61** | **+$5,810 ± $4,980** |
| proposed mechanics + \|gap\| > 5 bp | +0.0280 ± 0.0245 | [−0.017, +0.078] | +$5.25 | +$6,620 |
| proposed mechanics + both-agree | +0.0558 ± 0.0382 | [−0.016, +0.131] | +$10.86 | +$13,680 |

Read the interval, not the point estimate. **The recommended configuration's 95% interval is roughly
−$4,300 to +$16,000/yr on a $25k account.** That is a coin flip with a favourable tilt, not a strategy.

### Deliberate anti-overfit discount

I scanned ~15 entry gates × 3 horizons. Under a Bonferroni correction the mega-cap fade's z = 2.5 at
H = 20 is **not** significant. The reasons I still rank it first are (i) it survived an 18/18 session
train/test split at the same magnitude, (ii) its lookback structure is coherent rather than fitted
(30 min works, 10 min does not, since-open is weaker), and (iii) it has a mechanism —
index-vs-component dislocation — that is specific to QQQ rather than generic. Those are reasons for a
prior, not for a conclusion. **GUESS: I would discount the +0.025R point estimate to ~+0.010R as a
planning number, which puts the honest expectation at break-even.**

---

## 11. What I would NOT change, and why

- **`dte_pref = 1`, `dte_max = 2`.** Measured, −0.18R for 0DTE, z = −21. This is the strongest
  mechanics result in the study and the current setting is right.
- **`spread_max = 3`.** Never binds (QQQ ATM 1DTE measured 0.45%). That is not an argument for removing
  it — its job is to refuse a degraded quote, and it does that.
- **`strike_inc = 1.0`.** Confirmed on the live chain.
- **`chip_strike.select`'s lowest-leverage rule and the 0.25–0.85 delta band.** Measured +0.006R,
  monotone, z = 12. Correct as built.
- **The 1% risk cap and the feasibility-filter fix inside `select()`.** The fix (optimise inside the
  affordable set rather than rank-then-size) is what made QQQ tradeable at all; at $25k with a 32% stop
  it returns 1 contract on every rung from ATM−2 to ATM+2. Do not revisit.
- **`t3_pct`.** Measured no-op (+0.0003R ± 0.0005). Not worth an edit either way.
- **`COOLDOWN_S = 900`.** Correct as an anti-churn floor. The *new* constraint is a per-session cap, not
  a longer cooldown.
- **Chip's counter-side shadow.** Out of scope here, but it is the cheapest source of matched-pair data
  in the system and it is what let §4's counterfactuals be paired. Keep it.
- **`_score_ex_vwap`.** The diagnosis is right and the de-bias is well-built. I recommend removing the
  *cross trigger* it serves (§7.2) rather than the function — if the cross ever earns its way back, the
  de-bias will be needed.

---

## 12. Verdict: is positive expectancy reachable for a $25k account at $1.30/contract?

**Not provably. Here is the arithmetic in full.**

**The floor.** Every QQQ round trip costs $0.02 of spread + $1.30 of commission = **$1.32 per contract,
minimum**, which is 0.747% of a $4.42 ATM 1DTE premium and 0.0267R at a 28% stop. At one contract and 5
trades/day that is **−$1,660/yr** of pure friction, and the replay says the realised bleed at the
current configuration is 3–4× that (−$4.88/trade → −$6,150/yr) because of hold-time decay and the
scale-out's truncation on top. The proposed mechanics recover about a fifth of the excess in dollars
(−$4.88 → −$3.95/trade). **Friction alone consumes 6.7% of a $25k account per year; the current
configuration consumes 24.6%.**

**The bar.** Beating that floor requires **+0.91 bp of directional drift in QQQ over the hold** — about
5.1% of a typical 20-minute move. Translating (THEORY: `E[signed] = (2p−1)·E[|move|]` for a symmetric
move distribution), that is roughly **52.5% directional accuracy** on QQQ's next 20 minutes.

**What is actually available.** Across ~15 gates on 36 sessions:
- chip's current gate: **−0.07 ± 1.07 bp** — nothing.
- the best statistically-solid effect in QQQ (5-min mean reversion, z = 2.9): **+0.44 ± 0.15 bp** —
  under half the bar.
- the best gate that clears the bar and survives a split (mega-cap divergence fade): **+1.56 ± 0.64 bp**
  — clears by **+0.65 bp ± 0.64**, i.e. one standard error.

**Why it cannot be settled.** Per-trade dispersion is **0.55R**. MEASURED power requirement:

| edge to detect | trades needed (80% power, 95% conf) | sessions at 5 QQQ trades/day |
|---|---|---|
| +0.05R | 950 | 190 (**0.8 years**) |
| +0.03R | 2,639 | 528 (**2.1 years**) |
| +0.02R | 5,938 | 1,188 (**4.7 years**) |

The recommended configuration's point estimate is +0.025R. **The system would need roughly three years
of live trading to distinguish it from zero.** Any 20-, 40-, or 60-session result at this throughput is
inside the noise *by construction* — including this document's.

**So the honest answer, in three parts:**

1. **Yes, chip should keep trading QQQ** — in shadow. It is the cheapest instrument in the slate on a
   risk-adjusted basis (§8) and the mechanics fixes are real, well-powered, and free.
2. **No, no configuration measured here is demonstrably positive-expectancy.** The best one is
   break-even with a favourable tilt and a confidence interval that spans −$4,300 to +$16,000/yr.
3. **No, this system cannot ever prove otherwise at 4–6 signals/day.** That is the finding that should
   drive the decision. If chip is ever to carry real money on QQQ, the path is not a better backtest —
   it is more instruments, more signals, or a bigger edge. At 5 trades/day and a 0.55R dispersion, the
   measurement problem is the binding constraint, not the strategy.

**What I would actually do:** ship §9.1–9.5, keep `size_mult = 0` on chip, and treat the mega-cap
divergence gate as a live hypothesis with a pre-registered review at 200 QQQ shadow trades (≈ 40
sessions) — knowing in advance that 200 trades can only reject an edge larger than +0.11R, and that the
review's honest job is to catch a *catastrophe*, not to confirm a success.

---

### Appendix: reproduction

Scripts and cached data:
`/private/tmp/claude-501/-Users-rahul-Workspace-Copy-trading/53046cf7-c6bd-4419-be21-b10754d0fca6/scratchpad/qqq_sme/`

| file | what it does |
|---|---|
| `fetch_bars.py`, `fetch2.py` | IBKR 1-min RTH bars, chunked 5 D at a time (a single 40 D request times out) |
| `fetch_chain.py` | live QQQ/SPY 0DTE+1DTE ladder, ATM ± 3, both rights |
| `fetch_iv.py` | IV30 / HV30 daily history |
| `model.py` | variance clock + calibration to the live chain |
| `engine.py` | the replay: BS repricing, spread/commission model, `_shadow_scaleout` + 4 alternative exits |
| `decomp.py` | §4 QQQ-vs-SPY counterfactuals |
| `dte_strike.py`, `diag.py`, `modes.py` | §5 DTE, §9.5 strike rungs, §3.3 exit rules, martingale validation |
| `drift.py`, `gates.py`, `gates2.py` | §7 entry-gate drift study, chip tape-gate replication, train/test |
| `final.py`, `grid.py`, `paired.py`, `rec.py`, `nums.py` | §9–§12 full replay by gate, geometry grid, paired diffs, power |

No `.py` file in the repo was modified; `market.py` and `db.py` were imported read-only. IBKR
client_ids 16–18 were used. `DESK_ENV` was not set to test because nothing here writes desk state —
the engine reads bars and quotes and computes in memory.

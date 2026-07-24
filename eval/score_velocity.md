# Tape-score VELOCITY — "enter just before the rush" — reproduction and kill attempts

Quant review of one hypothesis: *the desk's entry gate fires on tape score **level** (`market.tape_strength
>= 62`), and level is anti-predictive because a high score means the move already happened; if there is
edge in this signal it is in the **rate of change** — "low now, rising fast" rather than "low and flat".*

**Scope:** this hypothesis only. **No `.py` file in the repo was modified or created. Nothing was
committed. No signal was posted to the desk. The live server (:8787) was not restarted.** All work ran
under `DESK_ENV=test`; the only live socket was a read-only IBKR historical pull on client_id 21. All
statistics go through `eval_harness` (`block_bootstrap`, `cost_model`, `compare_arms`, `trades_needed`).

Claims are labelled **MEASURED** (computed here from data I pulled myself), **THEORY** (arithmetic on
measured inputs), **GUESS** (judgement).

---

## 0. Verdict

**Score velocity carries no usable information. The hypothesis as stated is not merely unproven — the
point estimate is on the wrong side of zero everywhere in its own arm. Recommend: close it, do not
shadow it, do not re-propose it.**

| | |
|---|---|
| Grid searched | **1,280 configs, 1,249 eligible** (lookback × level band × velocity threshold × acceleration × horizon × direction) |
| Best result anywhere in the grid | **\|z\| = 2.86** — and it is *negative*: rising score from 50–62 → **−0.307 bp** at a 5-min horizon |
| Family-wise null (session sign-flip, 4,000 reps) | median max\|z\| **2.85**, 95th pct **3.77** |
| **Family-wise p** | **0.49** — the observed maximum sits at the 51st percentile of pure noise |
| The owner's arm ("low and RISING") across all 800 of its configs | mean-of-means **−0.185 bp**; **0 of 800** reach the payoff bar; best z **+1.73** |
| The mirror arm ("high and FALLING") — 155 configs *do* clear 1.7 bp | but median day-mix **+5.54 bp** vs median timing **−2.34 bp**: it is a hindsight accounting term, not a forecast |
| Best single config (+2.572 bp, z 2.41) decomposed | **day-mix +2.402, timing +0.170** — 93% of it is *which days fired*, not *when* |
| Purged walk-forward, 4 OOS blocks, 10,239 trades | **−0.032 bp, z −0.17** |
| Control: same grid on **IWM** | max\|z\| **3.07** — *higher* than SPY/QQQ; IWM family-wise p 0.38 |
| Control: is it price velocity in a costume? | score-velocity t **−2.39 → −1.11** once trailing price return is added; price velocity keeps t −2.23 |
| Worth per trade even taking the best number at face value (THEORY) | **+0.019 R** → **6,577 trades = 4.35 years** at 6 signals/day |
| What the study *rules out* | 896 of 1,249 configs have a 95% upper bound **below +1.7 bp** |

**Which kill did it:** the multiple-comparisons kill, on its own and decisively — family-wise p = 0.49,
with the observed maximum landing at the median of the null. Every other kill is confirmatory: the IWM
control is *stronger* than the real thing, the walk-forward is flat, the price-velocity control absorbs
the coefficient, and the one config that clears the payoff bar dissolves into a day-mix term under
decomposition. There is no residual worth shadowing.

**Operational note:** `chip_desk.TAPE_GATE_ENABLED = False` — the 62 bar has already been retired as a
gate and the score is now logged, not acted on. Nothing in this report requires an operational change.
It closes a question rather than opening one.

---

## 1. Setup

### 1.1 Data — MEASURED

IBKR `reqHistoricalData`, `whatToShow="TRADES"`, `useRTH=True`, 1-minute bars, pulled as 8 × `"1 M"`
chunks walked backwards. Read-only session, client_id 21.

| ticker | bars | sessions | range |
|---|---|---|---|
| SPY | 63,600 | 164 | 2025-11-24 → 2026-07-22 |
| QQQ | 63,600 | 164 | same |
| IWM | 63,600 | 164 | same (control only) |

164 sessions is 2.7–4.4× what prior reviews on this desk worked with (37–60). This matters: it is the
reason §5's "what this rules out" statement is tight enough to be worth anything.

### 1.2 The score is computed exactly as the live system computes it — MEASURED

The score is reconstructed by replicating `chip_desk._chip_indicators` → `market.indicators` →
`market.tape_strength` at every bar, vectorised. The reconstruction was then checked **bit-for-bit**
against the real functions: 6,000 random bars per ticker × both sides, each reference value computed the
long way — a 1,560-bar slice (chip's `"5 D"` pull) through `market.indicators`, then chip's session-VWAP
and price override, then `market.tape_strength`.

> **36,000 of 36,000 scores matched exactly. Zero mismatches.**

Getting there required fixing three things that a normal "close enough" reimplementation would have left
in, each of which shifts roughly 1 bar in 8,000 by a full score point:

1. **`np.round` is not Python's `round`.** `round(616.605, 2) = 616.61`; `np.round(616.605, 2) = 616.60`.
   `market.indicators` uses the builtin. A one-cent difference in `bb_up` moves the Bollinger-room
   component enough to move the final score by 1.
2. **EMA operation order.** `market._ema` is a truncated sequential recursion over `c[-30:]` / `c[-40:]`.
   A convolution with precomputed weights is algebraically identical and numerically is not; the last-bit
   difference flips `round(x, 2)`.
3. **Rolling std blocking.** A strided 2-D reduction sums its pairwise blocks differently from
   `c[-20:].std()`, with the same one-cent consequence.

None of these change a conclusion. They are recorded because the next reviewer will rebuild this and
should not spend the afternoon I spent finding them.

### 1.3 Where I cut for warm-up, and why it is not where the last review cut — MEASURED

**The EMAs are not the cold-start problem.** `chip_desk._tape` pulls `"5 D"` (~1,560 RTH bars), so
EMA9/EMA21/BB20/RSI always have prior-session history and are fully warm at 09:30. A review that
measured "09:30–10:00 on unwarmed EMAs" was reconstructing indicators from session-only bars — that is
not what chip does.

**The actual cold start is the session-anchored VWAP**, which is the largest single component of the
score (weight 0.35). At 09:35 the session VWAP is an average of five bars, so "price vs VWAP" is
structurally near zero:

| session bar | 0–5 | 5–10 | 10–15 | 15–20 | 20–30 | **30–45** | 45–60 | 60–90 | 240–390 |
|---|---|---|---|---|---|---|---|---|---|
| mean score | 45.6 | 49.6 | 51.8 | 52.8 | 53.8 | **54.1** | 54.1 | 54.1 | 53.8 |
| P(score ≥ 62) | 0.007 | 0.112 | 0.178 | 0.213 | 0.254 | **0.246** | 0.263 | 0.263 | 0.251 |
| lag-1 autocorr | — | 0.887 (10–20) | | | 0.909 (20–30) | **0.939 (30–60)** | | 0.943 (60+) | |

The distribution is still moving through bar 30 and flat thereafter.

> **Cut: the first 30 session bars are discarded. The first trusted bar is 10:00 ET.** Both the current
> score *and* the lagged score used for the derivative must be past the cut (`session_bar >= 30 + k`), so
> no derivative is ever taken across the cold start.

Sensitivity is in §4.7: cuts of 15 / 30 / 60 / 90 bars all give family-wise p between 0.46 and 0.91.

### 1.4 The bar — MEASURED (costs) + THEORY (elasticity)

Via `eval_harness.cost_model` and `friction_floor_r`, at a $2.10 ATM premium:

| | spread | commission | total | friction floor | break-even underlying drift at ω = 80 |
|---|---|---|---|---|---|
| SPY (stop 26) | 0.935% | 0.619% | **1.554%** | **−0.0598 R** | **1.94 bp** |
| QQQ (stop 32) | 0.751% | 0.619% | **1.370%** | **−0.0428 R** | **1.71 bp** |

The brief's 1.7 bp is exactly the QQQ number and it checks out; SPY needs 1.94 bp. Everything below is
scored against 1.7 bp as the *floor* of the bar, which is the generous reading.

### 1.5 Estimand and observation unit

One observation = (bar, side), side ∈ {C, P}, over SPY + QQQ pooled. The outcome is **signed forward
underlying drift** in bp: `(close[t+h]/close[t] − 1) × 10⁴`, negated for puts. Forward windows never
cross a session close (no overnight holds). Every SE is clustered on the **session**; the headline
configs are cross-checked against `eval_harness.block_bootstrap` in §4.1.

---

## 2. Baseline — reproducing the level result on underlying drift — MEASURED

Before testing velocity, the level claim itself, on 164 sessions:

| score band | n | drift @10m | z | drift @20m | z | drift @30m | z |
|---|---|---|---|---|---|---|---|
| <50 | 89,029 | +0.121 | +0.77 | +0.116 | +0.38 | +0.069 | +0.16 |
| 50–56 | 41,114 | −0.104 | −1.22 | −0.144 | −1.14 | +0.012 | +0.07 |
| 56–62 | 39,562 | +0.056 | +0.39 | +0.042 | +0.18 | −0.081 | −0.24 |
| 62–68 | 30,548 | −0.100 | −0.47 | −0.168 | −0.43 | −0.344 | −0.66 |
| 68–74 | 17,694 | −0.272 | −1.08 | −0.440 | −0.93 | −0.191 | −0.30 |
| ≥74 | 10,213 | −0.087 | −0.19 | +0.669 | +0.80 | +1.039 | +0.88 |

**The mild negative tilt at 62–74 is there in the point estimates and it is not significant anywhere**
(all |z| < 1.3). More usefully: the *magnitude* is 0.1–0.4 bp against a 1.7 bp bar.

> **This reframes the R table in the brief.** The −0.078 → −0.175 R by band is not mostly a directional
> statement. The friction floor alone is −0.043 R (QQQ) to −0.060 R (SPY) before theta; 0.3 bp of adverse
> drift is worth about −0.012 R. The score band is a weak modulation on top of a cost floor, not an
> anti-forecast with 0.1 R of signal in it. That is worth knowing independently of velocity.

---

## 3. The sweep

### 3.1 The grid, counted honestly

| axis | values | n |
|---|---|---|
| direction | rise-from-low, fall-from-high | 2 |
| lookback k for the derivative | 1, 2, 3, 5, 10 min | 5 |
| level band (rise) | [0,50) [0,56) [0,62) [50,62) [56,68) | 5 |
| level band (fall) | [62,∞) [68,∞) [74,∞) | 3 |
| velocity threshold | ≥2, ≥4, ≥6, ≥8 points | 4 |
| acceleration (2nd derivative) | ignored, or required same-signed | 2 |
| forward horizon | 5, 10, 20, 30 min | 4 |

**1,280 configs. 1,249 eligible** (n ≥ 50 and ≥ 8 sessions). That is the number the family-wise null is
built against — no config was dropped after seeing its result.

### 3.2 The owner's arm: score LOW (50–62) and RISING (vel ≥ 4) — MEASURED

Signed forward drift, bp (z, n):

| lookback | h=5m | h=10m | h=20m | h=30m |
|---|---|---|---|---|
| 1 min | −0.156 (−1.22, 8815) | −0.205 (−1.16, 8649) | −0.166 (−0.65, 8388) | −0.097 (−0.34, 8126) |
| **2 min** | **−0.307 (−2.86, 13048)** | −0.204 (−1.43, 12816) | −0.243 (−1.24, 12409) | −0.158 (−0.68, 12018) |
| 3 min | −0.226 (−2.10, 16415) | −0.132 (−0.92, 16134) | −0.180 (−0.93, 15634) | −0.159 (−0.66, 15158) |
| 5 min | −0.134 (−1.33, 21290) | +0.049 (+0.33, 20981) | −0.129 (−0.69, 20349) | −0.123 (−0.46, 19731) |
| 10 min | +0.076 (+0.80, 27072) | +0.016 (+0.10, 26688) | −0.015 (−0.07, 25789) | −0.015 (−0.05, 25061) |

Raising the velocity threshold does not help — it makes it worse, and the day-mix term stays near zero
throughout, so this is genuinely a within-day timing statement:

| rise 50–62, k=2, h=10 | n | mean | se | z | day-mix | timing |
|---|---|---|---|---|---|---|
| vel ≥ 2 | 26,377 | −0.109 | 0.103 | −1.05 | +0.044 | −0.153 |
| vel ≥ 4 | 12,816 | −0.204 | 0.142 | −1.43 | +0.067 | −0.271 |
| vel ≥ 6 | 7,082 | −0.491 | 0.194 | −2.53 | +0.025 | −0.516 |
| vel ≥ 8 | 3,969 | −0.480 | 0.277 | −1.74 | −0.019 | −0.460 |

Requiring positive acceleration moves the estimate by 0.02–0.05 bp and never changes a sign.

> **Across all 800 eligible rise configs: mean-of-means −0.185 bp; 0 clear the 1.7 bp bar; the best z in
> the entire arm is +1.73.** The hypothesis's own direction is uniformly on the wrong side of zero. If you
> traded it as stated you would be paying 1.55% of premium to buy a −0.2 bp drift.

### 3.3 The mirror arm: score HIGH (≥68) and FALLING (vel ≥ 4) — MEASURED

| lookback | h=5m | h=10m | h=20m | h=30m |
|---|---|---|---|---|
| 1 min | −0.058 (−0.12, 1142) | +0.595 (+0.71, 1123) | +1.316 (+0.87, 1093) | +1.660 (+0.85, 1041) |
| 2 min | +0.103 (+0.22, 1644) | +0.685 (+0.85, 1624) | +2.117 (+1.45, 1568) | +2.297 (+1.18, 1494) |
| 3 min | +0.541 (+1.26, 1939) | +1.255 (+1.65, 1910) | +2.380 (+1.82, 1840) | +2.582 (+1.45, 1760) |
| 5 min | +0.796 (+1.42, 2345) | +1.656 (+1.79, 2305) | +2.393 (+1.62, 2221) | +2.072 (+1.13, 2126) |
| 10 min | +1.108 (+1.83, 2844) | **+2.288 (+2.28, 2791)** | +2.746 (+1.63, 2699) | +1.606 (+0.84, 2558) |

This looks alive: 155 of 449 eligible fall configs clear the 1.7 bp bar, median +2.649 bp. **§4.6 shows
it is a day-mix artefact.** The single best config in the whole grid by z is here — `fall, k=5, band ≥68,
vel ≥6, h=10`: **+2.572 bp, block se 1.053, z +2.41, n = 974 over 110 sessions.**

---

## 4. Kill attempts

### 4.1 SE sanity — `eval_harness.block_bootstrap` vs the closed-form day-cluster SE

Everything above uses a closed-form session-clustered SE (needed because the family-wise null recomputes
the whole grid 4,000 times). Cross-checked against the harness on the two headline configs:

| config | harness mean | block se | 95% CI | n | sessions | eff_n | naive se | inflation | cluster z | bootstrap z |
|---|---|---|---|---|---|---|---|---|---|---|
| best +z | +2.572 | 1.053 | [+0.485, +4.583] | 974 | 110 | 285.7 | 0.570 | 1.85× | +2.41 | **+2.44** |
| best −z | −0.307 | 0.107 | [−0.519, −0.094] | 13,048 | 164 | 6442.4 | 0.075 | 1.43× | −2.86 | **−2.86** |

They agree to 0.03. The naive-SE inflation factor (1.4–1.9×) is the overlapping-window correction the
harness exists to enforce; `eff_n` of 286 out of 974 is the honest sample size behind the best number in
this study.

### 4.2 MULTIPLE COMPARISONS — the kill

Family-wise null: **session sign-flip**. Each replicate flips the sign of a whole session's contribution
**coherently across every config at once**, so the correlation structure of the grid (configs share bars,
share bands, share horizons) is preserved. It preserves each config's per-session trigger counts exactly
and randomises only whether that session's drift was up or down — which is precisely the null "the score,
in any form, knows nothing about direction or its timing." 4,000 replicates.

| null max\|z\| percentile | 5 | 25 | **50** | 75 | 90 | **95** | 99 |
|---|---|---|---|---|---|---|---|
| | 2.19 | 2.54 | **2.85** | 3.21 | 3.55 | **3.77** | 4.29 |

> **Observed max\|z\| = 2.862. Family-wise p = 0.49.**
> The single best result in a 1,249-config search sits at the **51st percentile** of pure noise. To clear
> FWER 0.05 this family needs **|z| ≥ 3.77**; nothing in the grid reaches 2.87.

(The prior 39,424-config study had a null median max|z| of 2.99. A 1,280-config family at 2.85 is the
same scale — the grid is heavily internally correlated, so the effective number of independent tests is
in the low hundreds, not the thousands.)

### 4.3 CONTROL — IWM, where the mechanism should not apply

chip does not trade IWM and has no tape model for it. Same grid, same code path:

| | max\|z\| | family-wise p |
|---|---|---|
| SPY + QQQ (the real thing) | 2.86 | 0.49 |
| **IWM** | **3.07** | 0.38 |

At the SPY/QQQ best-+z config, IWM gives +1.249 bp, z +1.39. At the best-−z config, −0.152 bp, z −0.88.

> **The control instrument scores *higher* than the target.** Whatever the grid is picking up is a
> property of a liquid US index future's minute bars, not of chip's score on chip's tickers.

### 4.4 PURGED WALK-FORWARD with embargo

Expanding-window over sessions, 5 chronological blocks, 1-session embargo between train and test. Best
config by train z is selected in-sample and evaluated OOS:

| block | train | selected config | train z | train mean | **OOS n** | **OOS mean** |
|---|---|---|---|---|---|---|
| 1 | 31 d | fall k=5 ≥62 vel≥8 acc≥0 h=30 | +3.59 | +9.803 bp | 378 | **−1.954 bp** |
| 2 | 64 d | rise k=2 <50 vel≥4 h=5 | +2.48 | +0.603 bp | 2,609 | **−0.122 bp** |
| 3 | 97 d | rise k=10 50–62 vel≥2 acc≥0 h=5 | +2.54 | +0.279 bp | 6,784 | **+0.077 bp** |
| 4 | 130 d | fall k=5 ≥62 vel≥8 h=10 | +2.57 | +1.732 bp | 468 | **+0.442 bp** |

> **Pooled OOS: n = 10,239, mean −0.032 bp, se 0.188, z −0.17.**
> Note block 1: a train z of +3.59 and a train mean of +9.8 bp — the kind of number that gets a signal
> shipped — delivers −1.95 bp out of sample. The selection never transfers.

### 4.5 CONTROL — is it just price velocity in a costume?

Day-clustered OLS of forward drift on score velocity, with and without the underlying's own trailing
return over the *same* lookback. Run over the whole level band, not just the thresholded tail, so the
regression sees the full relationship:

**Best −z config's band (50–62, k=2, h=5, n = 81,470):**

| model | β(score vel) | t | β(price vel) | t |
|---|---|---|---|---|
| score velocity alone | −0.0315 bp/pt | **−2.39** | — | — |
| + trailing price return | −0.0124 | **−1.11** | −0.0414 bp/bp | **−2.23** |
| price velocity alone | — | — | −0.0462 | **−2.50** |

corr(score velocity, price velocity) = **+0.42**.

**Best +z config's band (≥68, k=5, h=10, n = 27,582):** score velocity alone t = −1.76; with the price
control, score t = −1.54 and price t = −1.90; price alone t = −2.06. corr = +0.09.

> **Score velocity is a noisier copy of price velocity.** Adding the trailing underlying return halves
> the score coefficient and takes it below t = 1.11, while the price coefficient survives. And the
> baseline it is a noisier copy *of* is itself nothing: the desk previously measured QQQ 30-min
> autocorrelation at 0 (+0.36 ± 0.70), and the price-velocity coefficients here are −0.04 bp per bp,
> which over a ±20 bp trailing move is ±0.8 bp — under the 1.7 bp bar before a single cent of cost.

### 4.6 DECOMPOSITION — where the mirror arm's +2.6 bp actually comes from

This is the mechanistic kill for §3.3, and it is the most transferable finding in the report.

Split each config's mean into the part explained by **which sessions and which side fired** and the part
explained by **when within the session**:

```
mean    = Σ_{d,side} S_{d,side} / N
day-mix = Σ_{d,side} n_{d,side} · μ_{d,side} / N      μ_{d,C} = +day drift,  μ_{d,P} = −day drift
timing  = mean − day-mix
```

`day-mix` is what the *same per-session, per-side trigger counts* would earn by picking bars at **random**
inside each session. It is large, and it is hindsight:

> **MEASURED: corr( n_C − n_P , that session's mean signed drift ) = +0.833.**

On an up-trending session the call score sits high all day, so the call side triggers far more often —
and the session's forward drifts are positive. Counting triggers is a restatement of the session's
realised direction, discovered from the same price path that produced the score.

(This is the same failure the lead-lag gate had in `eval/qqq_quant.md` — "half the payoff is a
day-direction bet". Arrived at independently here, on an unrelated signal, with a different
decomposition. It is looking like a standing hazard of any signal built from a price-derived indicator
on this desk, not a quirk of one study.)

| the 155 fall configs that clear 1.7 bp | median |
|---|---|
| mean | **+2.649 bp** |
| **day-mix** | **+5.536 bp** (181% of the mean) |
| **timing** | **−2.336 bp** |
| how many have timing ≥ 1.7 bp on its own | **10 of 155**, none with \|z\| above 2.41 |

And the single best config in the grid:

| `fall, k=5, ≥68, vel≥6, h=10` | value |
|---|---|
| mean | +2.572 bp |
| **day-mix** | **+2.402 bp** |
| **timing** | **+0.170 bp** |

> **93% of the best number in this study is day-mix.** The score's actual within-session placement is
> worth +0.17 bp — a tenth of the payoff bar, and (§5) worth −0.055 R after friction, i.e. *below the
> do-nothing floor*.

### 4.6b Two controls that look obvious and are WRONG here — record this

I ran the brief's suggested "shuffled-within-day score" control and it returned **max|z| = 12.56** (and
12.37 on a second seed) — far *larger* than the real data's 2.86. A circular shift of the score within
the session returns max|z| ≈ 8 per replicate with a median |z| of 3.0–4.4 *across configs*.

Neither is evidence about the signal. **Both preserve the per-session, per-side trigger counts** — a
shuffle keeps the day's score distribution, a rotation keeps the whole trajectory — and therefore keep
the entire day-mix term of §4.6, while destroying the timing term that was cancelling it. They
manufacture 5–12σ out of an accounting identity.

> **Do not use a within-day shuffle or rotation as the null for this family.** The session sign-flip is
> the correct reference precisely because it randomises the session's return sign and so kills day-mix
> and timing together. Had I taken the shuffle control at face value I would have written "a scrambled
> score predicts four times better than the real one, therefore we are measuring the underlying" — a
> true-sounding sentence supported by a broken control.

### 4.7 Robustness — warm-up cut and live-behaviour variant

| warm-up cut | first trusted bar | eligible | max\|z\| | null median | crit(95%) | **FW p** |
|---|---|---|---|---|---|---|
| 15 bars | 09:45 | 1,250 | 2.65 | 2.83 | 3.85 | **0.65** |
| **30 bars** | **10:00** | 1,249 | 2.86 | 2.81 | 3.85 | **0.46** |
| 60 bars | 10:30 | 1,240 | 2.74 | 2.83 | 3.84 | **0.58** |
| 90 bars | 11:00 | 1,240 | 2.32 | 2.89 | 3.81 | **0.91** |

**Primary-side-only variant.** Live, `chip_desk` evaluates both directions and takes the stronger, so
only one side is a real candidate per lap. Restricting every observation to the side with the higher
score: max|z| = **2.58**, sign-flip null median 2.98, FW p = **0.82**. The closer the test gets to live
behaviour, the weaker it gets.

### 4.8 SESSION CONCENTRATION

| | best +z (fall ≥68 k=5 h=10) | best −z (rise 50–62 k=2 h=5) |
|---|---|---|
| sessions | 110 | 164 |
| per-session mean drift, min / p25 / med / p75 / max | −29.6 / −5.7 / −0.2 / +4.9 / +23.3 | −5.4 / −1.1 / −0.3 / +0.4 / +13.7 |
| share of sessions with the "right" sign | **0.47** | 0.60 |
| leave-one-session-out mean range | [+1.98, +2.79] (full +2.57) | [−0.354, −0.274] (full −0.307) |
| top-3 sessions' share of \|total contribution\| | 21.2% | 10.4% |

The best positive result in the grid gets the *right sign in fewer than half its sessions*. The median
session contributes −0.24 bp; the mean is dragged positive by a fat right tail. That is not
concentration in three days — it is worse: it is a coin flip with a skewed payoff, which is exactly what
a day-mix term looks like.

The negative result is genuinely stable (LOO range 0.08 bp wide, 60% sign agreement) — it is simply
tiny and inside the family-wise null.

---

## 5. Real quotes vs reconstructed — say which is which

**The sweep uses no reconstructed option prices at all.** Its estimand is *underlying* drift, taken
directly from IBKR 1-min closes — that part is exact. What is reconstructed is the **score**, and §1.2
validated it bit-for-bit against the live function. So: **score = exact replica, drift = real bars,
options = not involved.**

**The option-level replay on real quotes is NOT MEASURABLE.** `signal_desk.db.path` has real two-sided
ATM quotes, but after `eval_harness.load_pool`'s |Δ| ∈ [0.40, 0.60] filter:

| | contracts | ticks | sessions |
|---|---|---|---|
| SPY | 11 | 747 | 7 |
| QQQ | 6 | 757 | 5 |
| IWM | 0 | 0 | 0 |

The best config produces **0 triggers** in SPY's stored paths and 6 in QQQ's. Weaker fallbacks produce
16–23 entries across **3** bootstrap blocks. One of those returned a delta of **+0.184 R at z = 4.96** —
and it should be ignored: n = 16, 3 session blocks, and the harness's own tell, `se_inflation = 0.34×`
(the block SE came out *smaller* than the naive SE, which only happens when there are too few blocks to
resample). This is the exact failure mode `eval_harness` was written to prevent, reproduced here so it is
on the record rather than in a headline.

> **No claim in this report rests on a reconstructed option price, and no claim rests on the `path`
> table.** The real-quote arm is reported as insufficient, not as supporting or contradicting.

## 6. If you took the best number at face value anyway — THEORY

Linear elasticity, 1DTE ATM, ω ≈ 80, `eval_harness.cost_model` for friction:

| | drift | → % of premium | − friction | → R |
|---|---|---|---|---|
| best config, full mean | +2.572 bp | +2.058% | −1.554% (SPY) | **+0.0194 R** |
| best config, **timing component only** | +0.170 bp | +0.136% | −1.554% | **−0.0545 R** |
| the 1.7 bp bar itself | +1.700 bp | +1.360% | −1.370% (QQQ) | −0.0003 R |

Sample required to confirm, two-sided, 80% power, dispersion 0.55 R (`eval_harness.trades_needed`):

| effect | trades | sessions at 6/day | **years** |
|---|---|---|---|
| +0.019 R (the best case above) | 6,577 | 1,096 | **4.35** |
| +0.030 R (the prior benchmark) | 2,639 | 440 | 1.75 |
| +0.050 R | 950 | 158 | 0.63 |

Smallest effect visible in 250 trades (~2 months at 6/day): **0.098 R**.

> **This is the decisive practical fact regardless of the z.** Even granting the +2.572 bp — a number
> that is 93% day-mix, fails a walk-forward, and sits at the median of its own null — the resulting
> +0.019 R needs **4.35 years** of live trading to distinguish from zero. It is unconfirmable on this
> desk's throughput. Nothing about this hypothesis is worth a shadow slot.

## 7. What this study rules out — MEASURED

The 164-session sample is large enough that the negative result is informative, not merely inconclusive.
Detectable drift at 80% power, over the 1,249 eligible configs: **10th pct 0.40 bp, median 1.08 bp, 90th
pct 5.34 bp.**

> **896 of 1,249 configs have a 95% upper bound below +1.7 bp** — this study *excludes* a payoff-clearing
> effect for them, it does not merely fail to find one.

The configs it cannot rule out are the rare ones (n in the hundreds), which is a structural problem
rather than a data problem: a config that fires 5 times a week cannot both be confirmed here and be
worth building.

---

## 8. Why this should not come back

Recorded so the idea is not re-proposed:

1. **The framing was right and the answer is still no.** "Level says where the tape is, velocity says
   where it is going" is a good instinct and it was worth testing. It was tested across 1,280
   specifications and the best of them is at the 51st percentile of noise.
2. **The direction is wrong, not just the significance.** Every one of the 800 "low and rising" configs
   has a point estimate at or below zero. There is no sub-region to go back for.
3. **The mirror looked alive and was a hindsight artefact.** Any future version of this that reports
   "fade a high score that is rolling over, +2.6 bp" must show the day-mix / timing split of §4.6 before
   anything else. If day-mix is doing the work, it is not a forecast.
4. **It is downstream of price velocity, which is itself nothing.** Even at its strongest the score
   coefficient does not survive controlling for the underlying's own trailing return.
5. **It would be unconfirmable even if real.** 4.35 years at 6 signals/day.
6. **Beware the obvious controls.** Within-day shuffle and within-day rotation both return 5–12σ here for
   a structural reason (§4.6b). A future review that uses either will "discover" this signal again.

This is the seventh candidate signal on this desk to die, and it dies the same two deaths the lead-lag
gate died in `eval/qqq_quant.md`: a family-wise correction it cannot clear (there 0.93 against a null
median of 2.99; here 0.49 against 2.85), and a payoff that turns out to be a day-direction bet rather
than a within-day forecast (there "half the payoff"; here 93% of it). **Two independent reviews, on two
unrelated signals, have now found the same two failure modes in the same order.** The pattern is stable
enough to be worth naming: **on 1-minute SPY/QQQ, a derived indicator
of past price does not forecast the next 5–30 minutes at a magnitude that clears 1.5% round-trip
friction.** Every candidate so far has been a re-parameterisation of that same non-fact. The next
hypothesis is better spent somewhere the bar is not 1.7 bp — on execution cost, on sizing, or on exit
geometry, where the desk has already found real effects and where the friction floor is the thing being
attacked rather than the thing that has to be overcome.

---

## Appendix — reproduction

Analysis code lives outside the repo (scratchpad, session-local) by the file-ownership rule; this file is
the only artefact committed. To rebuild:

1. Pull 8 × `"1 M"` chunks of 1-min RTH TRADES bars for SPY/QQQ/IWM (client_id 20–26, `readonly=True`).
2. Reconstruct the score per §1.2 — and validate it bit-for-bit against `market.tape_strength` through a
   1,560-bar slice before using it. Watch the three float traps in §1.2.
3. Cut the first 30 session bars, and require `session_bar ≥ 30 + k` so derivatives never straddle the cut.
4. Observation = (bar, side); outcome = signed forward drift, never across a session close.
5. Cluster every SE on the session; cross-check the headline against `eval_harness.block_bootstrap`.
6. Family-wise null = **session sign-flip, coherent across all configs**. Not a within-day shuffle, not a
   rotation (§4.6b).
7. Decompose any survivor into day-mix and timing (§4.6) before believing it.

# The mega-cap divergence fade — independent reproduction and kill attempts

Quant review of the one entry signal in `eval/qqq_mechanics.md` §7.3 / §9.4 that was reported to
survive out of sample: *"fade the cap-weighted NVDA/AAPL/MSFT 30-minute lead over QQQ, +1.56 bp ± 0.64
at a 20-minute hold."*

**Scope:** this signal only. **No `.py` file in the repo was modified or created. Nothing was
committed. No signal was posted to the desk. The live server was not restarted.** All work ran under
`DESK_ENV=test` with a `mktemp -d` data dir; the only live socket used was a read-only IBKR historical
pull on client_id 37/38.

Claims are labelled **MEASURED** (computed here, from data I pulled myself), **THEORY** (arithmetic on
measured inputs), **GUESS** (judgement).

---

## 0. Verdict

**I reproduce the number exactly and I do not believe it. Recommend: do not build it, do not shadow it,
close the hypothesis.**

| | |
|---|---|
| Reproduced on the prior study's own window (37 sessions) | **+1.560 bp, day-block se 0.689, z 2.26** vs claimed +1.56 ± 0.64 |
| Reproduced on my 40 sessions | +1.536 bp ± 0.629, z 2.44 |
| Family-wise p over the plausible search space | **0.93** |
| Payoff that is a *within-day* signal rather than a day-direction bet | **+0.79 bp ± 0.49 (z 1.60)** |
| Same signal used to trade **IWM** instead of QQQ | **+1.29 bp ± 0.51 (z 2.54)** |
| Forward return of the mega-caps themselves after they "lead" | **−0.27 bp ± 0.61** (they do not revert) |
| Worth per trade, replayed through `db._shadow_scaleout` | **−0.004R, 95% CI [−0.036, +0.028]** |
| Worth at the desk's actual 5 trades/day throughput | **−0.051R, 95% CI [−0.151, +0.055]** |
| Trades to confirm +0.03R live at 5/day, 80% power | **3,487 – 4,986 (2.8 – 4.0 years)** |

Four kill attempts failed (walk-forward, session concentration, a placebo, and the QQQ-own-reversion
control — it is genuinely *not* mean reversion in a costume). Six succeeded, and two of those are
structural rather than statistical: the signal predicts an index with **no exposure to the names in the
signal** at least as well as it predicts QQQ, and the components it tells you to fade do not fade.

---

## 1. Data — MEASURED, pulled independently

| | |
|---|---|
| Source | IBKR `reqHistoricalData`, `TRADES`, `useRTH=True`, `1 min`, `durationStr="2 M"`, client_id 37 |
| Pulled | 2026-07-22, ~15:00 CDT |
| Symbols | QQQ, SPY, IWM, NVDA, AAPL, MSFT, AVGO, AMZN, META, GOOGL, TSLA, COST, NFLX (13 × ~15,600 bars) |
| Common sessions | **40**, 2026-05-26 → 2026-07-22 (superset of the prior study's 36–37) |
| Option chain | live QQQ 0DTE + 1DTE, ATM ± 2, both rights, client_id 38, 2026-07-22 post-close — used only to calibrate the replay's vol level |
| Missing-bar handling | forward-fill within session; a session is dropped for a symbol with < 380 of 390 RTH minutes (no session was dropped) |

Scratchpad (scripts + cached bars, nothing in the repo):
`/private/tmp/claude-501/-Users-rahul-Workspace-Copy-trading/53046cf7-c6bd-4419-be21-b10754d0fca6/scratchpad/leadlag/`

All standard errors below are **session-block bootstrap** (5,000 resamples of whole sessions with
replacement). Minute-level iid SEs on this data understate by roughly 3× because a 30-minute lookback
and a 20-minute forward window make consecutive minutes almost the same observation.

---

## 2. Reproduction — MEASURED, and one specification trap

Signal, as I implemented it from §9.4:

```
comp30[t] = ( 0.10·r30(NVDA) + 0.09·r30(AAPL) + 0.08·r30(MSFT) ) / 0.27
gap[t]    = ( comp30[t] − r30(QQQ) ) × 1e4          # bp
side      = PUT if gap > 0 else CALL                # "fade the leader"
payoff    = side · QQQ forward 20-min return, bp
window    minute 30 … 360 of RTH
```

| | drift, 20-min hold | z |
|---|---|---|
| **Prior study's window (37 sessions, 2026-05-29→07-22)** | **+1.560 ± 0.689** | +2.26 |
| My 40 sessions | +1.536 ± 0.629 | +2.44 |
| My 40 sessions, \|gap\| > 5 bp | +1.680 ± 0.755 | +2.23 |

**The reproduction is exact.** The prior agent's arithmetic is correct and their number is not a bug.

**But §9.4 as written does not produce this signal.** The spec says `composite = cap-weighted 30-min
return of (NVDA 0.10, AAPL 0.09, MSFT 0.08)`. Read literally — a weighted *sum* with weights summing to
0.27, i.e. a portfolio that is 73% cash — you get a different series, because it is 97% correlated with
minus QQQ's own 30-min return:

| composite definition | corr(gap, QQQ 30-min ret) | drift H=20 |
|---|---|---|
| weights renormalised to 1 (what reproduces) | −0.43 | +1.536 ± 0.629 |
| raw weighted sum, 0.27 gross (what §9.4 says) | **−0.97** | +0.656 ± 0.670 (z 0.98) |

MEASURED. Anyone implementing §9.4 verbatim would ship a QQQ mean-reversion signal by accident and
measure nothing. If any of this survives elsewhere, that line needs fixing.

---

## 3. Kill attempts that FAILED (the signal survived these)

### 3.1 Purged, embargoed walk-forward — survived

Splits are by *session*, so a 20-minute forward window can never cross a fold boundary; I additionally
embargo one full session on each side of every boundary.

| | result |
|---|---|
| Expanding window, 1-session embargo, 32 OOS sessions | **+1.63 bp, bootstrap se 0.78, z 2.10** |
| Same, \|gap\| > 5 | +1.63 ± 0.91, z 1.79 |
| 5-fold contiguous session blocks, test-fold drift | +1.37, +3.84, +0.84, +0.25, +2.09 — **all five positive** |
| 20/20 halves | +2.04 ± 0.85 / +1.03 ± 0.91 |

MEASURED. The rule has no fitted parameters, so a walk-forward is close to a formality here — it
confirms there is no *fitting*, which was never the risk. The risk is *selection*, and walk-forward
cannot see selection (§4.1).

### 3.2 Session concentration — survived

MEASURED, \|gap\| > 5, H = 20, 40 sessions: 26/40 sessions positive, median session mean +1.62 bp
(vs pooled +1.68), leave-one-session-out range **[+1.36, +1.93]**. Session-level sign-flip permutation
p = 0.038. **It is not 3 days out of 37.**

It is, however, tail-carried: dropping the top 3 contributing sessions takes it to +0.81 bp, and the
top 5 to **+0.32 bp** — under any break-even bar on the table. GUESS: that is ordinary heavy-tailed
return arithmetic, not by itself an indictment; I record it because it means the *margin over cost*
lives entirely in the right tail.

### 3.3 Is it just QQQ mean reversion in a cross-sectional costume? — survived, cleanly

This was the charter's leading suspicion. It is wrong.

MEASURED, day-clustered OLS, y = QQQ forward 20-min return in bp, n = 13,240 minutes / 40 sessions:

| model | gap30 | qqq30 |
|---|---|---|
| gap30 alone | **−0.0799 (z −2.05)** | — |
| qqq30 alone | — | +0.0042 (z +0.09) |
| both | **−0.0955 (z −1.99)** | −0.0283 (z −0.53) |

And trading QQQ's own 30-minute return directly: reversion +0.36 ± 0.70, momentum −0.36 ± 0.70 — zero
at every threshold. QQQ has **no** 30-minute autocorrelation in this sample, so there is nothing for the
gap to be a proxy *for*. The gap coefficient is unchanged by the control.

The one asymmetry worth recording (MEASURED, quintiles of QQQ's own 30-min return, \|gap\|>5):

| qqq30 quintile | mean qqq30 | fade payoff | frac PUT |
|---|---|---|---|
| 0 | −43.6 bp | **+3.85 ± 1.68** | 0.76 |
| 1 | −12.0 | +1.38 ± 0.71 | 0.66 |
| 2 | −0.1 | +1.40 ± 0.83 | 0.55 |
| 3 | +11.3 | +1.19 ± 0.80 | 0.49 |
| 4 | +39.1 | **−0.13 ± 1.34** | 0.31 |

Monotone. The whole effect is in states where QQQ has already fallen and the signal is short. Pure
downside momentum in that same quintile measures +1.47 ± 2.64 (nothing), so the fade is not *only*
momentum — but it is a signal that only exists on one side of the tape, which is a mild warning about
regime dependence in a 40-session sample.

### 3.4 Placebo — survived

Rebuild the identical trade using a **30-minute-stale** gap (`gap[t−30]`), which carries no live
information: **−0.28 ± 0.68**. The construction is not self-fulfilling and there is no forward-looking
leak in my pipeline. MEASURED.

---

## 4. Kill attempts that SUCCEEDED

### 4.1 Multiple comparisons — fatal

"NVDA/AAPL/MSFT, cap-weighted, normalised, 30-minute lookback, faded, 20-minute hold, \|gap\|>5" is
seven choices. I enumerated the space a reasonable person would have searched to land there:

| axis | values | n |
|---|---|---|
| universe | all 1-, 2-, 3-subsets of the top 6 QQQ names, plus top-5 / top-8 / top-10 baskets | 44 |
| weighting | cap, equal | 2 |
| normalisation | renormalised, raw sum | 2 |
| lookback | 5, 10, 15, 20, 30, 45, 60 min | 7 |
| horizon | 5, 10, 20, 30 min | 4 |
| threshold | 0, 3, 5, 10 bp | 4 |
| sign | fade, follow | 2 |
| | **nominal configs** | **39,424** |

MEASURED. Every config scored with the same session-block bootstrap. Then a **family-wise
session-sign-flip null** — flip the sign of each session's contribution and recompute *all* configs
together, which preserves the enormous correlation between neighbouring configs and does not assume
independence:

| | |
|---|---|
| Best \|z\| anywhere in the 39,424-config grid | **3.03** (`NVDA+AMZN, cap, raw, lb 5, H 5`) |
| Null distribution of the grid's max \|z\| | median **2.99**, 90th 3.53, 95th **3.69**, 99th 3.95 |
| Family-wise p for the grid's best config | **0.45** |
| **Family-wise p for the claimed config (\|z\| 2.47)** | **0.93** |
| Rank of the claimed config among 19,712 fade-side configs | **47th** |
| Implied effective independent tests | **N_eff ≈ 230** |

Read that table twice. **The single best-looking configuration in a 39,424-cell search is exactly what
pure noise produces**, and the claimed configuration is not even in the top 45 of that search. With
N_eff ≈ 230 the bar for 5% family-wise significance is \|z\| > 3.70; the claim is 2.47. Even granting
the prior review's own, far more generous accounting — *"of the ~15 gates I scanned this is the only
one that does that"* — the bar is \|z\| > 2.94, and it still fails.

The 18/18 split cannot rescue this, and the prior review's framing of it is where the error lives:
*"Of the ~15 gates I scanned this is the only one that does that."* Surviving a split-half is the
**selection criterion**, not evidence — with 15 gates at z ≈ 0 you expect roughly 15 × ¼ ≈ 4 to show
the same sign in both halves by chance, and you would then report the largest of those 4. One split is
one draw, and the draw was chosen after seeing it.

### 4.2 Half the payoff is a day-direction bet with n = 40, not n = 13,240 — fatal

Decompose the per-session payoff exactly:

```
payoff_d = mean_t[ side_t · fwd_t ]
         = mean_t[ side_t · (fwd_t − drift_d) ]   +   mean_t[side_t] · drift_d
              within-day timing                        between-day directional lean
```

MEASURED, session-equal-weight, 40 sessions, H = 20, threshold 0:

| component | value | z |
|---|---|---|
| TOTAL | +1.536 ± 0.635 | +2.42 |
| **within-day timing** | **+0.788 ± 0.494** | **+1.60** |
| **between-day lean** | **+0.748 ± 0.305** | +2.45 |

`corr(session net lean, session drift) = +0.36` over 40 sessions (SE ≈ 0.16). Sessions ran a mean
20-minute forward drift of −0.92 bp with a cross-session sd of 6.15 bp; the signal's net lean has sd
0.30 and leans short 55% of the time.

Two consequences:

1. **The intraday signal — the thing that would justify a 20-minute scalp — is +0.79 bp ± 0.49, z 1.60,
   not significant, and below every break-even bar on the table** (0.91 bp in the prior review, 1.72 bp
   in mine, §4.5).
2. The other half is one bet per session on the session's direction. That has **40 observations**, and
   a 40-session sample cannot distinguish `corr = +0.36` from luck. It also means the strategy's real
   risk is day-level directional risk, not scalp risk — which is not what the desk is sized for.

### 4.3 The IWM control — fatal, and structural

If this is a component-vs-index dislocation, the effect must live in QQQ and its constituents. It does
not. MEASURED, same gap built from NVDA/AAPL/MSFT vs QQQ, only the traded leg changes:

| traded leg | forward 20-min payoff | z |
|---|---|---|
| QQQ (the claim) | +1.536 ± 0.629 | +2.44 |
| **IWM (Russell 2000)** | **+1.294 ± 0.510** | **+2.54** |
| rest-of-QQQ basket (AVGO, AMZN, META, GOOGL, TSLA, COST, NFLX) | +0.680 ± 0.613 | +1.11 |
| SPY | +0.661 ± 0.385 | +1.72 |
| **the NVDA/AAPL/MSFT composite itself** | **−0.266 ± 0.606** | −0.44 |

A cross-sectional dislocation signal must have its **largest** reversion in the leg that dislocated.
The ordering is the opposite: the signal predicts **small caps** — an index with essentially zero
NVDA/AAPL/MSFT weight — with a *higher* z than QQQ, and predicts the three names in the signal with the
*wrong sign*.

What is left is a broad-market direction signal that happens to be computed from three stock prices. It
is not lead-lag, and calling it lead-lag is what made it look like an uncrowded structural edge instead
of a very crowded, very weak market-timing bet.

### 4.4 The stated mechanism is falsified, and the universe is illusory

- **"Fade the mega-caps."** After they lead, their own 20-minute forward return is −0.27 ± 0.61 bp
  *against* the fade. **They do not come back.** MEASURED. Whatever is happening, "the leaders revert"
  is not it.
- **NVDA contributes nothing.** MEASURED, lookback 30, H = 20, threshold 0:

  | universe | drift | z |
  |---|---|---|
  | cap .10/.09/.08 (the claim) | +1.536 ± 0.629 | +2.44 |
  | equal weight | +1.637 ± 0.673 | +2.43 |
  | **NVDA only** | **+0.410 ± 0.803** | **+0.51** |
  | AAPL only | +1.344 ± 0.689 | +1.95 |
  | MSFT only | +1.386 ± 0.742 | +1.87 |
  | NVDA + AAPL | +0.873 ± 0.553 | +1.58 |

  The headline "cap-weighted NVDA/AAPL/MSFT" is AAPL+MSFT with NVDA as ballast — and NVDA is precisely
  the name whose idiosyncratic options/retail flow the mechanism story depends on. The prior review's
  own note that "the result is insensitive to [the weights]" is correct, and it is a warning, not a
  reassurance: a signal insensitive to its own economic content is not measuring that content.
- **The SPY analogue fails.** The identical construction with SPY as the base: +0.45 ± 0.49. SPY's top
  three are the same three names.

### 4.5 It does not survive the trade — MEASURED

Replay: real QQQ 1-minute paths; **1DTE ATM** (chip's `dte_pref = 1`, `strike_inc = 1.0`); Black–Scholes
priced on a **measured variance clock** — per-minute intraday variance profile estimated over the same
40 sessions (median session RV ⇒ 15.5% annualised; measured overnight/intraday variance ratio 0.68) —
**calibrated against the live chain** (model ATM 1DTE at the 2026-07-22 close = $3.92 at the 1.10
multiplier vs market mid $3.95, IV 0.289); entry at the ask, exit at the bid with
spread = max($0.02, 0.4% of premium); `db.RT_COST_PER_CT` = $1.30; exits run through
**`db._shadow_scaleout` verbatim** at shipped QQQ geometry (stop 28 / t1 22 / t2 46 / t3 74) with the
20-minute time stop. Session-block bootstrap, 40 sessions.

| | E[R] | 95% CI |
|---|---|---|
| null — every minute, both sides | **−0.0421 ± 0.0024** | [−0.0465, −0.0370] |
| mega-cap fade, all qualifying minutes | **−0.0044 ± 0.0164** | [−0.0362, +0.0277] |
| mega-cap fade, \|gap\| > 5 bp | +0.0002 ± 0.0193 | [−0.0375, +0.0374] |
| paired, gate − null | +0.0377 ± 0.0153 | [+0.0085, +0.0680] |

Stable across vol multipliers 1.00 / 1.10 / 1.25. **The gate is a real improvement on the null and the
result is still not a positive-expectancy trade.** It moves chip from losing 4.2% of risk per trade to
losing 0.4%.

**The 0.91 bp break-even bar in `qqq_mechanics.md` is too low for the contract chip actually trades.**
THEORY, from measured inputs: the replay's sensitivity is +0.0245R per bp of drift, so break-even is
0.0421 / 0.0245 = **1.72 bp**, not 0.91. The reason is elasticity — the prior review's 0.91 bp is a
0DTE-like Ω ≈ 197 (its worked example: 278 minutes to expiry, premium $1.79); the live 1DTE ATM I
pulled is K 705 C, δ 0.514, S 705.64, mid $4.53 ⇒ **Ω ≈ 80**. Same cost as a fraction of premium, less
than half the leverage, so it needs about twice the drift. Measured drift 1.54 bp < 1.72 bp
break-even — which is exactly why the replay lands at −0.004R.

### 4.6 Deployment destroys what is left — MEASURED

The gate fires ~265 qualifying minutes/session (~24 entries/session under `COOLDOWN_S = 900`). The desk
supports ~5 signals/day. §9.4's answer is *"rank the day's candidates by \|gap\| and take the top 4–5."*
That specific selection rule is the one variant that measures **negative**:

| deployed rule (5/session, 15-min cooldown) | drift | E[R] |
|---|---|---|
| rank by \|gap\|, take top 5 (hindsight-optimal, an upper bound) | **−1.60 ± 1.95 bp** | −0.101 ± 0.045 |
| causal: first 5 minutes clearing \|gap\| > 5 | +0.03 ± 2.42 bp | −0.051 ± 0.052 |
| (reference) every qualifying minute | +1.68 ± 0.76 bp | +0.000 ± 0.019 |

The \|gap\| decile table shows why the extrapolation from "\|gap\|>5 helps" was unsafe — the response is
not monotone, deciles 1–8 are a flat +1.3 to +1.6 bp, and the extreme tail is 38 of 40 sessions' worth
of noise (decile 9: +4.84 ± 2.91, z 1.66). Capping at 5/session forces you to take the largest gap of
*quiet* days too, and the tail days that carry the whole effect get one slot instead of forty.

The prior review flagged this honestly — *"the deployed population is not the measured population —
flag it and expect a degradation of unknown size."* The size is now measured: **it is the entire
effect.**

---

## 5. The mechanism, and why I do not believe there is one

**Skeptic's prior (THEORY).** QQQ is at the centre of one of the most heavily arbitraged complexes in
world markets: creation/redemption by APs, NDX futures, QQQ options, single-stock futures and options,
and a dozen index-arb desks whose entire business is the basket-vs-ETF basis. A 30-minute,
5-basis-point predictable relationship between the top three components and the ETF would be a
free lunch of the most conspicuous possible kind. Prior probability that a 40-session desk study finds
one: very low. That prior should dominate a z of 2.5, and it does.

**But the signal is not actually an arbitrage claim, and it is important to say why.** `gap =
comp − QQQ` is not a basis. QQQ ≈ 0.27·comp + 0.73·rest, so `gap ≈ 0.73·(comp − rest)` — MEASURED,
corr(gap, direct mega-vs-rest7 breadth) = +0.54, and trading the direct breadth spread gives
+1.79 ± 0.73, slightly *better* than the original. So the quantity is **intraday breadth**: are the
mega-caps leading or lagging the rest of the index? Nothing forces that to zero and no arbitrageur
closes it. That is a legitimate object of study, and the arbitrage objection does not by itself kill it.

**So what would the mechanism have to be?** For "fade" to pay, one of these must hold:

1. *The leaders overshoot and revert.* MEASURED false: the composite's own forward return is
   −0.27 ± 0.61 bp against the fade.
2. *Narrow leadership predicts the broad basket falls* — the "unhealthy rally" story. MEASURED weak:
   the rest-of-QQQ basket gives +0.68 ± 0.61, z 1.11. And a breadth-quality effect is a *daily*
   phenomenon in every published treatment of it; asking it to resolve inside 20 minutes, at 1.5 bp,
   is not the same claim.
3. *It is a market-direction signal.* MEASURED consistent with everything: it predicts IWM as well as
   QQQ (z 2.54 vs 2.44), half the payoff is a between-day directional lean, and the leg with the most
   exposure to the signal's own names is the only one with the wrong sign.

**(3) is the only story the data supports, and (3) is not an edge.** "Something about the last 30
minutes of relative mega-cap performance forecasts the next 20 minutes of broad US equity direction" is
a claim about market timing, tested on 40 sessions, discovered inside a 39,424-cell search, worth 1.5
bp against a 1.7 bp cost bar. Every part of that sentence is a reason to discard it.

**GUESS, on why it looked strong.** Over these 40 sessions the market's intraday drift was mildly
negative (−0.92 bp per 20-min window) and the signal leans short 55% of the time; the between-day
lean correlated +0.36 with the day's direction. That is one 40-observation regression doing half the
work, dressed as a 13,240-observation scalp study. I would expect it to be sign-unstable out of sample
and I would not be surprised by either sign.

**On my own finding, held to the same standard.** The strongest thing I measured is the IWM control at
z 2.54. I do not believe that either, and for the same reason: it comes from the same 40 sessions and
the same search. I report it as *evidence against the lead-lag interpretation* — a relative statement,
robust to the level being noise — not as a tradable small-cap signal.

---

## 6. `chip_leadlag.py` — deliberately not written

The charter said to specify the module **if it survives**. It does not, so I have not created the file.
Adding 3 IBKR streaming lines, a new module, and a new gate to `chip_desk` for a signal whose honest
family-wise p is 0.93 and whose replayed expectancy is −0.004R would be a net negative even if the code
were free. Recorded here instead so nobody has to re-derive it:

- **If it were shipped anyway** (not recommended): universe NVDA/AAPL/MSFT; renormalised weighted-average
  30-min return (the `/0.27` matters — §2); `gap = comp30 − QQQ30` in bp; PUT if gap > 0; 10:00–15:30
  ET; 20-minute hold; halted/missing component → forward-fill within session, drop the minute if any
  component has no print in 5 minutes, drop the session for a component with < 380/390 RTH bars.
- **Cost:** 3 extra 1-min bar streams. `market.get_bars` covers it; `market.reap_subs` caps at 80 and
  chip currently runs 4–8 plus a 5-rung ladder, so the lines are available. That was never the
  constraint.
- **The real cost** is throughput: the gate produces ~24 entries/session against a ~5/day budget, and
  §4.6 shows every way of getting from 24 to 5 measures negative.

---

## 7. What it is worth, and what confirming it would cost

**Worth (MEASURED, session-block bootstrap, 40 sessions, shipped QQQ geometry, `_shadow_scaleout`):**

| population | E[R]/trade | 95% CI |
|---|---|---|
| every qualifying minute (not deployable) | **−0.0044** | [−0.0362, +0.0277] |
| deployed at 5/session, causal | **−0.0512** | [−0.1505, +0.0550] |
| deployed at 5/session, rank by \|gap\| | −0.1011 | [−0.1861, −0.0100] |

The most optimistic number in that table is the +0.028R upper confidence limit of the version the desk
cannot trade.

**Sample needed to confirm live (THEORY, from the measured per-session R dispersion; two-sided 5%,
80% power, 5 trades/day):**

| effect to detect | sessions | trades | years |
|---|---|---|---|
| +0.03R | 697 – 997 | **3,487 – 4,986** | **2.8 – 4.0** |
| +0.05R | 251 – 359 | 1,255 – 1,795 | 1.0 – 1.4 |

(range = the two selection rules in §4.6; the wider figure is the causal one, i.e. the one you could
actually run.)

This is the decisive practical fact and it is **worse** than the desk's existing +0.03R / 2,639-trade /
2.1-year benchmark, because throttling to 5 trades/day raises the per-trade variance (session-block
sd of the daily mean R is ~0.34 at 5 trades/day). A signal whose
point estimate is negative and which would take 2.5–5 years of live trading to distinguish from zero
is not a hypothesis the desk can afford to run, even in shadow — shadow capacity is finite and the
opportunity cost is the next hypothesis.

**Recommendation.** Close it. Keep the two findings that are actually reusable and cost nothing:

1. **QQQ has no 30-minute autocorrelation in this sample** (reversion +0.36 ± 0.70, momentum
   −0.36 ± 0.70). Any future gate built on a 30-minute trailing QQQ move starts from zero.
2. **The break-even drift bar for chip's contract is ~1.7 bp, not 0.91 bp** (§4.5) — the difference is
   1DTE vs 0DTE elasticity. Every future entry-signal search should be scored against 1.7 bp, which
   raises the bar on everything in `qqq_mechanics.md` §7.
3. And fix the §9.4 composite definition (§2) before anyone implements it.

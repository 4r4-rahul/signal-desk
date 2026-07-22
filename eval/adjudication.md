# Adjudication — chip, four contradictions

**Date** 2026-07-22 · **Role** lead quant · **Scope** resolve `eval/spy_mechanics.md` vs `eval/qqq_mechanics.md`
vs the desk's own prior findings; fix the measurement machine; ship what survives.

Everything below is labelled `MEASURED` / `THEORY` / `GUESS`. Every standard error is a **day-block
bootstrap** unless explicitly called out as naive. Effect sizes that do not clear 2 SE are called noise,
including my own.

---

## 0. The one-paragraph version

Three of the four headline findings I gave the owner were wrong, and they were wrong for **one shared
reason**: every measurement treated overlapping, within-session, within-contract minutes as independent
draws, and every measurement drew its benchmark from a different population than its strategy. chip is
**not** measurably worse than random — that headline is retracted. The friction floor is **4.9-6.1x
smaller** than I reported. The hold curve is real but it is a **0DTE** effect, not a 1DTE one. Time of
day is **undecidable** on desk data and all three prior claims about it are unsupported. The convergent
mechanics set survives, with **effect sizes roughly 10x smaller** than the SPY review claimed.

A fourth thing is also true and is the most important sentence in this document: **at ~5 signals/day
nothing about chip's entry quality is decidable in under two years.** Every hour spent hunting entry
alpha at this sample rate is an hour spent on a question the data cannot answer.

---

## 1. "chip is WORSE THAN RANDOM" — **RETRACTED**

### What was claimed
`chip_paper.py` (docstring) and `eval/spy_mechanics.md` §6: null −0.142R vs chip TAKE −0.219R, **z = −3.58**,
on 1,458 replayed chip TAKE minutes. I reported this to the owner as the headline finding.

### What is true

`MEASURED` — the artifact hypothesis the QQQ review proposed is **wrong**, and the finding is **also**
wrong, for a different reason.

The QQQ review suspected an entry-price / quote-staleness artifact: chip fires when the tape is moving,
so the ask it lifts is further from mid, so it eats a penalty the null never pays. That is a testable
claim and it is the right instinct. **It fails the test.**

I ran it on the only dataset that can test it — `path`, the desk's own stored **real IBKR bid/ask time
series** with the tape indicators recorded alongside, so `market.tape_strength` can be reconstructed at
every tick and the gate replayed against genuine quotes. (Neither specialist could test it: **both
replayed Black-Scholes synthetics with a deterministic spread that is a function of premium alone.** The
QQQ review says so explicitly — its engine cannot express quote staleness even in principle.)

Holding the fill convention identical across both arms, sweeping the convention:

| convention | SPY delta (chip − null) | QQQ delta |
|---|---|---|
| ask-in / bid-out | −0.1756 | −0.0245 |
| mid-in / mid-out | −0.1884 | −0.0250 |
| mid-in / bid-out | −0.1779 | −0.0248 |

**The delta does not move.** If it were a fill-marking artifact, the mid/mid row would collapse it. It
does not budge by more than 0.013R on SPY or 0.0005R on QQQ. And the direct test of the mechanism is
flat: mean entry spread on chip's minutes vs random minutes is **0.977% vs 1.008% (SPY)** and **0.790%
vs 0.788% (QQQ)** — chip's minutes are, if anything, *marginally tighter*. Momentum minutes are not
wide-quote minutes in this instrument.

So why did z = −3.58 appear? **The standard error.** Reconstructing both ways on the same data:

| | delta | iid-over-minutes SE → z | day-block SE → z | inflation |
|---|---|---|---|---|
| SPY | −0.1955 | 0.0369 → **−5.30** | 0.1114 → **−1.75** | **3.0x** |
| QQQ | −0.0189 | 0.0286 → −0.66 | 0.0174 → −1.08 | 0.6x |

A 20-minute forward return sampled every ~33 s overlaps its neighbours ~36-fold, and the pool is **12
session days / 25 contract-paths**. Nominal n = 1,458 minutes; **effective n ≈ 20**. z = −3.58 sits
exactly between the iid and block figures. It was manufactured by the SE, not by the market.

And the residual delta is **concentrated in the instrument chip does not trade**:

| | delta (chip − null), 20 m | z |
|---|---|---|
| SPY, explicit 0DTE (3 contracts / 3 days) | **−0.3882** | −2.75 |
| SPY, non-0DTE (8 contracts / 6 days) | **−0.0579** | −2.43 |
| QQQ, non-0DTE (5 contracts / 4 days) | **−0.0315** | −1.26 |

On SPY the whole effect lives in **two contract-days** (2026-07-17 −0.504, 2026-07-20 −0.387); drop
07-17 and the pooled delta falls from −0.176 to −0.075. Both are 0DTE. `dte_pref` is 1.

### Verdict

> **RETRACTED.** chip's entries are **not** measurably worse than random. At the DTE chip actually trades
> the gap is −0.058R ± 0.024 (SPY) and −0.032R ± 0.025 (QQQ) — inside noise on both, and carried by 0DTE
> contracts and two sessions. The z = −3.58 was an SE artifact.
>
> **The QQQ review's proposed explanation is also rejected.** It is not a fill-marking artifact; the delta
> is invariant to the entry convention and chip's minutes do not carry wider spreads. Their instinct to
> demand a fixed fill across arms was correct and is now enforced structurally; their diagnosis was not.

### Does this change the shadow-only decision? **No — and the reason changes.**

`size_mult` stays **0.0**. But the stated reason was wrong and must be corrected: chip is not held back
because it is worse than random. It is held back because **nothing is known either way**, and ~5
signals/day cannot make it known inside two years (§5). "No evidence of edge" is not "evidence of no
edge", and neither is a basis to size. Going live would require a pre-registered falsification gate, not
a retraction.

---

## 2. THE FRICTION FLOOR — **I was wrong by 4.9-6.1x**

### What was claimed
I told the owner the round trip is **4.55% of premium**, and the null expectancy **−0.142R**.

### What is true

`MEASURED` — I reproduced 4.545% exactly: it is the median bid/ask spread over all **41,542** two-sided
ticks in `path`. It is also **meaningless for SPY/QQQ**, because that population is set by instruments
chip never touches. Nine tickers with a median spread above 10% supply 9.2% of the ticks:

> PENG 50.0% · MU 46.2% · SPCX 28.6% · FIG 30.8% · XOM 40.0% · CELH 107.7% · SNOW 103.8%

Scoped to ATM (|Δ| 0.40-0.60 — the only options chip buys):

| | n ticks | median spread | penny-wide ($0.01) | + commission on $2.10 | **round trip** |
|---|---|---|---|---|---|
| **SPY ATM** | 971 | **0.935%** | 65.8% | 0.619% | **1.554%** |
| **QQQ ATM** | 871 | **0.751%** | 28.7% | 0.619% | **1.370%** |
| population (all names/DTEs) | 41,542 | 4.545% | — | — | — |

This **independently replicates the QQQ review to three decimals** (0.751 vs their 0.747, though theirs
was N = 1 quote from a single 15:14 snapshot and mine is 871 ticks over 12 days). The SPY review's 1.32%
is in the same family as my 1.554% — they used a $2.17 premium and a fitted spread model; the residual is
the commission denominator, not a disagreement.

In R, per instrument, at a $2.10 ATM premium:

| | friction floor | vs the −0.142R I quoted |
|---|---|---|
| SPY @ 26% stop | **−0.060R** | 2.4x overstated |
| QQQ @ 32% stop | **−0.043R** | 3.3x overstated |

**Commission is not a percentage and must never be stored as one.** $1.30/contract is 0.62% of a $2.10
contract and 0.26% of a $5.00 one. Every per-instrument number above therefore takes premium as an input.

### Verdict

> **My 4.55% / −0.142R is retracted as a hurdle for SPY/QQQ.** It was a statement about PENG and MU.
> The correct floors are −0.060R (SPY) and −0.043R (QQQ). The hurdle is real but **less than half** what I
> told the owner, which makes every "chip must clear −0.142R" conclusion built on it too pessimistic.

### Shipped
`db.friction_pct()` / `db.friction_floor_r()` — per-instrument, premium-aware, provenance-stamped.
Unmeasured instruments fall back to 4.545% **labelled `UNMEASURED_population_median`**, so the wrong
number can never again be quoted as if it were measured.

### Related bug found, NOT fixed — needs your decision
`db.SHADOW_HAIRCUT = 0.10` subtracts a flat 0.10R "for slippage" from **every** provider's shadow R, and
feeds `provider_edge()` → `size_mult` → **real money on tastytrade**. But `_shadow_r` already charges the
real observed bid/ask *and* commission. **This double-counts execution cost**, and the double-count
(0.10R) is *larger than the entire measured SPY ATM friction floor* (0.060R). It is also the last true
global constant in the cost path.

I did **not** change it. Correcting it *raises* every provider's blended R, which can size providers **up**
— the dangerous direction — and I have no measurement of the genuine residual (barrier sampling on ~33-63 s
polls; queue risk on the rungs). Changing a real-money sizing constant on an unmeasured basis is precisely
the failure mode this exercise exists to stop. **Flagging for your call.**

---

## 3. THE HOLD CURVE — **SPY is right on the mechanism; QQQ is right on the decision but for an untested reason**

### What was claimed
- Desk (original, drove `MAX_HOLD_S` 3600→1200): monotone 5 m −0.050R … 60 m −0.219R, "pure theta", 1DTE.
- SPY review: it is a **0DTE artifact**; at 1DTE the curve is flat (−0.046 → −0.059) and shortening *raises*
  the accuracy hurdle 54.9% → 56.9%.
- QQQ review: it is a **measurement of the absence of entry edge**; with a real gate the sign flips
  (20 m beats 5 m by +0.0208 ± 0.0128).

### What is true

`MEASURED` on real quotes, null arm (no gate), day-block SE:

| hold | SPY **0DTE** null | SPY **non-0DTE** null |
|---|---|---|
| 5 m | −0.0898 | −0.0274 |
| 20 m | −0.1720 | **+0.0077** |
| 60 m | −0.4135 | **+0.0822** |

**The SPY review's mechanism is confirmed.** At explicit 0DTE the curve is steeply monotone; at the DTE
chip trades it is **flat, and if anything rises with hold**. The original monotone curve was reported as
1DTE and is not. (QQQ's non-0DTE curve does decay — but QQQ's `dte` field is NULL on 4 of 5 contracts, so
"non-0DTE" there is an unreliable label, and I do not lean on it.)

**The QQQ review's mechanism is arithmetically true but empirically untested.** "Fixed cost, drift
accumulates with hold, so with zero drift shorter always wins" is `THEORY` — and it is correct as
arithmetic. But their claimed *empirical* sign flip rests on **−0.0208 ± 0.0128, z ≈ 1.6**, against a
well-determined null column (z ≈ +6). A z of 1.6 does not establish a sign flip. It is also measured on
`mega`, not on `megabig`, the gate they actually recommend deploying.

**The two mechanisms are not equivalent and the difference matters.** SPY says the slope is set by the
**instrument** (theta, i.e. DTE). QQQ says it is set by **entry quality**. On the evidence the DTE
coupling is large and replicated (0DTE vs non-0DTE differs by ~0.5R at 60 m); the entry-quality coupling
is unmeasured, because chip has no measurable entry edge to couple to.

### Verdict

> **Hold is not a free parameter — but it is coupled to DTE and theta, not to entry quality.**
> At `dte_pref=1` the curve is flat across 20-60 min, so `MAX_HOLD_S` is *nearly* free in that range and
> the cost of a wrong choice is small. **Keep 1200.** Both reviews' "do not shorten" survives; SPY's reason
> is the correct one. Shortening to 5 min is affirmatively bad: it cannot gain much on a flat curve and it
> raises the directional-accuracy hurdle (54.9% → 56.9%).
>
> **Retract** the claim that the monotone hold curve was a 1DTE finding, and the claim that it is "pure
> theta" at the DTE chip trades. It is a 0DTE finding. It is also **the second independent conclusion that
> turned out to be a 0DTE effect in disguise** (see §1) — which is itself the strongest argument in this
> document for `dte_pref = 1`.

I did **not** adopt QQQ's `MAX_HOLD_S` 1200 → 1500. It is justified by a z ≈ 1.6 result on a gate that is
not being deployed, and changing it re-baselines chip's sample for no measured gain.

---

## 4. TIME OF DAY — **all three claims unsupported; the desk cannot decide this**

### What was claimed
- Desk: 09:30-10:00 = **−0.267R**.
- QQQ review: 09:30-10:00 = **−9.55 ± 4.35 bp**, "an independent replication"; wants a hard blackout.
- SPY review: **opposite sign** (+0.018 ± 0.009R, the open is the *best* bucket); calls the desk's −0.267R
  a chip-selection effect.

### What is true

`MEASURED` — **the `path` table contains ZERO ATM ticks before 10:30 ET, on either ticker.** The desk has
never observed its own opening window with a usable two-sided ATM quote. The −0.267R therefore **cannot
be reproduced from desk data**, and no code that produced it survives.

The two reviews do not contradict each other, because **they are not measuring the same thing**:

- SPY measures **side-demeaned null R** on synthetic options. Demeaning by side removes any directional
  effect *by construction*, so a momentum-continuation-specific opening penalty **cannot appear** in that
  table. The SPY review does not acknowledge this.
- QQQ measures **signed drift of an EMA-continuation signal**. Different estimand entirely.

And the QQQ measurement has a decisive defect: its loop is `range(max(21,lo), min(hi, n-20))`, so the
"09:30-10:00" bucket is actually **09:51-09:59 — 9 minutes/session, N ≈ 333** — with EMAs seeded at the
day's first bar and given ~1 time constant to converge. **The blackout is aimed at 09:30-09:50, which was
never measured**, and the signal inside the window that *was* measured is materially a warm-up artifact.

My own TOD buckets on real quotes are uninformative and mutually inconsistent (QQQ 10:30-11:30 = −0.61R on
2 day-blocks), which is the honest answer at this sample size.

### Verdict

> **Neither "genuine per-ticker difference" nor "selection bias" — the question is UNDECIDED and currently
> UNDECIDABLE on desk data.** All three numbers are retracted as decision inputs: the desk's −0.267R
> (unreproducible, no surviving code, no data in the window), QQQ's −9.55 bp (measures 9 minutes, not 30,
> on unwarmed EMAs), and SPY's +0.018R (side-demeaning makes the effect unobservable by construction).
>
> **Do not ship a 09:30-10:00 blackout.** Note that the QQQ review itself concedes the blackout costs
> 0 trades under its own recommended gate — it is "free" precisely because it does nothing.
>
> **Actionable instead:** `path` records nothing before 10:30 because shadows only start when a signal is
> ingested. With the tape gate retired chip can emit from the open, which will populate the window for the
> first time. Revisit after ~20 sessions of actual coverage.

---

## 5. THE POWER LIMIT — stated plainly

`MEASURED` (dispersion 0.55R) / `THEORY` (the standard two-sided 80%-power formula):

| effect to detect | trades | years at 5/day |
|---|---|---|
| +0.03R | 2,639 | **2.09** |
| +0.05R | 950 | 0.75 |
| +0.10R | 238 | 0.19 |
| +0.18R (0DTE→1DTE) | 74 | 0.06 |

Smallest effect visible in **200 trades: +0.109R**. In the entire 12-day `path` pool (~25 independent
contract-blocks): **+0.31R**. Both figures are *optimistic floors* — they assume iid trades, and
overlapping intraday holds are not iid.

**What IS decidable at ~5 signals/day**
- Large mechanical effects. 0DTE vs 1DTE is −0.18R at z = −21: decidable in weeks, and it is the single
  largest lever in this study.
- Anything measurable **paired on identical entries** — exit model, target/stop geometry, hold. Pairing
  cancels the entry set, which is the dominant variance term, and buys 5-20x on the SE. This is why every
  mechanics number in §6 is paired.
- Cost. Friction is measurable off quotes directly, with no trades at all.

**What is NOT decidable**
- Any entry-signal edge below ~+0.10R. That includes every gate anyone has proposed: chip's tape score,
  the mega-cap lead-lag fade (+0.025R point estimate, self-discounted to +0.010R by its own author under
  Bonferroni), and any time-of-day rule.
- Whether chip's selection is good or bad. §1 could not resolve a −0.06R gap and never will at this rate.

**Design consequence, and it is the main recommendation of this document:** stop spending sample on entry
alpha and spend it on mechanics and cost, which are the decidable questions. Retiring the tape gate is
correct for exactly this reason — not because gating is wrong in principle, but because an ungated
sample is worth more than a gate that has never been shown to select anything. Accumulate; do not filter.

---

## 6. IMPLEMENTED — the convergent set, measured on the fixed machinery

All numbers **paired on identical entries**, real stored quotes, day-block bootstrap SE on the difference.
Positive = the shipped config is better.

| change | file | SPY (non-0DTE) | QQQ (non-0DTE) |
|---|---|---|---|
| **full shipped config** (t1 raise + single exit) | — | **+0.0014 ± 0.0011** (z 1.32) | **+0.0184 ± 0.0119** (z 1.55) |
| t1 raise alone (ladder kept) | `chip_desk` | −0.0007 ± 0.0050 | **+0.0304 ± 0.0146** (z 2.09) |
| ladder → single exit alone | `db` | +0.0021 ± 0.0016 | +0.0100 ± 0.0108 |
| ladder → **trailing** exit | — | −0.0032 ± 0.0025 | −0.0200 ± 0.0236 |

Levels, before → after (20 m, real quotes): SPY non-0DTE **+0.0077 → +0.0092**; QQQ non-0DTE
**−0.1660 → −0.1476**.

1. **`MIN_TAPE_MODERATE` retired as a gate.** `chip_desk.TAPE_GATE_ENABLED = False`. The score is still
   computed, still on every verdict, still stamped on every emit — it just no longer decides. One flag
   restores the old behaviour exactly.
   - **Correction to the brief:** retiring the 62 bar *alone* does **not** ~30x the sample — the bar fires
     on ~64% of minutes. The binding constraint was the **trigger** requirement beneath it (a fresh VWAP
     cross, or grade == 'strong'), and `_score_ex_vwap` already documents that the cross and the 62 bar
     are anti-correlated *by construction*. The 30x needs **both** retired. Both are retired.
   - **Deliberately kept:** the EMA135/EMA315 long-lookback trend gate. It is the only directional filter
     on this desk whose day-block CI excludes zero (+0.303R, 95% CI [+0.050, +0.539]). Retiring the
     unproven gates is not a reason to retire the proven one.
2. **Geometry.** SPY `t1` 20→26 (stop stays 26); QQQ `stop` 28→32, `t1` 22→32, `t2` 46→60, `t3` 74→95.
   Direction replicated across three independent measurements; magnitude established only on QQQ.
   - **Note:** the QQQ review's "stop 35 vs 28, z = 13.7" is **not** evidence for widening the stop — that
     row moved `t1` 22→28 simultaneously and double-counts the t1 effect. The QQQ stop widening is shipped
     on **sizing arithmetic** (a 32% stop on a ~$4.8 ATM still buys 1 contract under the $250 cap), not
     as edge.
3. **Exit model: 3-tier ladder → single exit, CHIP-SCOPED.** `db._exit_model()` keys off provider;
   `score_basis` carries `|exit=single`, so chip's rows re-score and **every human provider's history is
   bit-identical**. Verified in sandbox: all seven human providers' shadow-R averages unchanged.
   - **Correction to the SPY review's +0.0135R ± 0.0050.** That arm was coded as `t1=26, t2=9999` — with t2
     unreachable it still takes **half off at t1 and moves the stop to breakeven**. It never removed the
     scale-out; it measured a t1 raise plus rung removal. Replicated on real quotes it is **+0.0021 ±
     0.0016 (SPY)** and **+0.0100 ± 0.0108 (QQQ)** — same sign, ~6-10x smaller, not significant.
   - **Trailing exit rejected**, against the brief's "single *or* trailing": negative on both tickers.
4. **`dte_pref` stays 1.** Endorsed on independent grounds — §1 and §3 both turned out to be 0DTE effects.
5. **`MAX_HOLD_S` stays 1200.** See §3.
6. **`chip_strike.DELTA_MIN` 0.25 → 0.40** (not in the brief; added on my own cost measurement). Round trip
   by delta band on real quotes: SPY 4.25% at |Δ| 0.15-0.25 → 2.43% at 0.25-0.40 → **1.64%** at 0.40-0.60;
   QQQ 5.00% → 1.97% → **1.13%**. Below 0.40 delta chip pays 1.5-2.6x the round trip for the same call.

---

## 7. THE FIXED MEASUREMENT MACHINE — `eval_harness.py`

The recurring failure was not any one error; it was that **every review re-invented its own replay**, and
each re-invention was optimistic in a way nobody caught until the next review disagreed. Worse: **the
replay that produced the original headline was never saved**, so the desk's most-cited finding is
permanently unreproducible. The harness makes the four biases structurally hard to reintroduce.

| bias | structural fix |
|---|---|
| global cost median | `cost_model(ticker, premium)` — per-instrument, premium-aware, provenance-stamped; unmeasured names return `UNMEASURED_population_median` so a wrong number cannot masquerade as measured |
| asymmetric fill marking | `compare_arms()` takes **one** `convention` argument and threads it to both arms. There is no signature by which a caller can price the strategy at the ask and the null at the mid |
| iid SEs on clustered data | `block_bootstrap()` resamples **days or contracts, never ticks**; every summary reports `naive_se`, `se_inflation` and `eff_n` beside the block SE, so the inflation is visible rather than assumed away |
| null built by a different code path | `compare_arms()` builds both arms with the **same** `build_arm` call and the same scorer; the arms differ only by the gate predicate |
| unreproducible one-off scripts | `Arm.provenance` records gate, horizon, convention, exit model, block key and mechanics on every result |
| unstated sample size | `audit_pool()` returns contracts / ticks / **days**; print it at the top of every review |
| unstated power | `trades_needed()` / `detectable_effect()` — compute **first**, not last |

Also added: `compare_configs()`, a paired comparator over identical entries, because config changes must
be paired (§5) and both reviews got that right independently.

**Standing requirement:** future reviews use this harness, or state in writing which of the seven fixes
they are opting out of and why. A review that ships its own replay and does not report `n_days` and
`eff_n` should not be believed, including if I write it.

---

## 8. Explicit list of what I previously told the owner that was WRONG

1. **"chip's entries are worse than random, z = −3.58."** — **RETRACTED.** SE artifact; effective n ≈ 20,
   not 1,458. Inside noise at the DTE chip trades; carried by 0DTE and two sessions.
2. **"The round trip is 4.55% of premium."** — **RETRACTED** for SPY/QQQ. True figure 1.55% (SPY) / 1.37%
   (QQQ). The 4.55% is the median across instruments chip never trades.
3. **"The null expectancy is −0.142R."** — **RETRACTED.** −0.060R (SPY) / −0.043R (QQQ). I overstated the
   hurdle by ~2.4-3.3x, which made everything built on it too pessimistic — including item 1.
4. **"The hold curve is monotone at 1DTE and it is pure theta."** — **RETRACTED.** It is a 0DTE effect.
   At the DTE chip trades the curve is flat to slightly rising. The *decision* (keep 1200) stands.
5. **"09:30-10:00 is −0.267R."** — **RETRACTED as unreproducible.** `path` holds no ATM ticks before 10:30;
   no surviving code produced it.
6. **"chip is shadow-only because it is worse than random."** — **Wrong reason.** It is shadow-only because
   nothing is known either way and the sample rate cannot make it known. Corrected in `chip_paper.py`.

**Not wrong, and worth keeping:** `dte_pref = 1` (now supported by three independent lines), `MAX_HOLD_S
= 1200`, the EMA135/EMA315 trend gate, and the decision to keep `size_mult = 0.0`.

---

## 9. Operational notes

- **Restart required** for `chip_desk.py` / `chip_strike.py` / `db.py` to take effect: the live server on
  `:8787` imports all three at boot. **I did not restart it.** Under the desk restart procedure, kill by
  PID under `.venv` python (`pkill "python3"` misses the `Python server.py` process).
- **On restart**, `db._resync_shadow_r` re-scores chip's shadow rows once onto `askbid_comm_v1|exit=single`.
  Human providers are untouched — verified in sandbox, all seven averages bit-identical.
- **Sample-rate warning:** with the tape gate and the trigger both retired, chip's emit rate rises sharply
  and `COOLDOWN_S = 900` becomes the binding throttle. Expect materially more cards. This is intended —
  sample is the binding constraint — but watch the first session and note that the deployed population is
  no longer the measured population.
- Nothing was committed. No test signal was posted; all verification was by direct function call in a
  `DESK_ENV=test` sandbox on a copied DB. IBKR was not connected.
- `size_mult` for chip remains **0.0**.

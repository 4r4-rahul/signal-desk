# SPY — does it belong in chip's slate? A quantitative adjudication

**Scope:** SPY, decided against QQQ. Parallel SMEs own `eval/qqq_quant.md`, `eval/adjudication.md` and
`chip_leadlag.py`; nothing outside this file was touched. No `.py` file was modified, nothing was
committed, the live server was not restarted, no signal was posted to any ingest.

**Labels:** `MEASURED` = computed here from real data, sample stated. `THEORY` = algebra from measured
inputs. `GUESS` = judgement with no supporting sample.

**Sample.** IBKR 1-min RTH TRADES bars, **SPY and QQQ over the identical 36 sessions**, 2026-05-29 →
2026-07-21 (14,377 / 14,376 minutes). Option paths are Black-Scholes repriced on the variance clock
calibrated to the live 2026-07-22 15:14 ET chain, entry at ask, exit at bid, $1.30/contract, exits by a
verbatim port of `db._shadow_scaleout`. The model contains **zero alpha by construction**. This is the
same engine the QQQ reviewer used; I re-ran their §8 table first and reproduced it to four decimals
(SPY −0.0565 / QQQ −0.0359 at hold 20), so the two reviews are on one measuring stick. Spread quotes and
the spread-vs-premium fit come from 3,009 SPY / 3,379 QQQ real two-sided ticks in `signal_desk.db.path`
plus the 7/22 live chain. All SEs are **session-block bootstrap** (400–2,000 resamples over the 36
sessions), never naive-n.

---

## 0. Verdict first

**Drop SPY from chip's slate.** But the reason matters more than the verdict, because the reason is not SPY.

`MEASURED` SPY is worse than QQQ by **0.0206R ± 0.0019 per trade** at shipped geometry, and it is worse
on **36 of 36 sessions** — this is not a mean pulled by outliers, it is uniform. `MEASURED` **Set the
commission to $0.00/contract and the gap vanishes and inverts: SPY −0.1195 ± 0.0099 vs QQQ −0.1217 ±
0.0084.** The entire SPY-vs-QQQ gap is the fixed $1.30 fee landing on a contract whose stop is $57
instead of $93. SPY's penny spread is worth **+0.006R to SPY**; the fee costs it **−0.0126R**.

So the paradox resolves cleanly, and the answer is the boring one: **the spread is a *proportional* cost
and the commission is an *absolute* one.** SPY is a 0.54×-denomination QQQ — half the premium, 0.61× the
dollar move, 0.54× the dollar spread. Everything scales together except the $1.30, which does not scale
at all. SPY does not have the tightest spread in the options market *in the units that matter*; it has
the same ~0.4–1.0%-of-premium spread as QQQ, quoted on a smaller ticket.

**And the honest headline is that this is a choice between two negative-expectancy instruments.**
Neither ticker is positive under any mechanical configuration I tested. Dropping SPY buys +0.006R to
+0.021R per trade — **$1.50 to $5.00 on a $247 risk unit**. Against a baseline of −0.036R (QQQ, shipped)
it is a rounding correction on a losing strategy, not a fix. Do it because it is free, not because it
changes the outcome.

Four supporting findings, in order of size:

1. `THEORY`+`MEASURED` **The commission drag in R is `$1.30 / (dollar stop per contract)`, and the
   contract price cancels out of it entirely.** Verified numerically to three decimals. This means SPY's
   viability is a function of **dollar movement per contract**, not of contract price, and it kills the
   whole "make the contract more expensive" family of fixes. §2.
2. `MEASURED` **Sizing more contracts does nothing.** The $1.30 is per *contract*, not per ticket —
   `db.py:378` expresses it as a percentage of premium per contract, which is quantity-invariant. Going
   from 1 QQQ to 5 SPY contracts amortises nothing. §2a.
3. `MEASURED` **Deeper ITM and longer DTE both fail**, for two different reasons. Longer DTE leaves the
   commission drag *exactly* unchanged (0.0310R at 1DTE, 0.0310R at 5DTE). Deeper ITM does cut it
   (0.0309 → 0.0172) but the measured SPY dollar spread scales with premium, so the spread drag rises
   faster (0.0575 → 0.1047) and total cost gets **worse**. §2b–c.
4. `MEASURED` **No regime rescues SPY.** Six conditioner families, 17 cells; the best is +0.0114 ± 0.0061
   (high realised vol, z = 1.87, does not survive 17 comparisons). Worse: cutting the sample by vol
   tercile, SPY wins **0/12, 0/12, 0/12** and the gap *widens* with vol (−0.0181 → −0.0226). §4.

And on the item you flagged: `MEASURED` **the 09:30–10:00 window is mechanically the *cheapest* window on
SPY, not the worst** — realised σ₁ₘ is 4.77 bp there against 2.41 bp in the 14:30–15:30 dead zone, which
halves the fixed-cost drag. The SPY reviewer's read is right and the desk's −0.267R does not survive
contact with the data. **No 09:30 blackout on SPY.** §5.

---

## 1. The decomposition

### 1a. Structural inputs — `MEASURED`, 36 identical sessions

| | **SPY** | **QQQ** | SPY / QQQ |
|---|---|---|---|
| median spot | 747.11 | 719.77 | — |
| 1DTE ATM premium | **$2.57** | **$4.79** | 0.536 |
| premium as % of spot | 0.343 % | 0.666 % | 0.516 |
| realised σ₁ₘ | 3.38 bp | 5.76 bp | 0.588 |
| realised σ₂₀ₘ | **1.131 pt** (15.1 bp) | **1.854 pt** (25.8 bp) | 0.610 |
| ATM delta | 0.500 | 0.503 | — |
| **dollar move per contract per 1 σ₂₀** (`σ₂₀·Δ·100`) | **$56.6** | **$93.3** | **0.607** |
| quoted spread, 1DTE ATM | $0.010–0.017 | $0.020 | 0.5–0.85 |
| **spread as % of premium** | **0.39–0.78 %** | **0.42–0.45 %** | **≈ 1.0** |
| **commission as % of premium** | **0.507 %** | **0.271 %** | **1.87** |
| theta over 20 min (% of premium) | −1.06 % | −0.84 % | 1.26 |
| convexity gain over 20 min at ±1σ (% of premium) | +1.19 % | +0.96 % | 1.24 |
| **theta net of convexity** | **+0.13 %** | **+0.12 %** | **wash** |
| realised / priced session variance (median) | 0.961 | 0.875 | — |

Read the two bolded ratio rows together and the paradox is already over. **The premium ratio (0.536), the
dollar-move ratio (0.607) and the dollar-spread ratio (0.5–0.85) are all the same number.** SPY is QQQ
scaled down by roughly half in every dimension simultaneously. A cost that scales with the instrument —
the spread — therefore lands identically on both (0.39–0.78 % vs 0.42–0.45 % of premium). A cost that
does *not* scale — the $1.30 — lands 1.87× harder on SPY.

`MEASURED` Two subsidiary terms that people expect to matter and don't:

- **Theta and convexity net to a wash and are the same wash on both tickers** (+0.13 % vs +0.12 % of
  premium over a 20-minute hold). At 1DTE, decay and gamma cancel during RTH. This independently
  corroborates the prior SPY review's "+0.37 % median real 20-minute premium change." Theta is a 0DTE
  problem, not a SPY-vs-QQQ one.
- **The variance risk premium is real but small and runs the wrong way for the argument.** Realised /
  priced session variance is 0.961 on SPY and 0.875 on QQQ: buying options costs a little in both, and
  slightly *more* on QQQ. It does not explain SPY's deficit.

### 1b. Additive cost decomposition — `MEASURED`

Each ticker at its own shipped geometry (SPY 20/42/68 stop 26, QQQ 22/46/74 stop 28), 1DTE, hold 20,
27,360 null entries each, session-block bootstrap:

| | **SPY** | **QQQ** | **gap (SPY − QQQ)** |
|---|---|---|---|
| frictionless (no spread, no commission) | −0.0215 ± 0.0025 | −0.0110 ± 0.0023 | −0.0105 |
| + spread only | −0.0370 ± 0.0025 | −0.0261 ± 0.0025 | −0.0108 |
| + commission only | −0.0411 ± 0.0026 | −0.0208 ± 0.0024 | −0.0203 |
| **full (shipped)** | **−0.0565 ± 0.0027** | **−0.0359 ± 0.0025** | **−0.0206** |

Attribution of the −0.0206R gap:

| term | contribution | share | structural or artifact? |
|---|---|---|---|
| **spread** | **−0.0003** | **1 %** | neither — it is a **wash** |
| **commission** | **−0.0098** | **48 %** | **artifact of a fixed $1.30 against a cheaper contract** |
| geometry (SPY's ladder mis-scaled to SPY's own noise) | −0.0033 … −0.0060 | 16–29 % | artifact, fixable |
| path / theta residual | −0.0045 … −0.0072 | 22–35 % | structural but small |

The geometry / path split comes from a two-way swap (`MEASURED`, frictionless): SPY run with QQQ's ladder
improves to −0.0155 from −0.0215; QQQ run with SPY's ladder degrades to −0.0143 from −0.0110. On a common
ladder the frictionless gap is −0.0045 (QQQ's ladder) to −0.0072 (SPY's). SPY's shipped 26 % stop is a
*smaller* multiple of SPY's own option noise (1.18 σ₂₀ on the 36-session median premium) than QQQ's 28 %
is of QQQ's (1.44 σ₂₀), so SPY's ladder churns more through the breakeven-stop rung.

### 1c. Neutralise the geometry and the answer sharpens

Re-run with the stop set to a fixed multiple of *each instrument's own* 20-minute σ in **underlying**
terms — the structural fix the prior SPY review recommended in its §2d — with a 1:1 all-out bracket so
there is no ladder to confound. `MEASURED`, hold 20, 1DTE, K_stop = 1.0 σ₂₀, spread model "mid" (~1.0 %
of premium):

| term, in R | **SPY** | **QQQ** | SPY − QQQ |
|---|---|---|---|
| spread drag | 0.0575 | 0.0635 | **−0.0060 → SPY is BETTER** |
| **commission drag** | **0.0309** | **0.0183** | **+0.0126 → SPY is worse** |
| theta/convexity + path residual | 0.0598 | 0.0553 | +0.0044 |
| **E[R]** | **−0.1550 ± 0.0121** | **−0.1426 ± 0.0096** | **−0.0124** |

`MEASURED` **SPY's penny-wide quote is a genuine, measurable advantage — worth +0.0060R — and the
commission overwhelms it two to one.** That is the entire answer to "why does the tightest spread in the
market measure worse."

### 1d. The clincher — the commission counterfactual

`MEASURED`, same configuration, only the per-contract fee changed:

| fee / contract | SPY | QQQ | gap |
|---|---|---|---|
| **$1.30 (tastytrade today)** | −0.1550 ± 0.0121 | −0.1426 ± 0.0096 | **−0.0124** |
| $0.65 | −0.1372 ± 0.0110 | −0.1321 ± 0.0090 | −0.0051 |
| $0.30 | −0.1277 ± 0.0104 | −0.1265 ± 0.0087 | −0.0012 |
| **$0.00** | **−0.1195 ± 0.0099** | **−0.1217 ± 0.0084** | **+0.0022 → SPY ahead** |

**The gap is the fee. All of it.** At zero commission SPY is marginally the better instrument, consistent
with its slightly tighter percentage spread. The QQQ reviewer's conclusion — *"SPY is the marginal
instrument at this account size"* — is **correct as stated and correct for the reason they gave**, and I
would sharpen it: it is not about account size at all. It is about the **fee schedule**. The same $25k
account at $0.30/contract would have no reason to prefer QQQ.

---

## 2. The contract-price test

### 2a. `$1.30` is per contract, not per ticket — so sizing more contracts amortises nothing

`MEASURED` from the code, not from a replay. `db.py:378`:

```python
comm = (RT_COST_PER_CT / (prem * 100.0) * 100.0) if prem > 0 else 0.0
```

The fee enters as a percentage of premium **per contract**. Quantity cancels: at a fixed dollar risk `D`,
`qty = D / stop$_per_ct`, total fee `= qty · 1.30`, so `fee / D = 1.30 / stop$_per_ct` — independent of
`qty`. Buying 5 SPY contracts where you could buy 2 QQQ does **not** spread the fee thinner; it buys five
fees. The premise in the brief is wrong, and it is wrong in the code, not just in the model.

### 2b. `THEORY` — the invariant, and why contract price cannot be the lever

Under a stop set to `K` σ over the hold in underlying terms, `stop_pct = 100·K·σ_h·|Δ| / P`. Then

```
commission drag in R  =  (100 · 1.30/(100P)) / stop_pct  =  1.30 / (100 · K · σ_h · |Δ|)
                      =  $1.30 / (dollar stop per contract)
```

**The premium `P` cancels.** Raising the contract price raises the R-denominator in exactly the same
proportion. `MEASURED` verification, to three decimals: SPY stop $42.3/ct → measured drag 0.0309
(1.30/42.3 = 0.0307); QQQ stop $71.4/ct → 0.0183 (1.30/71.4 = 0.0182); SPY at 1.8 σ, stop $76.2/ct →
0.0171 (1.30/76.2 = 0.0171).

So the only quantity that matters is **dollars of underlying movement captured per contract**, which is
`σ_h · |Δ| · 100`. SPY delivers **$56.6** per 1 σ₂₀ against QQQ's **$93.3**. To equalise the fee drag SPY
must reach $93.3 — meaning Δ ≈ 0.82, or a 1.65 σ stop, or a 54-minute hold, or some product of the three.
Each has a price, and the next two sections pay it.

The same algebra, stated for the spread: `spread drag in R = spread_$ / (dollar stop per contract)`. If
the dollar spread is pinned (a true penny quote), ITM helps. If the dollar spread scales with premium,
it does not. Which one SPY is, is an empirical question, and it decides §2c.

### 2c. It scales. `MEASURED` — SPY's dollar spread grows with premium

Real two-sided ticks from `path` (n = 3,009 SPY), bucket medians:

| premium bucket | median premium | median spread $ | **spread %** |
|---|---|---|---|
| $0.4–1.0 | $0.73 | $0.010 | 1.37 % |
| $1.0–1.6 | $1.28 | $0.010 | 0.89 % |
| $1.6–2.4 | $2.01 | $0.020 | 0.92 % |
| $2.4–3.5 | $2.71 | $0.030 | 1.07 % |
| $3.5–5.0 | $4.18 | $0.040 | 1.02 % |
| $5.0–8.0 | $5.64 | $0.040 | 0.73 % |

The prior SPY review's 1DTE-by-delta table says the same thing more cleanly: $0.011 @ $1.13, $0.017 @
$2.17, **$0.043 @ $4.13** — a fitted ≈ 1.07 % of premium, flat. The 7/22 live chain agrees at the money
(SPY 748C 1DTE: $0.01 on $2.41 = 0.41 %) and agrees going in (745C 1DTE Δ 0.70: **$0.03 on $4.44 =
0.68 %**).

`MEASURED` **The penny is an at-the-money phenomenon. SPY's percentage spread is flat-to-rising as you go
in the money; it does not decay with contract price.** Because of that, the ITM lever raises the
denominator without shrinking the spread numerator proportionally, and it loses.

### 2d. The test itself — `MEASURED`

1DTE, hold 20, 1:1 all-out, σ-normalised stop at K = 1.0, spread model "mid" (≈ 1.0 % of premium, fitted
to the table above). `qty` is the integer contracts a $247 risk unit buys.

| config | n | E[R] | ± | premium | Δ | stop $/ct | qty | spread R | **comm R** | cost R |
|---|---|---|---|---|---|---|---|---|---|---|
| **SPY 1DTE ATM** | 8,424 | **−0.1550** | 0.0121 | $2.55 | 0.500 | 42.3 | 5 | 0.0575 | **0.0309** | 0.0884 |
| SPY 1DTE ITM 2 | 8,424 | −0.1525 | 0.0107 | $3.69 | 0.622 | 52.8 | 4 | 0.0665 | 0.0247 | 0.0913 |
| SPY 1DTE ITM 4 | 8,424 | −0.1539 | 0.0092 | $5.07 | 0.735 | 62.3 | 3 | 0.0772 | 0.0210 | 0.0982 |
| SPY 1DTE ITM 6 | 8,424 | −0.1569 | 0.0081 | $6.65 | 0.828 | 70.1 | 3 | 0.0899 | 0.0186 | 0.1085 |
| SPY 1DTE ITM 8 | 8,424 | −0.1590 | 0.0067 | $8.38 | 0.897 | 75.9 | 3 | 0.1047 | **0.0172** | 0.1219 |
| QQQ 1DTE ATM | 8,424 | −0.1426 | 0.0096 | $4.77 | 0.500 | 71.4 | 3 | 0.0635 | 0.0183 | 0.0818 |

**Deeper ITM is a wash-to-loss.** The commission drag does fall almost twofold (0.0309 → 0.0172, exactly
as the `1/(σ_h·Δ)` invariant predicts, since Δ goes 0.50 → 0.90). It is fully eaten by the spread drag
rising 0.0575 → 0.1047. Net cost gets *worse* and E[R] is flat within noise (−0.1550 vs −0.1590).

**Longer DTE is a strict no-op on the term that matters.** `MEASURED`, SPY ATM at K = 1.0: 1DTE commission
0.512 % of premium against a 16.5 % stop = **0.0310R**; 5DTE 0.251 % against an 8.1 % stop = **0.0310R**.
Identical to four decimals, exactly as the algebra requires. Longer DTE dilutes theta a little and buys
nothing on the fee.

**The one lever that works is a bigger dollar stop per contract** — and it works on both tickers equally,
so it does not change the ranking:

| | SPY E[R] | SPY cost R | QQQ E[R] | QQQ cost R | gap |
|---|---|---|---|---|---|
| K = 0.8 σ₂₀ | −0.2070 ± 0.0131 | 0.1105 | −0.1871 ± 0.0104 | 0.1022 | −0.0199 |
| K = 1.0 | −0.1550 ± 0.0121 | 0.0884 | −0.1426 ± 0.0096 | 0.0818 | −0.0124 |
| K = 1.4 | −0.0993 ± 0.0090 | 0.0631 | −0.0904 ± 0.0074 | 0.0584 | −0.0089 |
| K = 1.8 | −0.0717 ± 0.0070 | 0.0491 | −0.0653 ± 0.0059 | 0.0454 | −0.0064 |
| K = 2.4 | −0.0512 ± 0.0055 | 0.0368 | −0.0466 ± 0.0045 | 0.0341 | −0.0046 |
| hold 90 min (K = 1.0) | −0.0866 ± 0.0131 | 0.0417 | −0.0876 ± 0.0118 | 0.0385 | +0.0010 |

The prior SPY review warned that a wider stop is "a leverage dial, not an expectancy fix." That is right
for the spread — which is proportional to exposure and cancels — and **wrong for the commission**, which
is the one cost that is *not* proportional to exposure. Widening the stop at fixed dollar risk buys fewer
contracts, hence fewer $1.30s against the same risk, and that is a real reduction in drag, not an
illusion. It is nonetheless not alpha: it converges the strategy toward "hold longer, bet less often,"
i.e. away from scalping, and it does not make either ticker positive.

### 2e. The one thing SPY genuinely wins on, and why it doesn't save it

`MEASURED`, $25k at 1 % = $247 risk unit:

| | premium | stop $/ct | qty | **risk deployed** | fee | spread | total friction | E[$]/trade |
|---|---|---|---|---|---|---|---|---|
| SPY, 1.0 σ₂₀ stop | $2.55 | 42.3 | 5 | $212 (86 %) | $6.50 | $12.11 | $18.61 = 8.79 % of 1R | **−$32.78** |
| QQQ, 1.0 σ₂₀ stop | $4.77 | 71.4 | 3 | $214 (87 %) | $3.90 | $13.58 | $17.48 = 8.17 % of 1R | −$30.54 |
| SPY, 1.8 σ₂₀ stop | $2.55 | 76.2 | 3 | **$228 (92 %)** | $3.90 | $7.26 | $11.16 = 4.89 % of 1R | −$16.39 |
| QQQ, 1.8 σ₂₀ stop | $4.77 | 128.5 | **1** | **$128 (52 %)** | $1.30 | $4.53 | $5.83 = 4.54 % of 1R | −$8.39 |

`MEASURED` **SPY's real advantage at this account size is granularity, not spread.** At a wide stop QQQ
can only afford one contract and strands 48 % of the risk budget; SPY deploys 92 %. That is worth having —
**but only if expectancy is positive.** At negative expectancy, deploying more of your risk budget is
strictly worse, which is why QQQ's worse granularity shows up as a *better* dollar line (−$8.39 vs
−$16.39). This is the one term that would flip sign the day chip demonstrates edge, and it is the only
honest argument for keeping SPY on the bench rather than deleting it.

---

## 3. Break-even directional accuracy

`MEASURED` — oracle injected into the replay: the side is correct with probability `p`, wrong otherwise,
where "correct" is the sign of the underlying's move over the hold. 1DTE ATM, 1:1 all-out, hold 20,
spread model "mid":

| config | p = 0.50 | 0.55 | 0.60 | 0.65 | **break-even p** |
|---|---|---|---|---|---|
| **SPY, 1.0 σ₂₀ stop** | −0.1502 | −0.0756 | −0.0169 | +0.0469 | **0.613** |
| **SPY, 1.8 σ₂₀ stop** | −0.0653 | −0.0193 | +0.0191 | +0.0594 | **0.575** |
| QQQ, 1.0 σ₂₀ stop | −0.1201 | −0.0529 | +0.0058 | +0.0667 | 0.595 |
| QQQ, 1.8 σ₂₀ stop | −0.0564 | −0.0124 | +0.0255 | +0.0650 | 0.566 |

**SPY asks for ~1.0–1.8 pp more directional accuracy than QQQ at every stop width.** These hurdles are
higher than the prior SPY review's 54.9 % because that used the optimistic penny-floor spread model; under
the fitted ~1.0 % spread the hurdle is 57.5–61.3 %. `GUESS` The truth is somewhere between — the penny is
real at the money and the 1.0 % fit is contaminated by 0DTE and fast-tape ticks. What is **not**
model-sensitive is the SPY-minus-QQQ difference, which is ~1 pp in every variant.

---

## 4. Regime search — and its multiple-comparisons bill

**I tried six conditioner families, 17 usable cells, plus a six-bucket time-of-day cut on each ticker.**
All numbers are side-demeaned (SPY drifted over the sample; raw puts beat raw calls, and that is realised
direction, not edge) with session-block bootstrap SEs. Null = −0.0565 ± 0.0027.

| family | cell | n | R | vs null | z |
|---|---|---|---|---|---|
| **1. session realised vol** | low tercile | 9,120 | −0.0652 | −0.0087 ± 0.0031 | **−2.81** |
| | mid | 9,120 | −0.0592 | −0.0027 ± 0.0044 | −0.62 |
| | **high tercile** | 9,120 | **−0.0451** | **+0.0114 ± 0.0061** | **+1.87** |
| **2. trend day** (\|C−O\|/range) | chop | 9,120 | −0.0481 | +0.0084 ± 0.0058 | +1.44 |
| | mid | 9,120 | −0.0584 | −0.0019 ± 0.0049 | −0.39 |
| | trend | 9,120 | −0.0630 | −0.0065 ± 0.0042 | −1.55 |
| **3. overnight gap** (event proxy) | small | 8,360 | −0.0684 | −0.0119 ± 0.0042 | **−2.82** |
| | mid | 9,120 | −0.0562 | +0.0003 ± 0.0041 | +0.08 |
| | large | 9,120 | −0.0456 | +0.0108 ± 0.0056 | +1.95 |
| **4. SPY vol relative to QQQ vol** | terciles collapsed; only the mid cell had usable n | 9,120 | −0.0564 | +0.0001 ± 0.0041 | +0.03 |
| **5. IV/RV** | RV rich vs IV | 9,120 | −0.0489 | +0.0076 ± 0.0071 | +1.07 |
| | mid | 9,120 | −0.0563 | +0.0002 ± 0.0039 | +0.05 |
| | IV rich vs RV | 9,120 | −0.0643 | −0.0078 ± 0.0031 | **−2.52** |
| **6. time of day** | 09:30–10:00 | 2,160 | −0.0353 | **+0.0211 ± 0.0105** | **+2.02** |
| | 10:00–11:00 | 4,320 | −0.0664 | −0.0099 ± 0.0062 | −1.61 |
| | 11:00–13:00 | 8,640 | −0.0530 | +0.0034 ± 0.0047 | +0.74 |
| | 13:00–14:30 | 6,480 | −0.0589 | −0.0024 ± 0.0040 | −0.59 |
| | 14:30–15:30 | 4,320 | −0.0570 | −0.0005 ± 0.0040 | −0.12 |
| | 15:30–16:00 | 1,440 | −0.0670 | −0.0105 ± 0.0110 | −0.96 |

**Multiple-comparisons bill, stated honestly.** 17 cells searched for a positive SPY regime. Bonferroni at
α = 0.05 needs |z| > 2.9; the largest |z| anywhere is 2.82 and the largest *positive* z is 2.02. **Nothing
survives.** Under 17 roughly-independent tests the expected maximum |z| by chance is ≈ 2.5, and I observed
2.82 — indistinguishable from noise. I also ran, and am reporting rather than burying: 5 ITM offsets, 4
DTE variants, 5 stop widths and 4 hold lengths per ticker (§2d) — those are mechanical sweeps with
pre-stated algebraic predictions, not hypothesis searches, and I have not counted them in the bill.

**What I would still give weight to, and why.** The two significant cells are both *negative* and both
mechanically predicted: a dead tape (low-vol tercile, small-gap tercile) cannot pay a fixed fee, and an
IV-rich session charges you for variance you do not receive. `MEASURED` The low-vol result also
independently replicates the prior SPY review's dead-tape finding (−0.013R ± 0.004, z = −3.0) on a
different sample and a different geometry. A replicated, mechanistically-explained, correctly-signed
negative is worth more than a fresh unreplicated positive. **There is no positive regime here.**

### 4a. Per-session distribution — the finding that ends the regime question

`MEASURED`, shipped geometry, paired within session (36 sessions):

| | p10 | median | p90 | mean | sessions positive |
|---|---|---|---|---|---|
| SPY | −0.072 | −0.060 | −0.041 | **−0.0565** | **1 / 36** |
| QQQ | −0.051 | −0.038 | −0.022 | −0.0359 | 1 / 36 |
| **paired SPY − QQQ** | — | — | — | **−0.0206 ± 0.0019 (t = −10.7)** | **SPY better on 0 / 36** |

Best single SPY session in the whole sample: **+0.0013R** — and QQQ made +0.0235R that same day.

And the vol conditional, which is where a rescue would have to come from if it existed anywhere:

| vol tercile | SPY | QQQ | paired diff | SPY wins |
|---|---|---|---|---|
| low | −0.0652 | −0.0471 | −0.0181 ± 0.0017 | **0 / 12** |
| mid | −0.0592 | −0.0381 | −0.0211 ± 0.0022 | **0 / 12** |
| **high** | −0.0451 | −0.0224 | **−0.0226 ± 0.0052** | **0 / 12** |

`MEASURED` **High realised vol helps SPY (+0.020R from low to high tercile) and helps QQQ more (+0.025R),
so the gap widens.** The intuition that a volatile tape should favour the tighter-spread instrument is
exactly backwards here: vol dilutes a fixed dollar fee in proportion to the *dollar* move, and QQQ's
dollar move is 1.65× SPY's, so QQQ collects 1.65× the benefit. **There is no vol regime, no trend regime
and no event-day regime in which SPY is the better instrument in this sample.**

**Caveat with teeth.** All of §4 is one 36-session, two-month regime (2026-05-29 → 07-21) with a single
realised drift. A regime that did not occur in this window cannot be ruled out by it. What *is*
regime-independent is §2b: the algebra says SPY's fee drag is 1.65× QQQ's whenever SPY's dollar
volatility is 0.61× QQQ's, and that ratio is a stable property of the two indices, not of this sample.
`GUESS` I would expect the ranking to hold in any regime where QQQ remains the higher-beta index — i.e.
until mega-cap tech stops leading, at which point this whole analysis should be re-run rather than
extrapolated.

---

## 5. The 09:30–10:00 adjudication, for SPY

Three numbers were in conflict: the desk's **−0.267R**, the SPY review's **+0.018 ± 0.009R** (opposite
sign, called a chip-selection effect), and the QQQ review's **−9.55 bp ± 4.35** with a proposed blackout.

**They are not measuring the same thing, and only one of them has a sample.**

**(i) `MEASURED` — the mechanism. SPY's open is the cheapest window it has.** Realised σ₁ₘ by window, and
the fixed-fee drag that follows from it via §2b:

| window | SPY σ₁ₘ | **SPY $ stop / contract** | **SPY commission drag** | QQQ σ₁ₘ | QQQ commission drag |
|---|---|---|---|---|---|
| **09:30–10:00** | **4.77 bp** | **$79.7** | **0.0163 R** | 9.25 bp | 0.0087 R |
| 10:00–11:00 | 4.19 | $70.0 | 0.0186 | 7.36 | 0.0110 |
| 11:00–13:00 | 3.01 | $50.3 | 0.0258 | 5.05 | 0.0160 |
| 13:00–14:30 | 2.62 | $43.8 | 0.0297 | 4.11 | 0.0196 |
| **14:30–15:30** | **2.41** | **$40.2** | **0.0323** | 4.03 | 0.0200 |
| 15:30–16:00 | 3.30 | $55.3 | 0.0235 | 4.91 | 0.0164 |

SPY moves **1.98× as much per minute at the open as in the 14:30–15:30 dead zone**, which doubles the
dollar stop per contract and **halves the commission drag** (0.0163R vs 0.0323R). Mechanically, 09:30–10:00
is where SPY's structural disadvantage is smallest. Any claim that the open is SPY's worst window has to
overcome a 0.016R headwind before it starts.

**(ii) `MEASURED` — the null replay confirms it.** Side-demeaned, session-block bootstrapped, 2,160
entries: 09:30–10:00 = **−0.0353 vs the −0.0565 null, i.e. +0.0211 ± 0.0105 (z = +2.02)** — the **best of
six windows**, and the only one with a positive z above 1. This **independently reproduces the prior SPY
review's +0.018 ± 0.009** on a different sample (36 vs 44 sessions), a different geometry and a different
spread model. Two independent replications with a mechanical explanation.

**(iii) `MEASURED` — the desk's −0.267R has no sample behind it.** The desk's entire live SPY record at
09:30–10:00 is **n = 6**, mean **−0.069R**, **SE 0.563R**, values `[−0.75, −0.85, −1.43, +1.85, +1.86,
−1.10]`, contributed by **optionking ×4, jmt ×1, sulker ×1 — no chip trades at all.** The standard error
is **8× the point estimate**; this sample cannot distinguish −0.267R from +0.267R. And decisively:
**chip has emitted exactly one SPY signal in its entire history** (SPY 745P, 2026-07-20 **14:48** ET,
`channels/chip/paper_journal.csv`), with `provider_edge.chip` reading `shadow_n: 1`. **Chip has never
traded a SPY open.** A number describing what chip does at the open, on an instrument chip has traded once
at 14:48, cannot exist.

**(iv) The QQQ reviewer's −9.55 bp ± 4.35 does not transfer.** It is a QQQ measurement of a different
quantity — underlying-move edge in basis points, not replay expectancy in R. My QQQ null over the same
window is **+0.0068 ± 0.0091R**, i.e. also *not* a penalty in R terms. Whatever the bp finding is
measuring on QQQ, it is not the mechanical cost of the window, and I would not port a blackout across
tickers on it.

> ### `ADJUDICATED` — no 09:30–10:00 blackout on SPY.
> The window is SPY's **cheapest** by a factor of two in fixed-fee drag, it measures as SPY's **best** of
> six windows in the null (+0.0211 ± 0.0105, replicating a prior independent measurement), and the
> contrary evidence is six human-provider trades with an SE eight times its own size and zero chip
> participation. **The desk's −0.267R should be retired as a decision input.** If the desk wants an
> opening-window rule on SPY it should be the *opposite* one, and even that is only +0.021R ± 0.011 — one
> cell out of 17 searched, so I am **not** recommending it either. Ship neither rule.

---

## 6. Is this decidable from chip's live data? No, and that is the point

`THEORY` from the measured per-trade dispersion of ~0.55R. Trades needed to resolve an effect at 80 %
power, 5 % two-sided:

| effect to resolve | trades needed | at 5 signals/day, all tickers | **SPY-only at chip's ~1-in-8 SPY rate** |
|---|---|---|---|
| +0.010R | 23,716 | 18.8 yr | 157 yr |
| **+0.020R** (the SPY→QQQ switch) | **5,929** | **4.7 yr** | **39 yr** |
| +0.030R | 2,635 | 2.1 yr | 17 yr |
| +0.050R | 949 | 0.8 yr | 6.3 yr |

`MEASURED` chip's live SPY record is **n = 1**. The desk's total SPY record across all providers is
**n = 24, mean −0.196R, SE 0.300R** — which cannot reject zero, let alone resolve a 0.02R instrument
preference.

**Stated plainly: the SPY-vs-QQQ question is undecidable from chip's live output and will remain so for
decades at ~5 signals/day.** It has to be decided mechanically — on the cost algebra in §2b and the
36-session replay in §1 — or not at all. Anyone proposing to "run SPY a bit longer and see" is proposing
a 39-year experiment. Conversely, **do not treat the drop as reversible on evidence**; it will have to be
reversed on a change in inputs (the fee schedule), not on a change in results.

---

## 7. Verdict, with the error bar

> ### **DROP SPY from chip's slate. Keep it in the pricer, not in the emitter.**
>
> `MEASURED` SPY costs **0.0206R ± 0.0019 per trade** more than QQQ at shipped geometry and loses on
> **36 of 36 sessions** (paired t = −10.7). Neutralise the geometry and it is **0.0124R ± 0.0061**;
> across the full spread-model band (§7a) the penalty is **0.0050R to 0.0153R**, always the same sign,
> and SPY wins at most **16 of 36 sessions** in the most SPY-favourable variant tested.
>
> **Expected value of the change: +0.006R to +0.021R per trade, i.e. $1.50–$5.00 on a $247 risk unit.**
>
> **This does not make chip positive.** Every configuration of both instruments measured negative. The
> honest framing is that this is a **choice between two negative-expectancy instruments**, and QQQ is the
> less negative one by about one third. Best measured SPY: −0.035R (prior review, optimistic spread) to
> −0.051R (this review at a 2.4 σ stop). Best measured QQQ: −0.026R to −0.047R. Break-even directional
> accuracy: **SPY 57.5–61.3 %, QQQ 56.6–59.5 %.** Nothing in this review closes either gap to zero.

### 7a. Error bar — sensitivity of the verdict to the one weak input

The spread model is the only materially uncertain input. Three calibrations: `opt` = live-chain ATM penny
floor, `mid` = fitted 1DTE-by-delta ≈ 0.95 %, `cons` = `path` bucket medians ≈ 1.35 %.

| spread model | stop | SPY | QQQ | **paired gap** | SPY wins |
|---|---|---|---|---|---|
| opt | 1.0 σ | −0.1202 | −0.1049 | −0.0153 ± 0.0048 | 13/36 |
| opt | 1.8 σ | −0.0531 | −0.0445 | −0.0086 ± 0.0023 | 12/36 |
| mid | 1.0 σ | −0.1550 | −0.1426 | −0.0123 ± 0.0061 | 13/36 |
| mid | 1.8 σ | −0.0717 | −0.0653 | −0.0064 ± 0.0024 | 12/36 |
| cons | 1.0 σ | −0.1813 | −0.1718 | −0.0095 ± 0.0070 | 16/36 |
| cons | 1.8 σ | −0.0866 | −0.0816 | −0.0050 ± 0.0026 | 16/36 |

The absolute levels move a lot with the spread assumption; **the sign of the gap never does**, and the
gap narrows monotonically as you assume a wider spread — which is intuitive, since a wider proportional
spread dilutes the share of total cost that the fixed fee represents. **Worst case for the drop
recommendation: the gain is +0.0050R ± 0.0026 rather than +0.0206R.** Still positive, still one-signed,
but at that end of the band the change is worth ~$1.20/trade and is a housekeeping decision, not a
strategy decision.

### 7b. The three conditions that reverse this

1. **`MEASURED` — commission at or below $0.30/contract.** At $0.30 the gap is −0.0012 ± 0.009 (dead
   heat); at $0.00 SPY is **ahead** by +0.0022. This is the real lever, and it is a *broker* decision, not
   a *ticker* decision. If a route to sub-$0.30 options commissions exists it is worth strictly more than
   every mechanical finding in this review or the two before it combined — it is worth ~0.012R on SPY and
   ~0.007R on QQQ, on every trade, forever.
2. **`MEASURED` — chip demonstrates positive expectancy.** SPY's granularity advantage (deploys 92 % of a
   $247 risk unit at a wide stop against QQQ's 52 %) is worth nothing at negative expectancy and becomes
   the deciding term at positive expectancy. Keep SPY on the bench for that day; it is currently n = 1
   away from having ever been tested.
3. **`GUESS` — a regime where SPY's dollar volatility approaches QQQ's.** The entire result is driven by
   σ_SPY/σ_QQQ ≈ 0.61. If that ratio moves toward 1.0 the fee penalty moves toward 1.0 with it. Re-run
   §1a rather than extrapolating; it is a ten-minute check.

### 7c. What *not* to do with this file

- Do **not** read it as "SPY is illiquid" or "SPY's spread is a problem." `MEASURED` SPY's spread is the
  better of the two, worth +0.0060R, and it is the only term in the whole decomposition on which SPY wins.
- Do **not** implement a 09:30–10:00 SPY blackout. §5. The evidence points the other way, and the
  other-way evidence is one cell out of 17 and should not be shipped either.
- Do **not** attempt to rescue SPY with deeper ITM, longer DTE or bigger contract counts. §2. All three
  are ruled out by algebra that was then confirmed numerically, not by a noisy backtest.
- Do **not** expect the drop to show up in the equity curve. +0.006–0.021R per trade at ~0.6 SPY signals
  per day is ~$1–3 per day of expected improvement, against a per-trade dispersion of 0.55R ≈ $135.
  It will be invisible for years (§6). Take it because the algebra is certain, not because you will see
  it.

---

## 8. Method appendix

| | |
|---|---|
| bars | IBKR 1-min RTH TRADES, SPY + QQQ, **identical 36 sessions** 2026-05-29 → 2026-07-21 (cached pull; a fresh 90/60/45-day pull on `client_id 41` was attempted and returned IBKR error 162 pacing timeouts on the long durations, so the cached identical-window sample was used rather than two mismatched windows) |
| option pricing | Black-Scholes on the variance clock calibrated to the 2026-07-22 15:14 ET live chain; reproduces observed ATM 1DTE mid to −0.4 % |
| spread | three fitted models (§7a) anchored on 3,009 SPY / 3,379 QQQ real two-sided `path` ticks and the live chain |
| costs | entry at ask, exit at bid, $1.30/contract, matching `db.RT_COST_PER_CT` and the `askbid_comm_v1` basis |
| exits | verbatim port of `db._shadow_scaleout`, plus an all-out 1:1 bracket and a stop-only variant |
| SEs | session-block bootstrap, 400–2,000 resamples over the 36 sessions, on the paired difference where a paired comparison exists |
| drift | every conditional number in §4 is side-demeaned |
| hypothesis cells searched | **17** regime cells + 6 time-of-day windows per ticker; mechanical sweeps (moneyness, DTE, stop width, hold) had pre-stated algebraic predictions and are reported in full |
| safety | read-only throughout; `client_id 41` (in the 40–45 band); no ingest POST; no server restart; no `.py` modified; nothing committed |

Scratch scripts (not part of the repo):
`s1_struct.py` structural inputs · `s2_decomp.py` cost/geometry decomposition · `s3_norm.py` σ-normalised
runner · `s4_price.py` premium ladder · `s5_vrp.py` variance risk premium · `s6_engine2.py` pluggable
spread runner · `s7_terms.py` term isolation + paired sessions · `s8_regime.py` regime cuts ·
`s9_tod.py` time-of-day cost structure · `s10_final.py` oracle + commission counterfactual + power ·
`s11_sens.py` spread sensitivity.

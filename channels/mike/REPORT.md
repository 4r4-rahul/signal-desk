# SPX Mike (mike-scalps) — Complete Analysis & Execution Plan

**Channel:** Trend Analytics → `mike-scalps`, trader **SPX Mike**
**Data:** 19,677 messages, **2025-02-09 → 2026-06-20 (~16 months)**
**Documented trades (from his recaps):** ~1,900, winners *and* losers
**Instrument:** SPX + large-cap options, mostly 0–1 DTE scalps & EOD lottos
**Analysis date:** 2026-06-21

---

## 1. Headline

Mike posts near-daily recaps logging **every** trade with entry→exit and %, plus his own
win-rate tallies — so unlike JMT this is a **real, honest-looking track record**. Validated
against his own posted numbers (our parse ~83% win vs his daily 57–100%, mostly 70–85%).

**But his quoted edge is NOT your edge.** Three filters stand between his recap and your
account: (1) self-reporting, (2) fill slippage on cheap options, (3) you can't take every
trade. The plan below is built on the *discounted, realistic* edge — not his quotes.

## 2. The documented record (his quotes)

| Metric | Value |
|---|---|
| Trades | ~1,900 over 131 active days (median **13 trades/day**, max 54) |
| Win rate | **~83%** (his own: 57–100%, mostly 70–85%) |
| Median return | **+61%/trade** |
| Mean return | +110% (fat-tail skewed) |
| Avg winner / loser | +141% / −45% |
| Distribution | p10 −26% · p25 +19% · p50 +61% · p75 +141% · p90 +293% |
| Longest losing streak | **6 in a row** |

**Segments (win rate is ~83% almost everywhere — selection doesn't help):**
- Tickers: SPX (n=1159), SPY (93% win), plus TSLA/NVDA/MSFT/META/SMCI/NBIS.
- Premium: cheap ≤$0.50 options show the *best quotes* (86% win, +100% median) — **but that's
  exactly where real slippage is worst.** A trap, not an edge.

## 3. The reality check (why his numbers can't be yours)

**Compounding proof:** at his quoted returns, $1 → ~**69× in 200 trades**. Impossible. So the
quotes embed selection bias + unrealistic fills.

**Slippage sensitivity — your edge as your real fills worsen:**
```
 slippage   exp/trade   win%    PF
     0%       +110%      83%   17.3   <- his quotes (fantasy)
     5%        +87%      78%   11.8
    10%        +66%      71%    7.7
    15%        +48%      61%    4.8
    20%        +31%      50%    2.9   <- realistic for cheap 0DTE
```
On cheap (≤$0.50) options slippage is ~2× worse — **that's where the edge evaporates.**
Realistic takeaway: **positive expectancy is plausible IF you execute on liquid contracts
with tight fills — but it's a fraction of his quotes, and unproven for you until forward-tested.**

## 3b. Lotto vs Normal — two different strategies

```
                  n     win   median   →$0    moonshot   avgW    avgL
NORMAL          1839    84%    +63%     2%       16%     +138%   -44%
LOTTO             59    66%    +43%    12%       19%     +199%   -59%
  ├ EOD lotto     29    72%    +67%    17%       31%     +303%   -83%
  └ intraday      30    60%    +18%     7%        7%      +78%   -44%
```
- **Normal = high-probability scalp** (84% win, only 2% total losses). Your core.
- **Lotto = lottery ticket** (12–17% go to **$0**, ~6–8× the normal rate), paid for by rare
  +200–400% EOD moonshots. **Intraday lottos are the weakest bucket of all (+18% median).**
- Same arithmetic mean (~+45% after slippage), but the lotto's zero-rate makes it far more
  dangerous to compound → **size lottos much smaller.**

## 3c. Data integrity / cross-checks (READ THIS)

We stress-tested the analysis against Mike's own numbers and for bias:

1. **Parser is accurate** ✅ — my win-rate 79.1% vs his stated 76.4% (median diff +0.1 pts),
   trade counts match. I'm reading his record faithfully, not inventing edge.
2. **Edge is stable** ✅ — across all 16 months, win rate 72–100%, median +47–108%, **no losing
   month** in the recaps. Not a one-period fluke.
3. **🚩 DAY-COVERAGE BIAS (biggest caveat)** — he traded on **315 days** but recapped only
   **156 (44%)**. **56% of trading days are invisible to us.** If he skips bad days, the real
   win rate is **overstated, possibly a lot.** Everything here is conditional on days he published.
4. **⚠️ Trim overstatement** — headline % is the *best* trim (`1.5/2.4/3.6 → +300%`), but pieces
   sold lower, so blended position returns are below the headline median +61%.

**Net:** the documented edge is real and consistent, but two upward biases (unrecapped days +
peak-trim reporting) mean **your realistic edge is materially lower than the quotes.** The
forward tracker is the only way to measure the unbiased truth.

## 4. Position sizing

- **Full Kelly = 93% of bankroll (a fat-tail artifact of fantasy returns). Ignore it entirely.**
- Use **fixed fractional sizing: ~1% of account per trade** (hard cap 2%).
- Historical worst losing streak was 6; at 1%/trade × −45% avg loss, even **10 straight losers
  ≈ −4.5%** — survivable. That's the point of small, equal bets.
- Cap **total open risk at ~5%** and set a **daily stop** (see plan).

---

## 5. EXECUTION PLAN

### A. What to trade (selection)
1. **Don't try to pick "the good ones."** ML proved entry features can't predict winners
   (AUC ~0.55, no model beats baseline). Either follow the system or don't.
2. **Trade liquid names only:** SPX, SPY, mega-cap tech (NVDA, TSLA, MSFT, META, AVGO).
   Premium sweet spot **$0.50–$2.00** (enough movement, tolerable spread).
3. **Cap your count: 3–5 best setups/day**, not all 13–54. Fewer trades, better fills > spraying.

### A2. Normal vs Lotto (size to the risk, not the average)
4. **Normal liquid scalps = core. ~1% per trade.** 84% win, 2% total losses — the consistent edge.
5. **Lottos = ¼-size house-money flyers. ~0.25–0.5%, only after you're green, cap 1–2/day.**
   12–17% go to **zero** — expect to lose the whole premium often; the payoff is rare moonshots.
6. **If you take lottos, EOD lottos only** (31% moonshot). **Skip intraday lottos** (weakest bucket).
7. **Skip cheap ≤$0.50 options** in both buckets — widest spreads, worst real fills.

### B. Entry
5. **Limit orders only**, at or within ~5–10% of Mike's quoted entry. Never market-buy options.
6. If you can't fill within ~10% of his price in ~30–60s, **SKIP IT.** Chasing kills the edge.
7. Act fast (these are scalps) but only on contracts with tight bid/ask.

### C. Exit (your original problem — solved by rule)
8. **Scale out, don't hold for the moon.** Median winner is +60%, not +293%.
9. Concrete: **sell ⅔ at +30–50%**, move stop to **breakeven** on the rest, **trail the runner**.
10. **Hard stop at −40% to −50%** (his avg loser is −45%). No averaging down on losers.
11. **Time stop:** intraday scalps don't get held into the close hoping — exit by your rule or EOD.

### D. Risk controls
12. **1% risk/trade, max 2%. Max ~5% total open.**
13. **Daily loss limit:** stop trading for the day after **−3% account** or **4 consecutive losers**.
14. After a red day, **size down 50%** the next day until you're green again.

### E. Prove it before scaling (mandatory)
15. **Paper-trade or trade minimum size for 2–4 weeks**, logging YOUR fills vs Mike's quotes.
16. Compute YOUR realized expectancy after real slippage. **Only scale up if it's clearly positive.**
17. Use the forward tracker (to be built) to automate this logging.

---

## 6. Bottom line
Mike is a genuinely strong, well-documented scalper **on paper**, and his edge is internally
consistent across 16 months. **But two upward biases — 56% of trading days unrecapped, and
headline %s being peak-trims — mean the real edge is materially below the quotes.** The *size*
of your edge depends almost entirely on execution: liquid contracts, tight limit entries,
disciplined scale-out exits, and small fixed sizing (normal 1%, lotto ¼-size house-money).
**Do not trade his quoted +110%. Trade a conservatively-discounted version, separate your
normal and lotto buckets, and let your own forward-tracked fills tell you the truth before
you size up.**

## 7. Reproduce
```
python3 load.py mike "channels/mike/messages.json"
python3 parse_recaps.py mike            # realized results from recaps
.venv/bin/python mike_ml.py             # ML: outcomes not predictable
.venv/bin/python mike_analysis.py       # segments, drawdown, Kelly/MC, slippage
```

# JMT (Royal Trading Academy — `jmoney-callouts`) — Edge Analysis

**Channel:** Royal Trading Academy → `jmoney-callouts`, trader **JMT™(CGBG)**
**Data captured:** 5,820 messages, **2022-06-27 → 2026-06-18** (~4 years)
**Instrument:** SPY short-dated options (0–5 DTE), median premium ~$1.61
**Analysis date:** 2026-06-21
**Status:** First-pass, free daily-data reconstruction. Directional, not precise. (See caveats.)

---

## 1. The headline finding: the public record is a highlight reel

The channel posts entries well but barely documents outcomes — and the few it documents
are cherry-picked winners:

| Evidence | Count |
|---|---|
| Unique entry signals | 784 |
| Clean priced exits (`entry>exit`) | 21 |
| …of which **wins / losses** | **21 / 0 (100% win)** 🚩 |
| Win-flavored words ("profit","banger","runner") | 423 |
| Loss-flavored words ("stopped out","took a loss") | 8 |

A 100% documented win rate is statistically impossible in real options trading. **You
cannot compute honest expectancy from what the channel publishes.** This selection bias is
likely a core reason the channel *looks* far more profitable than copying it actually is.

## 2. What the chat reliably tells us (trade profile)

- **Frequency:** ~3.8 signals/week (~15/month). 784 total.
- **Direction:** 54% calls / 46% puts (trades both ways).
- **Cost:** median premium $1.61 (~$161/contract); range $0.54–$8.50.
- **Horizon:** ~98% are 0–2 day trades (fast scalps); only 2% swings.
- **Timing:** clustered at the open (9–11am ET) and power hour (3pm ET); evenly across weekdays.
- **His own exit doctrine (410 management messages):** *"scale out, take profits ~30–40%,
  leave a small runner, don't be greedy."*
- **Winner brags cluster at +30% median** (middle-half +15% to +62%; only ~11% reach +100%).

## 3. Unbiased reconstruction (704 of 708 trades modeled)

Method: back out implied vol from each entry premium, reprice the option along SPY's daily
path, simulate exit rules. Where target and stop are both reachable in one day, daily data
can't say which came first → results shown as **pessimistic..optimistic** range.

```
EXIT RULE                 n            EXPECTANCY   WIN%   AVG W   AVG L     PF
-------------------------------------------------------------------------------
Hold to expiry          704               -14.1%     26    218%     94%   0.80
TP +10% / stop -50%     704        -13.5..+7.2  %     95     10%     50%   4.07
TP +15% / stop -50%     704        -11.0..+10.2 %     93     15%     51%   3.77
TP +20% / stop -50%     704         -8.4..+12.8 %     90     20%     51%   3.46
TP +25% / stop -50%     704         -6.1..+15.4 %     87     25%     51%   3.42
TP +30% / stop -50%     704         -4.5..+17.7 %     85     30%     51%   3.29
TP +40% / stop -50%     704         -1.7..+21.6 %     80     40%     51%   3.10
TP +50% / stop -50%     704         -1.8..+21.0 %     71     50%     51%   2.44
TP +75% / stop -50%     704         -6.3..+20.7 %     57     75%     51%   1.94
TP +100% / stop -50%    704         -6.2..+17.0 %     45    100%     51%   1.61
Scale 1/2@+30,run+75    704         -5.4..+24.7 %     66     52%     29%   3.52
```
*Expectancy = % of premium risked, per trade. Win%/AvgW/AvgL/PF = optimistic case.*

**Breakdown @ TP+40%/stop-50% (pessimistic..optimistic):**
```
  Calls               n= 378    -2.0..+22.7 %   win 81%
  Puts                n= 326    -1.3..+20.2 %   win 78%
  Morning (<12 ET)    n= 380    +1.5..+22.3 %   win 81%   <- positive even worst-case
  Power hour (>=14)   n= 216    -8.1..+13.9 %   win 71%   <- much worse
  0-1 DTE             n= 262    -4.5..+26.8 %   win 85%   <- higher reward & risk
  2+ DTE              n= 442    -0.0..+18.5 %   win 76%   <- steadier
```

## 4. Conclusions

1. **Holding to expiry loses (−14%/trade, true win rate only 26%).** These options decay
   fast; left alone, ~3 of 4 expire near worthless. Likely a core driver of past losses.
2. **Early profit-taking flips it positive.** Every TP rule beats holding.
3. **The edge is real but fragile and execution-dependent.** The pessimistic..optimistic
   band is wide because free data is daily; which end you land near depends on YOUR exit
   discipline (did you bank the target before the stop hit?).
4. **Best profiles:** scale-out (½ at +30%, runner to +75%, PF 3.5) and fixed TP +40%
   (PF 3.1). Both match the trader's own doctrine.
5. **Favor morning signals; be cautious on power-hour entries.** Calls ≈ Puts.

## 5. Provisional trading plan (data-grounded, not financial advice)

1. **Risk a fixed ~1% of account per trade.** Contracts ≈ (account × 1%) ÷ (premium × $50)
   assuming a −50% cut.
2. **Always set the stop** (channel SL, or −50% on premium). Capping losers is what makes
   the math work.
3. **Scale out: bank ~half at +30%, trail the rest toward +75%.**
4. **Favor morning entries; size down / skip power-hour.**
5. **Never hold to expiry hoping for the moon.**

## 6. Caveats (trust this the right amount)

This is a **directional first pass**. It assumes flat implied volatility, uses a daily-price
proxy for the entry spot, ignores fills/slippage/commissions, and **cannot resolve intraday
order** (hence the wide band). The reconstructed median MFE (+114%) is higher than the
trader's bragged +30%, confirming the optimistic end is too rosy — **the truth sits toward
the middle/lower part of each range.**

**To get a precise answer:** (a) buy ~1 month of intraday options data (~$30) to collapse
the band, and/or (b) run the forward tracker to prove the live edge on real trades.

## 7. How to reproduce
```
python3 load.py jmt channels/jmt/messages.json   # raw messages -> jmt.db
python3 parse_trades.py jmt                       # -> trades table (708 trades)
python3 reconstruct.py jmt                        # -> simulation tables above
```

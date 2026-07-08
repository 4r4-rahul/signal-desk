# $15k Account — Operating System (data-derived, all providers)

Built from deep analysis of 6 providers + SME question framework. Prediction is a dead end
(proven); the edge is in PROVIDER SELECTION + EXECUTION + RISK, which this encodes.

## The 7 rules

1. **Diversify across Sulker (primary) + Mike (diversifier).** They're −0.10 correlated;
   a 50/50 blend cuts daily volatility ~32% (104% → 70%) for the same return. The one free lunch.
2. **Tilt to the concentrated edge:** IWM (89% win) & SPX core (82%), calls > puts, non-lotto,
   liquid $0.50–$2.00 contracts. Skip cheap ≤$0.50 (slippage) and lottos (PF 7).
3. **Exit = scale + TRAIL (not a fixed target).** Edge is outlier-driven (top 10% of trades =
   46% of profit). Bank ⅔ in the +25–50% zone (57–92% of trades reach it), TRAIL the last ⅓ to
   keep fat-tail winners. A tight fixed target clips the trades that pay for everything.
4. **Size small & fixed: 1% risk/trade** (~3–6 contracts on $0.50–$1.00 options). Kelly says 92%
   but that's a fat-tail/self-report artifact — ignore it.
5. **Daily stop is sacred: −3% of account OR 4 losses in a row → done for the day.** Losses
   cluster at 2× independence (correlated 0DTE), so a red day is far worse than per-trade math
   implies (worst historical day −78% equal-weight). The daily stop keeps you alive.
6. **Monitor edge decay monthly** (`edge_ops.py` Q8). Both stable now (Sulker improving). Pull
   capital from any provider whose rolling win rate drops >6 pts below its history.
7. **Validate with the tracker.** Log real fills vs quotes (`tracker.py`); scale size up only if
   YOUR post-slippage expectancy is clearly positive.

## Key numbers (documented, self-reported — treat as optimistic ceilings)
- Pooled win ~81%, median +57%/trade, PF ~17 (Mike+Sulker, 2322 trades).
- Best segments: IWM 89%/PF37, SPX 82%, NVDA +100% median (small n).
- Reaches +20%: 92% of trades · +30%: 77% · +50%: 57% · +75%: 18%.
- Mike/Sulker daily correlation −0.10; blend daily std 70% vs ~104% solo.
- Edge stable: Mike 83%→79%, Sulker 77%→89% (recent).

## What does NOT work (proven, so you don't waste money on it)
- Predicting which trade wins: forward AUC ~0.55 (coin-flip). Neural net overfits worst.
- Trusting JMT / Prince / Prince-$500 / Option King: highlight reels (hide losers).
- Trading their quoted returns: real edge is far lower after slippage + unrecapped days.

## Reproduce
```
python3 cross_provider.py       # scorecard, segment edges, exit-target curve
.venv/bin/python edge_ops.py    # daily risk, outlier concentration, Kelly, correlation, decay
.venv/bin/python deep_predict.py# proof that per-trade prediction has no reliable edge
```

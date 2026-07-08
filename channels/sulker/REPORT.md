# Sulker (Trend Analytics — sulker-options) — Report

**Trader:** Sulker (Spain-based, gamma/flow-driven scalper)
**Data:** 9,139 messages, **2025-03-05 → 2026-07-01 (~16 months)**
**Documented trades (recaps):** ~560, winners AND losers, with entry / realized % / contract-high (MFE)
**Instruments:** Liquid ETFs & index — **SPY, QQQ, IWM, SPX** (+ some NVDA/TSLA/AAPL)
**Analysis date:** 2026-07-01
**Verdict: the most trustworthy + most executable provider of the six. PRIMARY.**

## Why Sulker ranks #1 for a follower
1. **Most transparent** — posts recaps with realized % AND "contract high" (MFE); documents
   losses (−100% zero-heros, −55% days). Only **28% of trading days unrecapped** (Mike: 56%).
2. **Most executable** — trades **liquid ETF options (SPY/QQQ/IWM/SPX)** with tight spreads, so
   your fills land close to his quotes (unlike Mike's cheap stock lottos).
3. **Realistic exits** — captures only ~30% of each trade's peak (takes profits early), so his
   quoted +65% median is *bankable*, not an unreachable top.
4. **Educational** — explains the gamma/flow reasoning, so you can follow with conviction.

## Numbers (documented; self-reported → optimistic ceiling)
- Win rate ~**80%**, median **+65%/trade**, avg winner +161% / avg loser −46%, PF ~17.
- Best segments: **IWM 89% win / PF 37**, SPX core, calls > puts.
- **Exit map (from MFE):** reaches +20% (92% of trades) · +30% (77%) · +50% (57%) · +75% (18%).

## Execution notes
- Trade the liquid ETFs he specializes in — that's the follower advantage.
- **Scale out ⅔ in the +25–50% zone, trail the last ⅓** (edge is outlier-driven; keep the tail).
- 1% risk/trade, daily stop −3% or 4 losses. Diversify with Mike (−0.10 correlated).

## Caveats (same discipline as any self-report)
- Self-reported; 28% of days unrecapped → true edge below quotes.
- Headline %s can be peak trims; realized-% parse is approximate (MFE sample thin, ~88).
- **Prove your real edge with the tracker before scaling.**

## Reproduce
```
python3 load.py sulker channels/sulker/messages.json
python3 sulker_recaps.py            # realized % + MFE / exit-discipline
python3 tracker.py sulker sync      # forward-test live
```

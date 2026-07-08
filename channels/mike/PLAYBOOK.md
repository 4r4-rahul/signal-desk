# Mike Signals — My Playbook ($15k account, 1% risk)

## Position size: contracts = 15,000 ÷ (4,500 × premium)
| Premium | Contracts | $ outlay | $ risked | Scale? |
|---|---|---|---|---|
| $0.50 | 6 | $300 | $135 | yes |
| $0.75 | 4 | $300 | $135 | yes |
| $1.00 | 3 | $300 | $135 | yes |
| $1.50 | 2 | $300 | $135 | partial |
| $2.00 | 1 | $200 | $90 | no (full exit) |

Sweet spot: **$0.50–$1.00 options → 3–6 contracts** (best for scaling). Liquid names only
(SPX, SPY, NVDA, TSLA, MSFT, META, AVGO).

## Entry
- Limit order within ~10% of Mike's price. No fill in ~60s → **skip** (log it).

## Stop
- Hard **−45% of premium** (or Mike's underlying stop if given). **No averaging down.**

## Exits
**3+ contracts:** sell **⅔ at +35–40%** → move stop on rest to **breakeven** → **trail runner**
(exit if it drops ~25% from peak, else **hard exit by EOD**).
**2 contracts:** sell 1 at +40%, trail the other (BE stop).
**1 contract:** **full out at +40–50%** (can't scale a 1-lot).

Example — SPX 7500C @ $1.00 × 3 contracts (risk $135):
stop $0.55 · at $1.40 (+40%) sell 2 · last contract stop→$1.00 (BE) · trail · close by EOD.

## Lottos (now allowed, tiny)
- 0.4% = $60 budget, max loss = full premium → **1–2 contracts** on $0.30–$0.50 lottos.
- **EOD only, after you're green, 1–2/day.** Accept full loss, never add.

## Daily guardrails
- Max 3–5 trades/day.
- **Daily stop: down $450 (3%) or 4 losses → done for the day.**

## Log every trade
```
python3 tracker.py mike sync
python3 tracker.py mike take <handle> <fill>
python3 tracker.py mike close <handle> <exit>   (or --pct N)
python3 tracker.py mike skip <handle>
python3 tracker.py mike score
```
After 2–4 weeks run `score`; scale risk to 1.5–2% ONLY if your real (post-slippage) edge is clearly positive.

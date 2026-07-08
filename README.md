# Copy-Trading Edge Analysis

Measure the *real* edge of Discord options-signal providers (not their hype), find how to
execute them profitably, and forward-test on your own fills. Built for a $15k account.

## Start here
1. **[ACCOUNT_PLAN.md](ACCOUNT_PLAN.md)** — the $15k operating system (the 7 rules). READ FIRST.
2. **[COMPARISON.md](COMPARISON.md)** — 6 providers ranked honestly (only 2 are trustworthy).
3. **channels/mike/PLAYBOOK.md** — exact contracts / stops / exits for your account size.

## The verdict (tl;dr)
- **Only Sulker & Mike document their losses** → the only two worth following. Diversify across
  both (they're −0.10 correlated → ~32% less drawdown).
- **JMT, Prince (×2), Option King** = highlight reels (hide losers). Avoid.
- **You cannot predict which trade wins** (proven: forward AUC ~0.55, neural net overfits). The
  edge is provider-selection + execution + risk, not prediction.

## How it works
```
extension/            Browser scraper (per-channel, crash-safe). Load unpacked in Chrome.
load.py <ch>          Scraped JSON -> channels/<ch>/<ch>.db
parse_recaps.py mike  Realized results from recaps (Mike)
sulker_recaps.py      Realized results + contract-high/MFE (Sulker)
cross_provider.py     Scorecard, segment edges, exit-target curve (all providers)
edge_ops.py           Daily risk, outlier concentration, Kelly, correlation, edge-decay
mike_ml.py            ML proof: outcomes not predictable
deep_predict.py       Deep NN + vectors + regime; shows the leakage trap; honest forward test
tracker.py <ch>       FORWARD TRACKER — log live alerts + YOUR fills -> real edge
```

## Providers (channels/<name>/)
`sulker` ✅ primary · `mike` ✅ diversifier · `jmt` ✗ · `prince` ✗ · `prince_sac` ✗ · `optionking` ⚠️

## Daily forward-test workflow (the point of all this)
```
# refresh data: scrape channel briefly, then:
python3 load.py sulker channels/sulker/messages.json
python3 tracker.py sulker sync            # pull new live alerts
python3 tracker.py sulker list            # see open (🎰 = lotto)
python3 tracker.py sulker take <id> 1.05  # your real entry fill
python3 tracker.py sulker close <id> 1.55 # your exit  (or --pct 45)
python3 tracker.py sulker score           # YOUR edge vs his quotes
```
Run 2–4 weeks. Scale size up only if your post-slippage edge is clearly positive.

## Environment
Python 3.14; ML deps in `.venv` (numpy, scikit-learn). Market data cached in `data/`
(SPY & VIX daily via Yahoo). Run ML scripts with `.venv/bin/python`.

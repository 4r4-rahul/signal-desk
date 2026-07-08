# Provider Comparison — 6 channels, honest ranking

Analysis date: 2026-07-01. Trustworthy P&L sources = only the two that document losses
(Mike, Sulker). The other four are highlight reels used for structure/metadata only.

## Scorecard

| Provider | Msgs | Entries | Win:Loss msgs | Documented outcomes | Instruments | Verdict |
|---|---|---|---|---|---|---|
| **Sulker** | 5,318 | 1,102 | 232:13 | ✅ 562 (+ contract-high/MFE) | Liquid ETFs (SPY/QQQ/IWM/SPX) | ✅ **Primary** |
| **Mike** | 13,264 | 3,831 | 187:84 | ✅ 1,760 | SPX + stocks + cheap lottos | ✅ **Diversifier** |
| JMT | 2,701 | 850 | 331:9 | ❌ highlight reel | SPY | Avoid |
| Prince callouts | 2,920 | 425 | 719:1 | ❌ highlight reel | Stocks | Avoid |
| Prince $500 challenge | 1,290 | 51 | 418:1 | ❌ hype vs $500→$1k/6mo | Stocks | Avoid (cautionary tale) |
| Option King | 3,165 | 1,287 | 231:0* | ⚠️ conversational, parseable | SPX + stocks | Medium — deeper look optional |

\*Option King's loss language differs from our detector; he's less biased than the 231:0 implies
(earlier read ~3.5:1). Middle tier: no clean recaps, but structured entry/exit prices.

## The core lesson
**Only 2 of 6 document their losses.** The flashiest channels (Prince's "+267%!", JMT's 100%
"wins") are the *least* honest — proven by Prince's public $500 challenge reaching only ~$1,000
in 6 months despite constant huge-% callouts. **Hype-% ≠ account growth.**

## Deep-analysis findings (pooled Mike+Sulker, 2,322 documented trades)
- Win ~81%, median +57%/trade, PF ~17 (self-reported → optimistic ceiling).
- **Edge concentrates:** IWM 89% win / PF 37, SPX 82%, calls > puts, lottos worse (PF 7).
- **Outlier-driven:** top 10% of trades = 46% of profit → **trail runners, don't cap winners.**
- **Losses cluster 2×** (correlated 0DTE) → worst day −78%; **daily stop is essential.**
- **Mike & Sulker are −0.10 correlated** → 50/50 blend cuts daily vol ~32% (**diversify!**).
- **Prediction is a dead end:** honest forward AUC ~0.55; a neural net overfit worst; a text
  model's "0.71" was data leakage (reading outcome words). No reliable trade-picker exists.
- **Edges stable:** Mike 83%→79%, Sulker 77%→89% (recent). No decay.

## Recommendation
**Diversify across Sulker (primary) + Mike (uncorrelated diversifier).** Execute with the
$15k operating system in **[ACCOUNT_PLAN.md](ACCOUNT_PLAN.md)**. Drop JMT & Prince (both).
Forward-test with the tracker before scaling size.

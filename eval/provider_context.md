# Per-Provider Context — dialect, numbers, linking, exits

**Status:** domain spec, owned by the provider-language SME. Companion to `provider_profiles.json` (v`2026-07-23.1`).
**Builds on:** `eval/message_taxonomy.md` (19-type taxonomy) and `eval/routing_corpus.jsonl` (366 labelled messages).
This document does **not** redefine message types or the resolution algorithm — it supplies only what is
provider-*specific*. Where the taxonomy already states a rule, this file adds evidence or a per-provider
exception, never a duplicate.

**Scope:** `prince`, `prince_small`, `optionking`, `jmt`. `mike` and `sulker` were eliminated from scope.
`chip` is synthetic and needs no dialect model. `?`/`unknown` is not a provider.

**Every rule is labelled** MEASURED (counted in that provider's own history, n stated) / INFERRED
(consistent with the evidence, n too small to be sure) / GUESS (plausible, no direct evidence).

---

## 0. Sample sizes — how much weight each profile deserves

Counted from `logs/inbound.log` `RECV` lines, 2026-07-06 → 2026-07-23, deduplicated on normalised text
(whitespace-, case- and @mention-insensitive).

| provider | RECV | distinct | repost rate | active days | first → last | verdict |
|---|---|---|---|---|---|---|
| optionking | 165 | 78 | **53%** | 13 | 07-06 → 07-22 | entries strong (68 contract msgs); exits n=3, P&L n=6 |
| mike | 133 | 109 | 18% | 4 | 07-07 → 07-10 | *out of scope* |
| prince | 120 | 100 | 17% | 14 | 07-06 → 07-23 | **strong — profile all categories** |
| sulker | 119 | 76 | 36% | 5 | 07-06 → 07-10 | *out of scope* |
| jmt | 106 | 78 | 26% | 12 | 07-06 → **07-21** | numbers/entries strongest in corpus; **exits thin (n=5)** |
| prince_small | 62 | 59 | **5%** | 12 | 07-07 → 07-23 | moderate; exits n=4 |
| ? / unknown | 33 | 32 | — | 2 | — | not a provider — drop before parsing |

**On including `jmt`.** The brief said to include it only if the sample genuinely supports a profile.
It does, for the two things that matter most: it has the **largest clean entry sample in the corpus**
(15 fully-specified template entries) and the **most measurable number convention** (`SL:` denomination,
n=15, 100% consistent, with a directional proof). Since one of the named failures is precisely
*"jmt's `SL:748.90` vs prince's `s/l 1.29` — the same keyword meaning things 500x apart"*, omitting jmt
would leave that failure unaddressed. Its **exit** vocabulary is thin (5 messages, 4 phrases) and is
marked as an admitted gap rather than padded out.

### Method and its limits

Sources read: `logs/inbound.log` (1,465 parsed lines → 764 `RECV`), `channels/*/live_signals.jsonl`
(206 records with `raw`), `channels/*/closed_trades.jsonl`, `signal_desk.db`, and the two prior SME
artifacts. Everything ran read-only against a copy of the DB in a scratchpad; nothing was posted, no
`.py` was touched, no server restarted.

> **Truncation ceiling — read this before trusting any rule about message tails.**
> `logs/inbound.log` truncates text at **~120 characters**. `channels/*/live_signals.jsonl` `raw` fields
> truncate at **~200**. `eval/routing_corpus.jsonl` also tops out at 200. **No source in this repo holds
> a full-length provider message.** Every rule below about how a message *starts* is well grounded;
> rules about what appears late in a long message are correspondingly weaker, and I have flagged the
> places where this bites.

A second limitation: the first 60 `RECV` lines carry a `HH:MM:SS` timestamp with **no date**. They are
included in counts and quoted with `[?]` for the date, but excluded from every gap measurement.

---

## 1. EXIT LANGUAGE — the highest-priority section

An unrecognised exit leaves a dead position live on the board. This is worse than a missed entry: a
missed entry costs opportunity, a missed exit costs real money on a position the desk believes it still
holds. Today's failure was exactly this — prince's `OUT of $GOOGL calls` was `REJECT`ed and the position
stayed open.

### 1.1 The finding that reframes the whole problem: providers mostly *don't* post exits

I traced every identifiable position per provider from entry to last mention.

| provider | identifiable entries | explicit exits | **exit rate** | conf |
|---|---|---|---|---|
| prince | ~15 | 5 | **33%** | MEASURED |
| prince_small | ~9 | 4 | **44%** | INFERRED (small n) |
| optionking | ~68 | 3 | **4%** | MEASURED |
| jmt | 15 | 5 | ~33% | INFERRED (thin) |

prince positions that were opened, ran, and **never got an exit message**: AMD 73C, PLTR 134C (last seen
+213%), HIMS 36C (+45%), TSLA 405C (+388%), MSFT 395C (+107%), GOOGL 385C, QQQ 705P, QQQ 705C (+40%),
CELH 33C.

**Consequence (INFERRED, but the inference is forced):** perfect exit-phrase recognition would still
leave two-thirds of prince's board and *nineteen-twentieths* of optionking's board un-closed. Exit
parsing is necessary and not sufficient. The desk needs a **staleness rule** alongside it —
expiry-based auto-close, plus a flag on any position with no message referencing it for more than one
trading day. I own the language model, not the position model, so I state the requirement and leave the
mechanism to the integration.

### 1.2 prince — full exits

Five explicit exits in 14 days. All quoted verbatim with dates:

```
2026-07-23 16:16  OUT of $GOOGL calls @everyone absolute horrible drop        <- MISSED TODAY
2026-07-20 22:04  Cutting $NVDA @everyone this market is wild. Was a good start but its just been knifing for an hour
2026-07-17 16:00  Profits locked im out @everyone that was a grind but job done for me!!!    (no ticker)
2026-07-15 15:34  OUT OF AAPL @everyone
2026-07-06 18:08  Cut Out at 0.80 per contract @everyone gotta take that profit the moment you see it
```

Phrase set (MEASURED, n=5): `OUT of` · `OUT OF` · `im out` · `Cut Out at <px>` · `Cutting $<TICKER>`.

Two structural properties, both MEASURED:

* **Case is unstable.** `OUT of` (07-23), `OUT OF` (07-15), `im out` (07-17) — same author, same week.
  Matching must be case-insensitive. A case-sensitive `OUT OF` pattern would have caught 07-15 and
  missed 07-23, which is exactly the observed failure mode.
* **40% of his exits name no ticker at all** (2 of 5: `Cut Out at 0.80 per contract`, `Profits locked
  im out`). These are resolvable only against an open book with exactly one candidate.

Not exits (MEASURED — each of these contains an exit-ish token and must not close anything):

```
2026-07-08 13:38  Probably cutting half the SOFI calls today          <- hedged INTENTION, no fill
2026-07-16 13:45  First 15 has closed.                               <- the 15-minute candle
2026-07-06 16:18  We tapped the $134 price target and came right back down   <- commentary
```

**Exit *prices* from prince are unreliable even when the exit *fact* is not.** `Cut Out at 0.80 per
contract` follows an entry at 1.10, yet the same message claims *"Was up 35%"*. 0.80 is 27% *below* 1.10.
Either the price or the claim is wrong and the text cannot say which. UNSURE — recommend closing the
position on his exit but marking the exit price `provider_stated, unverified` and preferring a market
mark for P&L.

### 1.3 prince_small — full exits, and the buried-exit trap

```
2026-07-10 15:43  Sold NVDA here at 84% @everyone
2026-07-08 13:35  Sold GOOGL calls as .31 @everyone will see how the next hour plays out
2026-07-14 17:06  Cutting AAPL, didn't know NVDA was moving like that woulda been out otherwise.
2026-07-08 16:55  Up 83% on AAPL calls @everyone price target has been HITselling here. Moving onto the next trade!! Very clean trade
```

Phrase set (MEASURED, n=4): `Sold <TICKER>` · `Sold <TICKER> calls as <px>` · `Cutting <TICKER>` ·
`selling here`.

**The buried-exit trap (MEASURED, severity high).** The 07-08 message *opens* `Up 83% on AAPL calls`.
Classified on its head it is a `PNL_UPDATE` — and a `PNL_UPDATE` never closes anything. But
`selling here` + `Moving onto the next trade` make it a **full EXIT**. The exit verb is in the middle of
a brag. **Scan the whole message for exit verbs, never just the opening clause.** Note also the missing
space in `HITselling` — tokenising on whitespace alone loses the verb entirely; tokenise on case
transitions too. This is the single most likely place a prince_small exit goes missing, and it is
aggravated by the 120-char log truncation: on a longer message the exit verb may fall *past the cut*.

Note the idiolect `as` where he means `at` (`Sold GOOGL calls as .31`). MEASURED, n=1 — flagged so a
price is still extracted, not asserted as a general rule.

Not exits (MEASURED):

```
2026-07-10 16:23  I sold too early
2026-07-10 16:24  I sold too early. NVDA blowing
```
Regret commentary posted 40 min *after* the real exit was already recorded at 15:43. Treating these as
exits double-closes a position that is already flat.

### 1.4 optionking — the 4% problem

Three exit messages against ~68 entries:

```
2026-07-20 19:26  SPY 500% up guys.. sold all
2026-07-09 16:09  Sold all SPY with 150%..
2026-07-08 13:50  Sold all NVDA
```

Partial exits: `Left runner 2 cons now..` (07-20), `secure some profit guys` (07-09).

His only complete observed lifecycle, which is worth memorising because it exercises four message types
in 189 minutes (all MEASURED, from `logs/inbound.log` lines 1280–1297 and following):

```
16:17  @everyone SPY 745P at 0.50 lotto 0DTE      ENTRY
17:20 / 18:40 / 18:42   (same text re-scraped)    REPOST x3 — position still OPEN, not re-entries
18:45  SPY 2.45 now.. 380% up guys..              PNL_UPDATE  (0.50 x 4.9 = 2.45 — the arithmetic checks out)
18:45  Left runner 2 cons now..                   TRIM
19:26  SPY 500% up guys.. sold all                EXIT
```

**Staleness is not optional for optionking (MEASURED, critical).** With a 4% exit rate his exit
messages cannot close his book. What saves it is expiry: **42 of his 68 contract-bearing messages are
tagged `0DTE`/`ODTE`/`EOD`/`1DTE`**, so expiry-based auto-close covers most of the board. The residue is
the dangerous part — **8 `weekly expiry` positions with no close signal of any kind**. Those are the
ones that will rot.

### 1.5 jmt — thin, and honestly so

```
2026-07-15 18:30  Stopped out here @JMoneyAlerts got to greedy
2026-07-15 18:30  Stopped out @JMoneyAlerts
2026-07-15 18:30  Selling my runners here at 40% gonna find an re entry @JMoneyAlerts
2026-07-08 16:11  Stopped out on runners @JMoneyAlerts
```

Every observed voluntary close is a **stop-out or a runner sale**. `all out`, `flat`, `done` have never
been seen from him. **GAP ADMITTED:** 4 phrases from 5 messages does not close a vocabulary. Any
unrecognised jmt message containing `out`/`sold`/`closed`/`flat`/`done` near a percentage should be
escalated to the operator, not dropped.

Two jmt messages that look like exits and are not (MEASURED, severity high):

```
2026-07-15 16:15  Down 28% if hourly closes below my SL I'm out    <- CONDITIONAL. Contains "I'm out". He is still in.
2026-07-14 13:37  Down 20% and close below SL but I'm gonna hold till tomorrow   <- he OVERRIDES his own stop
```

The second one has a consequence beyond parsing: **jmt's stated `SL:` is a reference level, not a
commitment.** He states a stop and then holds through it. The desk must not auto-close a jmt position
because the stated SL printed.

### 1.6 Cross-provider exit summary

| | full-exit phrases | partial | anaphoric rate |
|---|---|---|---|
| prince | OUT of/OUT OF, im out, Cut Out at, Cutting $X | (intentions only) | 2/5 name no ticker |
| prince_small | Sold X, Sold X calls as, Cutting X, selling here | — | low; usually names the ticker |
| optionking | Sold all X, sold all | Left runner N cons, secure some profit | 3/3 name the ticker |
| jmt | Stopped out (+here/on runners), Selling my runners | taking some off, leaving runners | all bare — safe, he holds one position |

The union is small enough to state plainly: **out · sold · sell/selling · cut/cutting · stopped ·
done · flat · all out · closed**, matched case-insensitively, anywhere in the message — then filtered
by the per-provider "not an exit" lists above (conditionals, intentions, regret, candle-closes).
Recall matters more than precision here: a false exit prompts a question, a missed exit holds a dead
position.

---

## 2. NUMBER CONVENTIONS — which number is which, per provider

This is where the desk keeps failing, and the failures are not random: they are all cases where a number
that already means something else got reused as a strike or a stop.

### 2.1 Stop denomination — the single most dangerous field

The same keyword means things **500x apart** across providers. Stated per provider, with evidence.

| provider | keyword | denomination | n | conf | proof |
|---|---|---|---|---|---|
| prince | `s/l`, `stop limit`, `stop loss` | **PREMIUM** | 8 | MEASURED | ratio band, below |
| prince_small | `stop`, `stop limit`, `stop loss` | **PREMIUM** | 5 | MEASURED | ratio band, below |
| optionking | `SL <n>%` | **PERCENT OF PREMIUM** | 26 | MEASURED | always carries `%` |
| optionking | `SL <n>` (bare) | **ABSOLUTE PREMIUM** | 2 | MEASURED | 07-13, 07-22 |
| jmt | `SL:` | **UNDERLYING (SPY price)** | 15 | MEASURED | directional proof, below |

**prince — stop/premium ratio (MEASURED, n=8, band 0.71–0.85, median 0.80):**

| contract | premium | stop | ratio |
|---|---|---|---|
| AMD 73C | 1.81 | 1.29 | 0.713 |
| QQQ 705P (07-16) | 3.00 | 2.20 | 0.733 |
| QQQ 700C (07-17) | 2.95 | 2.20 | 0.746 |
| CELH 33C (07-06) | 1.25 | 1.00 | 0.800 |
| MSFT 395C (07-10) | 3.10 | 2.50 | 0.806 |
| NVDA 210C (07-17) | 2.10 | 1.70 | 0.810 |
| HIMS 36C (07-10) | 2.15 | 1.80 | 0.837 |
| GOOGL 370C (07-21) | 3.40 | 2.90 | 0.853 |

**prince_small — same house style (MEASURED, n=5, band 0.73–0.81, median 0.78):**
1.03→0.80 · 0.90→0.70 · 0.82→0.60 · 1.18→0.95 · 1.10→0.85.

This band is the practical disambiguator, because **the stop value appears on either side of the
keyword** in both providers' prose:

```
2026-07-21  ... 3.40 entry / 2.90 stop limit / $359.59 first price target   <- value PRECEDES
2026-07-16  Filled now @everyone 3.00 for the 705 puts stop loss 2.20        <- value FOLLOWS
2026-07-17  ... $700.70 Price target stop limit 2.20                         <- value FOLLOWS
```

A naive "number after the stop keyword" rule extracts `July 24` from *"1.80 stop limit July 24 expiry"*
and `$359.59` (the price target) from the 07-21 GOOGL message. A naive "number before" rule extracts
`705` (the strike) from the 07-16 QQQ message. **Neither word-order rule works. Pick the nearest
numeric candidate on either side whose ratio to the premium falls in 0.65–0.95, and veto everything
else.** (INFERRED — the band is measured, its use as a selector is my recommendation.)

**jmt — `SL:` is an SPY price, with a directional proof (MEASURED, n=15).**
All 15 values sit in 743.85–754.30 against an SPY spot near 750. The proof that this is an *underlying*
stop rather than a coincidence is directional:

* **calls: SL below the strike in 13 of 13** — `750C … SL: 747.65`, `760C … SL: 748`, `755C … SL:750.30`, `756C … SL:752.20`
* **puts: SL above the strike in 2 of 2** — `740P 2.08 7/9 SL:744.50`, `752P 1.55 7/17 SL:754.30`

That flip is only coherent for an underlying stop. `|SL − strike|` ranges 2.00–12.00 SPY points
(median 3.80). If read as a premium, `750C 1.65 … SL:747.65` implies a stop **453x** the entry —
**veto any jmt SL/premium ratio above 5.**

**optionking — percent by default, absolute by exception.** 26 of 28 carry `%`
(`SL 20%` ×17, `SL 30%` ×7, `SL 40%` ×1; also written `SL.20%`). The two bare exceptions are
`SPX 7535C at 1.50 SL 1.00 0DTE` (07-13) and `PLTR 130C at 1.20 SL 1.00 07/24 expiry` (07-22) — in both,
the bare number is *less than the premium*, which is the disambiguator. Never an underlying price.

### 2.2 Price target vs strike — why magnitude cannot save you

prince and prince_small both state price targets as **underlying levels**, and those levels sit
right on top of their strikes.

**prince (MEASURED, n=13):** `|target − strike| / strike` — median **0.95%**, max 9.15%,
and **4 of 13 are exactly equal to the strike**:

| | strike | target | Δ |
|---|---|---|---|
| PLTR 134C | 134 | 134 | **0.00%** |
| QQQ 700C | 700 | 700 | **0.00%** |
| AAPL 300C | 300 | 300 | **0.00%** |
| IWM 290P | 290 | 290 | **0.00%** |
| QQQ 705C | 705 | 704 | 0.14% |
| QQQ 722P | 722 | 719.89 | 0.29% |
| NVDA 210C | 210 | 212 | 0.95% |
| MSFT 395C | 395 | 389.90 | 1.29% |
| AAPL 325P | 325 | 321 | 1.23% |
| QQQ 722P | 722 | 715 | 0.97% |
| GOOGL 370C | 370 | 359.59 | 2.81% |
| HIMS 36C | 36 | 38.29 | 6.36% |
| CELH 33C | 33 | 36.02 | 9.15% |

**prince_small (MEASURED, n=5):** median 1.46%, max 2.64%, 1 of 5 exactly equal.

**The rule this forces:** magnitude, range checks and "is it near the spot" all fail. **Only the label
disambiguates** — `Price target` / `PRICE TARGET` / `first price target` / `target`. A number carrying
that label is an underlying price and can never be a strike, a premium or a stop.

**The useful corollary (INFERRED).** Because the target sits on the strike so reliably, when an entry
states a target but *no* strike, the target is the best available strike estimate — 4/13 exact,
11/13 within 3%. This is precisely today's IWM failure:

```
2026-07-23 15:08  From the LIVE: IM IN IWM PUTS THAT EXPIRE TOMORROW / 1.03 limit / no stop yet
                  LETS DROPPP / 290 price target @everyone                        <- REJECTED, no strike
2026-07-23 15:14  20% ON $IWM $290 Puts @everyone Be ready for the flush!!        <- strike appears, +6.5 min
2026-07-23 16:13  IWM 290P 1.03 limit expires tomorrow
```

The strike **was in the original message**, labelled as the price target — and the truth was 290 either
way. Propose it, mark `strike_source = inferred_from_target`, and **ask**. Do not card it silently:
CELH and HIMS show the target can miss the strike by 6–9%, and a 9% strike error is a different
contract.

### 2.3 Per-provider number extraction, with the traps

**prince** — `$` prefix beats adjacency (MEASURED):
```
AAPL $300 JULY 10 Calls 0.85 entry No stop yet, playing the momentum Price target 300
```
"the number adjacent to `Calls`" yields **10** — the day of the month. The truth is **300**, the
`$`-prefixed token. *A date component must never become a strike.*

```
Watch $GOOGL $385c July 10 / 85c per contract
```
Two `c`-suffixed numbers. The first (`$385c`) is strike+right; the second (`85c`) is **85 cents of
premium**. Reading `85c` as an 85-strike call is the mirror image of the same trap.

**prince_small** — the strike is routinely *absent* and recoverable only anaphorically (MEASURED):
```
2026-07-21 19:43  In SMCI july 31 / 1.67 per contract @everyone
```
No strike. `31` is the day of month. Truth: **SMCI 25C**, from `Watching SMCI 25 calls July 31`.
Premium may also be a **range** — `0.85-0.90 entry` (07-20), filled at 0.90, the top of the range.

Quantity vs premium (INFERRED): `In 1 contract at 1.91` — `1` is quantity, `1.91` is premium, and there
is **no ticker at all**.

**optionking** — template-positional, with four observed deformations (MEASURED):
`AAPL.330C at 1.30` (period for space) · `NVDA 200 at 0.10 lotto 0DTE` (**no right letter** — assume C
but flag `assumed_call`) · `AMZN 255C lotto 1DTE.. small.size` (**no premium** — card with
`premium=null`, do not drop) · `SPX 8520C` (typo, edited to `7520C` 16s later — a strike more than 5%
from the index is a typo, not a contract).
`lotto`, `size 0`, `small size`, `go small`, `N cons` are **size flags, not contract fields** — 27 of 68
entries carry `lotto`.
His `PT 7575 7580` (07-10) and `PT 7560 8570` (07-14) are **two index levels**, ladder style. INFERRED,
n=2 only.

**jmt** — the ticker is **never stated** and is always SPY (MEASURED: 1 exception in 106 messages,
`SPY 752C 1.97 7/15 SL:748.5`, 07-14). Template order is strike+right, premium, expiry, `SL:`.
Beware backfilled historical expiries — `580C 1.64 3/17` is four months in the past.

### 2.4 The P&L-percentage guard

`303% on $SMCI calls … My price target for today is $32.16` minted a phantom 32.16 strike (SMCI trades
in 0.50 increments; that contract does not exist). Stated as a per-provider rule because the leading
form differs by author but the guard is the same:

**A message whose leading numeric token is a percentage can never mint a strike, premium or expiry.**
prince leads with `Up N% on` / `N% ON`; prince_small with `Up N%` / `WE ARE UP N%` / `N% on`;
jmt with a bare `N%`; optionking with `<TICKER> <px> now.. N% up guys`. All MEASURED.

---

## 3. ANAPHORA AND LINKING — measured per provider, not globally

The taxonomy's global windows (W1 120 min / W2 5 days / W3 30 min) are sound as defaults. Measured
per provider, the distributions are different enough to be worth splitting.

### 3.1 Watch → fill gaps, per provider

**prince (MEASURED, n=4 explicit same-session pairs):**

| setup | watch | fill | gap |
|---|---|---|---|
| QQQ 700C | 07-17 13:58:56 (2.40) | `im in at 2.95 per contract` 14:01:33 | **2.6 min** |
| GOOGL 385C | 07-07 13:50:52 (0.85) | `IN AT 0.93` 14:00:40 | **9.8 min** |
| QQQ 705P | 07-16 13:45:50 | `Filled now @everyone 3.00` 13:59:48 | **14.0 min** |
| PLTR 134C | 10:14:22 (2.05) | `Getting in PLTR here @2.03` 10:29:31 | **15.2 min** |

Every same-session prince fill landed **within 16 minutes**. Recommended window **45 min** (~3x headroom,
still well short of a session).

**prince_small (MEASURED, n=5) — a much wider distribution:**

| setup | gap |
|---|---|
| NFLX 900C (07-07) | 0.07 min *(4 s — scrape-burst artifact; ordering is right, spacing is not)* |
| NVDA 205C (07-10) | 2.9 min |
| AAPL 325C (07-23) | 7.3 min |
| AMZN 260C (07-20) | 52.1 min |
| AAPL 315C (07-08) | 60.9 min |

median 7.3, max **60.9**. A 45-minute window would have missed AMZN 260C *and* AAPL 315C.
Recommended window **90 min**. **Do not share a window between prince and prince_small** — that is the
whole point of measuring per provider.

**optionking and jmt: not applicable (MEASURED).** Neither has posted a single watch message —
0 of 165 and 0 of 106 respectively. Every contract-bearing message from them is a live entry. There is
no watch→fill link to make, and any linking machinery aimed at them is aimed at nothing.

### 3.2 Cross-session links

| provider | setup | watch → evidence of fill | conf |
|---|---|---|---|
| prince | NVDA 210C `Limit order set` 07-17 19:56 → `Up 23% on NVDA calls` 07-20 13:31 | **65.6 h** | MEASURED |
| prince | GOOGL 370c watched 07-15 14:03 (2.04) → entered 07-21 16:24 (3.40) | **6.0 days** | MEASURED |
| prince_small | WMT 115C watched 07-15 14:23 → `STILL looking to get in` 07-16 18:54 → `Wmt calls are up over 120%` 07-20 22:20 | **127.9 h** | MEASURED, n=1 |

Cross-session linking must require the message to **name the ticker**, and must cap at the watched
contract's own expiry. The 6-day figure is an *observed maximum*, not a safety margin.

### 3.3 Arrival-order inversion — prince_small specifically

**MEASURED, n=2, severity high.** His messages reach the desk out of order:

* **SMCI:** the anchoring watch `Watching SMCI 25 calls July 31` **arrived 07-22 13:30:54, i.e. 17.8 h
  *after* the fill it was supposed to anchor** (`In SMCI july 31 / 1.67`, 07-21 19:43:45).
* **AAPL 07-08:** the fill `In $AAPL calls at 1.03` arrived at **17:08** — after the 16:29–16:55 P&L run
  *and* after the 16:55 exit (`price target has been HIT selling here`). The whole position appeared to
  open after it had already closed.

Two consequences: link on the **provider's post time**, never the desk's receive time; and **re-run
linkage when a late watch arrives**, because the anchor a fill needed may show up a day later.

### 3.4 Bare anaphora — the window depends on how many positions the provider runs

This is the axis the global rule misses. Recommended windows are INFERRED from the measured book sizes.

| provider | typical concurrent positions | bare anaphora | recommended |
|---|---|---|---|
| **jmt** | **one** (single SPY position) | nearly all management is bare — `12%`, `40%`, `taking some off here` | **safe** — resolve to the single open position, window ~1 trading day |
| prince | 1–3 | 2 of 5 exits name no ticker | 30 min, require exactly one candidate |
| prince_small | 1–2 | fills omit ticker+strike (`In 1 contract at 1.91`) | 90 min, require exactly one candidate |
| **optionking** | **many** | `Avg here.. guys.. manage ur risk` (07-15, three positions open) | **default to MUST_ASK** — usually unresolvable |

jmt and optionking sit at opposite ends: identical bare-anaphora *syntax*, opposite correct *handling*,
purely because of book size. When optionking *does* name the contract (`SPX 7520C up 110% guys`,
`NVDA 205C up 50%`) the link is unambiguous — use it, and if the desk does not hold that contract, log
an orphan P&L rather than opening a position.

**UNSURE:** the single-position assumption for jmt underpins his generous window and was not
exhaustively verified across all 12 days. It held everywhere I looked, but I did not prove it.

---

## 4. REPOSTS — genuine re-entry vs verbatim re-scrape

The browser extension re-reads the visible message list, so identical texts reappear. The per-provider
rates differ by an order of magnitude, and so must the handling.

| provider | repost rate | observed repeat-gap range | dedupe horizon needed | conf |
|---|---|---|---|---|
| optionking | **53%** (36 of 74 distinct texts repeat, 75 gaps) | 0.8 min → **1460 min (24.3 h)** | **≥ 25 h** | MEASURED |
| jmt | 26% | 0.4 min → 12942 min | see the short-text warning below | MEASURED |
| prince | 17% (11 texts) | 0.0 min → **4440 min (74 h)** | **≥ 74 h** | MEASURED |
| prince_small | **5%** (2 texts, both WMT) | 1124 → 4842 min | ≥ 81 h | MEASURED |

### 4.1 The distinguisher: same contract, different premium

**MEASURED, holds for both optionking and prince.**

* **REPOST** — same `(ticker, strike, right)` **and** same premium **and** same SL.
  optionking's `SPY 745P at 0.50 lotto 0DTE` re-appeared at 17:20, 18:40 and 18:42 on 07-20 while the
  position was open and profitable. A repost is evidence the position is **still on his board**, not
  that he re-entered.
* **GENUINE RE-ENTRY** — same contract, **different premium**.
  `SPX 7550C at 1.10 lotto` (07-09 16:40) then `SPX 7550C at 0.70 lotto...1 con only` (07-09 18:41).
  `AAPL 315C at 0.90` (07-09) then `AAPL 315C at 0.18 lotto` (07-10).
* prince marks re-entry **lexically as well**: `Back in QQQ $722 puts here at 0.90, new day new setup`
  (07-07 15:17) — `Back in` + `new day new setup` + a premium changed from 1.10 to 0.90. Also
  `Back in calls` (jmt), `gonna find an re entry` (jmt).

### 4.2 Two provider-specific hazards

**jmt — do not text-dedupe his short messages (MEASURED, severity high).**
His bare percentages recur naturally across days as genuinely *new* P&L updates: `20%` appeared 5 times
spanning 12,949 min; `40%` 3 times spanning 14,354 min; `12%` 4 times. Text-identical does **not** mean
duplicate for messages under ~14 characters. Restrict text-dedupe to texts ≥ 14 chars, or require a
`msg_id` match. Separately, the scraper replays him in ~0.4-minute bursts (nine messages inside one
second at `2026-07-15T18:30:25`): a sub-2-minute gap on a *long* text is a scrape artifact, whereas a
long gap on a *short numeric* text is a real new update.

**prince_small — a watch and its fill are not duplicates (MEASURED, this is today's failure).**
```
2026-07-23 14:04:57  Watching $AAPL 325 call July 25 / 1.18 limit entry / 0.95 stop / Waiting for entry
2026-07-23 14:12:14  In $AAPL calls at 0.82 and will add a stop limit for 0.60 @everyone
```
The desk deduped the fill as a repost of the watch. **Different premium (1.18 vs 0.82) plus a different
verb (`Watching` vs `In`) means a different message, always.** His 5% repost rate is the lowest of the
four — a near-duplicate from prince_small is far more likely a genuine second post than an artifact,
which is the opposite of the correct prior for optionking.

### 4.3 Edits

**optionking is the only profiled provider who genuinely edits (MEASURED, n=2).**
`SPX 8520C at 2.20` → `SPX 7520C at 2.20 … (edited)` 16 s later; `META 660C at 2.50` → `META 665C at
2.50 … (edited)` 2 min later. An edit **re-strikes the existing card** and must never mint a second
position — `SPX 8520C` never existed and the desk must not hold a phantom.
prince re-posts rather than edits. prince_small posts near-identical watches 25 s apart with a changed
premium (`1.15 entry` 16:07:09 → `1.10 entry` 16:07:34 on 07-08) — treat as an edit of the watch, not
two watches.
The edit footer (`(edited)` + `Monday, July 20, 2026 at 9:02 AM`) is **never** an expiry and its digits
are never a strike. Note the footer clock does not agree with the log UTC timestamp; do not use it as a
time source.

**Log-truncation caveat (MEASURED).** prince's GOOGL 370c entry normalises differently across
re-scrapes purely because the 07-21 copy was cut at 120 chars and the 07-23 copy was not. Dedupe on the
**first ~100 characters**, whitespace- and @mention-insensitive, or long prose entries will double-card.

---

## 5. P&L UPDATES — never a contract, always a liveness signal

Per the taxonomy, a `PNL_UPDATE` may emit at most `{ticker, right}` and only for linking. What is
per-provider is **how much liveness information the absence of P&L carries.**

| provider | P&L msgs | median/active day | max/day | monotonic? | conf |
|---|---|---|---|---|---|
| jmt | 51 | 5 | 7 | **no** — reports drawdowns honestly | MEASURED |
| prince | 39 | 4 | 7 | yes, near-monotonic | MEASURED |
| prince_small | 28 | 3 | 7 | yes | MEASURED |
| **optionking** | **6 over 4 days** | 2 | 2 | — | MEASURED |

**The asymmetry that matters (MEASURED).** prince, prince_small and jmt narrate a position densely —
prince's TSLA run on 07-09 was `10, 45, 55, 70, 128, 165, 188`; prince_small's AMZN on 07-20 was
`35, 42, 50, 55, 67, 70`. For these three, **a P&L update in the last hour is strong evidence the
position is open**, and a full session of silence is weak evidence it is gone.

**optionking is the opposite.** Six P&L messages in thirteen days against ~68 entries. **Absence of P&L
is not evidence of a close for him — it is his normal state.** Any staleness heuristic keyed on "no
recent chatter" will mass-close his book wrongly. Use expiry for him instead (§1.4).

jmt's non-monotonicity is a useful signal in its own right: `Back down to 30% still holding`,
`Down 56% with the gap down gonna hold`, `Back up 60%`. A falling percentage from jmt is a normal
update, not a corruption.

**The mark-vs-entry trap (MEASURED, optionking).** `SPY 2.45 now.. 380% up guys..` — 2.45 is the
**current mark** of a position entered at 0.50, not a new entry at 2.45. The arithmetic confirms it
(0.50 × 4.9 = 2.45). Disambiguators: the word `now`, an accompanying `%`, and the **absence of both a
strike and the word `at`**. This pattern is byte-similar to a bare entry shorthand, so the absence of a
strike is doing the real work.

---

## 6. WHEN NOT TO RESPOND — per-provider noise

Common to all four: bare `@everyone` / `@JMoneyAlerts` / `@Options Day Trade Alerts` mention tokens
(strip before parsing), GIF and URL-only posts, and `Test`/empty messages.

**prince** — macro-news paragraphs (the 07-08 NATO summit post), chart narration (`Honestly this thing
did a full V shape recovery`, `1H, 4H and 1D look way better`), `First 15 has closed.` (a candle, not a
position), congratulation posts, and `May enter today` (07-15 — no ticker anywhere in scope, so it is
`MUST_ASK`, *not* a watch).

**prince_small** — `$9 run in 30 mins is definitely what the higher time frames said`,
`We just need 1 trade. Just 1.`, klipy.com GIF links, `I sold too early`, and hype tails
(`if you been struggling please make sure…`).

**optionking** — `AMZN about to pop up.` (commentary), `manage ur risk` / `go small` (a `WARNING` to
attach as a risk flag, not a trade), and the most dangerous single noise message in the corpus:

```
2026-07-22 16:38  Microsoft Price Target Maintained With a $646.00/Share by Bernstein
```

A **news headline** carrying a company name, the words `Price Target`, and `$646.00`. Every surface
feature says "contract". Detect on: an analyst-firm name, `Maintained`/`Reiterated`/`Initiated`,
`/Share`, and a company's *long* name where this provider otherwise always uses a ticker.

**jmt** — level talk in the same numeric range as his strikes
(`What I'm watching for … I wanna see spy break and hold above this supply`), `manage your trade` /
`ya on your own` (WARNING), `we still in?` (QUESTION), and the 07-21 personal health update (CHAT).

**Not a provider at all.** The 33 `?`/`unknown` messages are an AGORA ops bot (`CEO [12:16 ET] Alert:
[Scalping Flow Warden] BROKEN …`, `Midday Status … Realized -$1,500`) plus unmapped room chat
(`Spy calls anyone?`, `What's the amzn call?`). Two of these carded historically. Drop on provider
resolution, before the parser runs.

---

## 7. DIALECT QUICK REFERENCE

Verbs only; the full phrase lists with examples live in `provider_profiles.json`.

| | prince | prince_small | optionking | jmt |
|---|---|---|---|---|
| **new entry** | `In X`, `Swinging $X`, `I'm gonna take a shot at`, `From the LIVE: IM IN` | `In $X calls at`, `In X <month> <day>` | bare template with `@everyone` **prefix** | bare 4-line template |
| **watch** | `Watching`, `Watch swings`, `Watchimg` *(his typo)*, `Keeping an eye on`, `Limit order set` | `Watching`, `I'm eyeing`, `Waiting for entry`, `STILL looking to get in` | **never** | **never** |
| **fill of a watch** | `IN AT <px>`, `im in at <px> per contract`, `Filled now` | `In 1 contract at <px>`, `IN AT <px> on X` | n/a | n/a |
| **add** | `Adding X at <px>` | — | `Avg here..`, `new avg is <px>` | `Back at B/E gonna add more` |
| **trim** | (intentions only) | — | `Left runner N cons now..` | `taking some off`, `leaving runners here` |
| **full exit** | `OUT of`, `im out`, `Cutting $X`, `Cut Out at` | `Sold X`, `Cutting X`, `selling here` | `Sold all X` | `Stopped out` |
| **stop moved** | `<px> stop limit` / `stop loss <px>` | `I will implement a stop limit at <px>` | `SL <n>%` | `SL:<underlying>` |
| **P&L** | `Up N% on $X calls` | `WE ARE UP N%` | `X <px> now.. N% up guys` | bare `N%` |
| **sign-off** | `@everyone` (suffix) | `@everyone` (suffix) | `@everyone` (**prefix**) | `@JMoneyAlerts` + emoji |

Two idiosyncrasies worth carrying into the prompt because they change the parse:
`Watchimg` is prince's recurring misspelling of *watching* (3 occurrences) — a watch-detector keyed on
the correct spelling silently drops those setups. And optionking's `@everyone` is a **prefix on
entries**, which makes its presence a positive entry signal for him specifically, whereas for prince and
prince_small it is a trailing broadcast token that carries no information at all.

---

## 8. RELIABILITY — is the stated premium achievable?

| provider | posts | premium fidelity | conf |
|---|---|---|---|
| prince | intentions **and** fills, mixed | watch → fill: 2.05→2.03 (−1%), 0.85→0.93 (+9%), 2.40→**2.95 (+23%)**, 3.00→3.00 (0%) | MEASURED n=4 |
| prince_small | watches then fills | 1.15/1.10→1.03 (−6…−10%), 1.35→**1.91 (+41%)**, 0.85‑0.90→0.90, 1.50→1.30 (−13%), 1.18→**0.82 (−31%)** | MEASURED n=5 |
| optionking | live fills — the template *is* the entry | not measurable from text (no watch→fill pairs). IBKR verification is the only check | GUESS |
| jmt | live fills — the template *is* the entry | not measurable from text | GUESS |

**prince_small's deviation is large and bidirectional (−31% to +41%).** The operational consequence:
a fill whose premium differs by more than ~25% from its watch should still **link** — that is his
observed norm — but must raise a price-drift flag, because the same signature is also consistent with a
silent strike change. Never treat a watch premium as a fill for either prince or prince_small.

**jmt's stop is not a commitment (MEASURED, n=2).** He states an `SL:` and then holds through it
(07-14, 07-15). Mirror it as a reference level; apply the desk's own risk rule for the actual stop.

**optionking's size flags are real risk instructions.** 27 of 68 entries carry `lotto`, `size 0`,
`1 con only` or `small size`. These should propagate to sizing, not be discarded as commentary.

---

## 9. WHAT I AM UNSURE ABOUT, AND WHY

Listed so that nothing below gets applied with more confidence than it has earned.

1. **Exit vocabularies are not closed sets.** prince n=5, prince_small n=4, optionking n=3, jmt n=5.
   With samples this small, the absence of `all out` / `flat` / `done` from a provider is far more
   likely a sampling gap than a real fact about their speech. **Do not treat any phrase list here as
   exhaustive.** Escalate unrecognised exit-shaped messages rather than dropping them.
2. **The 120/200-character truncation ceiling.** No source in this repo holds a full-length message.
   Rules about message *heads* are solid; rules about *tails* are not. This matters most for
   prince_small's buried exits (§1.3) — a `selling here` late in a long message may never have reached
   the log at all, which means my exit-rate figures are, if anything, **over**-estimates.
3. **prince's exit prices.** `Cut Out at 0.80` contradicts its own `Was up 35%` against a 1.10 entry.
   I trust his exit *fact*, not his exit *price*.
4. **Position counts are reconstructed, not authoritative.** The "identifiable entries" column in §1.1
   comes from reading his message stream, not from the desk's position table. The exit *rates* are
   therefore approximate; the *ordering* between providers (optionking ≪ prince < prince_small) is
   robust, the absolute percentages are not.
5. **jmt may be stale.** His last message is 2026-07-21, immediately preceded by a personal health
   note. The whole profile may describe a provider who has stopped posting.
6. **jmt's single-position assumption** underpins his generous bare-anaphora window. It held everywhere
   I looked across 12 days but I did not exhaustively prove it. If he ever runs two SPY positions at
   once, every bare `N%` becomes ambiguous and the window must collapse to optionking's MUST_ASK rule.
7. **optionking's `PT` semantics rest on n=2**, one of which contains a typo (`PT 7560 8570`, where
   8570 should be 7570). INFERRED at best.
8. **optionking's `Avg here..`** is genuinely ambiguous: `Avg here.. new avg is 1.10` (07-20) states a
   new blended basis, but bare `Avg here.. guys.. manage ur risk` (07-15, three positions open) states
   nothing at all and is MUST_ASK. I could not determine whether bare `Avg here..` implies a fill.
9. **The prince ↔ prince_small cross-posting finding is n=3** (of 100 and 59 distinct texts): three
   verbatim prince texts re-appearing in prince_small 76–115 min later. It looks like one author running
   two rooms, and if so, carding both **doubles real-money risk on a single idea**. But three pairs is
   not enough to assert it as a rule — I flag same-contract entries across the two within 3 h as
   *suspected* duplicates and recommend against auto-merging.
10. **Two 07-23 prince_small messages are excluded from all counts** — `AAPL 325C Jul 25 @0.82` and
    `AAPL 325C 07/25 @0.82`, arriving 3 s and 5 s after the real prose fill. They look
    machine-normalised rather than human. Worth confirming they are not test injections leaking into
    the live channel.
11. **Provider resolution failed for today's messages.** Every 07-23 prince and prince_small line in
    `logs/inbound.log` carries `msg_id = ?` rather than a Discord channel id. A per-provider profile is
    useless if the provider cannot be resolved at parse time. That is outside my file ownership, but it
    gates everything in this document.

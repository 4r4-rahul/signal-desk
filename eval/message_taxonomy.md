# Message Taxonomy & Routing Spec — Options Copy-Trading Desk

**Status:** authoritative domain spec. Owned by the trading-desk SME, not by the parser.
**Companion:** `eval/routing_corpus.jsonl` (labelled ground truth).
**Derived from:** 706 real inbound messages (`logs/inbound.log`, 2026-07-06 → 2026-07-22),
cross-referenced against `channels/*/live_signals.jsonl`, `channels/*/closed_trades.jsonl`,
and live IBKR chain data pulled 2026-07-22.

> **Why this document exists.** Every rule below is written against a real message that
> produced a real wrong card in the last two weeks. Where a rule is a guess, it is labelled
> a guess. Where a message is unresolvable, the correct answer is `MUST_ASK` — not a
> confident label. A wrong card is manually traded on tastytrade with real money; a
> question costs the user five seconds.

---

## 0. Ground truth: the four failures this spec must prevent

| # | Message | Desk produced | Truth | Root cause |
|---|---|---|---|---|
| F1 | `In SMCI july 31\n1.67 per contract` (prince_small, 07-21 19:43) | `SMCI 31C Jul-31 @ 1.67` (contract quoted $0.21) | `SMCI 25C Jul-31 @ 1.67`, strike from the prior watch. Ran +303%. | **Expiry day-of-month read as strike**, and the anaphoric strike was never sought. |
| F2 | `303% on $SMCI calls. Best overnight trade this month My price target for today is $32.16` (07-22 14:00) | `SMCI 32.16C` — a strike that does not exist on the chain | P&L update. **No contract at all.** | **Price target read as strike**; a P&L update was allowed to mint a card. |
| F3 | `Watching $PLTR $134 calls … entry around 2.05` (10:14) carded; `Getting in PLTR here @2.03` (10:29) **rejected**; `Up 10% on $PLTR calls` (10:33) | watch kept at 2.05, real fill discarded | Fill at 2.03 attaches to the 10:14 watch and upgrades it to ENTERED | **Anaphoric fill not linked** — bare `PLTR` + premium, no strike, so the parser dropped it. |
| F4 | `Watching SMCI 25 calls / July 31 / First price target would be that 25.66 area off the 1H` | nothing carded | a `WATCHING` record with ticker+strike+right+expiry and **premium null** | **Watch with no premium refused to card**, so F1 had nothing to attach to. |

Live verification (IBKR, 2026-07-22, SMCI spot **31.41**):

```
SMCI 25.0 C 2026-07-31  bid 6.55 / ask 7.15   mid 6.85   (intrinsic 6.41)   <- 1.67 -> 6.85 = +310%
SMCI 31.0 C 2026-07-31  mid 2.24
SMCI 32.16 C 2026-07-31 -> "contract not found at IBKR"   <- F2 strike is not listed
SMCI listed strikes 20-40: 20, 20.5, 21, … 32, 32.5, 33 …  (0.50 increment; 32.16 impossible)
```

This is the whole spec in one line: **a number that already means something else must not be
reused, and a number that is missing must be found or asked for — never invented.**

---

## 1. Providers and their dialects

Six human providers plus one synthetic. Dialect matters: the same token means different
things per provider, and a provider-blind parser is what produced F1.

| Provider | Volume | Underlying(s) | Shape | Ticker stated? | Notes |
|---|---|---|---|---|---|
| `optionking` | 154 | SPY/SPX/mega-cap | `@everyone TSLA 400C at 1.60 SL 20% 0DTE` | always | Most machine-readable. `SL n%` = % of premium. `lotto` = size flag, not a contract field. Posts edits. |
| `mike` | 133 | SPX + single names | `MSFT 7/10 405C @ 1.7`, `7495c 2.3 eod lot`, `meta 672 1.7` | often omitted for SPX | Sloppiest. Lowercase tickers, missing C/P, typos (`msttr`, `spacx`, `sndk`), bare `.5`/`.12` follow-ups. |
| `sulker` | 119 | ETFs + single names | `NVDA 0DTE 197.5c .89, chasing …` | always | `0DTE`/`1DTE`/`intraweek` before the strike. Multi-ticker watch lists. Posts formal recaps. |
| `prince` | 112 | large-cap + QQQ/SPY | prose: `Swinging $HIMS $36c\n2.15 limit buy\n1.80 stop limit\nJuly 24 expiry\nPRICE TARGET $38.29` | usually | Prose with a **price target that looks exactly like a strike**. Source of F1-family bugs. |
| `prince_small` | 53 | large-cap | prose, looser than prince | often anaphoric | Source of F1–F4. Posts fills as bare `In TICKER … @ price`. |
| `jmt` | 106 | **SPY only, implicit** | `751C\n1.62\n7/8\nSL:748.90` | **never** | Ticker is always SPY. `SL:` is an **underlying** price, not a premium. Management is a bare percentage: `31% taking some off here`. |
| `chip` | 6 | QQQ/SPY | `QQQ 705C @ 2.06 0DTE` | always | Desk's own shadow provider. Machine-generated, always well-formed. |
| `?` (unmapped) | 23 | — | `CEO [12:16 ET] Alert: [Scalping Flow Warden] BROKEN …` | — | **Not a signal provider.** A monitoring bot whose feed leaked into ingest. Must route to `NON_PROVIDER` and never card. It carded twice. |

---

## 2. Message types

Types are exhaustive and mutually exclusive per message. A message that contains two
actions (`Trimmed @ 5` + `all out @ 6.2` in one line) is split; if it cannot be split
cleanly it is `MUST_ASK`.

### 2.1 Type table

| Type | One-line definition | Desk action | Creates a position? |
|---|---|---|---|
| `ENTRY` | Provider is in, or is entering right now, and the message fully specifies the contract. | Create card, status `NEW`/`ENTERED`. | yes |
| `WATCH` | A named setup the provider is *not* in: eyeing, resting a limit, "looks nice", "may enter". | Create card, status `WATCHING`, no fill, no shadow P&L. | yes (as a watch) |
| `FILL` | Announcement that a previously-watched setup is now filled. Usually anaphoric. | Upgrade the linked `WATCH` to `ENTERED` at the stated price. If no watch is linkable and the contract is fully specified, degrade to `ENTRY`. | no (upgrades) |
| `ADD` | Adding to an existing position (scale-in / average-down). | Update avg price + size on the linked position. | no |
| `TRIM` | Selling part of an existing position. | Record partial exit; position stays open. | no |
| `EXIT` | Closing the whole position (voluntary, target, or stop). | Close the linked position at the stated/implied price. | no |
| `STOP_SET` / `STOP_MOVE` | Setting or moving a stop on an open position. | Update the stop on the linked position. | no |
| `TARGET_SET` | Stating or moving a profit target on an open position. | Update targets on the linked position. | no |
| `PNL_UPDATE` | Reporting an unrealized/realized % or $ on an existing position. Includes brags. | Optionally mark-to-provider on the linked position. **Never mints a contract.** | no |
| `RECAP` | End-of-day / weekly summary listing several closed trades. | Reconcile against closed trades for edge measurement. Never opens anything. | no |
| `COMMENTARY` | Market view, level talk, plan-for-the-day, chart narration with no actionable contract. | Ignore (optionally store as context). | no |
| `WARNING` | Risk caution: "manage ur risk", "go small", "it is risky", "ya on your own". | Attach as a risk flag to the most recent card by the same provider, if any. | no |
| `QUESTION` / `CHAT` | Banter, questions to the room, off-topic. | Ignore. | no |
| `NOISE` | URL-only, GIF, image, emoji, test messages, empty. | Ignore. | no |
| `EDIT` | Discord edit of a previously posted message (carries the `(edited)` + timestamp footer). | Re-price / re-strike the existing card in place. Do **not** create a second card. | no |
| `REPOST` | The scraper replayed a message the desk already ingested (identical text, same provider). | Deduplicate and skip. | no |
| `NON_PROVIDER` | Not from a trading provider (bot/ops feed, unmapped channel). | Drop before parsing. | no |
| `MUST_ASK` | Human-unresolvable: an action is clearly intended but the referent or a required field cannot be recovered from the text or the context. | Raise a question to the operator. Do not card, do not guess. | no |

### 2.2 Field contracts per type

Legend: **R** = required (absence ⇒ `MUST_ASK`), **O** = optional, **F** = forbidden
(presence ⇒ the parse is wrong and must be vetoed).

| Type | ticker | strike | right | expiry | premium | stop | targets | qty | links_to |
|---|---|---|---|---|---|---|---|---|---|
| `ENTRY` | R | R | R | R* | R | O | O | O | F |
| `WATCH` | R | R | R | O | **O** | O | O | O | O |
| `FILL` | R† | R† | R† | R*† | R | O | O | O | **R** |
| `ADD` | R† | R† | R† | O† | R | O | O | O | **R** |
| `TRIM` | R† | O† | O† | O† | O | F | O | O | **R** |
| `EXIT` | R† | O† | O† | O† | O | F | F | F | **R** |
| `STOP_SET`/`STOP_MOVE` | R† | O† | O† | O† | F | **R** | F | F | **R** |
| `TARGET_SET` | R† | O† | O† | O† | F | F | **R** | F | **R** |
| `PNL_UPDATE` | O† | **F** | O† | **F** | **F** | F | F | F | O |
| `RECAP` | O | O | O | O | O | F | F | F | F |
| `COMMENTARY`/`WARNING`/`QUESTION`/`CHAT`/`NOISE`/`NON_PROVIDER` | F | F | F | F | F | F | F | F | F |
| `EDIT` | inherits from the edited message | | | | | | | | **R** |
| `REPOST` | — | — | — | — | — | — | — | — | R (the original) |
| `MUST_ASK` | O | O | O | O | O | O | O | O | O |

`*` **expiry**: required, but "required" means *resolvable*, not *stated*. `0DTE` / `today`
resolves to the message date; `1DTE`/`tomorrow` to the next trading day; `weekly expiry` /
`this week` / `Friday` / `intraweek` to that week's Friday; `next week` to the following
Friday. If none of these is present and the provider has no default (e.g. optionking always
states one; jmt always states `m/d` or `0DTE`) → `MUST_ASK`.

`†` **anaphoric fields**: may be absent from the message text and recovered from the linked
prior message under §3. If the link cannot be made, the field is `null` and the message is
`MUST_ASK` — never defaulted.

**The three hard structural rules:**

1. `PNL_UPDATE` **must never emit a strike, expiry or premium.** F2 is exactly this
   violation. `303% on $SMCI calls … price target is $32.16` may emit at most
   `{ticker: SMCI, right: C}` and only for the purpose of *linking to* an existing position.
2. `WATCH` **must card even with a null premium.** F4. A watch with `{ticker, strike, right}`
   is a complete, useful record — it is the anchor a later `FILL` attaches to. Refusing it
   costs the whole trade.
3. `EXIT`/`TRIM`/`STOP_*` **must never create a position.** If no open position can be
   linked, the message is an orphan: log it, flag the provider's book as out of sync, and
   ask. Do not back-fill a position from an exit.

**Ticker-level watch.** A `WATCH` that names a ticker and a right but **no strike**
(`Watching DINO for CALLs, we have room till 90`) is recorded as a *ticker-level* watch. It
is a real observation about what the provider is looking at, so dropping it loses
information — but it **cannot anchor a fill**, because there is no contract to inherit. Any
later anaphoric fill on that ticker still has to state or ask for the strike.

### 2.3 Type detail, with real examples

Every example below is verbatim from `logs/inbound.log`.

---

#### `ENTRY`
Firm, present-tense possession or acquisition, **and** the contract is fully specified in
this message (or trivially by provider default, e.g. jmt ⇒ SPY).

Markers: `In X`, `IN AT`, `Getting in`, `taking`, `took`, `bought`, `filled`, `im in`,
`Back in`, `Swinging`, `Trying calls here`, `add back to`, or a bare provider template with
no watch language at all (optionking / jmt / mike / sulker post entries as pure templates).

```
@everyone TSLA 400C at 1.60 SL 20% 0DTE                     optionking  -> TSLA 400C 0DTE @1.60, stop 20% of premium
751C\n1.62\n7/8\nSL:748.90                                   jmt         -> SPY 751C 2026-07-08 @1.62, stop = SPY 748.90
MSFT 7/10 405C @ 1.7                                         mike        -> MSFT 405C 2026-07-10 @1.70
NVDA 200c 1DTE .89, chasing for the squeeze ... i have SL     sulker      -> NVDA 200C 1DTE @0.89, stop present but unstated
TSLA 405c July 10 1.45 entry @everyone                       prince      -> TSLA 405C 2026-07-10 @1.45
In QQQ Puts $722 expiring today\n1.10 fill\nno stop yet\n
  Scalping with a price target $719.89                       prince      -> QQQ 722P 0DTE @1.10, target(underlying) 719.89
```

Note the last one: `$722` is the strike (adjacent to `Puts`), `1.10` is the premium
(`fill`), `$719.89` is an **underlying** target. Three numbers, three roles, zero overlap.

**Watch-language ambiguity.** `Swinging $HIMS $36c / 2.15 limit buy / 1.80 stop limit` is an
ENTRY in this house's usage (prince says "swinging" only about positions he holds) but
`2.15 limit buy` is literally a resting order. The corpus labels it `ENTRY` with
`hard: true`; a `WATCH` label is defensible and the engine should not be penalised
catastrophically for it. What is *not* defensible is dropping it.

---

#### `WATCH`
A named contract the provider is considering, with an explicit statement that they are not
in yet, or planning language with no possession.

Markers: `Watching`, `Watch`, `Keeping an eye`, `eyeing`, `on my radar`, `might be a play`,
`could be a play`, `looks nice/good/clean`, `let's see`, `limit order set` (a *resting*
order is not a fill), `may enter`, `thinking about`, `waiting for`, `if it breaks`,
`STILL looking to get in`.

```
Watching SMCI 25 calls / July 31 / May even go this week expiry /
  First price target would be that 25.66 area off the 1H          -> SMCI 25C, expiry AMBIGUOUS (07-24 or 07-31), premium null, target 25.66
Watching $PLTR $134 calls / Looking for an entry around 2.05 /
  July 10 @everyone / Price target $134                            -> PLTR 134C 2026-07-10, planned premium 2.05, target 134
Watch swings $QQQ $700c / Around 2.40 per contract / Price target $700 -> QQQ 700C, premium 2.40, expiry MISSING (arrives in the fill)
Watch TSLA 410 calls 3.25 @everyone                                -> TSLA 410C, premium 3.25, expiry MISSING
Watching AMZN 245c / NVDA 197.5c (again) / META 600c                -> THREE watches from one message
Keeping an eye on the $60 calls July 31                            -> MUST_ASK: no ticker anywhere in scope
I'm eyeing AMZN 260 calls for swing / July 24 expiry /
  0.85-0.90 entry / Currently it's hovering around the $250 level  -> AMZN 260C 2026-07-24, planned premium 0.85-0.90; 250 is SPOT, not a strike
```

Two structural notes:
* **A watch may have a premium and still be a watch.** `2.05`/`2.40`/`3.25` are *planned*
  entry prices. They must be stored as `planned_premium`, not as a fill, and must not start
  shadow P&L.
* **A multi-ticker watch produces N independent watch records**, each separately linkable.
  sulker's three-line watch produced three separate fills 21/23/24 minutes later — at a
  *different strike* for AMZN (245 watched, 247.5 entered).

---

#### `FILL`
The provider is now in a setup they previously posted. Almost always anaphoric: it states a
price and little else.

```
Getting in PLTR here @2.03 @everyone                     -> links to "Watching $PLTR $134 calls" 15m earlier
IN AT 1.30 on NFLX @everyone                             -> links to "Watch NFLX 900c July 10 1.50"
GOOGL pulling back now, 1H/4H/1D look better than AAPL / IN AT 0.93   -> links to "Watch $GOOGL $385c July 10 / 85c per contract"
expiry july 20 im in at 2.95 per contract @everyone /
  $700.70 Price target stop limit 2.20                   -> links to "Watch swings $QQQ $700c"; supplies the MISSING expiry
Filled now @everyone 3.00 for the 705 puts stop loss 2.20 -> links to "Watching QQQ 705 puts expiring tomorrow"
In SMCI july 31 / 1.67 per contract @everyone             -> F1. links to "Watching SMCI 25 calls July 31"
In $AAPL calls at 1.03 stop loss 0.80. 1 contract          -> links to "Watching $AAPL $315 calls July 10 / 1.15 entry" 61m earlier
In 1 contract at 1.91. Price target 208                    -> links to "Watching NVDA 205 calls expiring today. 1.35 entry" 3m earlier
In AMZN calls at 0.90. Stop limit 0.70                     -> links to "I'm eyeing AMZN 260 calls … 0.85-0.90 entry" 52m earlier
```

**A fill overrides the watch on any field it restates.** sulker watched `AMZN 245c` and
entered `AMZN 247.5c`; the fill wins. The fill also *supplies* fields the watch lacked
(`expiry july 20` on the QQQ 700C).

**A fill that restates a wildly different premium is a flag, not a silent link.**
`Watching NVDA 205 calls … 1.35 entry` → `In 1 contract at 1.91` is +41% on the premium in
3 minutes. Plausible (NVDA was running) but also consistent with a strike change. Corpus
labels it `FILL` + `hard: true`; the engine should link **and** raise a price-drift flag.

---

#### `ADD` (scale-in / average-down)
```
added 1.05 @Options Day Trade Alerts                     mike   -> AMBIGUOUS: BABA 110C and NBIS 235C both open. MUST_ASK.
added small MSFT 390c .5                                 mike   -> ADD, fully specified
Avg here.. new avg is 1.10.. manage ur risk guys..       optionking -> ADD, new average price stated, contract anaphoric
SPY 1,08 new avg @Options Day Trade Alerts  im gonna be adding in the red candles   sulker -> ADD, new avg 1.08 (comma decimal!)
new average is 1 @Options Day Trade Alerts               mike   -> ADD, avg 1.00
Back at B/E gonna add more here and swing the rest       jmt    -> ADD, no price
added back mu 1000c 1.7                                  mike   -> ADD, fully specified
add back to tsla call .39 on 417                         mike   -> ADD, TSLA 417C @0.39 (word order inverted)
```
`new avg` semantics: the number is the **new blended cost basis**, not the price paid on
this clip. Treating it as a fill price corrupts the position's P&L.

---

#### `TRIM` (partial exit)
```
31% taking some off here                                 jmt        -> TRIM, size unstated, at +31%
115% gonna take 3 off here have 3 left                   jmt        -> TRIM 3 of 6 at +115%
NBIS 130% 1/2 out                                        mike       -> TRIM half at +130%
tsla 410 3/4 out 3.7                                     mike       -> TRIM 3/4 at premium 3.70
Trimmed @ 5                                              mike       -> TRIM at 5.00, contract anaphoric
AAPL +70% UP, more than half out                         sulker     -> TRIM > half at +70%
50% leaving runners here                                 jmt        -> TRIM most, keep runners
Left runner 2 cons now..                                 optionking -> TRIM to 2 remaining
40% just gonna leave some runners here and swing the rest into the weekend   jmt -> TRIM + hold-overnight declaration
```
A trim is a P&L update **and** a size change. Do not classify as `PNL_UPDATE` — the size
change is the part that matters for edge measurement.

---

#### `EXIT`
```
All out @ 6.2 have a good day                            mike       -> full exit at 6.20
Cut Out at 0.80 per contract @everyone                   prince     -> full exit at 0.80
Sold all NVDA                                            optionking -> full exit, price unstated
Sold all SPY with 150%..                                 optionking -> full exit at +150%
SPY 500% up guys.. sold all                              optionking -> full exit at +500%
Stopped out on runners                                   jmt        -> stop-out of the remainder
Stopped out here @JMoneyAlerts got to greedy             jmt        -> stop-out
stopped out META                                         mike       -> stop-out, ticker stated
Sold NVDA here at 84%                                    prince_small -> full exit at +84%
Cutting @Options Day Trade Alerts                        mike       -> full exit, NOTHING else stated -> anaphora
cutting MSFT .15                                         mike       -> full exit MSFT at 0.15
cutting all plays except spx 7580 meta 675 and tsla 410  mike       -> BULK exit of everything except three named -> hard
Cutting $NVDA @everyone this market is wild              prince     -> full exit NVDA
OUT OF AAPL @everyone                                    prince     -> full exit AAPL, price unstated
Profits locked im out @everyone                          prince     -> full exit, ticker anaphoric (QQQ 700C)
Market up 27 points cons aren't doing a thing. Cutting here at break even (1.05 avg)  mike -> full exit at 1.05
NVDA out                                                 sulker     -> full exit NVDA
all out                                                  mike       -> full exit, everything
```
`cutting all plays except …` is a **bulk** operation over the provider's whole open book.
It is the single most destructive message type to mis-handle, and it is not expressible as
a link to one position. Treat as `EXIT` with `scope: book_except([…])` — or, if the engine
cannot represent that, `MUST_ASK`.

---

#### `STOP_SET` / `STOP_MOVE`
```
sl 2                                                     mike -> stop at premium 2.00 on the position opened 8s earlier (META 660P @3)
added stop at 3.1                                        mike -> stop 3.10 (above the 3.00 entry -> this is a stop MOVE to lock profit)
added stop loss on meta @ 2                              mike -> stop 2.00 on META
Back down 11% I will implement a stop limit at 0.97      prince_small -> stop 0.97 (premium)
set sell order @ 5 for 1/2                               mike -> that is a TARGET_SET (limit sell), not a stop
gonna set 5.00                                           sulker -> TARGET_SET at premium 5.00
```
Stop denomination is the trap — see §4.6.

---

#### `PNL_UPDATE`
By volume the largest class (≈27% of all messages). Zero of them should ever produce a
contract.

```
Up 10% on $PLTR calls @everyone                          prince
303% on $SMCI calls. … My price target for today is $32.16   prince_small   <- F2
124% ON $PLTR CALLS @everyone                            prince
12%                                                      jmt    (bare percentage, ticker anaphoric)
Back down to 30% still holding                           jmt
Down 56% with the gap down gonna hold                    jmt
BABA 100%                                                mike
these are up 300%                                        mike
SPY 2.45 now.. 380% up guys..                            optionking  (2.45 is the CURRENT MARK, not a new entry)
SPX 7520C up 110% guys                                   optionking  (names the contract -> unambiguous link, still not a card)
NVDA 205C up 50% secure some profit guys                 optionking  (names a contract the desk may not have -> orphan P&L)
I'm up 107% on MSFT calls @everyone going to take profits once it hits $400   prince (P&L + TARGET_SET)
WE ARE UP 234% on $SMCI calls @everyone If we break $30 it can go crazy       prince_small (234 is a %, 30 is an underlying level)
IWM +50% UP, only runner for the possible 300 break      sulker (P&L + TRIM-implied + level 300)
Down 20% and close below SL but I'm gonna hold till tomorrow   jmt (P&L + explicit override of the stop)
```

`SPY 2.45 now.. 380% up guys..` deserves a call-out: the pattern `TICKER <decimal>` is
byte-identical to mike's entry shorthand `META 675 2.5`. The disambiguator is the word
`now` plus the accompanying `%` — see §4.9.

---

#### `RECAP`
```
Trade Recap 7/7 / SPX 7495C 2.3 → 6.2 | +169.6% / MSFT 405C 7/10 1.7 → 2.8 | +64.7% / SPX 7500P …
EOD Recap | 7/9/26 / No new scalps posted today. / — Yesterday (7/8) Summary — / BABA 110C: .70 → 1.25 (+79%) …
Recap 6 July 2026 / META 600C (Lotto) @ 0.30 +400% / SPY 750C @ 0.58 +180% / IWM 300C @ 0.62 +50% …
```
Contains full contract specs *in the past tense*. This is the most dangerous "looks exactly
like N entries" class in the corpus. Detect on: the word `recap`/`summary`, a date in the
header, `→`/`->` between two prices, and multiple contracts in one message. Route to
reconciliation, **never** to carding.

---

#### `COMMENTARY`, `WARNING`, `QUESTION`, `CHAT`, `NOISE`
```
COMMENTARY  About this PUTs. 740 is heavy PUT zone for SPY … VIX squeezing above 17!
COMMENTARY  remember SPX 7460 as long as VIX climbs to 17
COMMENTARY  298 key, the bears are there.
COMMENTARY  MSFT can be midday plays for 400 level
COMMENTARY  Monitors off / NVDA --> 200 aim for the rest / MSFT --> 392.5 for add more / IWM --> we need 298 break
COMMENTARY  First 15 has closed.
COMMENTARY  looks like test 7530
COMMENTARY  we fkd up not trading this today this stock is up at 1930 from 1770
COMMENTARY  $9 run in 30 mins is definitely what the higher time frames said
COMMENTARY  We tapped the $134 price target and came right back down.
WARNING     102% and are itm please manage your trade ya on your own
WARNING     Manage ur risk / go small / it is risky
QUESTION    spy put in 1 min anyone?
QUESTION    if anyone wants to buy the 160c lmk
QUESTION    I'm up 42% I'm still holding … we still in?
CHAT        gonna go pray to the porcelian throne for 3 more hours
CHAT        Brb plz dont kill the market
CHAT        why did i buy only 1
CHAT        We just need 1 trade. Just 1.
CHAT        Just to update yall / Around Father's Day I started having terrible migraines …
NOISE       https://klipy.com/gifs/poop-dancing-poop-3
NOISE       https://x.com/SPX_Mike/status/2075644211731267654?s=20
NOISE       Test 2.0
NOISE       75          (bare number, no context)
NOISE       .5          (bare number)
```
`Monitors off / NVDA --> 200 aim / MSFT --> 392.5 for add more / IWM --> we need 298 break`
is the single richest commentary trap: four ticker+number pairs, none of them a contract.
The `-->`/`for`/`we need` construction is level talk.

`looks like test 7530` and `remember SPX 7460` are SPX **levels**, in the same numeric range
as SPX strikes, from the same providers who post SPX strikes. Only the verb saves you.

---

#### `EDIT`
Discord edits arrive as a full re-post with a trailing `(edited)\n<Weekday, Month D, YYYY at H:MM AM>` footer.
```
@everyone SPX 8520C at 2.20 SL 30% strictly.. 0DTE                                    <- original, typo
@everyone SPX 7520C at 2.20 SL 30% strictly.. 0DTE (edited) / Monday, July 20, 2026 at 9:02 AM   <- corrected
@everyone META 660C at 2.50 SL 20% 1DTE
@everyone META 665C at 2.50 SL 20% 1DTE (edited) / Tuesday, July 21, 2026 at 11:57 AM
GOOGL 0DTE 360p .80s   ->   GOOGL 0DTE 360p .75s (edited)
AAPL 0DTE 310p .43 only couple cons for now  ->  … No SL (edited)
META 375C @ 2.9  ->  META 675C @ 2.9 (edited)         <- mike typo'd the strike by 300
```
Two rules: (a) the footer date is **never** an expiry and its digits are never a strike;
(b) an edit re-prices/re-strikes the existing card — it must not create a second position.
`SPX 8520C` never existed; the desk must not hold a phantom.

---

#### `REPOST`
The browser extension re-scrapes the visible message list, so identical texts reappear —
sometimes days later, sometimes in bursts (`2026-07-15T18:30:25` replayed nine jmt messages
in one second). 164 of 706 messages (23%) produced no action at all, mostly for this reason.
Dedupe on `(provider, normalized_text)` with a wide horizon, and on `msg_id` when present.

**Critical corollary:** repost replay means **desk arrival order ≠ provider post order**.
The `Watching SMCI 25 calls` message reached the desk at `2026-07-22T13:30:54`, **17.8 hours
after** the `In SMCI july 31` fill it was supposed to anchor. See §3.5.

---

#### `NON_PROVIDER`
```
CEO [12:16 ET] / Alert: [Chief Financial Officer (CFO)] … 30-day profit factor …
Midday Status — 13:03 ET / AGORA MIDDAY | 1:02 PM ET | Jul 17 | Paper / P&L / Realized -$1,500 …
St. Louis Fed Q2 GDP Nowcast: 2.06%
```
23 messages from an unmapped channel (`provider = ?`). Two of them **carded**. Any message
whose provider does not resolve to a known signal provider must be dropped before the parser
runs.

---

#### `MUST_ASK`
Reserved for messages where a competent human trader, given the full prior context, still
cannot say what the trade is. These are not failures of the engine — emitting `MUST_ASK` is
the *correct* output.

```
added 1.05                        two open positions (BABA 110C, NBIS 235C); 1.05 fits neither cleanly
Keeping an eye on the $60 calls July 31    no ticker in this message or anywhere in the preceding hour
$SPY Limit order for 55 contracts at 1.30 placed.   no strike, no right, no expiry; 55 is a QUANTITY
May enter today                   no ticker, no contract, no antecedent within the window
done .6                           mike, after posting two contracts in the prior 2 minutes
7540p /5                          mike; "/5" is neither a price nor a size in any consistent reading
.5 sell orders                    is .5 a price or half the position?
i said on live add .45            references an out-of-band voice channel the desk cannot see
im in both sides of 148/149 put and call   SPCX straddle; strikes 148/149 never posted with premiums
Avg here.. guys.. manage ur risk  optionking; three positions open, no ticker, no price
75                                bare number
Down 38% on AAPL going to see how the first hourly candle performs   AAPL P&L, but prince_small had no AAPL card open at the time -> orphan
```

---

## 3. Anaphora — when a message refers to a prior message

### 3.1 Definition
A message is **anaphoric** if it asserts a trade action but omits at least one of
`{ticker, strike, right}`. Anaphora is the normal case for `FILL`, `ADD`, `TRIM`, `EXIT`,
`STOP_MOVE` and `PNL_UPDATE` — 199 of 706 messages (28%) routed to the management path.

### 3.2 Measured watch → fill gap distribution

Eleven watch→fill pairs in the corpus where the provider explicitly announced both sides:

| provider | setup | gap |
|---|---|---|
| prince | QQQ 700C | 2.6 min |
| prince_small | NVDA 205C | 2.9 min |
| prince | GOOGL 385C | 9.8 min |
| prince | QQQ 705P | 14.0 min |
| prince | PLTR 134C | 15.2 min |
| sulker | NVDA 197.5C | 21.1 min |
| sulker | AMZN 245C → 247.5C | 22.7 min |
| sulker | META 600C | 24.4 min |
| prince_small | AMZN 260C | 52.0 min |
| sulker | HOOD 119C | 58.0 min |
| prince_small | AAPL 315C | 60.9 min |

```
n = 11   min 2.6   p25 9.8   median 21.1   mean 25.8   p75 52.0   p90 58.0   max 60.9  (minutes)
8/11 within 30 min      11/11 within 61 min
```

And the ones that **crossed a session boundary** — where the fill was never explicitly
announced and only a later P&L or exit reveals it:

| provider | setup | watch → first evidence of a fill |
|---|---|---|
| prince_small | GOOGL 385C → `Sold GOOGL calls as .31` | 22.5 h |
| prince_small | WMT 115C → `STILL looking to get in` | 28.5 h |
| prince | NVDA 210C (`Limit order set`) → `Up 23% on NVDA calls` | 65.6 h (over a weekend) |
| prince_small | WMT 115C → `Wmt calls are up over 120%` | 127.9 h |
| prince_small | SMCI 25C — watch **arrived at the desk** after the fill | −17.8 h (inverted) |

### 3.3 The windows, and why

**W1 — same-session link, ticker named or exactly one candidate: 120 minutes.**
Every explicit gap is ≤ 61 min; 120 min gives ~2× headroom while still being shorter than
half a session, so a window opened at the bell cannot reach into the close. *This is the
window that would have saved F3 (PLTR, 15.2 min).*

**W2 — named-ticker link with matching right: until the watched contract's own expiry,
capped at 5 trading days.**
A swing watch is legitimately live until it expires. `Watchimg NVDA 210 calls July 24 …
Limit order set` posted 07-17 was still a live resting order on 07-20. Justification is the
cross-session table above: 4 of the 5 highest-value links needed >1 session, and the 5-day
cap is set by the longest observed (WMT, 127.9 h ≈ 5.3 trading days) — the cap is the
*observed maximum*, not a safety margin, precisely because a stale link is dangerous.
W2 requires the message to **name the ticker**. `In SMCI july 31 / 1.67` names SMCI, so W2
applies and F1 is recoverable. `added 1.05` names nothing, so W2 never applies to it.

**W3 — bare anaphora (no ticker at all): 30 minutes AND exactly one open position.**
Every recoverable bare-anaphora message in the corpus refers to a position opened in the
preceding half hour: mike's `trimmed 2.25` (same minute), `cutting .75` (7 min), `done 1.7`
(1.5 min), `sl 2` (8 seconds), `added stop at 3.1` (4 min). Beyond 30 minutes with more than
one position open, the referent is genuinely not recoverable and the answer is `MUST_ASK`.

**W4** — never link across providers. Two providers holding the same ticker is common
(prince and prince_small both held AAPL 315C on 07-08).

**W5** — never link to a contract whose expiry has passed, or to a position already closed.

**W6** — the window is measured on the **provider's post time**, not the desk's receive time.

### 3.4 Resolution algorithm

```
candidates = provider's records with status in {WATCHING, OPEN, PARTIAL}
             and expiry >= message_date
             and post_time within the applicable window (W1/W2/W3)

filter by every field the message DOES state:  ticker, right, expiry, strike
filter by premium plausibility:
    the stated price must lie in [0.30x, 4x] of the candidate's last known mark
    (a scalp can double in 3 minutes; it cannot go 10x and stay the same contract)

if len(candidates) == 1  -> LINK. confidence high.
if len(candidates) == 0  -> if the message fully specifies a contract, treat as ENTRY.
                            otherwise MUST_ASK (orphan).
if len(candidates) >  1  -> MUST_ASK. Do not pick.
```

**Recency is never sufficient on its own.** It is a tiebreaker only when the surviving
candidates differ on a field the message *states* (e.g. two open QQQ positions, one call one
put, and the message says "puts"). Picking the most recent of two equally-plausible
positions is how a fill gets attached to the wrong trade, and the desk then reports a fake
P&L on both.

**Worked example of a correct refusal.** mike, 2026-07-08:
```
13:38:17  BABA 110C .7          -> open
13:42:10  NBIS 235C @ 1.8       -> open
13:53:46  BABA 100%             -> BABA now ~1.40
14:00:54  added 1.05
```
`1.05` is 1.5× the BABA entry but 0.75× the BABA current mark, and 0.58× the NBIS entry.
Both survive the plausibility filter. **Two candidates ⇒ MUST_ASK.** The desk currently
guesses; it should not.

### 3.5 Late-arriving antecedents (the repair path)

Because the scraper backfills, a `WATCH` can arrive *after* the `FILL` it anchors. This is
not hypothetical — it is exactly what happened with SMCI.

Required behaviour: when a `WATCH` is ingested whose **provider post time precedes** an
already-carded message from the same provider that is (a) anaphoric, (b) names the same
ticker, and (c) falls inside W2 of the watch — the desk must **re-resolve the existing
card**, not merely record a new watch. A card whose strike came from a low-confidence path
(bare-shorthand, or vetoed by §5) must be re-openable for repair for as long as the position
is live.

Without this, F1 stays broken even after the watch arrives.

### 3.6 Concurrency reality

Providers routinely run 3–6 concurrent positions. Peak observed: mike on 2026-07-10 held
SPX 7580C, META 675C, TSLA 410C, MU 1000c, SNDK 1860P and an SPCX 148/149 straddle
simultaneously, and posted `down to runners on 0dtes` — an untargetable bulk statement over
that whole book. Any anaphora scheme that assumes one open position per provider will be
wrong for mike roughly half the time.

---

## 4. Number disambiguation

This is where the desk fails. Every number in a message has exactly one role. The parser
must assign roles by **consume-once priority**: once a span is claimed for a role, its
digits are blanked and cannot be re-read as anything else. (`signal_engine._mask_dates`
already does this for dates and targets; the principle needs to cover every role.)

### 4.0 The roles

`strike` · `premium` · `underlying_target` · `premium_target` · `stop_premium` ·
`stop_underlying` · `stop_percent` · `expiry` · `quantity` · `pnl_percent` ·
`chart_level` · `clock_time` · `dollar_amount` · `size_fraction` · `noise`

### 4.1 Consumption order (highest priority first)

| # | Role | Trigger | Examples |
|---|---|---|---|
| 0 | strip | edit footers, URLs, `@everyone`, `@JMoneyAlerts…`, `@Options Day Trade Alerts`, emoji | `(edited)\nMonday, July 20, 2026 at 9:02 AM` |
| 1 | `pnl_percent` | `\d+(\.\d+)?%` **not** preceded by `SL`/`stop`/`risk` | `Up 10%`, `303%`, `+169.6%`, `-44.4%` |
| 2 | `stop_percent` | `SL|S/L|stop` + `\d+%` | `SL 20%`, `SL 30% strictly`, `SL 40%` |
| 3 | `quantity` | `\d+ ?(con|cons|contract|contracts|lots)`, `\d+ (off|left)`, `only \d con` | `1 contract`, `2 cons`, `5 cons`, `55 contracts`, `take 3 off here have 3 left` |
| 4 | `size_fraction` | `\d/\d` before `out|off` | `1/2 out`, `3/4 out`, `2/2` |
| 5 | `expiry` | month-name ± day; `m/d`; `m/d/yy`; `NDTE`; `today`/`tomorrow`/`EOD`/`weekly`/`intraweek`/`this week`/`next week`/`Friday` | `july 31`, `7/24`, `JULY 10`, `0DTE`, `1DTE`, `expiring today`, `weekly expiry` |
| 6 | `clock_time` | `\d{1,2}(:\d\d)? ?(am|pm|ET)`, bare `330 PM`, `3 pm`, `First 15` | `3 pm`, `11 AM`, `waiting for 330 PM` |
| 7 | `stop_*` | `SL`, `S/L`, `stop`, `stop loss`, `stop limit`, `risk to` + a number → denomination per §4.6 | `s/l 1.29`, `SL:748.90`, `stop limit 1.80`, `added stop at 3.1` |
| 8 | `*_target` | `price target`, `PT`, `tgt`, `target`, `first price target`, `looking for`, `aim for`, `room (for|till)`, `set sell order @`, `gonna set` → denomination per §4.5 | `PRICE TARGET $38.29`, `PT 7575 7580`, `Price target 208`, `gonna set 5.00` |
| 9 | `strike` | binds to a right marker (§4.2) | `$134 calls`, `751C`, `197.5c`, `410 calls`, `call … on 417` |
| 10 | `premium` | binds to a price marker (§4.3) | `@ 1.81`, `at 0.90`, `1.67 per contract`, `.55`, `85c per contract` |
| 11 | `chart_level` | anything left that is on the underlying's scale, near a level verb | `298 key`, `remember SPX 7460`, `if we break $30`, `>312.5`, `room till 90` |
| 12 | `noise` | everything else | `75`, `.5` with no context |

### 4.2 `strike` — binding rules

A number is a strike **only** if it is bound to a right marker, or matches a whitelisted
provider template.

**S1 — adjacency (primary and strongest evidence).**
```
NNN(C|P)            751C, 740P, 197.5c, 7550C, 12C, 212.5C
$NNN(c|p)           $73c, $36c, $134c, $722, $385c
NNN (call|put)s?    134 calls, 25 calls, 410 calls, 705 puts, 315 call
(call|put)s? … (on|at|strike) NNN   within 20 chars:  "tsla call .39 on 417"
(on|at|strike) NNN … (call|put)s?   within 20 chars
NNN strike
```

**S2 — provider template (only after roles 1–8 have consumed their spans).**
```
jmt          ^(\d{3,4})(C|P)\n(prem)\n(m/d|0DTE)\nSL:(underlying)      ticker := SPY
optionking   TICKER (\d+(\.\d+)?)(C|P) at (prem)
sulker       TICKER (0DTE|1DTE|intraweek)? (\d+(\.\d+)?)(c|p) (prem)
mike         TICKER? (m/d)? (\d+(\.\d+)?)(c|C|p|P)? @? (prem)
chip         TICKER (\d+)(C|P) @ (prem) (N)DTE
```

**S3 — no right marker anywhere ⇒ no strike.** A message with no `c`/`p`/`call`/`put` token
and no provider template has **no strike**. It is either anaphoric or it is not a trade.
This alone kills F2: `303% on $SMCI calls … price target for today is $32.16` — `calls` is
present but `32.16` is not adjacent to it *and* is already consumed as a target at priority 8.

**S4 — never take a strike from a consumed span.** Kills F1 (`july 31` consumed at
priority 5), F2 (`$32.16` consumed at priority 8), and the three live bugs below.

**S5 — plausibility band.** For DTE ≤ 10, `|strike/spot − 1| ≤ 0.25` covers every legitimate
strike in the corpus. Outside the band, require S1 adjacency **and** chain existence (§5) —
do not accept a template match.

**S6 — anaphoric strike.** A right marker with no adjacent number means the strike lives in
a prior message. Resolve per §3, or emit `null`. **Never synthesize.**

**Live bugs S4 fixes** (all three are in `channels/prince/live_signals.jsonl` today):
```
"In AAPL 325 put expiring today / 1.64 entry / $321 Price target"
   carded AAPL 321P     truth AAPL 325P    (321 is the target)
"Watchimg NVDA 210 calls July 24 / 2.10 entry and 1.70 stop loss / Price target $212"
   carded NVDA 212C     truth NVDA 210C    (212 is the target)
"In $QQQ 705 calls tomorrow expiry / 2.10 / Price target $704 today"
   carded QQQ 704C      truth QQQ 705C     (704 is the target)
"I'm eyeing AMZN 260 calls … 0.85-0.90 entry / Currently it's hovering around the $250 level"
   carded AMZN 250C     truth AMZN 260C    (250 is spot)
```
In all four the correct strike is *adjacent to the right word* and the wrong one is not.
Adjacency beats every other heuristic.

### 4.3 `premium` — binding rules

**P1 — magnitude.** Equity/ETF options in this corpus: `0.05 – 6.00`. SPX: up to `~6`.
Absolute bound `0.01 – 60`. A 3+ digit integer is never a premium.

**P2 — form.** A leading-dot decimal is *always* a premium: `.7`, `.55`, `.14`, `.19`, `.4-.5`.
No strike is ever written that way.

**P3 — price markers.** `@`, `at`, `for`, `per contract`, `each`, `entry`, `fill`, `filled`,
`limit buy`, `avg`, `cost`.

**P4 — shorthand pairing.** In `TICKER NNN PREM` (mike) the strike is the integer/half-integer
with ≥ 2 digits and the premium is the decimal `< 100` that follows: `META 675 2.5`,
`NVDA 212.5 .14`, `meta 672 1.7`, `TSLA 417.5 .35`.

**P5 — ranges.** `0.85-0.90`, `.80 - .70`, `.4-.5` ⇒ record both; plan against the worse
(higher) fill.

**P6 — `85c` is 85 cents, not an 85-strike call.** Prince used both forms in the same
message pair. Disambiguator: `\d{1,3}c` followed by `per contract` / `each` / `cents`, or
whose value is implausible as a strike for that ticker, is a **premium**.
```
Watch $GOOGL $385c July 10 / 85c per contract       -> strike 385, premium 0.85
```

**P7 — a premium is never equal to the strike**, and never equal to a number already
consumed as a target, stop, or level.

**P8 — comma decimals.** sulker writes `SPY 1,08 new avg`. `\d,\d\d` in a price position is
a European decimal → 1.08.

**P9 — the "for" trap.** `Filled now @everyone 3.00 for the 705 puts stop loss 2.20` — the
desk carded premium **2.20** (the stop) instead of **3.00**. `for` binds the premium to the
contract that follows it; `stop loss` at priority 7 must consume 2.20 *before* the premium
scan runs.

### 4.4 `expiry`

```
july 31 | JULY 10 | July 24 expiry | 17 July | 3/17 | 7/8 | 7/24 | 07/17
0DTE | 1DTE | ODTE (typo'd zero) | expiring today | expiring tomorrow | today | tomorrow
weekly expiry | this week | intraweek | next week | for Friday | eod | EOD
```
* **A date's digits can never be a strike.** F1.
* `weekly expiry` / `intraweek` / `this week` / `Friday` ⇒ the Friday of the message's week.
* `next week` ⇒ the following Friday. `expiring next week` (prince, `AMZN 260c`) is a
  *range* of one week — resolve to that Friday.
* `0DTE` on a non-Friday for SPY/QQQ/SPX/IWM is a real daily expiry; for a single name it
  usually means the nearest Friday. This distinction has bitten: sulker's
  `NVDA 0DTE 197.5c` on a Monday means the Friday chain.
* **Genuinely ambiguous expiry is real.** `Watching SMCI 25 calls July 31 / May even go this
  week expiry` states two candidate expiries. Leave `null` on the watch; the fill
  (`In SMCI july 31`) resolves it.
* An **edit footer** date (`Monday, July 20, 2026 at 9:02 AM`) is never an expiry.

### 4.5 `target` — underlying vs premium

| Form | Denomination | Examples |
|---|---|---|
| `price target $X` / `PRICE TARGET $X` / `Price target X` | **underlying** | `$719.89`, `$38.29`, `$389.90`, `208`, `$134` |
| `first price target … area` | underlying | `that 25.66 area off the 1H` |
| `PT n n` (optionking) | underlying | `PT 7575 7580`, `PT 7560 8570` |
| `set sell order @ X`, `gonna set X`, `looking for X` on a premium scale | **premium** | `set sell order @ 5 for 1/2`, `gonna set 5.00` |
| `room (for\|till) X`, `aim for X`, `looking for X-Y` on the underlying scale | underlying level | `room for 120`, `room till 90`, `looking for 300-302` |

Decision rule when the label is absent: compare the number's scale to the **strike**. Within
±15% of the strike ⇒ underlying. Within the premium band and < 20% of the strike ⇒ premium.
This is unambiguous for every case in the corpus because strikes and premiums differ by
1–3 orders of magnitude.

`My price target for today is $32.16` — labelled, underlying, and **must not be a strike**. F2.

### 4.6 `stop` — three denominations

| Form | Denomination | Real example | Meaning |
|---|---|---|---|
| `SL n%` | **percent of premium** | optionking `SL 20%`, `SL 30% strictly` | exit at 0.80× / 0.70× entry |
| `SL: nnn.nn` where the value is near **spot** | **underlying** | jmt `751C … SL:748.90` (SPY ≈ 750) | exit when SPY trades 748.90 |
| `s/l n.nn` / `stop limit n.nn` where the value is near the **premium** | **premium** | prince `In $AMD $73c July 8 @1.81 / s/l 1.29` | exit at 1.29 per contract |

**The disambiguator is magnitude, never the keyword.** jmt and prince both write "SL"; jmt
means an SPY price 500× larger than the premium. Test: `|stop − premium| / premium < 1` ⇒
premium-denominated; `|stop − strike| / strike < 0.10` ⇒ underlying-denominated. In the
corpus these two never overlap.

Edge: `added stop at 3.1` on a position entered at 3.00 is a stop **above** entry — a
profit-lock move, not a mis-parse. Do not veto a stop that exceeds entry.

### 4.7 `quantity`
```
1 contract | 2 cons | 5 cons | 1 con only | only 1 here | 55 contracts | taking 1
take 3 off here have 3 left | Left runner 2 cons | lotto 1con | size 0 (= smallest)
```
`$SPY Limit order for 55 contracts at 1.30 placed.` — 55 is a quantity, 1.30 is a premium,
and there is **no strike and no right**. The desk logged `MISS?`. Correct label: `MUST_ASK`.

`lotto size 0`, `size small`, `go small`, `low size`, `starter` are **size qualifiers**, not
quantities, and not part of the contract.

### 4.8 `pnl_percent` vs everything
A bare `\d+%` at the head of a message from jmt/optionking/mike is *always* P&L on the open
position: `12%`, `31% taking some off here`, `102% and are itm`, `380% up guys`. It is never
a stop and never a size. The only `%` that is not P&L is one attached to `SL`/`stop`/`risk`
(priority 2, consumed first) or a sizing statement (`recommend 25% if u need`).

### 4.9 `chart_level` — the residue, and why it must stay residue
Numbers that survive to priority 11 are levels. They are the largest single source of
phantom strikes because they are on the same scale as strikes and appear next to tickers.
```
298 key, the bears are there.                      IWM level, not a 298 strike
remember SPX 7460 as long as VIX climbs to 17      SPX level 7460 and a VIX level 17
looks like test 7530                               SPX level
MSFT --> 392.5 for add more                        MSFT level
NVDA --> 200 aim for the rest                      NVDA level
we have room till 90 (DINO)                        underlying level
>312.5 (AAPL)                                      trigger level
If we break $30 it can go crazy (SMCI)             underlying level
this stock is up at 1930 from 1770 (SNDK)          two underlying prices
Currently it's hovering around the $250 level      SPOT, and it became a phantom AMZN 250C
if hourly closes below my SL                       no number at all
```
Level verbs to key on: `break`, `above`, `below`, `over`, `under`, `hold`, `test`, `key`,
`room (for|till)`, `-->`, `aim`, `level`, `area`, `zone`, `support`, `resistance`,
`hovering around`, `we need`, `as long as`.

### 4.10 `ticker`

**K1** `$TICK` → ticker. **K2** ALL-CAPS 1–5 letters not in the stopword list → ticker.
**K3** lowercase → ticker only when a strike/premium is adjacent and it matches a known
symbol (`tsla 420C .66`, `qqq 719C @ .25`, `meta 665P @ 2`, `spy 755p .15`).
**K4** never promote these lowercase words to tickers:
`now, in, at, on, so, all, out, it, be, is, if, or, an, are, for, one, two, up, down, next,
gap, key, cut, run, hold, eod, dte, pt, sl, b/e, avg, itm, otm`.
> `Filled now @everyone 3.00 for the 705 puts` carded ticker **NOW** (ServiceNow). The word
> was lowercase and sentence-medial. This is the single cheapest fix in the spec.

**K4-exception:** the same tokens uppercase *with an adjacent contract* are legitimate
tickers — `Watching SO breakout. 100c intraweek` (Southern Co), `Watching DINO for CALLs`.
Require CAPS **and** (`$` prefix or an adjacent strike/right) to override the blacklist.

**K5 implicit ticker by provider:** jmt ⇒ SPY always. mike/optionking/sulker: a bare 4-digit
strike in 4000–9999 ⇒ SPX. mike also posts bare 3-digit SPX strikes (`7495c`) — 4-digit rule
covers those.

**K6 typos** (edit distance ≤ 1 against the known-symbol list, only with an adjacent
strike+premium): `msttr`→MSTR, `spacx`/`SPCX`, `Avgo`→AVGO, `sndk`→SNDK, `Watchimg`→watching,
`ODTE`→0DTE, `callls`→calls, `AAPL.330C`→AAPL 330C.

**K7 SPCX / SPACX** appears as a real symbol in this corpus with strikes 149–180. It is
*not* a typo of SPX. Never coerce a 3-digit-strike SPCX into SPX.

---

## 5. Model-free sanity vetoes

These run **after** the parse and **before** the card. Any veto ⇒ do not card; either
re-resolve via anaphora or ask. They need no model and no judgement — just the chain and
the spot.

| ID | Veto | Rationale / real case |
|---|---|---|
| **V1** | **Strike granularity.** Strike must be a multiple of 0.50 (or 1 / 2.50 / 5 / 25 as the chain dictates). | `SMCI 32.16C` fails on arithmetic alone, before any network call. Cheapest possible veto for F2. |
| **V2** | **Strike exists on the chain** for that expiry. | IBKR: `SMCI 32.16 C 20260731 -> contract not found`. SMCI ladder is 0.50-wide. |
| **V3** | **Expiry is a listed expiry.** | `july 31` must resolve to `20260731`, which is listed. A weekend or a non-listed date ⇒ veto. |
| **V4** | **Intrinsic floor.** Call: `premium ≥ (spot − strike) − tol`. Put: `premium ≥ (strike − spot) − tol`. `tol = max(0.05, 0.02 × spot)`. | An `SMCI 25C` with spot 31.41 has 6.41 of intrinsic and **cannot** cost 1.67. This is the veto that tells you the *spot has moved*, i.e. the watch price is stale — not that the strike is wrong. Direction matters: it fires on the truth as well as on the error, so it must trigger a *re-quote at the message's own timestamp*, not an unconditional reject. |
| **V5** | **Price mismatch vs live mid.** `abs(stated − mid) / max(mid, 0.05) > 0.6` ⇒ the contract resolved is not the contract named. | Caught F1 in production: stated 1.67 vs `SMCI 31C` mid 0.21 (−87%). Already implemented; keep it. |
| **V6** | **Moneyness band.** DTE ≤ 7 and `abs(strike/spot − 1) > 0.30` ⇒ veto unless the message says `lotto`. | Guards against a level or a target promoted to a strike. |
| **V7** | **Number reuse.** The same numeric token may not fill two roles. | `In AAPL 325 put … $321 Price target` — 321 is a target, so it cannot also be the strike; 325 is bound to `put`, so it cannot also be a target. |
| **V8** | **P&L structural veto.** A message classified `PNL_UPDATE`/`RECAP`/`COMMENTARY` that nonetheless produced a strike/premium/expiry ⇒ the classification or the extraction is wrong. Drop and log. | F2. |
| **V9** | **Premium ≤ strike**, and `premium ≤ 0.40 × spot` for DTE ≤ 30. | Catches a strike/premium swap. `SMCI 32.16C @ 32.16` violates both. |
| **V10** | **Ticker qualifies** at the broker, **and** was not derived from a lowercase stopword. | The `NOW` bug. |
| **V11** | **Right vs. language.** `put`/`p`/`puts` in the message and `right = C` (or vice versa) ⇒ veto. | Prince's `In AAPL 325 put … Up 22% on $AAPL Calls` (one minute later, then corrected to `PUTS`) shows providers self-correct; the desk should not have to guess. |
| **V12** | **Stop sanity.** A premium-denominated stop must be > 0 and < 3× premium; an underlying stop must be within ±20% of the strike; a percent stop within 5–90%. | Catches a target read as a stop. |
| **V13** | **Freshness.** If the message's post time is more than 30 minutes before ingest, quote the chain **as of the post time** (or mark the card `stale-quote`) before applying V4/V5. | The repost/backfill path routinely delivers hours-old messages; V4/V5 against a current spot will veto correct parses. |

**Veto is not the same as reject.** A V4/V5 hit on an anaphoric message means "the strike
you used is probably wrong" — the correct response is to run §3 resolution again with the
strike unbound, not to throw the message away. F3 was lost by throwing the message away.

---

## 6. Confidence and escalation

| Confidence | Condition | Desk behaviour |
|---|---|---|
| ≥ 0.90 | Every field read from this message via S1 adjacency or a provider template; all vetoes pass. | Card automatically. |
| 0.70–0.89 | One or more fields resolved from context, exactly one candidate, all vetoes pass. | Card, flagged `resolved-from-context`, show the antecedent on the card. |
| 0.40–0.69 | Fields resolved from context with a price-drift flag, or a veto that a re-quote cleared. | Card as `PENDING`, ping the operator, do not size it. |
| < 0.40 | > 1 candidate antecedent, or a required field unresolvable. | `MUST_ASK`. No card. |

`MUST_ASK` is a first-class, correct output. The corpus contains 20+ of them and any engine
that labels them confidently is *worse* than one that asks.

---

## 7. Known limits of this spec

Stated plainly, because the corpus is the ground truth everything else is measured against.

1. **`ENTRY` vs `WATCH` on prince's "swinging/limit buy" prose is genuinely soft.** Prince
   says `Swinging $HIMS $36c / 2.15 limit buy` and never confirms a fill; two days later he
   posts `Up 45% on $HIMS calls`. Both labels are defensible. Corpus marks them `hard`.
2. **The 5-day W2 cap is fitted to a 12-session sample.** WMT at 127.9 h is the single
   longest observation, and the cap equals it. If a provider swings 2+ weeks the cap is
   wrong. Revisit after 60 sessions.
3. **`0DTE` on single names** is inferred, not stated. sulker's `NVDA 0DTE 197.5c` posted on
   a Monday almost certainly means the Friday chain, but he may mean a genuine daily where
   one is listed. Medium confidence.
4. **Bulk operations** (`cutting all plays except spx 7580 meta 675 and tsla 410`,
   `down to runners on 0dtes`, `2/2 both over 100%`) have no field-level representation here.
   They need a book-scoped action verb the current card model does not have.
5. **Out-of-band references** (`i said on live add .45`) reference a voice channel. Not
   recoverable at any confidence. Always `MUST_ASK`.
6. **Provider self-contradiction within a minute** (`Up 22% on $AAPL Calls` → `Up 22% on
   $AAPL PUTS`) means even the label "what the provider meant" is unstable. The corpus
   labels the *corrected* reading and marks the pair `hard`.
7. **36 of the 366 corpus messages are still truncated at ~120 chars** because the log
   truncated them and no full copy survives in `channels/` or the DB. They carry
   `text_truncated_in_log: true`. None of them is load-bearing for a strike/premium
   decision, but an engine scored on them is being scored on partial input.

---

## 8. The corpus: `eval/routing_corpus.jsonl`

366 labelled messages, all verbatim from `logs/inbound.log` (full text recovered from
`channels/*/live_signals.jsonl` and `signal_desk.db` where the log truncated).

### Schema (one JSON object per line)

| field | meaning |
|---|---|
| `id` | `<provider>-<NNNN>` where NNNN is the message's ordinal in `logs/inbound.log` |
| `provider` | as resolved by the desk (`?` = unmapped, i.e. `NON_PROVIDER`) |
| `ts` | timestamp exactly as logged (the first ~12 messages are a local-time backfill with no date) |
| `text` | the message, verbatim |
| `prior_context` | up to 4 preceding messages **from the same provider**, `{id, ts, text}` |
| `type` | one of §2.1 |
| `should_card` | should the desk create a NEW tracked record (an entry or a watch)? |
| `links_to` | id of the message this one refers to, or `null` |
| `expect` | `{ticker, strike, right, expiry, premium}` — the **correct** values. `null` means *must be null* (either forbidden for the type, or genuinely unresolvable). |
| `expect_all` | present only on multi-contract messages (sulker's three-ticker watch): the full array of contracts the message should produce |
| `why` | the SME's reasoning, including the live-bug reference where one exists |
| `hard` | genuinely tricky — ambiguous referent, a number in two roles, a typo, a veto case, or a shape the desk currently gets wrong |
| `desk_action_observed` | what the live desk actually did (`CARD`, `REJECT`, `MGMT`, `NONE`, …) — for diffing against today's behaviour |
| `text_truncated_in_log` | the full message was not recoverable |
| `arrival_order_inverted` | the antecedent reached the desk *after* this message (SMCI only) |

### Distribution

```
366 records   220 hard (60%)   141 should_card   147 linked

ENTRY 123 | PNL_UPDATE 51 | COMMENTARY 34 | WATCH 26 | EXIT 23 | MUST_ASK 22 | TRIM 18
REPOST 15 | FILL 15 | RECAP 7 | EDIT 7 | ADD 5 | NOISE 5 | CHAT 5 | NON_PROVIDER 4
QUESTION 2 | STOP_SET 2 | TARGET_SET 1 | STOP_MOVE 1

mike 93 | prince 83 | sulker 61 | jmt 46 | optionking 40 | prince_small 37 | ? 4 | chip 2
```

Hard cases by type: ENTRY 60, COMMENTARY 26, PNL_UPDATE 26, MUST_ASK 22 (all), WATCH 22,
FILL 15, TRIM 11, EXIT 9, EDIT 7, REPOST 6, RECAP 4, ADD 4, NON_PROVIDER 3, STOP/TARGET 4,
QUESTION 1.

### Scoring guidance

Not all errors cost the same. Suggested weights when measuring an engine against this file:

| error | severity | why |
|---|---|---|
| carding a wrong **strike** or **right** | **critical** | this is the failure that loses money (F1, F2, and the four live bugs in §4.2) |
| carding anything from a `PNL_UPDATE` / `RECAP` / `COMMENTARY` / `NON_PROVIDER` | **critical** | mints a position out of nothing (F2) |
| dropping a `FILL` that had a linkable antecedent | **high** | F3 — the real trade is lost while a stale watch is tracked |
| dropping a `WATCH` because a field is null | **high** | F4 — destroys the anchor a later fill needs |
| linking to the **wrong** open position | **high** | corrupts two positions at once |
| emitting a confident label where the corpus says `MUST_ASK` | **high** | a confident wrong answer is worse than a question |
| emitting `MUST_ASK` where the corpus has a confident label | **low** | costs the operator five seconds |
| wrong premium with the right contract | **medium** | recoverable by re-quoting the chain |
| `ENTRY` vs `WATCH` confusion on the messages flagged `hard` | **low** | genuinely soft (§7.1) |

An engine that scores well by never asking is not doing well. Track the `MUST_ASK` recall
and the false-confidence rate as first-class metrics.

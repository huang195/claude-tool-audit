# claude-tool-audit

Price three ways to cut your Claude Code bill **without changing anything the
model sees.** Same model, same full context, byte-identical prompts — only the
price paid for carrying that context changes.

```
python3 claude-tool-audit.py            # the three levers, priced against your history
python3 claude-tool-audit.py --report   # + a paste-able block of aggregate numbers
python3 claude-tool-audit.py --measure  # + verify lever 1 with 2 API calls
python3 claude-tool-audit.py --apply    # + switch off the tools you never call
python3 claude-tool-audit.py --verbose  # + every tool, + lever-2 sensitivity sweep
```

No dependencies beyond `python3`. Single file.

## Why

Measured over one developer's full 30-day history — 56 cache lineages, 4,470
requests, 1.12 B tokens, $1,108 at Opus list — **88% of the money went on writing
and re-reading context that had already been sent.** New input was 1.3% of spend;
output was 10.7%.

| component | share of spend |
|---|---|
| cache **read** | 47.1% |
| cache **write** | 40.9% |
| output | 10.7% |
| uncached input | 1.3% |

The counter-intuitive part: a **93% cache-hit rate** sounds excellent and is what
most people have. But hit rate is counted in tokens and the bill is not. **A cache
write bills 12.5× a cache read**, so the 6.5% of input tokens that get *written*
cost nearly as much as the 93.5% that get *read*.

That means the big levers are not "say less" or "use a cheaper model" — both of
which cost you answer quality. The big levers are about not paying twice for the
same bytes.

## The three levers

| | lever | reference machine | how you get it |
|---|---|---|---|
| 1 | drop tool schemas you never call | **5.2%** | one list in `settings.json`, today |
| 2 | refresh the prompt cache before it expires | **+12.1 pts** | a proxy in the request path |
| 3 | batch independent tool calls | **+4.1 pts** | a `CLAUDE.md` instruction |
| | **compounded** | **21.4%** | |

None of the three changes the model, the context, or the information available to
it. Your own numbers will differ — that is the point of running it.

Lever 1's ceiling sits well above the 5.2% this script offers by default, and who
owns the policy decides where in that range you land — see the ladder below.

### Lever 1 — tool schemas you never call

Claude Code sends the full JSON schema of **every declared tool on every request**.
The manifest sits ahead of the system prompt and the conversation, and is re-sent
and re-billed every turn whether the tool is called or not. Off the wire on a
headless `claude --print "say ok"` in an empty directory: **34,851 of 39,216
prompt tokens — 89% of the request — were tool schemas.**

Restricting a tool actually removes its schema from the wire; it does not merely
block execution. Verified by capturing the real requests three ways:

| lever | effect on the wire |
|---|---|
| `permissions.deny` in `settings.json` | removed exactly the named tools |
| `--disallowedTools <names>` | removed exactly the named tools |
| `--tools <explicit list>` | removed exactly the 22 others |
| `--strict-mcp-config` | removed exactly the MCP tools |

So the saving is real. The script finds which tools *you* have never called,
prices them against *your own* history, and offers to deny them.

#### How much is on the table depends on who owns the list

`settings.json` gets you exactly one global decision: which tools you never called
in the whole window. Something sitting in the request path can decide per session.
Priced on the same 30 days:

| policy | lever 1 | who can do it |
|---|---|---|
| the 9 tools flagged safe — the default here | **5.2%** | `settings.json` |
| all 14 never called in 30 days | **6.8%** | `settings.json --include-check-first` |
| strip per session at the first cold start | **7.4%** | a proxy |
| per-session oracle, knows the future | 8.0% | nobody |
| per-request oracle, the upper bound | 8.5% | nobody |

**The ceiling is arithmetic, not policy.** The strippable manifest is 30,090 tokens
against a mean context of 251,538 — **12.0% of context**. Delete every tool and you
save 12%; only 11 of 25 were ever called, so ~8.5% is all the dead ones are worth.
Anything beyond that has to come from Model Context Protocol (MCP) schemas, which
this script cannot size.

Two things make the dynamic version worth more than its extra 1.2 points suggest.
It needs **no per-developer action**, so a team-wide saving is not multiplied by the
fraction of people who actually maintain a list. And it can afford to be aggressive
**because it can recover**: this script deliberately holds back 5 tools worth 5,267
tokens — **1.6 points left on the table purely because a static list cannot undo a
bad guess.**

**Timing matters more than the list does.** The manifest sits first in the request,
so changing it invalidates the entire prefix. Measured on the reference machine,
changing it at an arbitrary request costs **$11.61** in full re-writes — a fifth of
the lever. Changing it at a cold start costs **nothing**, because the write was
happening anyway. Only something watching the request stream knows when that
moment is, and it is the same state lever 2 needs.

### Lever 2 — cache expiry

The prompt cache lives **5 minutes**. Pause longer and the next request
**re-writes** the whole conversation at $6.25/M instead of **re-reading** it at
$0.50/M. A refresh request re-reads the same bytes and generates nothing, so the
model's view is byte-identical — only the price differs, by 12.5×.

The waste is extremely concentrated. On the reference machine, requests following
a >5-minute idle gap were **4.8% of all requests** but carried **77.7% of all
cache-write tokens.**

**The policy must be adaptive, and that is the whole story.** Breakeven is
`6.25 / 0.50` = 12.5 refreshes ≈ **50 minutes of idle**: refresh a short gap, let
a long one expire. Deciding per gap from the context size and the gap length is
what makes this safe, and `--verbose` shows why it is not a tuning question:

| refresh interval | adaptive (decide per gap) | blanket (refresh everything) |
|---|---|---|
| 120 s | **+7.2%** | −336% |
| 180 s | **+10.2%** | −215% |
| 240 s | **+12.1%** | −154% |
| 270 s | **+12.9%** | −133% |
| 290 s | **+13.4%** | −122% |

Adaptive saves money at every interval. Blanket is catastrophic at every
interval — 2.2× to 4.4× the baseline bill. **Do not implement the blanket form.**

Nothing in Claude Code does this today; it needs a component in the request path
that retains the last request body per session and re-issues a minimal version on
a timer. That is why lever 2 is the one number in the report you cannot act on
locally — it is there to size the work.

### Lever 3 — batch independent tool calls

**83.3% of tool-bearing requests issue exactly one tool call**, and every request
re-reads the entire context regardless. Total tool calls are fixed, so raising
calls-per-request removes whole round trips — same work, same results, and lower
latency, so this one improves the experience rather than trading against it.

Two things stop a pair of calls being merged, and the second is easy to miss:

- **data dependency** — the second call's arguments come from the first's output;
- **ordering dependency** — batched calls may execute concurrently, so the second
  cannot require the first's side effect to have landed.

How loose that makes the bound was measured, not assumed. Walking the reference
machine's history for runs of consecutive single-call requests and classifying each
adjacent pair:

| adjacent pair | count | share |
|---|---|---|
| ambiguous: shell, at least one mutates | 1,514 | 53.7% |
| ambiguous: everything else | 581 | 20.6% |
| dependent: writes a target it had to inspect | 325 | 11.5% |
| independent: two read-only shell commands | 258 | 9.2% |
| independent: two look-ups in a row | 139 | 4.9% |

Only **397 of 2,817 pairs are provably independent** — 9.0% of all requests. The
default target of 1.5 calls/request needs 729 merges, so it assumes roughly **1.8×
more batching than is demonstrable.** Pass `--batch-target 1.367` for the measured
floor; that is where the 4.1% in the table above comes from, against 7.5% at the
default target.

**This lever is self-measuring, which is the useful part.** The report prints your
mean calls per request. Add the instruction, re-run in a week, and see whether 1.23
moved. No prediction needed.

The instruction itself:

```markdown
## Batch independent tool calls

When you need several pieces of information and none depends on another's result,
issue all the calls in ONE message rather than one per turn. Every request
re-reads the entire conversation, so three serialized reads cost three full
context re-reads; batched, they cost one.

Batch: reading several known files, several independent greps, `git status` +
`git log` + `git diff`, checking several unrelated services.

Do NOT batch when the next call's target comes from the previous call's output,
or when one command's side effect must land before the next runs — batched calls
may execute concurrently.
```

## What it does

**1. Offline transcript scan.** Reads `~/.claude/projects/**/*.jsonl` and builds
three things, deduplicating on ids — transcripts repeat records across session
resume and context compaction, so counting rows inflates every total:

- tool calls per tool, deduplicated on each `tool_use` block's own id;
- per-request token counts, deduplicated on each assistant message's id, grouped
  into **cache lineages** keyed on session id and subagent flag (a subagent runs
  on its own prompt prefix, so its requests can never warm the main
  conversation's cache — merging them would hide idle gaps);
- tool calls per assistant message, unioned across records, because a message
  streams across several transcript records and the one carrying `usage` is often
  not the one carrying the `tool_use` blocks. Getting that wrong undercounts by
  about 4×.

**2. Lever arithmetic.** Levers 2 and 3 replay your real per-request token counts
under a different policy, applied cumulatively so the percentages add. That is
arithmetic on measured data, not a simulation — but it assumes the policy behaves
as specified.

**3. `--measure` (optional).** Two `claude --print --output-format json` calls in
a temp directory with the same prompt, one with the deny list applied. The
prompt-token delta is exactly what those schemas cost you per turn, on your Claude
Code version. The measured figure then replaces the estimate everywhere. No proxy,
no credentials handled.

**4. `--report`.** Prints a compact block of **aggregate numbers and built-in tool
names only** — no paths, project names, branches, prompts, or MCP server names —
so results can be pooled across a team without leaking anything. It is printed to
your terminal for you to read before you send it anywhere.

**5. `--apply`.** Backs up `settings.json`, shows you the exact additions,
prompts, then writes **only** `permissions.deny`. Everything else in the file is
left alone. Undo is one line.

## Sample output

It opens with the answer, then shows the work. Abridged:

```
THE SHORT VERSION
-----------------
  Over the last 30 days you spent $1,106.86 across 4,460 requests
  (1,123,205,837 tokens), priced at the rates below. 88.0% of that went on
  writing and re-reading context you had already sent -- not on new input,
  and not on output.

  Three changes would recover $236.57 (21.4% of the bill) WITHOUT changing
  the model, shrinking the context, or hiding anything from it. The model
  sees byte-identical prompts in every case.

    cumulative                                      cost    saved
    your last 30 days                          $1,106.86        -
    + 1. drop never-called tool schemas        $1,049.80    5.2%
    + 2. refresh cache before it expires         $916.09   17.2%
    + 3. batch tool calls (1.23 -> 1.37/req)     $870.28   21.4%

  Lever 1 you can switch on today. Lever 3 is a CLAUDE.md instruction.
  Lever 2 needs something in the request path -- a proxy -- because Claude
  Code has no way to refresh its own cache.

WHERE THE MONEY WENT
--------------------
                             tokens       cost   share
    cache read        1,043,231,773    $521.62  47.1%
    cache write          72,436,594    $452.73  40.9%
    output                4,741,254    $118.53  10.7%
    uncached input        2,796,216     $13.98   1.3%

LEVER 1 -- TOOL SCHEMAS YOU NEVER CALL  (5.2%, switch on today)
---------------------------------------------------------------
    tool                tokens      cost   what it is
    Workflow             7,924    $30.77   runs multi-agent workflows (opt-in by keyword)
    DesignSync           3,287    $12.76   syncs a component library to claude.ai/design
    ScheduleWakeup       1,809     $7.03   paces /loop when you run it without an interval
    ...

LEVER 2 -- CACHE EXPIRY  (12.1%, needs a proxy)
-----------------------------------------------
    requests after an idle gap over 300s   214 of 4,460  (4.8%)
    their share of your cache-write tokens               77.7%
    gaps where refreshing is the cheaper move 121 of 214  (56.5%)
    refresh requests that would be added  409
    your cache-write tokens                72,436,594 -> 33,614,368  (-54%)

LEVER 3 -- ONE TOOL CALL PER REQUEST  (4.1%, a CLAUDE.md line)
--------------------------------------------------------------
    calls in request     count    share
    0                      393    8.8%
    1                    3,386   75.9%
    2                      547   12.3%
    ...
    target calls/request     requests cut     of all
    1.5                             729     16.3%
    2.0                           1,564     35.1%
```

Then `WHAT YOUR HISTORY SHOWS` — including spend concentration, which is usually
the most actionable line in the whole report (**4 of 56 cache lineages were 85.6%
of the bill; the median lineage was $0.24**, so none of this needs to change how
you work day to day) — an `MCP SERVERS` section, a `HOW THESE NUMBERS WERE MADE`
footer, and the exact `settings.json` snippet if you would rather paste it by hand.

With `--measure` it adds a block that replaces the estimate with ground truth:

```
MEASURING LEVER 1 AGAINST THE REAL API
--------------------------------------
    every tool on         39,240 tokens in the request
     9 tools off          23,125 tokens in the request
    ----------------------------------------------
    you save              16,115 tokens, every request
                           41.1% of that request

  The estimate below said 16,119 tokens, so measured / estimated = 1.00.
  The measured number is used from here on.
```

The estimate and the wire delta agreed to 0.03% there. Don't read that as
precision guaranteed for you — it is one machine on one Claude Code version, and
it is why `--measure` exists.

## Reporting back

If you are running this as part of a team measurement, run:

```
python3 claude-tool-audit.py --measure --report
```

and send back the block under `PASTE THIS BACK`. It is JSON containing request
and token counts, the four component shares, the three lever percentages, the
never-called tool names, cold-start concentration, mean tool calls per request,
and the prices used. **No paths, project names, branches, prompts, or MCP server
names.** Read it before you send it — it is all on screen.

**The single biggest open question is variance between developers**, not any
individual number. On one machine, lever 2 ranged from 65.9% to 81.2% of write
tokens between a single session and thirty days, so it will vary more between
people. That is what pooling these blocks answers.

## Privacy

It reads **tool names, `tool_use` ids, timestamps, session ids, and token counts**
from your transcripts. It never reads prompts, tool arguments, tool results, or
completions. Transcripts also contain your working directory, git branch, and last
prompt; none of those are read into the report or the `--report` block.

It sends **nothing anywhere** unless you pass `--measure`, which makes exactly two
`claude --print` calls with the fixed prompt `say ok`. `--apply` only edits your
local `settings.json`, after a backup. `--report` prints to your terminal; where
it goes after that is your decision.

## Caveats, stated up front

- **Levers 2 and 3 are counterfactuals.** They replay your real per-request token
  counts under a different policy — arithmetic on measured data, not a simulation,
  but they assume the policy behaves as specified.
- **Lever 3's default target is an upper bound; `--batch-target 1.367` is the
  measured floor.** Only 397 of 2,817 adjacent single-call pairs on the reference
  machine were provably independent, so the default 1.5 assumes about 1.8× more
  batching than is demonstrable. The lever is self-measuring — prefer re-running
  after a week over arguing the estimate.
- **Lever 2 is priced adaptively.** A blanket "always refresh" policy loses money
  badly — see the sweep above. `--verbose` prints both so the gap is visible.
- **"Never called" is not "never useful."** A tool you have not needed yet may be
  the right tool next week. The script splits candidates into **safe to turn off**
  and **check first**, holds the latter back unless you pass
  `--include-check-first`, and says on the row when a tool you *do* use depends on
  one. Every change is one list in `settings.json` to undo.
- **Lever 1 is capped by the manifest's share of context.** 30,090 strippable
  tokens against a 251,538-token mean context is 12.0% of context; the dead subset
  was worth at most 8.5% even given perfect foresight. Any larger figure has to come
  from MCP schemas, which this script does not size.
- **Changing the manifest mid-session is not free.** It sits first in the request,
  so any change invalidates the whole prefix. At a cold start that costs nothing;
  at an arbitrary request it cost $11.61 on the reference machine. This matters only
  if you build the dynamic version — a `settings.json` edit takes effect on the next
  session, which starts cold anyway.
- **Per-tool token figures carry roughly ±15%.** Schema JSON does not tokenize
  uniformly: prose-heavy descriptions land near 2.8 chars/token, punctuation-dense
  ones near 2.4. The ratio is calibrated against a direct wire measurement of a
  9-tool bundle spanning both, which is the population a deny list actually
  covers. `--measure` replaces the estimate with ground truth.
- **Dollars depend on your denominator.** The per-turn token saving is solid.
  Converting it to dollars needs a request count, and a gateway's request count
  may include traffic that never carries this manifest (background summarization,
  other apps sharing a key). The script uses your transcripts, the conservative
  choice.
- **Tool names change between Claude Code versions.** Anything in your transcripts
  that the shipped table does not recognize is reported rather than silently
  ignored, and is never proposed for denial.
- **MCP schemas are not sized.** They are often the largest part of a manifest.
  The script lists configured MCP servers and flags any with zero calls in the
  window, but cannot price them. Drop unused servers from your config, or use
  `--strict-mcp-config` per session.
- **Prices default to Anthropic list** for the Opus tier. If you bill through a
  discounted gateway, pass `--price-cache-write` / `--price-cache-read` /
  `--price-input` / `--price-output`; every number rescales.
- **`ttl:"1h"` is not an alternative to lever 2.** Requesting it on Opus 5 or
  Sonnet 5 returns tokens in the `ephemeral_5m` bucket with `1h = 0`, with and
  without the beta header — it is silently downgraded. Only Haiku 4.5 honours it.

## Options

| flag | effect |
|---|---|
| `--days N` | lookback window (default 30) |
| `--report` | print a paste-able block of aggregate numbers |
| `--measure` | verify lever 1 with 2 API calls |
| `--apply` | prompt to write `permissions.deny` |
| `--yes` | with `--apply`, skip the confirmation |
| `--include-check-first` | also offer — and price in — the check-first tools (5.2% → 6.8% here) |
| `--verbose` | every tool you called, plus the lever-2 sensitivity sweep |
| `--settings PATH` | edit a different settings file |
| `--refresh-every S` | lever 2 refresh interval, under the 300 s TTL (default 240) |
| `--batch-target N` | lever 3 target tool calls per request (default 1.5; 1.367 is the measured floor) |
| `--price-*` | override list prices, USD per Mtok |

## License

Apache 2.0.

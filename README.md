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

Measured over one developer's full 30-day history — 53 sessions, 3,631 requests,
1.06 B tokens, $1,032 at Opus list — **89% of the money went on writing and
re-reading context that had already been sent.** New input was 1.1% of spend;
output was 9.9%.

| component | share of spend |
|---|---|
| cache **read** | 47.6% |
| cache **write** | 41.3% |
| output | 9.9% |
| uncached input | 1.1% |

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
| 1 | drop tool schemas you never call | **4.4%** | one line in `settings.json`, today |
| 2 | refresh the prompt cache before it expires | **+13.0 pts** | a proxy in the request path |
| 3 | batch independent tool calls | **+7.8 pts** | a `CLAUDE.md` instruction |
| | **compounded** | **25.2%** | |

None of the three changes the model, the context, or the information available to
it. Your own numbers will differ — that is the point of running it.

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

### Lever 2 — cache expiry

The prompt cache lives **5 minutes**. Pause longer and the next request
**re-writes** the whole conversation at $6.25/M instead of **re-reading** it at
$0.50/M. A refresh request re-reads the same bytes and generates nothing, so the
model's view is byte-identical — only the price differs, by 12.5×.

The waste is extremely concentrated. On the reference machine, requests following
a >5-minute idle gap were **5.6% of all requests** but carried **81.2% of all
cache-write tokens.**

**The policy must be adaptive, and that is the whole story.** Breakeven is
`6.25 / 0.50` = 12.5 refreshes ≈ **50 minutes of idle**: refresh a short gap, let
a long one expire. Deciding per gap from the context size and the gap length is
what makes this safe, and `--verbose` shows why it is not a tuning question:

| refresh interval | adaptive (decide per gap) | blanket (refresh everything) |
|---|---|---|
| 120 s | **+8.5%** | −345% |
| 240 s | **+14.0%** | −157% |
| 290 s | **+15.4%** | −124% |

Adaptive saves money at every interval. Blanket is catastrophic at every
interval — 2.2× to 4.6× the baseline bill. **Do not implement the blanket form.**

Nothing in Claude Code does this today; it needs a component in the request path
that retains the last request body per session and re-issues a minimal version on
a timer. That is why lever 2 is the one number in the report you cannot act on
locally — it is there to size the work.

### Lever 3 — batch independent tool calls

**84.2% of tool-bearing requests issue exactly one tool call**, and every request
re-reads the entire context regardless. Total tool calls are fixed, so raising
calls-per-request removes whole round trips — same work, same results, and lower
latency, so this one improves the experience rather than trading against it.

The script prices it by removing only those requests' context re-reads. Their
output still happens, so this is the conservative half of the effect. It does
assume the batched calls are genuinely independent, which makes it an upper bound.

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
  Over the last 30 days you spent $1,095.99 across 4,394 requests
  (1,112,764,628 tokens), priced at the rates below. 88.1% of that went on
  writing and re-reading context you had already sent -- not on new input,
  and not on output.

  Three changes would recover $271.41 (24.8% of the bill) WITHOUT changing
  the model, shrinking the context, or hiding anything from it. The model
  sees byte-identical prompts in every case.

    cumulative                                      cost    saved
    your last 30 days                          $1,095.99        -
    + 1. drop never-called tool schemas        $1,039.75    5.1%
    + 2. refresh cache before it expires         $906.05   17.3%
    + 3. batch tool calls (1.23 -> 1.50/req)     $824.59   24.8%

  Lever 1 you can switch on today. Lever 3 is a CLAUDE.md instruction.
  Lever 2 needs something in the request path -- a proxy -- because Claude
  Code has no way to refresh its own cache.

WHERE THE MONEY WENT
--------------------
                             tokens       cost   share
    cache read        1,033,531,844    $516.77  47.2%
    cache write          71,795,928    $448.72  40.9%
    output                4,665,950    $116.65  10.6%
    uncached input        2,770,906     $13.85   1.3%

LEVER 1 -- TOOL SCHEMAS YOU NEVER CALL  (5.1%, switch on today)
---------------------------------------------------------------
    tool                tokens      cost   what it is
    Workflow             7,924    $30.77   runs multi-agent workflows (opt-in by keyword)
    DesignSync           3,287    $12.76   syncs a component library to claude.ai/design
    ScheduleWakeup       1,809     $7.03   paces /loop when you run it without an interval
    ...

LEVER 2 -- CACHE EXPIRY  (12.2%, needs a proxy)
-----------------------------------------------
    requests after an idle gap over 300s   211 of 4,394  (4.8%)
    their share of your cache-write tokens               77.7%
    gaps where refreshing is the cheaper move 121 of 211  (57.3%)
    refresh requests that would be added  409
    your cache-write tokens                71,795,928 -> 33,023,456  (-54%)

LEVER 3 -- ONE TOOL CALL PER REQUEST  (7.4%, a CLAUDE.md line)
--------------------------------------------------------------
    calls in request     count    share
    0                      389    8.9%
    1                    3,331   75.8%
    2                      542   12.3%
    ...
    target calls/request     requests cut     of all
    1.5                             714     16.3%
    2.0                           1,537     35.0%
```

Then `WHAT YOUR HISTORY SHOWS` — including spend concentration, which is usually
the most actionable line in the whole report (**4 of 53 sessions were 90% of the
bill; the median session was $0.24**, so none of this needs to change how you work
day to day) — an `MCP SERVERS` section, a `HOW THESE NUMBERS WERE MADE` footer,
and the exact `settings.json` snippet if you would rather paste it by hand.

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
- **Lever 3 assumes the batched calls are independent.** Calls that must run in
  sequence cannot be merged, so treat its number as an upper bound.
- **Lever 2 is priced adaptively.** A blanket "always refresh" policy loses money
  badly — see the sweep above. `--verbose` prints both so the gap is visible.
- **"Never called" is not "never useful."** A tool you have not needed yet may be
  the right tool next week. The script splits candidates into **safe to turn off**
  and **check first**, holds the latter back unless you pass
  `--include-check-first`, and says on the row when a tool you *do* use depends on
  one. Every change is one list in `settings.json` to undo.
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
| `--include-check-first` | also offer the check-first tools |
| `--verbose` | every tool you called, plus the lever-2 sensitivity sweep |
| `--settings PATH` | edit a different settings file |
| `--refresh-every S` | lever 2 refresh interval, under the 300 s TTL (default 240) |
| `--batch-target N` | lever 3 target tool calls per request (default 1.5) |
| `--price-*` | override list prices, USD per Mtok |

## License

Apache 2.0.

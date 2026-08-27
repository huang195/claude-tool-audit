# claude-tool-audit

Find the Claude Code tool schemas you pay for on every turn but never call.

```
python3 claude-tool-audit.py                  # report only, reads nothing but your transcripts
python3 claude-tool-audit.py --measure        # + verify the estimate with 2 API calls
python3 claude-tool-audit.py --apply          # + prompt to disable the never-used ones
```

No dependencies beyond `python3`. Single file.

## Why

Claude Code sends the full JSON schema of **every declared tool on every API
request**. The tool manifest sits first in the request, ahead of the system
prompt and the conversation, and it is re-sent and re-billed every turn whether
a tool is ever called or not.

Measured off the wire on a headless `claude --print "say ok"` in an empty
directory: **34,851 of 39,216 prompt tokens — 89% of the request — were tool
schemas.**

Restricting a tool actually removes its schema from the wire; it does not merely
block execution. Verified by capturing the real requests three ways:

| lever | effect on the wire |
|---|---|
| `permissions.deny` in `settings.json` | removed exactly the named tools |
| `--disallowedTools <names>` | removed exactly the named tools |
| `--tools <explicit list>` | removed exactly the 22 others |
| `--strict-mcp-config` | removed exactly the MCP tools |

So the saving is real. This script finds which tools *you* have never called,
prices them against *your own* history, and offers to deny them.

## What it does

**1. Offline transcript scan.** Reads `~/.claude/projects/**/*.jsonl` and counts
tool calls over a window (default 30 days), deduplicating on each `tool_use`
block's own id — transcripts repeat records across session resume and context
compaction, so counting rows inflates every total. It also pulls your own turn
count and your own uncached / cache-write / cache-read split from the `usage`
blocks, so the dollar figures are grounded in your history rather than
extrapolated from someone else's.

**2. `--measure` (optional).** Two `claude --print --output-format json` calls in
a temp directory with the same prompt, one of them with the deny list applied.
The prompt-token delta is exactly what those schemas cost you per turn, on your
Claude Code version. No proxy, no credentials handled.

**3. `--apply`.** Backs up `settings.json`, shows you the exact additions,
prompts, then writes **only** `permissions.deny`. Everything else in the file is
left alone. Undo is one line.

## Sample output

It opens with the answer, then shows the work. Abridged:

```
==========================================================================
Claude Code tool audit                                        last 30 days
==========================================================================

THE SHORT VERSION
-----------------
  14 of the 25 built-in tools were sent to the model on every one of your
  4,503 requests, and you never called them once.

  Turning off the 9 that are safe to remove would take 16,119 tokens out
  of every request, worth about $64.09 over these 30 days.

  A further 5 are unused too, but removing one of those could break a
  workflow, so they are listed separately and left alone unless you ask
  for them.

  Next step:
    python3 claude-tool-audit.py --measure
        check that number against the real API (2 requests)
    python3 claude-tool-audit.py --apply
        turn them off -- asks first, backs up your settings

WHAT YOUR HISTORY SHOWS
-----------------------
  period       2026-07-28 to 2026-08-27
  requests     4,503  (one per exchange with the model)
  tool calls   5,141  across 19 different tools
  most used    Bash 3,875, Read 448, Edit 253, TaskUpdate 133, Write 132,
               TaskCreate 85
               ...and 13 more (--verbose for all of them)

SAFE TO TURN OFF -- 9 tools, 16,119 tokens per request, $64.09 over 30 days
---------------------------------------------------------------------------
  Never called, and nothing else you use depends on them.

    tool                tokens      cost   what it is
    Workflow             7,924    $31.51   runs multi-agent workflows (opt-in by keyword)
    DesignSync           3,287    $13.07   syncs a component library to claude.ai/design
    ScheduleWakeup       1,809     $7.19   paces /loop when you run it without an interval
    CronCreate           1,337     $5.32   schedules a prompt to run later
    PushNotification       650     $2.58   sends you a desktop or phone notification
    NotebookEdit           593     $2.36   edits Jupyter .ipynb cells
    WebSearch              305     $1.21   searches the web; an MCP search tool replaces it
    CronDelete             130     $0.52   cancels a scheduled prompt
    CronList                84     $0.33   lists scheduled prompts

CHECK FIRST -- 5 tools, 5,267 tokens per request, $20.94 over 30 days
---------------------------------------------------------------------
  Never called either, but removing one of these could break a workflow,
  so they are left out unless you pass --include-check-first.

    tool                tokens      cost   what it is
    SendMessage          1,619     $6.44   how Claude reaches subagents spawned by Agent
                                           (you called Agent 73 times in this window)
    EnterWorktree        1,469     $5.84   keep if your repo documents a worktree workflow
    ...
```

Then a `MCP SERVERS` section, a `HOW THESE NUMBERS WERE MADE` footer with the
assumptions and the privacy note, and the exact `settings.json` snippet if you
would rather paste it by hand than run `--apply`.

With `--measure` it adds a block that replaces the estimate with ground truth:

```
MEASURED AGAINST THE REAL API
-----------------------------
  Sending two requests with the prompt "say ok" from an empty directory --
  one with your normal tools, one with these 9 switched off. The
  difference is exactly what they cost you, on your machine and your
  Claude Code version.

    every tool on         39,240 tokens in the request
     9 tools off          23,125 tokens in the request
    ----------------------------------------------
    you save              16,115 tokens, every request
                           41.1% of that request
                          $64.15 over your last 4,510 requests

  The estimate above said 16,119 tokens, so measured / estimated = 1.00.
```

The estimate and the wire delta agreed to 0.03% there. Don't read that as
precision guaranteed for you — it is one machine on one Claude Code version, and
it is why `--measure` exists.

## Privacy

It reads **tool names, `tool_use` ids, timestamps, and token counts** from your
transcripts. It never reads prompts, tool arguments, tool results, or
completions.

It sends **nothing anywhere** unless you pass `--measure`, which makes exactly
two `claude --print` calls with the fixed prompt `say ok`. `--apply` only edits
your local `settings.json`, after a backup.

## Caveats, stated up front

- **"Never called" is not "never useful."** A tool you have not needed yet may be
  the right tool next week. The script splits candidates into **safe to turn off**
  and **check first** for this reason, holds the latter back unless you pass
  `--include-check-first`, and says on the row when a tool you *do* use depends
  on one. Every change is one list in `settings.json` to undo.
- **Per-tool token figures carry roughly ±15%.** Schema JSON does not tokenize
  uniformly: prose-heavy descriptions land near 2.8 chars/token, punctuation-dense
  ones near 2.4. The ratio is calibrated against a direct wire measurement of a
  9-tool bundle spanning both, which is the population a deny list actually
  covers. `--measure` replaces the estimate with ground truth.
- **Monthly dollars depend on your denominator.** The per-turn token saving is
  solid. Converting it to dollars needs a turn count, and a gateway's request
  count may include traffic that never carries this manifest (background
  summarization, other apps sharing a key). The script uses your transcripts,
  which is the conservative choice.
- **Tool names change between Claude Code versions.** Anything in your
  transcripts that the shipped table does not recognize is reported rather than
  silently ignored, and is never proposed for denial.
- **MCP schemas are not sized.** They are often the largest part of a manifest.
  The script lists configured MCP servers and flags any with zero calls in the
  window, but cannot price them. Drop unused servers from your config, or use
  `--strict-mcp-config` per session.
- **Prices default to Anthropic list** for the Opus tier. If you bill through a
  discounted gateway, pass `--price-cache-write` / `--price-cache-read` /
  `--price-input` / `--price-output`.

## Options

| flag | effect |
|---|---|
| `--days N` | lookback window (default 30) |
| `--measure` | verify the estimate with 2 API calls |
| `--apply` | prompt to write `permissions.deny` |
| `--yes` | with `--apply`, skip the confirmation |
| `--include-check-first` | also offer the check-first tools |
| `--verbose` | list every tool you called, not just the top few |
| `--settings PATH` | edit a different settings file |
| `--price-*` | override list prices, USD per Mtok |

## License

Apache 2.0.

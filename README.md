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

```
History: 171 transcript files, 4478 API turns, 5119 tool calls  2026-07-28 .. 2026-08-27
Claude Code versions seen: 2.1.237, 2.1.238, 2.1.241
Your input tokens split: 0.2% uncached / 6.5% cache-write / 93.3% cache-read
=> at that mix, every 1,000 tokens of tool schema costs you $3.95 across 4478 turns

TOOLS YOU CALLED (19)
  Bash                                   3867
  Read                                    446
  Edit                                    241
  ...

DECLARED BUT NEVER CALLED (14)
  tool                   tokens   $/window  note
  ----------------------------------------------
   Workflow                7924     $31.33  multi-agent orchestration; opt-in by keyword
   DesignSync              3287     $13.00  syncs a component library to claude.ai/design
   ScheduleWakeup          1809      $7.15  paces /loop dynamic mode
  !SendMessage             1619      $6.40  needed to talk to subagents you spawn with Agent
  !EnterWorktree           1469      $5.81  git worktree workflow; keep if your repo documents one
  ...
  low-risk subtotal         16119 tokens  $63.74 over 4478 turns
  check-first subtotal       5267 tokens  $20.83  (marked !)
  never-called schemas are 71% of your declared manifest by wire bytes
```

And with `--measure`, on the same machine:

```
MEASURING (2 API calls, prompt "say ok", empty directory)...
  baseline manifest           39238 prompt tokens
  with  9 tools denied        23123 prompt tokens
  measured saving             16115 tokens/turn (41.1%)
  at your own volume       $63.51 over 4466 turns
  (table estimated 16119; measured/estimate = 1.00)
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
  the right tool next week. The script splits candidates into **low-risk** and
  **check-first** (`!`) for this reason and holds the latter back unless you pass
  `--include-check-first`. Every change is one line in `settings.json` to undo.
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
| `--include-check-first` | also offer the `!`-flagged tools |
| `--settings PATH` | edit a different settings file |
| `--price-*` | override list prices, USD per Mtok |

## License

Apache 2.0.

# claude-tool-audit

Price two ways to cut your Claude Code bill **without changing anything the model
sees.** Same model, same full context, byte-identical prompts — only the price
paid for carrying that context changes.

```bash
curl -fsSL https://raw.githubusercontent.com/huang195/claude-tool-audit/main/claude-tool-audit.py \
  | python3 - --report --measure
```

That is the whole setup. Single file, standard library only, no install. It reads
your local transcripts and prints a block for you to paste back.

Prefer to look before you run — reasonable, since it reads your dev history:

```bash
curl -fsSL https://raw.githubusercontent.com/huang195/claude-tool-audit/main/claude-tool-audit.py -o audit.py
less audit.py
python3 audit.py --report --measure
```

`--measure` makes three `claude --print` calls with the fixed prompt `say ok`,
about **$0.75** in total. Without it everything still works except one number
(see [MCP schemas](#the-one-number-transcripts-cannot-show)).

## Why

Measured over one developer's 30-day history — 62 cache lineages, 4,627 requests,
1.13 B tokens — **88% of the money went on writing and re-reading context that had
already been sent.** New input was 1.3% of spend; output was 10.8%.

| component | share of spend |
|---|---|
| cache **read** | 47.4% |
| cache **write** | 40.5% |
| output | 10.8% |
| uncached input | 1.3% |

The counter-intuitive part: a **93% cache-hit rate** sounds excellent and is what
most people have. But hit rate is counted in tokens and the bill is not. **A cache
write bills 12.5× a cache read** ($6.25 vs $0.50 per million), so the 6.5% of
input tokens that get *written* cost nearly as much as the 93.5% that get *read*.

So the levers that matter are not "say less" or "use a cheaper model" — both of
which cost answer quality. They are about not paying twice for the same bytes.

## The two levers

| | lever | saved | why nothing local can do it |
|---|---|---|---|
| **A** | drop tool schemas this developer never calls | **7.7%** | needs *that* developer's call history |
| **B** | refresh a cache lineage before its 5-minute timer expires | **12.6%** | Claude Code cannot refresh its own cache |
| | **A + B** | **18.4%** | |

Both live in the request path. Neither changes the bytes the model receives.

### A — tool schemas declared every request, never called

Every request carries the full JSON schema of every available tool, whether or not
you use it. On the reference machine that manifest is **39,300 tokens**, 12.4% of a
243k mean context, and 14 of 25 built-in tools had never been called in 30 days.

Two things make this worth doing in the request path rather than as a fixed list.
It is **per developer** — your dead tools are not your teammate's. And the manifest
sits *first* in the request, so changing it invalidates the entire cached prefix
behind it: the same edit costs **$0.00** at a cold start, when the prefix was going
to be rewritten anyway, and **$11.61** at an arbitrary request mid-conversation.
Only something watching the request stream knows which moment it is in.

The script also reports a **ceiling** (8.2% here) — what perfect per-lineage
trimming would achieve — and how many lineages ever reach a usable trim point
(15 of 62). The gap between 7.7% and 8.2% is the cost of being careful.

Tools that were never called but *partner* one that is in use are kept, not
dropped: `SendMessage` stays because `Agent` is used.

### B — cache expiry

After 5 minutes idle the prompt cache drops. The next request re-writes the whole
conversation at 12.5× the price of re-reading it. Keeping it warm with cheap reads
first is byte-identical to the model.

The measurement that makes this a real finding:

| | |
|---|---|
| requests arriving after an idle gap | 212 of 4,627 (**4.6%**) |
| their share of all cache writes | **77.6%** |
| gaps where refreshing is cheaper than expiring | 123 of 212 |
| refresh requests that adds | 409 |

4.6% of requests carry 77.6% of the most expensive token class. That concentration
is what makes a narrow intervention worth anything.

**The policy must be selective, and getting that wrong inverts the result:**

| refresh interval | adaptive | blanket |
|---|---|---|
| 120 s | **+7.6%** | −335.7% |
| 180 s | **+10.7%** | −213.8% |
| 240 s | **+12.6%** | −152.9% |
| 270 s | **+13.4%** | −132.7% |
| 290 s | **+13.9%** | −121.4% |

*adaptive* refreshes a gap only when that is cheaper than letting it expire;
*blanket* refreshes always. Breakeven is `cache_write / cache_read` = 12.5
refreshes ≈ 50 minutes of idle, so 89 of the 212 gaps should be allowed to die.

Adaptive is positive at every interval tested, so the gain is not an artifact of a
lucky setting. Blanket **multiplies** the bill. The value is not in "refresh the
cache" — it is in *deciding per gap*, which requires knowing how long this
particular lineage has been idle.

## The one number transcripts cannot show

`--measure` runs three `claude --print` calls that differ only in the tool
manifest, so each prompt-token delta is exactly what those schemas cost on your
machine and your Claude Code version. The third passes `--strict-mcp-config`,
which starts with no Model Context Protocol servers at all — that delta is the
size of your MCP tool descriptions.

```
    every tool on         39,300 tokens in the request
     9 tools off          23,185 tokens in the request
    no MCP servers        38,889 tokens in the request
    dead built-ins        16,115 tokens, every request  (41.0%)
    MCP tool schemas         411 tokens, every request  ( 1.0%)
```

411 tokens is noise on that machine. On someone running five MCP servers it may be
the largest term in the manifest, and nothing else reports it. That variance is
most of the reason to collect this from more than one person.

## Privacy

From each transcript record the script reads **only**: tool names, `tool_use` ids,
timestamps, session ids, and token counts. It never reads prompts, tool arguments,
tool results, or completions. It writes no files and changes no settings.

Nothing leaves your machine unless you pass `--measure`, which sends the fixed
string `say ok`.

The `--report` block is aggregate numbers and built-in tool names only — no paths,
project names, branches, prompts, arguments, or MCP server names. MCP tools are
counted, never named, because a server name can identify an internal system. The
one line in the human-readable output that does name a server is marked for you to
delete before pasting.

## Options

```
--days N              window to analyse (default 30)
--report              add the paste-able aggregate block
--measure             weigh the real manifest with 3 `claude --print` calls
--refresh-every S     refresh interval to price lever B at (default 240)
--include-check-first count partner tools as droppable too
--verbose             per-tool detail
--price-* N           override list prices, USD per million tokens
```

## Caveats, stated up front

- Both levers are **counterfactuals**: they replay your real per-request token
  counts under a different policy. Arithmetic on measured data, not a simulation —
  but they assume the policy behaves as specified.
- Lever B is priced with the **adaptive** rule. The blanket form loses badly; the
  output shows both so the sign flip is visible. Do not implement the blanket form.
- One quantity in lever B is modelled rather than measured: the cache write on a
  refreshed request is estimated as the mean write across warm requests (gaps
  ≤ 60 s), because there is no observation of what that request *would* have
  written had the cache survived. The 409 refresh requests' own cost is charged in
  full.
- "Never called" is not "never useful." A tool you have not needed yet may be the
  right tool next week, so lever A has to be able to put it back.
- Per-tool token estimates come from a wire capture of Claude Code 2.x and carry
  roughly ±15%. The aggregate is the solid number, and `--measure` replaces the
  estimate with a direct measurement.
- Prices default to Opus list. If your gateway bills at a discount, every
  percentage here is unchanged — only the dollars scale.

## What to send back

The `PASTE THIS BACK` block at the end of `--report`. Please run it as
`--report --measure` if you can; the MCP figure only exists that way.

## License

Apache 2.0. See [LICENSE](LICENSE).

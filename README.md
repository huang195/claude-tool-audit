# claude-tool-audit

Price two ways to cut your Claude Code bill **without changing anything the model
sees.** Same model, same full context, byte-identical prompts — only the price
paid for carrying that context changes.

```bash
curl -fsSL https://raw.githubusercontent.com/huang195/claude-tool-audit/main/claude-tool-audit.py \
  | python3 -
```

That is the whole setup. Single file, standard library only, no install, no flags.
It reads your local transcripts, prints about 65 lines, and exits. Send the output
back.

Prefer to look before you run — reasonable, since it reads your dev history:

```bash
curl -fsSL https://raw.githubusercontent.com/huang195/claude-tool-audit/main/claude-tool-audit.py -o audit.py
less audit.py
python3 audit.py
```

It makes no API calls and touches no network, so it costs nothing to run.

## Why

Measured over one developer's 30-day history — 69 cache lineages, 4,691 requests,
1.11 B tokens — **88% of the money went on writing and re-reading context that had
already been sent.** New input was 1.3% of spend; output was 10.9%.

| component | share of spend |
|---|---|
| cache **read** | 47.4% |
| cache **write** | 40.4% |
| output | 10.9% |
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
| **A** | drop tool schemas this developer never calls | **7.9%** | needs *that* developer's call history |
| **B** | refresh a cache lineage before its 5-minute timer expires | **12.5%** | Claude Code cannot refresh its own cache |
| | **A + B** | **18.5%** | |

Both live in the request path. Neither changes the bytes the model receives.

### A — tool schemas declared every request, never called

Every request carries the full JSON schema of every available tool, whether or not
you use it. On the reference machine that manifest is **12.8% of a 235k mean
context**, and 14 of 25 built-in tools had never been called in 30 days —
**16,119 tokens on every single request**, for nothing.

Two things make this worth doing in the request path rather than as a fixed list.
It is **per developer** — your dead tools are not your teammate's. And the manifest
sits *first* in the request, so changing it invalidates the entire cached prefix
behind it: the same edit costs **$0.00** at a cold start, when the prefix was going
to be rewritten anyway, and **$11.61** at an arbitrary request mid-conversation.
Only something watching the request stream knows which moment it is in.

The script also reports a **ceiling** (8.5% here) — what perfect per-lineage
trimming would achieve — and how many lineages ever reach a usable trim point
(15 of 69). The gap between 7.9% and 8.5% is the cost of being careful.

Tools that were never called but *partner* one that is in use are kept, not
dropped: `SendMessage` stays because `Agent` is used.

### B — cache expiry

After 5 minutes idle the prompt cache drops. The next request re-writes the whole
conversation at 12.5× the price of re-reading it. Keeping it warm with cheap reads
first is byte-identical to the model.

The measurement that makes this a real finding:

| | |
|---|---|
| requests arriving after an idle gap | 211 of 4,691 (**4.5%**) |
| their share of all cache writes | **77.2%** |
| gaps where refreshing is cheaper than expiring | 123 of 211 |
| refresh requests that adds | 409 |

4.5% of requests carry 77.2% of the most expensive token class. That concentration
is what makes a narrow intervention worth anything.

**The policy must be selective, and getting that wrong inverts the result:**

| refresh interval | adaptive | blanket |
|---|---|---|
| 120 s | **+7.5%** | −339.9% |
| 180 s | **+10.5%** | −216.6% |
| 240 s | **+12.5%** | −155.1% |
| 270 s | **+13.2%** | −134.6% |
| 290 s | **+13.7%** | −123.2% |

*adaptive* refreshes a gap only when that is cheaper than letting it expire;
*blanket* refreshes always. Breakeven is `cache_write / cache_read` = 12.5
refreshes ≈ 50 minutes of idle, so 88 of the 211 gaps should be allowed to die.

Adaptive is positive at every interval tested, so the gain is not an artifact of a
lucky setting. Blanket **multiplies** the bill. The value is not in "refresh the
cache" — it is in *deciding per gap*, which requires knowing how long this
particular lineage has been idle.

## Privacy

From each transcript record the script reads **only**: tool names, `tool_use` ids,
timestamps, session ids, and token counts. It never reads prompts, tool arguments,
tool results, or completions. It writes no files and changes no settings.

**Nothing leaves your machine.** There are no API calls and no network access — it
reads local files, prints, and exits.

The output is aggregate numbers and built-in tool names only — no paths, project
names, branches, prompts, arguments, or MCP server names. Model Context Protocol
servers and tools are **counted, never named**, because a server name can identify
an internal system. There is nothing in the output you need to redact.

## Options

None are needed. If you want them:

```
--days N              window to analyse (default 30)
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
  roughly ±15%. The aggregate is the solid number; any single tool's line is not.
- Prices default to Opus list. If your gateway bills at a discount, every
  percentage here is unchanged — only the dollars scale.

## What to send back

The whole output. It is about 65 lines and contains nothing identifying beyond
built-in tool names.

## License

Apache 2.0. See [LICENSE](LICENSE).

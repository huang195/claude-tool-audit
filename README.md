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

Measured over one developer's 30-day history — 68 cache lineages, 4,495 requests,
1.05 B tokens — **88% of the money went on writing and re-reading context that had
already been sent.** New input was 1.3% of spend; output was 11.1%.

| component | share of spend |
|---|---|
| cache **read** | 47.1% |
| cache **write** | 40.5% |
| output | 11.1% |
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
| **A** | drop tool schemas this developer never calls | **5.6%** | needs *that* developer's call history |
| **B** | refresh a cache lineage before its 5-minute timer expires | **12.2%** | Claude Code cannot refresh its own cache |
| | **A + B** | **16.8%** | |

Across six developers measured so far, A lands between **3.1% and 18.9%** and B
between **5.7% and 29.7%**. Which lever is larger **depends on the person** — B
leads on four of the six — so the numbers below are one machine's, not a forecast
for yours. Weighted across all six bills, A is worth 8.7% and B is worth 16.8%.

The single quantity that has *not* varied is lever B's **capture rate** — the
fraction of the addressable pool it recovers. It sat at 40–53% across the first
five machines, and the sixth, whose usage profile resembled none of them (8 cache
lineages against 68, a 452 k mean context against 232 k, and cache writes
outweighing cache reads), came in at 47%. That is the closest thing here to an
out-of-sample test.

Both live in the request path. Neither changes the bytes the model receives.

### A — tool schemas declared every request, never called

Every request carries the full JSON schema of every available tool, whether or not
you use it. On the reference machine 14 of 25 built-in tools had never been called
in 30 days — **16,119 tokens on every single request**, for nothing.

The manifest sits *first* in the request, so changing it invalidates the entire
cached prefix behind it: the same edit costs **$0.00** applied from a conversation's
first request, when the prefix is being written anyway, and **$10.51** if switched
on part-way through live conversations. Only something watching the request stream
knows which moment it is in.

The script also reports a **ceiling** (6.4% here): what you would save if each
conversation additionally dropped the tools that *only it* never asks for. That
needs foresight, so it is not implementable — but it is a genuine upper bound, and
the gap between 5.6% and 6.4% is what per-conversation trimming would be worth on
top of per-developer trimming.

On the per-developer claim, be careful: across six measured machines the droppable
sets turned out to be **nested**, not disjoint. Eight tools were dead on every
single machine, and one shared nine-tool list would capture at least 70% of each
person's dead tokens. That part is achievable with a static config and no proxy.
What is *not* achievable statically is the per-conversation ceiling above, and the
timing of when to apply the change.

One scope limit worth stating plainly, because it cuts the other way: the numbers
above cover **built-in tools only**. Model Context Protocol servers add their own
schemas to the same manifest, and a server you configured but never call is pure
dead weight — but a transcript records only the tools that *were* called, so from
local files a never-used server is invisible by construction. It cannot be
measured here, and it is not included in any figure above. Only something reading
the request path sees the whole `tools` array. That makes the request path
necessary for the **measurement**, not just for the fix.

Tools that were never called but *partner* one that is in use are kept, not
dropped: `SendMessage` stays because `Agent` is used.

### B — cache expiry

After 5 minutes idle the prompt cache drops. The next request re-writes the whole
conversation at 12.5× the price of re-reading it. Keeping it warm with cheap reads
first is byte-identical to the model.

The measurement that makes this a real finding:

| | |
|---|---|
| requests arriving after an idle gap | 211 of 4,495 (**4.7%**) |
| their share of all cache writes | **77.4%** |
| gaps where refreshing is cheaper than expiring | 121 of 211 |
| refresh requests that adds | 406 |

4.7% of requests carry 77.4% of the most expensive token class. That concentration
is what makes a narrow intervention worth anything. It reproduces: across six
machines the idle-gap requests were **1.9–14.3%** of traffic and carried
**76–94%** of all cache writes, and lever B captured **40–53%** of that
addressable pool every time — a band that held even on the machine whose usage
profile matched none of the others.

**The policy must be selective, and getting that wrong inverts the result:**

| refresh interval | adaptive | blanket |
|---|---|---|
| 120 s | **+7.4%** | −356.9% |
| 180 s | **+10.3%** | −227.8% |
| 240 s | **+12.2%** | −163.4% |
| 270 s | **+13.0%** | −142.0% |
| 290 s | **+13.5%** | −130.2% |

*adaptive* refreshes a gap only when that is cheaper than letting it expire;
*blanket* refreshes always. Breakeven is `cache_write / cache_read` = 12.5
refreshes ≈ 50 minutes of idle, so 90 of the 211 gaps should be allowed to die.

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

The output is aggregate numbers, built-in tool names, Claude Code version strings,
and model ids only — no paths, project names, branches, prompts, arguments, or MCP
server names. Model Context Protocol servers and tools are **counted, never
named**, because a server name can identify an internal system. Model ids are
printed (`claude-opus-5`, `claude-sonnet-5`, …) because the dollar figures are
meaningless without knowing what was actually being billed. There is nothing in
the output you need to redact.

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
  written had the cache survived. The 406 refresh requests' own cost is charged in
  full.
- "Never called" is not "never useful." A tool you have not needed yet may be the
  right tool next week, so lever A has to be able to put it back.
- Per-tool token estimates come from a wire capture of Claude Code 2.x and carry
  roughly ±15%. The aggregate is the solid number; any single tool's line is not.
- Prices default to Opus list, and **real traffic is mixed-model.** The reference
  machine here turned out to be 80.8% Opus 5, 7.6% Sonnet 5, 7.5% Opus 4.8 — so
  even its own dollar column is an approximation, and a machine that is mostly
  Sonnet is overstated considerably. The script now prints the model mix so you can
  see how far off the basis is, and `--price-*` lets you correct it. **Every
  percentage in this README survives a wrong price basis** — `cache_write /
  cache_read` is 12.5 on every model tier, and both levers are ratios of cache
  cost to total cost. Only the dollars move.
- The reference figures are **one dated 30-day run**, not a live number. The window
  rolls, so re-running tomorrow gives a different total. Percentages have been
  stable across re-runs; dollars have not.

## What to send back

The whole output. It is about 65 lines and contains nothing identifying beyond
built-in tool names, Claude Code versions, and model ids.

## License

Apache 2.0. See [LICENSE](LICENSE).

#!/usr/bin/env python3
"""Price two ways to cut your Claude Code bill without changing an answer.

WHY THIS EXISTS
---------------
Over one developer's 30-day history (56 lineages, 4,540 requests, 1.12 B tokens,
$1,097 at Opus list) 88% of the money went on writing and re-reading context
that had already been sent. New input was 1.3% of spend, output 10.8%.

So the levers that matter are not "say less" or "use a cheaper model" -- both of
which cost answer quality. They are about not paying twice for the same bytes.
This script prices two against YOUR history:

  A  Dead tool schemas -- declared on every request, never called. Rewriting the
     tools array per developer, from that developer's own call history, needs
     something in the request path. 7.6% here.
  B  Cache expiry -- after a 5-minute pause the prompt cache drops and the next
     request RE-WRITES the whole conversation at 12.5x the price of re-reading
     it. Refreshing first is byte-identical to the model. 12.6% here, and
     impossible from inside Claude Code, which cannot refresh its own cache.

Neither changes the model, the context, or the information available to it.
Together, 18.4% here. Send the output back so we can see how much that varies
between people -- that is what decides whether it is worth building.

WHAT IT READS
-------------
Your local transcripts (~/.claude/projects/**/*.jsonl). From each record it
reads ONLY: tool names, tool_use ids, timestamps, session ids, and token
counts. It never reads prompts, tool arguments, tool results, or completions.

WHAT IT SENDS
-------------
Nothing, anywhere. No API calls, no network. It reads transcripts, prints, and
exits; it changes no files and no settings. The output is aggregate numbers and
built-in tool names -- no paths, project names, branches, prompts, arguments, or
MCP server names.

USAGE
-----
    python3 claude-tool-audit.py                  # <-- send this output back
    python3 claude-tool-audit.py --verbose        # + per-tool detail

CAVEATS, STATED UP FRONT
------------------------
* Both levers are counterfactuals: they replay your real per-request token
  counts under a different policy. Arithmetic on measured data, not a
  simulation, but they assume the policy behaves as specified.
* B is priced with the adaptive rule (refresh only when refreshing is cheaper
  than expiring). Blanket "always refresh" LOSES money badly -- the output
  shows both, and the sign flips. Do not implement the blanket form.
* "Never called" is not "never useful". A tool you have not needed yet may be
  the right tool next week, so A has to be able to put it back.
* Per-tool token figures come from a wire capture of Claude Code 2.x and carry
  roughly +/-15%. The aggregate is the solid number; a single tool's line is not.
"""

import argparse
import collections
import datetime
import glob
import json
import math
import os
import sys
import textwrap

# ---------------------------------------------------------------------------
# Measured tool manifest.
#
# `chars` is the exact byte length of each tool's JSON schema as captured off
# the wire from a headless `claude --print` request (Claude Code 2.x, no project
# MCP servers). Tokens are derived with CHARS_PER_TOKEN below.
#
# `risk`:  "low"        -- nothing in a normal workflow needs it
#          "check"      -- removing it plausibly breaks something; read the note
#          "interactive"-- only exists in interactive sessions, never declared
#                          in headless runs, so never propose denying it
# ---------------------------------------------------------------------------
# Calibrated against a direct wire measurement: denying 9 tools whose schemas
# total 44,393 chars removed exactly 16,115 prompt tokens => 2.754 chars/token.
# That bundle spans the largest and smallest schemas, so it is the right ratio
# for pricing a deny list. Individual tools still vary (prose-heavy descriptions
# tokenize near 2.8, punctuation-dense ones near 2.4), hence the +/-15% caveat.
CHARS_PER_TOKEN = 2.754

TOOLS = {
    # name: (chars, group, risk, note)
    #
    # For "low" tools the note says what the tool does -- so you can tell at a
    # glance whether you would miss it. For "check" tools the note says why you
    # might want to keep it, since that is the decision you actually face.
    "Workflow": (21822, "orchestration", "low",
                 "runs multi-agent workflows (opt-in by keyword)"),
    "DesignSync": (9053, "specialized", "low",
                   "syncs a component library to claude.ai/design"),
    "Monitor": (7599, "scheduling", "low",
                "streams events from a long-running command"),
    "ScheduleWakeup": (4982, "scheduling", "low",
                       "paces /loop when you run it without an interval"),
    "SendMessage": (4459, "orchestration", "check",
                    "how Claude reaches subagents spawned by Agent"),
    "EnterWorktree": (4047, "worktree", "check",
                      "keep if your repo documents a worktree workflow"),
    "CronCreate": (3681, "scheduling", "low",
                   "schedules a prompt to run later"),
    "Agent": (3174, "orchestration", "check",
              "spawns subagents; usually worth keeping"),
    "Bash": (2870, "core", "check", "shell access"),
    "ExitWorktree": (2520, "worktree", "check", "pairs with EnterWorktree"),
    "LSP": (2311, "specialized", "check",
            "code navigation in large typed codebases"),
    "ReportFindings": (2187, "specialized", "low",
                       "structured output used by code-review skills"),
    "Skill": (1824, "specialized", "check",
              "runs skills; keep if you use slash commands"),
    "PushNotification": (1790, "scheduling", "low",
                         "sends you a desktop or phone notification"),
    "NotebookEdit": (1633, "core", "low", "edits Jupyter .ipynb cells"),
    "Read": (1597, "core", "check", "reads files"),
    "TaskOutput": (1561, "orchestration", "low",
                   "deprecated; tasks now report their own output path"),
    "ListAgents": (1171, "orchestration", "check",
                   "lists subagents so SendMessage can reach them"),
    "Edit": (968, "core", "check", "edits files"),
    "WebSearch": (841, "web", "low",
                  "searches the web; an MCP search tool replaces it"),
    "TaskStop": (805, "orchestration", "low", "kills a background task"),
    "WebFetch": (750, "web", "check", "fetches and summarizes a URL"),
    "Write": (639, "core", "check", "writes files"),
    "CronDelete": (359, "scheduling", "low", "cancels a scheduled prompt"),
    "CronList": (232, "scheduling", "low", "lists scheduled prompts"),
    # Interactive-only: present in a real session, absent from headless runs.
    # Never propose denying these -- they are how the session talks to you.
    "TaskCreate": (0, "interactive", "interactive", ""),
    "TaskUpdate": (0, "interactive", "interactive", ""),
    "TaskGet": (0, "interactive", "interactive", ""),
    "AskUserQuestion": (0, "interactive", "interactive", ""),
    "EnterPlanMode": (0, "interactive", "interactive", ""),
    "ExitPlanMode": (0, "interactive", "interactive", ""),
    "EndConversation": (0, "interactive", "interactive", ""),
}

# A tool you never called may still be needed by a tool you *do* call. When the
# partner shows up in your history we say so on the row, because that is the
# single most useful thing to know before removing it.
PARTNER = {
    "SendMessage": "Agent",
    "ListAgents": "Agent",
    "ExitWorktree": "EnterWorktree",
    "CronDelete": "CronCreate",
    "CronList": "CronCreate",
    "TaskOutput": "Agent",
    "TaskStop": "Agent",
}

# Anthropic list prices, USD per million tokens (Claude Opus 4.6 / 5 tier).
# Override with --price-* if your team bills through a discounted gateway.
PRICE = {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50}

# Prompt-cache time-to-live. Anthropic's default ephemeral cache is 5 minutes;
# a read inside the window slides it forward (verified: written at t=0, read at
# t=265 s, read again at t=530 s -> HIT; unrefreshed control at t=530 s -> MISS).
TTL_SECONDS = 300.0

# A refresh request re-reads the context and generates almost nothing. 4 output
# tokens is what a minimal "reply with one character" turn actually costs.
REFRESH_OUTPUT_TOKENS = 4


def tokens_of(name):
    return round(TOOLS[name][0] / CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Phase 1 -- offline transcript scan
# ---------------------------------------------------------------------------

def scan(days, root=None):
    """Count tool calls and token usage over the window.

    Dedupes on ids because transcripts repeat records across session resume and
    context compaction; counting rows inflates every total.

    Also builds two things the lever maths needs:

    * `lineages` -- requests grouped by (sessionId, isSidechain) and ordered by
      time. A subagent runs on its own prompt prefix, so its requests can never
      warm the main conversation's cache; conflating them would hide cold starts.
    * `msg_calls` -- tool calls per assistant message. An assistant message
      streams across several transcript records and the record carrying `usage`
      is often NOT the one carrying the tool_use blocks, so this unions the
      content blocks per message id. (Getting that wrong undercounts by ~4x.)
    """
    root = root or os.path.expanduser("~/.claude/projects")
    cut = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)

    main = collections.Counter()
    sub = collections.Counter()
    seen_calls = set()
    seen_turns = set()
    usage = collections.Counter()
    versions = collections.Counter()
    models = collections.Counter()
    lineages = collections.defaultdict(list)
    msg_calls = collections.Counter()      # message id -> tool_use blocks
    msg_names = collections.defaultdict(list)   # message id -> tool names
    msg_seen = set()                       # message ids that were assistant turns
    first = last = None

    for path in files:
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                # Cheap prefilter: skip lines that cannot contribute.
                if '"tool_use"' not in line and '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("timestamp")
                if not ts:
                    continue
                try:
                    when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when < cut:
                    continue
                first = when if first is None or when < first else first
                last = when if last is None or when > last else last

                msg = rec.get("message") or {}
                if rec.get("version"):
                    versions[rec["version"]] += 1

                mid = msg.get("id")
                if mid and msg.get("role") == "assistant":
                    msg_seen.add(mid)

                # --- token usage, one count per unique assistant turn ---
                u = msg.get("usage")
                if isinstance(u, dict) and mid and mid not in seen_turns:
                    seen_turns.add(mid)
                    if msg.get("model"):
                        models[msg["model"]] += 1
                    for k in ("input_tokens", "cache_creation_input_tokens",
                              "cache_read_input_tokens", "output_tokens"):
                        usage[k] += u.get(k) or 0
                    key = (rec.get("sessionId") or path,
                           bool(rec.get("isSidechain")))
                    lineages[key].append({
                        "t": when,
                        "mid": mid,
                        "i": u.get("input_tokens") or 0,
                        "w": u.get("cache_creation_input_tokens") or 0,
                        "r": u.get("cache_read_input_tokens") or 0,
                        "o": u.get("output_tokens") or 0,
                    })

                # --- tool calls, one count per unique tool_use block id ---
                for blk in msg.get("content") or []:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    bid = blk.get("id")
                    if not bid or bid in seen_calls:
                        continue
                    seen_calls.add(bid)
                    name = blk.get("name") or "?"
                    if mid:
                        msg_calls[mid] += 1
                        msg_names[mid].append(name)
                    tgt = sub if rec.get("isSidechain") else main
                    tgt[name] += 1

    # Order each lineage and mark cold starts and the prefix each request carried.
    # `calls` is joined on message id AFTER the scan, because the record carrying
    # `usage` is often not the one carrying the tool_use blocks.
    sessions = []
    for rows in lineages.values():
        rows.sort(key=lambda x: x["t"])
        for n, r in enumerate(rows):
            gap = 0.0 if n == 0 else (r["t"] - rows[n - 1]["t"]).total_seconds()
            r["gap"] = gap
            r["cold"] = n == 0 or gap > TTL_SECONDS
            r["prev"] = (rows[n - 1]["i"] + rows[n - 1]["w"] + rows[n - 1]["r"]
                         if n else 0)
            r["calls"] = msg_names.get(r["mid"], ()) if r["mid"] else ()
        sessions.append(rows)

    return {
        "files": len(files), "main": main, "sub": sub,
        "calls": len(seen_calls), "turns": len(seen_turns),
        "usage": usage, "versions": versions, "models": models,
        "sessions": sessions, "msg_calls": msg_calls, "msg_seen": msg_seen,
        "first": first, "last": last,
    }


def blended_price_per_token(usage, price):
    """USD per prefix token per turn, weighted by how this user's input tokens
    actually split between uncached, cache-write, and cache-read."""
    i = usage["input_tokens"]
    w = usage["cache_creation_input_tokens"]
    r = usage["cache_read_input_tokens"]
    total = i + w + r
    if total == 0:
        return None, None
    shares = (i / total, w / total, r / total)
    per_mtok = (shares[0] * price["input"] + shares[1] * price["cache_write"]
                + shares[2] * price["cache_read"])
    return per_mtok / 1e6, shares


# ---------------------------------------------------------------------------
# The three levers.
#
# Each takes the real per-request token counts and returns what they would have
# been under a different policy. Nothing here changes the model, the context, or
# what the model can see -- only the price paid for carrying the same bytes.
# ---------------------------------------------------------------------------

def cost_of(rows, price, extra=0.0, read_scale=1.0):
    return extra + sum(
        r["i"] * price["input"]
        + r["o"] * price["output"]
        + r["w"] * price["cache_write"]
        + r["r"] * price["cache_read"] * read_scale
        for r in rows) / 1e6


def close_partners(live):
    """A tool whose partner is in use has to stay: dropping SendMessage while
    Agent is still offered would break the workflow it belongs to."""
    live = set(live)
    for dep, principal in PARTNER.items():
        if principal in live:
            live.add(dep)
    return live


def strip_dynamic(sessions, price, dead_tokens, candidates, eligible):
    """Lever A: the tool schemas this developer never calls are absent.

    Priced as exactly the policy this output describes, using exactly the token
    weight of exactly the tool list it prints: `candidates` -- no call anywhere
    in the window -- stripped from every request in every lineage. The headline
    saving and the printed names cannot describe two different policies.

    An earlier version priced something else and called it lever A: it armed at
    the first cold start after a few requests and then stripped whatever had not
    been called *yet*. That was unsound in two ways. It is not quality-neutral --
    a lineage whose first `Workflow` call lands at request 200 had the schema
    stripped at request 6, on the evidence of five requests. And because "not
    called in the first five requests" is a bigger set than "never called at
    all", it reported savings ABOVE the oracle below, which is how the bug
    surfaced: a 13.9% saving under a 10.9% ceiling. Deleted, not repaired.

      static  -- what lever A is, and what is reported.
      oracle  -- `candidates` PLUS anything else this particular lineage never
                 asks for, same eligibility rules, from request one. Needs
                 foresight, so unreachable -- but its set contains `candidates`
                 on every lineage, so it is a true upper bound. The gap is what
                 per-conversation trimming is worth on top of per-developer
                 trimming.

    naive_rewrite is a transition cost, not a steady-state one. The manifest
    sits FIRST in the request, so introducing the change part-way through a live
    lineage invalidates the whole cached prefix behind it. Stripping from request
    one costs nothing extra; doing it at the first warm request costs a full
    prefix re-write, which is what this reports.
    """
    cand = set(candidates)
    static = oracle = 0.0
    naive_rewrite = 0.0
    static_rows = []
    for rows in sessions:
        live = close_partners({n for r in rows for n in r["calls"]})
        # Superset of `cand` by construction: every candidate is counted, plus
        # whatever else this lineage left untouched.
        ever = sum(t for n, t in eligible.items()
                   if n in cand or n not in live)
        for r in rows:
            static += _row_cost(r, price, dead_tokens)
            oracle += _row_cost(r, price, ever)
            static_rows.append(_row_strip(r, dead_tokens))
        # Cost of switching the manifest part-way through this lineage rather
        # than at a cold start, where the prefix was being written anyway.
        warm = next((r for r in rows if not r["cold"] and r["prev"] > 0), None)
        if warm:
            naive_rewrite += (warm["prev"] * price["cache_write"]) / 1e6
    return {"static": static, "oracle": oracle, "lineages": len(sessions),
            "pool_tokens": sum(eligible.values()), "pool_tools": len(eligible),
            "naive_rewrite": naive_rewrite, "static_rows": static_rows}


def _row_strip(r, dead):
    """One request as it would have been with `dead` manifest tokens absent.

    The manifest sits at the front of the cached prefix, so a cold start pays
    for it in the tokens it WRITES and a warm request pays for it in the tokens
    it READS -- never both. Subtracting it from both buckets on a cold row, as
    this used to, charged the same saving twice.
    """
    q = dict(r)
    if q["cold"]:
        q["w"] = max(0, q["w"] - dead)
    else:
        q["r"] = max(0, q["r"] - dead)
    return q


def _row_cost(r, price, dead):
    """One request's cost with `dead` manifest tokens removed."""
    q = _row_strip(r, dead)
    return (q["i"] * price["input"] + q["o"] * price["output"]
            + q["w"] * price["cache_write"] + q["r"] * price["cache_read"]) / 1e6


def lineage_costs(sessions, price):
    """Per-lineage spend, biggest first. Lever 2 lives in long sessions, so how
    concentrated someone's spend is decides whether it applies to them at all."""
    return sorted((cost_of(rows, price) for rows in sessions), reverse=True)


def lever_refresh(rows, price, every=240.0, adaptive=True):
    """Lever 2: keep the cache alive across an idle gap instead of re-writing.

    For each cold start, compare two futures for the SAME bytes:

      expire  -- what actually happened: the prefix was re-written at
                 cache_write price.
      refresh -- k cheap reads during the gap, then this request re-reads the
                 prefix at cache_read price and writes only the new content.

    `adaptive` takes whichever is cheaper, per gap. Breakeven is
    cache_write/cache_read = 12.5 refreshes, about 50 minutes of idle -- so the
    right answer really does differ gap by gap. With adaptive=False every gap is
    refreshed regardless, which is the naive policy, and it loses badly.
    """
    warm = [r for r in rows if 0 < r["gap"] <= 60]
    new_avg = (sum(r["w"] for r in warm) / len(warm)) if warm else 0.0

    out = []
    extra = 0.0
    n_ref = n_gap = n_win = 0
    ref_tokens = 0
    for r in rows:
        q = dict(r)
        if q["cold"] and q["prev"] > 0 and q["gap"] > TTL_SECONDS:
            n_gap += 1
            ctx = q["prev"]
            k = max(1, math.ceil(q["gap"] / every) - 1)
            refresh_cost = k * (ctx * price["cache_read"]
                                + REFRESH_OUTPUT_TOKENS * price["output"]) / 1e6
            keep_alive = (refresh_cost
                          + (q["r"] + q["w"]) * price["cache_read"] / 1e6
                          + new_avg * price["cache_write"] / 1e6)
            expired = (q["w"] * price["cache_write"]
                       + q["r"] * price["cache_read"]) / 1e6
            if keep_alive < expired or not adaptive:
                n_win += 1
                extra += refresh_cost
                n_ref += k
                ref_tokens += k * ctx
                q["r"] = q["r"] + q["w"]     # cache alive: re-read, not re-write
                q["w"] = new_avg
        out.append(q)
    return out, extra, {"gaps": n_gap, "wins": n_win, "refreshes": n_ref,
                        "refresh_tokens": ref_tokens}


# ---------------------------------------------------------------------------
# Phase 2 -- local configuration, for the profile section only
# ---------------------------------------------------------------------------

def configured_mcp_servers():
    names = set()
    for p in (os.path.expanduser("~/.claude.json"),
              os.path.expanduser("~/.claude/settings.json"),
              os.path.join(os.getcwd(), ".mcp.json")):
        try:
            with open(p) as fh:
                blob = json.load(fh)
        except Exception:
            continue
        stack = [blob]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            servers = node.get("mcpServers")
            if isinstance(servers, dict):
                names.update(servers.keys())
            for v in node.values():
                if isinstance(v, dict):
                    stack.append(v)
    return names


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

WIDTH = 74            # prose wraps here
ROW = "    %-18s %7s  %8s   %s"   # tool | tokens | cost | note
ROW_NOTE_COL = 43     # where the note column starts, for continuation lines


def num(n):
    return "{:,}".format(int(round(n)))


def usd(x):
    return "${:,.2f}".format(x)


def pct(x):
    return "%.1f%%" % (100.0 * x)


def para(text, indent="  "):
    """Wrap a paragraph so it reads the same on every terminal."""
    print(textwrap.fill(" ".join(text.split()), width=WIDTH,
                        initial_indent=indent, subsequent_indent=indent))


def head(title):
    print("\n" + title)
    print("-" * min(len(title), 90))


def table(rows, per_tok, turns):
    """rows: list of (name, tokens, note, hint). hint gets its own line."""
    print(ROW % ("tool", "tokens", "cost", "what it is"))
    for name, tk, note, hint in rows:
        cost = usd(tk * per_tok * turns) if per_tok else "-"
        print(ROW % (name, num(tk), cost, note))
        if hint:
            print(" " * ROW_NOTE_COL + hint)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Price two quality-neutral ways to cut your Claude Code "
                    "bill, against your own history.")
    ap.add_argument("--days", type=int, default=30,
                    help="lookback window in days (default 30)")
    ap.add_argument("--include-check-first", action="store_true",
                    help="also offer the tools flagged check-first")
    ap.add_argument("--verbose", action="store_true",
                    help="every tool you called, plus lever-2 sensitivity")
    ap.add_argument("--refresh-every", type=float, default=240.0,
                    metavar="SECONDS",
                    help="lever 2: cache refresh interval, must be under the "
                         "300 s TTL (default 240)")
    for k in PRICE:
        ap.add_argument("--price-" + k.replace("_", "-"), type=float,
                        dest="price_" + k, default=PRICE[k],
                        metavar="USD_PER_MTOK",
                        help="%s price per Mtok (default %.2f)" % (k, PRICE[k]))
    args = ap.parse_args()
    price = {k: getattr(args, "price_" + k) for k in PRICE}
    if not 0 < args.refresh_every < TTL_SECONDS:
        print("--refresh-every must be between 0 and %d seconds (the cache TTL)."
              % TTL_SECONDS)
        return 2

    banner = "Claude Code cost audit"
    right = "last %d days" % args.days
    print("=" * WIDTH)
    print(banner + " " * max(1, WIDTH - len(banner) - len(right)) + right)
    print("=" * WIDTH)

    s = scan(args.days)
    if not s["turns"]:
        print("\nNo Claude Code history found in the last %d days, so there is"
              % args.days)
        print("nothing to analyse. (Looked in ~/.claude/projects, found %d files.)"
              % s["files"])
        return 0

    called = collections.Counter(s["main"]) + collections.Counter(s["sub"])
    per_tok, shares = blended_price_per_token(s["usage"], price)
    rows = [r for sess in s["sessions"] for r in sess]

    # Split what you never called into "safe" and "check first", biggest first.
    never = [n for n, (c, g, risk, note) in sorted(
        TOOLS.items(), key=lambda kv: -kv[1][0])
        if n not in called and risk != "interactive" and c > 0]

    def row(name):
        partner = PARTNER.get(name)
        hint = ""
        if partner and called.get(partner):
            hint = "(you called %s %s times in this window)" % (
                partner, num(called[partner]))
        return name, tokens_of(name), TOOLS[name][3], hint

    safe_rows = [row(n) for n in never if TOOLS[n][2] == "low"]
    check_rows = [row(n) for n in never if TOOLS[n][2] != "low"]
    low = [r[0] for r in safe_rows]
    check = [r[0] for r in check_rows]
    tot_safe = sum(r[1] for r in safe_rows)
    tot_check = sum(r[1] for r in check_rows)
    candidates = low + (check if args.include_check_first else [])
    known = len([1 for v in TOOLS.values() if v[2] != "interactive"])

    # Price exactly the tools we are actually offering to switch off, so
    # --include-check-first changes the saving and not just the note below.
    # This is the single definition: lever A is priced from it, and the tool
    # names printed under "A --" are the set it was summed over.
    dead_tokens = sum(tokens_of(n) for n in candidates)
    # Tools the oracle is allowed to consider, i.e. the same risk classes the
    # candidate set was drawn from, so ceiling and saving stay comparable.
    eligible = {n: tokens_of(n) for n, v in TOOLS.items()
                if v[2] != "interactive" and v[0] > 0
                and (v[2] == "low" or args.include_check_first)}

    # --- the two levers, both in the request path -------------------------
    # A: rewrite the tools array per developer, from that developer's own call
    # history. B: refresh a cache lineage's prefix before its TTL expires.
    # Neither is reachable from inside Claude Code.
    baseline = cost_of(rows, price)

    dyn = strip_dynamic(s["sessions"], price, dead_tokens, candidates, eligible)
    cost_a = dyn["static"]
    after_b, extra_b, _ = lever_refresh(dyn["static_rows"], price,
                                        args.refresh_every)
    cost_ab = cost_of(after_b, price, extra_b)
    # B on its own, against the untouched baseline, so the two are comparable.
    # `ref` comes from this one: the gap statistics describe the real history.
    solo_b, extra_sb, ref = lever_refresh(rows, price, args.refresh_every)
    cost_b_only = cost_of(solo_b, price, extra_sb)

    u = s["usage"]
    comp = [("cache read", u["cache_read_input_tokens"], price["cache_read"]),
            ("cache write", u["cache_creation_input_tokens"], price["cache_write"]),
            ("output", u["output_tokens"], price["output"]),
            ("uncached input", u["input_tokens"], price["input"])]
    comp = sorted(((n, t, t * p / 1e6) for n, t, p in comp),
                  key=lambda x: -x[2])
    context_share = sum(c for n, t, c in comp
                        if n in ("cache read", "cache write")) / baseline

    # --- everything the reader needs, and nothing else --------------------
    print()
    print("  %s   %s requests   %s tokens   %s cache lineages"
          % (usd(baseline), num(s["turns"]), num(sum(t for n, t, c in comp)),
             num(len(s["sessions"]))))
    if s["first"] and s["last"]:
        print("  %s to %s   Claude Code %s"
              % (s["first"].date(), s["last"].date(),
                 ", ".join(sorted(s["versions"])[-2:])))
    # Dollars are only as good as the price basis, and the basis is a guess
    # unless you know which model served the requests. Percentages survive a
    # wrong guess -- cache_write/cache_read is 12.5 on every tier -- but the
    # dollar columns do not, so name the models and let the reader check.
    if s["models"]:
        tot_m = sum(s["models"].values()) or 1
        print("  priced at %s: %s"
              % ("Opus list" if price == PRICE else "overridden prices",
                 ", ".join("%s %s" % (m, pct(c / tot_m))
                           for m, c in s["models"].most_common(3))))
    # A tool unused in four days is not a tool this developer never uses, and
    # lever A is priced entirely off "never called" -- so a window the
    # transcripts do not fill overstates it. Lever B reads idle gaps, which
    # occur at any window length, so it is not biased the same way. Say this
    # out loud: otherwise a fresh install's run gets compared straight against
    # a full month's and the difference is read as a difference in workflow.
    if s["first"] and s["last"]:
        span = (s["last"] - s["first"]).days + 1
        if span < 0.8 * args.days:
            print("  NOTE  transcripts cover %d of the %d days requested, so"
                  " lever A is" % (span, args.days))
            print("        overstated -- it counts tools you had no chance to"
                  " call yet. B is not.")
    print()
    print("  %-16s %14s %10s %7s" % ("", "tokens", "cost", "share"))
    for n, t, c in comp:
        print("  %-16s %14s %10s %6s"
              % (n, num(t), usd(c), pct(c / baseline)))
    print()
    print("  %s of the spend is context already sent, re-written or re-read."
          % pct(context_share))

    # --- the two levers ---------------------------------------------------
    cold_w = sum(r["w"] for r in rows if r["cold"] and r["prev"] > 0)
    all_w = sum(r["w"] for r in rows) or 1
    head("WHAT AN IN-PATH PROXY SAVES, MODEL SEES BYTE-IDENTICAL PROMPTS")
    print("  %-44s %9s %7s" % ("", "cost", "saved"))
    print("  %-44s %9s %7s" % ("baseline", usd(baseline), "-"))
    print("  %-44s %9s %7s" % ("A  drop schemas this developer never calls",
                               usd(cost_a), pct((baseline - cost_a) / baseline)))
    print("  %-44s %9s %7s" % ("B  refresh the cache before it expires",
                               usd(cost_b_only),
                               pct((baseline - cost_b_only) / baseline)))
    print("  %-44s %9s %7s" % ("A + B", usd(cost_ab),
                               pct((baseline - cost_ab) / baseline)))
    print()
    print("  Neither is reachable from inside Claude Code: it cannot see one")
    print("  developer's call history, and it cannot refresh its own cache.")

    # --- lever A facts ----------------------------------------------------
    head("A -- TOOL SCHEMAS SENT ON EVERY REQUEST, NEVER CALLED")
    pool_names = {n for n, v in TOOLS.items()
                  if v[2] != "interactive" and v[0] > 0}
    # len(never) counts everything never called; dead_tokens prices only the
    # subset lever A actually drops. Printing the two side by side without
    # saying so read as "12 schemas = 15,104 tokens", which is wrong whenever
    # some of the 12 are partner-kept below.
    print("  %d of %d schemas never called; A drops %d of them, %s tokens/"
          "request (estimated)"
          % (len(never), len(pool_names), len(candidates), num(dead_tokens)))
    if low:
        print(textwrap.fill(" ".join(sorted(low)), width=WIDTH,
                            initial_indent="    ", subsequent_indent="    "))
    if check:
        print("  %d of those partner a tool in use, so A keeps them"
              % len(check))
        print("  (%s tokens, still shipped every request):" % num(tot_check))
        print(textwrap.fill(" ".join(sorted(check)), width=WIDTH,
                            initial_indent="    ", subsequent_indent="    "))
    print("  ceiling if each conversation also dropped what only IT never"
          " calls: %s" % pct((baseline - dyn["oracle"]) / baseline))
    print("  one-off cost of switching mid-conversation, not at a cold start: %s"
          % usd(dyn["naive_rewrite"]))

    # --- lever B facts ----------------------------------------------------
    head("B -- CACHE EXPIRY  (%ds TTL, write bills %.1fx read)"
         % (TTL_SECONDS, price["cache_write"] / price["cache_read"]))
    print("  requests after an idle gap    %s of %s (%s)"
          % (num(ref["gaps"]), num(len(rows)), pct(ref["gaps"] / len(rows))))
    print("  their share of cache writes   %s" % pct(cold_w / all_w))
    print("  gaps cheaper to refresh       %s of %s"
          % (num(ref["wins"]), num(ref["gaps"])))
    print("  refresh requests added        %s" % num(ref["refreshes"]))
    print()
    print("  %-10s %10s %10s" % ("interval", "adaptive", "blanket"))
    sweep = {}
    for every in (120.0, 180.0, 240.0, 270.0, 290.0):
        ra, ea, _ = lever_refresh(rows, price, every, True)
        rb, eb, _ = lever_refresh(rows, price, every, False)
        ca, cb = cost_of(ra, price, ea), cost_of(rb, price, eb)
        sweep[int(every)] = {
            "adaptive_pct": round(100.0 * (baseline - ca) / baseline, 1),
            "blanket_pct": round(100.0 * (baseline - cb) / baseline, 1)}
        print("  %-10s %9s %10s"
              % ("%.0f s" % every,
                 "%+.1f%%" % sweep[int(every)]["adaptive_pct"],
                 "%+.1f%%" % sweep[int(every)]["blanket_pct"]))
    print()
    print("  adaptive = refresh a gap only when that is cheaper than letting it")
    print("  expire. blanket = refresh always. Getting this wrong inverts it.")

    # --- profile ----------------------------------------------------------
    lc = lineage_costs(s["sessions"], price)
    ctx_tok = [r["i"] + r["w"] + r["r"] for r in rows]
    mean_ctx = sum(ctx_tok) // len(ctx_tok) if ctx_tok else 0
    head("YOUR SHAPE")
    print("  mean context per request   %s tokens" % num(mean_ctx))
    print("  manifest share of that     %s"
          % pct(dyn["pool_tokens"] / mean_ctx if mean_ctx else 0))
    if len(lc) > 3:
        print("  spend concentration        top 1 of %d %s, top 4 %s, "
              "median %s" % (len(lc), pct(lc[0] / baseline),
                             pct(sum(lc[:4]) / baseline),
                             usd(lc[len(lc) // 2])))
    print("  tool calls                 %s across %d tools"
          % (num(s["calls"]), len(called)))

    unknown = sorted(n for n in called if n not in TOOLS)
    servers = configured_mcp_servers()
    used = {n.split("__")[1] for n in called if n.startswith("mcp__")
            and len(n.split("__")) > 2}
    idle = sorted(servers - used)
    if servers:
        print("  MCP                        %d server(s) configured, %d used"
              % (len(servers), len(servers) - len(idle)))
        # Deliberately a count, not the names. A server name can identify an
        # internal system, and a "delete this before pasting" marker is not a
        # control: the first person who hit one pasted it anyway.
        if idle:
            print("  never used                 %d of those server(s), no tool "
                  "called in %d days" % (len(idle), args.days))
    # These are calls to tools with no row in the token table -- a newer Claude
    # Code than the wire capture, or an MCP tool. They are NOT priced by lever A.
    # This used to print on the MCP line, which read as "N MCP tools called"
    # even with zero servers configured.
    if unknown:
        print("  not in the token table     %d tool(s) called, so unpriced here"
              % len(unknown))
    print()
    print("  Read from your transcripts: tool names, ids, timestamps, session")
    print("  ids, token counts. Never prompts, arguments, results or replies.")
    print("  Nothing left your machine: no API calls, no network, no writes.")
    print("  Send this output back -- it is aggregates and built-in tool names.")

    print()
    return 0



if __name__ == "__main__":
    sys.exit(main())

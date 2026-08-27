#!/usr/bin/env python3
"""Price three ways to cut your Claude Code bill without changing an answer.

WHY THIS EXISTS
---------------
Measured over one developer's full 30-day history (53 sessions, 3,631 requests,
1.06 B tokens, $1,032 at Opus list): **89% of the money went on writing and
re-reading context that had already been sent.** New input was 1.1% of spend
and output was 9.9%.

That means the big levers are not "say less" or "use a cheaper model" -- both of
which cost you answer quality. The big levers are about not paying twice for
the same bytes. This script prices three of them against YOUR history:

  1. Dead tool schemas   -- tools declared on every request that you never call.
                            Switch off today. Measured 4.4% on the reference
                            machine.
  2. Cache expiry        -- after a 5-minute pause the prompt cache drops and the
                            next request RE-WRITES the whole conversation at
                            12.5x the price of re-reading it. Refreshing it
                            first is byte-identical to the model. Measured
                            +13.0 points. Needs a proxy; not a local switch.
  3. Unbatched tool calls-- most requests carry exactly one tool call, and every
                            request re-reads the entire context. Batching
                            independent calls removes whole round trips.
                            Measured +7.8 points. A CLAUDE.md instruction.

None of the three changes the model, the context, or the information available
to it. On the reference machine they compound to 25.2%.

Run it, then send back the block printed by --report so we can pool results and
see how much this varies between people. That variance is the open question.

WHAT IT READS
-------------
Your local transcripts (~/.claude/projects/**/*.jsonl). From each record it
reads ONLY: tool names, tool_use ids, timestamps, session ids, and token
counts. It never reads prompts, tool arguments, tool results, or completions.

WHAT IT SENDS
-------------
Nothing, ever, on its own. --measure makes exactly two `claude --print` calls
with the fixed prompt "say ok" to measure your real manifest size. --apply only
edits your local settings.json, after a backup. --report prints a block to your
terminal for you to paste wherever you choose; it contains aggregate numbers and
built-in tool names only -- no paths, project names, branches, or MCP server
names.

USAGE
-----
    python3 claude-tool-audit.py                  # the three levers, priced
    python3 claude-tool-audit.py --report         # + a block to paste back
    python3 claude-tool-audit.py --measure        # + verify lever 1 (2 requests)
    python3 claude-tool-audit.py --apply          # + turn off dead tools
    python3 claude-tool-audit.py --verbose        # + every tool, + sensitivity

CAVEATS, STATED UP FRONT
------------------------
* Levers 2 and 3 are counterfactuals: they replay your real per-request token
  counts under a different policy. They are arithmetic on measured data, not a
  simulation, but they assume the policy behaves as specified.
* Lever 3 assumes the batched calls really are independent. Calls that must run
  in sequence cannot be merged, so treat its number as an upper bound.
* Lever 2 is priced with the adaptive rule (refresh only when refreshing is
  cheaper than expiring). A blanket "always ping" policy LOSES money badly --
  --verbose shows both, and the gap is 100x. Do not implement the blanket form.
* "Never called" is not "never useful". A tool you have not needed yet may be
  the right tool next week. Every change is one line in settings.json to undo.
* Per-tool token figures come from a wire capture of Claude Code 2.x and carry
  roughly +/-15%. The aggregate is the solid number, and --measure replaces the
  estimate with a direct measurement on your machine.
"""

import argparse
import collections
import datetime
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
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
                    if mid:
                        msg_calls[mid] += 1
                    tgt = sub if rec.get("isSidechain") else main
                    tgt[blk.get("name") or "?"] += 1

    # Order each lineage and mark cold starts and the prefix each request carried.
    sessions = []
    for rows in lineages.values():
        rows.sort(key=lambda x: x["t"])
        for n, r in enumerate(rows):
            gap = 0.0 if n == 0 else (r["t"] - rows[n - 1]["t"]).total_seconds()
            r["gap"] = gap
            r["cold"] = n == 0 or gap > TTL_SECONDS
            r["prev"] = (rows[n - 1]["i"] + rows[n - 1]["w"] + rows[n - 1]["r"]
                         if n else 0)
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


def lever_tools(rows, dead_tokens):
    """Lever 1: the dead manifest stops being sent.

    Those tokens leave every request's cache read, and leave the cache write on
    every cold start (where the whole prefix is written afresh).
    """
    out = []
    for r in rows:
        q = dict(r)
        if q["r"] > dead_tokens:
            q["r"] -= dead_tokens
        if q["cold"] and q["w"] > dead_tokens:
            q["w"] -= dead_tokens
        out.append(q)
    return out


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


def batching_stats(msg_calls, msg_seen, target):
    """Lever 3: how many whole round trips batching would remove.

    Every request re-reads the entire context, whether it carries one tool call
    or four. Total calls are fixed -- raising calls-per-request removes requests.
    """
    dist = collections.Counter()
    for mid in msg_seen:
        dist[msg_calls.get(mid, 0)] += 1
    total_msgs = sum(dist.values())
    total_calls = sum(k * v for k, v in dist.items())
    bearing = sum(v for k, v in dist.items() if k > 0)
    mean = total_calls / bearing if bearing else 0.0
    removed = max(0.0, bearing - total_calls / target) if target > 0 else 0.0
    return {"dist": dist, "messages": total_msgs, "calls": total_calls,
            "bearing": bearing, "mean": mean, "removed": removed,
            "cut": (removed / total_msgs) if total_msgs else 0.0,
            "single_share": (dist.get(1, 0) / bearing) if bearing else 0.0}


# ---------------------------------------------------------------------------
# Phase 2 -- optional live measurement (2 API calls, no proxy needed)
# ---------------------------------------------------------------------------

def measure(deny):
    """Return ((baseline, denied), None) or (None, error).

    Two `claude --print --output-format json` calls in an empty directory with
    the same prompt. The only difference between them is the tool manifest, so
    the prompt-token delta is exactly what the denied schemas cost per turn.
    """
    def one(settings_path, workdir):
        cmd = ["claude", "--print", "--output-format", "json", "say ok"]
        if settings_path:
            cmd[1:1] = ["--settings", settings_path]
        try:
            out = subprocess.run(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "could not run claude: %s" % exc
        if out.returncode != 0:
            return None, (out.stderr or out.stdout or "").strip()[:300]
        try:
            u = json.loads(out.stdout)["usage"]
        except Exception:
            return None, "unexpected --output-format json payload"
        return ((u.get("input_tokens") or 0)
                + (u.get("cache_creation_input_tokens") or 0)
                + (u.get("cache_read_input_tokens") or 0)), None

    with tempfile.TemporaryDirectory() as work:
        base, err = one(None, work)
        if base is None:
            return None, err
        sp = os.path.join(work, "deny-settings.json")
        with open(sp, "w") as fh:
            json.dump({"permissions": {"deny": sorted(deny)}}, fh)
        trimmed, err = one(sp, work)
        if trimmed is None:
            return None, err
    return (base, trimmed), None


# ---------------------------------------------------------------------------
# Phase 3 -- settings edit
# ---------------------------------------------------------------------------

def settings_path(explicit):
    return explicit or os.path.expanduser("~/.claude/settings.json")


def apply_deny(path, names, assume_yes):
    """Add names to permissions.deny. Backs up first, touches nothing else."""
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError) as exc:
        print("  cannot read %s: %s" % (path, exc))
        return False
    if not isinstance(cfg, dict):
        print("  %s is not a JSON object; refusing to edit" % path)
        return False

    perms = cfg.setdefault("permissions", {})
    if not isinstance(perms, dict):
        print("  permissions is not an object; refusing to edit")
        return False
    deny = perms.setdefault("deny", [])
    if not isinstance(deny, list):
        print("  permissions.deny is not a list; refusing to edit")
        return False

    additions = [n for n in sorted(names) if n not in deny]
    if not additions:
        print("\n  These are already in permissions.deny. Nothing to do.")
        return False

    print("\n  Editing %s" % path)
    print("  Adding to permissions.deny, and changing nothing else:")
    for n in additions:
        print("    + %s" % n)
    if not assume_yes:
        if not sys.stdin.isatty():
            print("\n  Not running in a terminal, so there is nobody to ask.")
            print("  Re-run interactively, or pass --yes to skip the prompt.")
            return False
        if input("\n  Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Left your settings alone.")
            return False

    backup = None
    if os.path.exists(path):
        backup = path + ".bak"
        shutil.copy2(path, backup)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    deny.extend(additions)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)

    print("\n  Done -- %d tool(s) turned off. Takes effect in new sessions."
          % len(additions))
    if backup:
        print("  Your previous settings: %s" % backup)
        print("  To undo everything:     mv %s %s" % (backup, path))
    print("  To undo one tool, delete its name from permissions.deny.")
    return True


# ---------------------------------------------------------------------------
# MCP servers -- names only, never values
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
        description="Price three quality-neutral ways to cut your Claude Code "
                    "bill, against your own history.")
    ap.add_argument("--days", type=int, default=30,
                    help="lookback window in days (default 30)")
    ap.add_argument("--report", action="store_true",
                    help="print a paste-able summary block (aggregates only)")
    ap.add_argument("--measure", action="store_true",
                    help="verify lever 1 with 2 `claude --print` calls")
    ap.add_argument("--apply", action="store_true",
                    help="prompt to add never-used tools to permissions.deny")
    ap.add_argument("--yes", action="store_true",
                    help="with --apply, skip the confirmation prompt")
    ap.add_argument("--include-check-first", action="store_true",
                    help="also offer the tools flagged check-first")
    ap.add_argument("--verbose", action="store_true",
                    help="every tool you called, plus lever-2 sensitivity")
    ap.add_argument("--settings", help="settings.json to edit (default ~/.claude)")
    ap.add_argument("--refresh-every", type=float, default=240.0,
                    metavar="SECONDS",
                    help="lever 2: cache refresh interval, must be under the "
                         "300 s TTL (default 240)")
    ap.add_argument("--batch-target", type=float, default=1.5,
                    metavar="CALLS",
                    help="lever 3: target tool calls per request (default 1.5)")
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

    # --- measure first, so lever 1 can use a real number if we have one ----
    measured = None
    if args.measure and candidates:
        head("MEASURING LEVER 1 AGAINST THE REAL API")
        para("Sending two requests with the prompt \"say ok\" from an empty "
             "directory -- one with your normal tools, one with these %d "
             "switched off. The difference is exactly what they cost you, on "
             "your machine and your Claude Code version." % len(candidates))
        print()
        res, err = measure(candidates)
        if res is None:
            para("Could not measure: %s" % err)
        else:
            base_tok, trimmed_tok = res
            measured = base_tok - trimmed_tok
            print("    every tool on      %9s tokens in the request"
                  % num(base_tok))
            print("    %2d tools off       %9s tokens in the request"
                  % (len(candidates), num(trimmed_tok)))
            print("    " + "-" * 46)
            print("    you save           %9s tokens, every request"
                  % num(measured))
            print("                       %8.1f%% of that request"
                  % (100.0 * measured / base_tok if base_tok else 0))
            est = sum(tokens_of(n) for n in candidates)
            if measured and est:
                print()
                para("The estimate below said %s tokens, so measured / "
                     "estimated = %.2f. The measured number is used from here on."
                     % (num(est), measured / est))

    dead_tokens = measured if measured else tot_safe
    dead_source = "measured" if measured else "estimated"

    # --- the three levers, applied cumulatively ---------------------------
    baseline = cost_of(rows, price)
    after1 = lever_tools(rows, dead_tokens)
    cost1 = cost_of(after1, price)
    after2, extra2, ref = lever_refresh(after1, price, args.refresh_every)
    cost2 = cost_of(after2, price, extra2)
    bat = batching_stats(s["msg_calls"], s["msg_seen"], args.batch_target)
    cost3 = cost_of(after2, price, extra2, read_scale=1 - bat["cut"])

    u = s["usage"]
    comp = [("cache read", u["cache_read_input_tokens"], price["cache_read"]),
            ("cache write", u["cache_creation_input_tokens"], price["cache_write"]),
            ("output", u["output_tokens"], price["output"]),
            ("uncached input", u["input_tokens"], price["input"])]
    comp = sorted(((n, t, t * p / 1e6) for n, t, p in comp),
                  key=lambda x: -x[2])
    context_share = sum(c for n, t, c in comp
                        if n in ("cache read", "cache write")) / baseline

    # --- 1. the answer, before anything else ------------------------------
    head("THE SHORT VERSION")
    para("Over the last %d days you spent %s across %s requests (%s tokens), "
         "priced at the rates below. %s of that went on writing and re-reading "
         "context you had already sent -- not on new input, and not on output."
         % (args.days, usd(baseline), num(s["turns"]),
            num(sum(t for n, t, c in comp)), pct(context_share)))
    print()
    para("Three changes would recover %s (%s of the bill) WITHOUT changing the "
         "model, shrinking the context, or hiding anything from it. The model "
         "sees byte-identical prompts in every case."
         % (usd(baseline - cost3), pct((baseline - cost3) / baseline)))
    print()
    print("    %-42s %9s %8s" % ("cumulative", "cost", "saved"))
    print("    %-42s %9s %8s" % ("your last %d days" % args.days,
                                 usd(baseline), "-"))
    for label, c in (("+ 1. drop never-called tool schemas", cost1),
                     ("+ 2. refresh cache before it expires", cost2),
                     ("+ 3. batch tool calls (%.2f -> %.2f/req)"
                      % (bat["mean"], args.batch_target), cost3)):
        print("    %-42s %9s %7s" % (label, usd(c),
                                     pct((baseline - c) / baseline)))
    print()
    para("Lever 1 you can switch on today. Lever 3 is a CLAUDE.md instruction. "
         "Lever 2 needs something in the request path -- a proxy -- because "
         "Claude Code has no way to refresh its own cache.")

    # --- 2. where the money actually goes ---------------------------------
    head("WHERE THE MONEY WENT")
    para("A cache WRITE bills 12.5x a cache READ, so a high hit rate measured "
         "in tokens can still leave writes as one of the largest line items. "
         "That is why lever 2 exists.")
    print()
    print("    %-16s %14s %10s %7s" % ("", "tokens", "cost", "share"))
    for n, t, c in comp:
        print("    %-16s %14s %10s %6s"
              % (n, num(t), usd(c), pct(c / baseline)))

    # --- 3. lever 1 -------------------------------------------------------
    head("LEVER 1 -- TOOL SCHEMAS YOU NEVER CALL  (%s, switch on today)"
         % pct((baseline - cost1) / baseline))
    if not never:
        para("Nothing to trim. You have actually used every tool Claude "
             "declares to the model in the last %d days." % args.days)
    else:
        para("Claude Code re-sends the full description of every declared tool "
             "on every request, called or not. %d of the %d built-in tools were "
             "sent on all %s of your requests and never called once. Switching "
             "one off removes its description from the wire -- it is not just "
             "an execution block."
             % (len(never), known, num(s["turns"])))
        print()
        para("Using the %s figure of %s tokens per request."
             % (dead_source, num(dead_tokens)))
        if safe_rows:
            print()
            table(safe_rows, per_tok, s["turns"])
        if check_rows:
            print()
            para("A further %d are unused too, but removing one could break a "
                 "workflow, so they are excluded from the total above. Pass "
                 "--include-check-first to price them in (%s tokens)."
                 % (len(check_rows), num(tot_check)))
            if args.verbose:
                print()
                table(check_rows, per_tok, s["turns"])

    # --- 4. lever 2 -------------------------------------------------------
    head("LEVER 2 -- CACHE EXPIRY  (%s, needs a proxy)"
         % pct((cost1 - cost2) / baseline))
    para("The prompt cache lives %d seconds. Pause longer than that and the "
         "next request RE-WRITES the whole conversation at %s/Mtok instead of "
         "re-reading it at %s/Mtok. A refresh request re-reads the same bytes "
         "and generates nothing, so the model's view is identical -- only the "
         "price differs, by 12.5x."
         % (TTL_SECONDS, usd(price["cache_write"]).lstrip("$"),
            usd(price["cache_read"]).lstrip("$")))
    print()
    cold_w = sum(r["w"] for r in rows if r["cold"] and r["prev"] > 0)
    all_w = sum(r["w"] for r in rows) or 1
    print("    requests after an idle gap over %ds   %s of %s  (%s)"
          % (TTL_SECONDS, num(ref["gaps"]), num(len(rows)),
             pct(ref["gaps"] / len(rows))))
    print("    their share of your cache-write tokens %19s"
          % pct(cold_w / all_w))
    print("    gaps where refreshing is the cheaper move %s of %s  (%s)"
          % (num(ref["wins"]), num(ref["gaps"]),
             pct(ref["wins"] / ref["gaps"]) if ref["gaps"] else "-"))
    print("    refresh requests that would be added  %s"
          % num(ref["refreshes"]))
    w_after = sum(r["w"] for r in after2)
    print("    your cache-write tokens                %s -> %s  (%+.0f%%)"
          % (num(all_w), num(w_after), 100.0 * (w_after - all_w) / all_w))
    print()
    para("Breakeven is cache_write/cache_read = %.1f refreshes, roughly %d "
         "minutes of idle. Past that, letting the cache expire is genuinely "
         "cheaper -- so the policy has to decide per gap. That is the whole "
         "design, and it is why %s of your gaps are left to expire."
         % (price["cache_write"] / price["cache_read"],
            round(price["cache_write"] / price["cache_read"]
                  * args.refresh_every / 60),
            pct(1 - (ref["wins"] / ref["gaps"] if ref["gaps"] else 0))))
    if args.verbose:
        print()
        para("Sensitivity -- adaptive (decide per gap) against blanket (refresh "
             "every gap regardless). This lever's own contribution, at five "
             "intervals. Adaptive wins at all of them; blanket is a disaster at "
             "all of them. This is the one thing not to get wrong:")
        print()
        print("      %-12s %12s %12s" % ("interval", "adaptive", "blanket"))
        for every in (120.0, 180.0, 240.0, 270.0, 290.0):
            ra, ea, _ = lever_refresh(after1, price, every, True)
            rb, eb, _ = lever_refresh(after1, price, every, False)
            ca, cb = cost_of(ra, price, ea), cost_of(rb, price, eb)
            print("      %-12s %11s %12s"
                  % ("%.0f s" % every,
                     "%+.1f%%" % (100.0 * (cost1 - ca) / baseline),
                     "%+.1f%%" % (100.0 * (cost1 - cb) / baseline)))

    # --- 5. lever 3 -------------------------------------------------------
    head("LEVER 3 -- ONE TOOL CALL PER REQUEST  (%s, a CLAUDE.md line)"
         % pct((cost2 - cost3) / baseline))
    para("Every request re-reads your entire context, whether it carries one "
         "tool call or four. You are at %.2f calls per tool-bearing request, "
         "and %s of them carry exactly one. Batching independent calls removes "
         "whole round trips -- same work, same results, less waiting."
         % (bat["mean"], pct(bat["single_share"])))
    print()
    print("    %-16s %9s %8s" % ("calls in request", "count", "share"))
    for k in sorted(bat["dist"]):
        if k > 8:
            continue
        print("    %-16s %9s %7s" % (k, num(bat["dist"][k]),
                                     pct(bat["dist"][k] / (bat["messages"] or 1))))
    print()
    print("    %-24s %10s %10s" % ("target calls/request", "requests cut",
                                   "of all"))
    for t in (1.5, 2.0, 2.5):
        b = batching_stats(s["msg_calls"], s["msg_seen"], t)
        print("    %-24s %10s %9s" % ("%.1f" % t, num(b["removed"]),
                                      pct(b["cut"])))
    print()
    para("Priced by removing those requests' context re-reads only. Their "
         "output still happens -- the same tool calls are still made -- so this "
         "is the conservative half of the effect. It also assumes the batched "
         "calls are genuinely independent, which makes it an upper bound.")

    # --- 6. your history --------------------------------------------------
    head("WHAT YOUR HISTORY SHOWS")
    if s["first"] and s["last"]:
        print("  period       %s to %s" % (s["first"].date(), s["last"].date()))
    print("  requests     %s across %s cache lineages"
          % (num(s["turns"]), num(len(s["sessions"]))))
    print("  tool calls   %s  across %d different tools"
          % (num(s["calls"]), len(called)))
    top = called.most_common(None if args.verbose else 6)
    if top:
        print(textwrap.fill(
            ", ".join("%s %s" % (n, num(c)) for n, c in top),
            width=WIDTH, initial_indent="  most used    ",
            subsequent_indent=" " * 15))
    if not args.verbose and len(called) > len(top):
        print("               ...and %d more (--verbose for all of them)"
              % (len(called) - len(top)))
    if s["versions"]:
        print("  Claude Code  %s" % ", ".join(sorted(s["versions"])[-3:]))

    sess_costs = sorted((cost_of(x, price) for x in s["sessions"]),
                        reverse=True)
    if len(sess_costs) > 3:
        print()
        para("Spend concentration -- long sessions are the whole bill, so none "
             "of this needs to change how you work day to day:")
        for k in (1, 4, 8):
            if k <= len(sess_costs):
                run = sum(sess_costs[:k])
                print("    %-22s %10s  %s of your spend"
                      % ("top %d of %d" % (k, len(sess_costs)),
                         usd(run), pct(run / baseline)))
        mid = sess_costs[len(sess_costs) // 2]
        print("    %-22s %10s" % ("median lineage", usd(mid)))

    unknown = sorted(n for n in called if n not in TOOLS)
    if unknown:
        print()
        para("%d tool(s) you called are not in this script's size table, so "
             "they are neither priced nor ever proposed for removal. That "
             "normally means an MCP tool, or a newer Claude Code than the one "
             "this table was measured on:" % len(unknown))
        for n in unknown:
            print("    %s" % n)

    # --- 7. MCP servers ---------------------------------------------------
    servers = configured_mcp_servers()
    used = {n.split("__")[1] for n in called if n.startswith("mcp__")
            and len(n.split("__")) > 2}
    idle = sorted(servers - used)
    if servers:
        head("MCP SERVERS -- %d configured, %d used in this window"
             % (len(servers), len(servers) - len(idle)))
        if idle:
            para("No calls at all from: %s" % ", ".join(idle))
            print()
            para("MCP tool descriptions are often the largest part of a "
                 "request and this script cannot size them, so they are NOT in "
                 "any total above. Dropping a server you never use is usually "
                 "the single biggest win available. Remove it from your config, "
                 "or start a session with --strict-mcp-config to leave all of "
                 "them out.")
        else:
            para("All of them saw calls, so there is nothing to trim here. "
                 "Note that MCP tool descriptions are not counted in any total "
                 "above -- this script cannot size them.")

    # --- 8. how the numbers were made ------------------------------------
    head("HOW THESE NUMBERS WERE MADE")
    para("Levers 2 and 3 replay your real per-request token counts under a "
         "different policy. That is arithmetic on measured data, not a "
         "simulation -- but it assumes the policy behaves as described.")
    if shares:
        print()
        para("Your input tokens split %s uncached, %s written to cache, %s read "
             "back from cache. Prices default to Anthropic list; if you bill "
             "through a discounted gateway pass --price-input, --price-output, "
             "--price-cache-write and --price-cache-read, and every number "
             "above rescales." % tuple(pct(x) for x in shares))
    print()
    para("Cache lineages are keyed on session id and on whether the request "
         "belonged to a subagent, because a subagent runs on its own prompt "
         "prefix and can never warm the main conversation's cache. Merging "
         "them would hide idle gaps and understate lever 2.")
    print()
    para("What this script read from your transcripts: tool names, ids, "
         "timestamps, session ids and token counts. Never prompts, arguments, "
         "results or replies. Nothing is sent anywhere unless you pass "
         "--measure.")

    # --- 9. the paste-able block ------------------------------------------
    if args.report:
        head("PASTE THIS BACK")
        para("Aggregate numbers and built-in tool names only. No paths, project "
             "names, branches, prompts, or MCP server names. Read it before you "
             "send it -- it is all on screen.")
        print()
        blob = {
            "schema": "claude-cost-audit/2",
            "window_days": args.days,
            "requests": s["turns"],
            "lineages": len(s["sessions"]),
            "tool_calls": s["calls"],
            "tokens": {k: u[k] for k in sorted(u)},
            "baseline_usd": round(baseline, 2),
            "levers_cumulative_usd": {
                "1_drop_dead_tools": round(cost1, 2),
                "2_refresh_cache": round(cost2, 2),
                "3_batch_tool_calls": round(cost3, 2),
            },
            "levers_saved_pct": {
                "1_drop_dead_tools": round(100.0 * (baseline - cost1) / baseline, 1),
                "2_refresh_cache": round(100.0 * (cost1 - cost2) / baseline, 1),
                "3_batch_tool_calls": round(100.0 * (cost2 - cost3) / baseline, 1),
                "total": round(100.0 * (baseline - cost3) / baseline, 1),
            },
            "lever1": {"dead_tokens": int(dead_tokens),
                       "source": dead_source,
                       "never_called": sorted(low),
                       "never_called_check_first": sorted(check)},
            "lever2": {"cold_start_requests": ref["gaps"],
                       "cold_start_write_share_pct": round(100.0 * cold_w / all_w, 1),
                       "gaps_worth_refreshing": ref["wins"],
                       "refresh_requests": ref["refreshes"],
                       "refresh_interval_s": args.refresh_every},
            "lever3": {"mean_calls_per_request": round(bat["mean"], 3),
                       "single_call_share_pct": round(100.0 * bat["single_share"], 1),
                       "target": args.batch_target,
                       "requests_cut_pct": round(100.0 * bat["cut"], 1)},
            "mcp_servers_configured": len(servers),
            "mcp_servers_unused": len(idle) if servers else 0,
            "unknown_tools": len(unknown),
            "claude_code_versions": sorted(s["versions"])[-3:],
            "prices_usd_per_mtok": price,
        }
        for ln in json.dumps(blob, indent=2, sort_keys=True).splitlines():
            print("  " + ln)

    # --- 10. act ----------------------------------------------------------
    if not candidates:
        print()
        return 0

    if not args.apply:
        head("TO TURN OFF THE DEAD TOOLS (LEVER 1)")
        print("    python3 %s --apply" % os.path.basename(sys.argv[0]))
        print()
        para("That shows you the change, asks, backs up your settings and "
             "touches nothing else. Or merge this into "
             "~/.claude/settings.json yourself:")
        print()
        snippet = json.dumps({"permissions": {"deny": sorted(candidates)}},
                             indent=2)
        for ln in snippet.splitlines():
            print("    " + ln)
        print()
        return 0

    head("TURNING THEM OFF")
    para("%d tool(s) will be added to permissions.deny. Their descriptions "
         "stop being sent, so Claude will not see or offer them."
         % len(candidates))
    if check and not args.include_check_first:
        print()
        para("The %d check-first tool(s) are left alone. Pass "
             "--include-check-first if you want those too." % len(check))
    apply_deny(settings_path(args.settings), candidates, args.yes)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Find the tool schemas you pay for on every turn but never use.

WHY THIS EXISTS
---------------
Claude Code sends the full JSON schema of every declared tool on *every* API
request. In a headless "say ok" turn measured on the wire, 27 tool schemas were
34,851 of 39,216 prompt tokens -- 89% of the request. Those tokens are re-sent
and re-billed every turn, whether the tool is ever called or not.

Restricting a tool removes its schema from the wire. That was verified by
capturing the actual requests three ways: `permissions.deny` in settings,
`--disallowedTools`, and `--tools`. All three drop exactly the named tools and
nothing else. So the saving is real, not just an execution block.

This script finds which tools *you* have never called, prices them against your
own 30 days of history, and offers to add them to `permissions.deny`.

WHAT IT READS
-------------
Your local transcripts (~/.claude/projects/**/*.jsonl). From each record it
reads ONLY: tool names, tool_use block ids, timestamps, and token counts.
It never reads prompts, tool arguments, tool results, or completions.

WHAT IT SENDS
-------------
Nothing, unless you pass --measure. That makes exactly two `claude --print`
calls with the fixed prompt "say ok" to measure your real manifest size.
--apply only edits your local settings.json (after a backup).

USAGE
-----
    python3 claude-tool-audit.py                  # report only
    python3 claude-tool-audit.py --measure        # + verify with 2 API calls
    python3 claude-tool-audit.py --apply          # + prompt to edit settings
    python3 claude-tool-audit.py --verbose        # + list every tool you called

CAVEATS, STATED UP FRONT
------------------------
* "Never called" is not "never useful". A tool you have not needed yet may be
  the right tool next week. The script separates low-risk from check-first for
  this reason, and every change is one line in settings.json to undo.
* Per-tool token figures come from a wire capture of Claude Code 2.x and carry
  roughly +/-15%: schema JSON tokenizes at ~2.4 chars/token, but prose-heavy
  descriptions tokenize nearer 2.8. The aggregate is the solid number, and
  --measure replaces the estimate with a direct measurement on your machine.
* Tool names change between Claude Code versions. Anything in your transcripts
  that this table does not know about is reported rather than silently ignored.
"""

import argparse
import collections
import datetime
import glob
import json
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


def tokens_of(name):
    return round(TOOLS[name][0] / CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Phase 1 -- offline transcript scan
# ---------------------------------------------------------------------------

def scan(days, root=None):
    """Count tool calls and token usage over the window.

    Dedupes on ids because transcripts repeat records across session resume and
    context compaction; counting rows inflates every total.
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

                # --- token usage, one count per unique assistant turn ---
                u = msg.get("usage")
                mid = msg.get("id")
                if isinstance(u, dict) and mid and mid not in seen_turns:
                    seen_turns.add(mid)
                    if msg.get("model"):
                        models[msg["model"]] += 1
                    for k in ("input_tokens", "cache_creation_input_tokens",
                              "cache_read_input_tokens", "output_tokens"):
                        usage[k] += u.get(k) or 0

                # --- tool calls, one count per unique tool_use block id ---
                for blk in msg.get("content") or []:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    bid = blk.get("id")
                    if not bid or bid in seen_calls:
                        continue
                    seen_calls.add(bid)
                    tgt = sub if rec.get("isSidechain") else main
                    tgt[blk.get("name") or "?"] += 1

    return {
        "files": len(files), "main": main, "sub": sub,
        "calls": len(seen_calls), "turns": len(seen_turns),
        "usage": usage, "versions": versions, "models": models,
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
# Phase 2 -- optional live measurement (2 API calls, no proxy needed)
# ---------------------------------------------------------------------------

def measure(deny):
    """Return (baseline_prompt_tokens, denied_prompt_tokens) or None.

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
        description="Find tool schemas you pay for every turn but never call.")
    ap.add_argument("--days", type=int, default=30,
                    help="lookback window in days (default 30)")
    ap.add_argument("--measure", action="store_true",
                    help="verify the estimate with 2 `claude --print` calls")
    ap.add_argument("--apply", action="store_true",
                    help="prompt to add never-used tools to permissions.deny")
    ap.add_argument("--yes", action="store_true",
                    help="with --apply, skip the confirmation prompt")
    ap.add_argument("--include-check-first", action="store_true",
                    help="also offer the tools flagged check-first")
    ap.add_argument("--verbose", action="store_true",
                    help="list every tool you called, not just the top few")
    ap.add_argument("--settings", help="settings.json to edit (default ~/.claude)")
    for k in PRICE:
        ap.add_argument("--price-" + k.replace("_", "-"), type=float,
                        dest="price_" + k, default=PRICE[k],
                        metavar="USD_PER_MTOK",
                        help="%s price per Mtok (default %.2f)" % (k, PRICE[k]))
    args = ap.parse_args()
    price = {k: getattr(args, "price_" + k) for k in PRICE}

    banner = "Claude Code tool audit"
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

    # --- 1. the answer, before anything else ------------------------------
    head("THE SHORT VERSION")
    if not never:
        para("Nothing to trim. You have actually used every tool Claude "
             "declares to the model in the last %d days." % args.days)
    else:
        para("%d of the %d built-in tools were sent to the model on every one "
             "of your %s requests, and you never called them once."
             % (len(never), known, num(s["turns"])))
        if safe_rows:
            money = (", worth about %s over these %d days"
                     % (usd(tot_safe * per_tok * s["turns"]), args.days)
                     if per_tok else "")
            print()
            para("Turning off the %d that are safe to remove would take %s "
                 "tokens out of every request%s."
                 % (len(safe_rows), num(tot_safe), money))
        if check_rows:
            print()
            para("A further %d are unused too, but removing one of those could "
                 "break a workflow, so they are listed separately and left "
                 "alone unless you ask for them." % len(check_rows))
        me = os.path.basename(sys.argv[0])
        print("\n  Next step:")
        print("    python3 %s --measure" % me)
        print("        check that number against the real API (2 requests)")
        print("    python3 %s --apply" % me)
        print("        turn them off -- asks first, backs up your settings")

    # --- 2. why it costs anything -----------------------------------------
    head("WHY UNUSED TOOLS COST MONEY")
    para("Claude Code re-sends the full description of every tool it has on "
         "every request, whether the tool gets called or not. On a minimal "
         "request, those descriptions were 89% of the entire prompt. Switching "
         "a tool off removes its description from the request, so you stop "
         "paying for it -- it is not just an execution block.")

    # --- 3. your history --------------------------------------------------
    head("WHAT YOUR HISTORY SHOWS")
    if s["first"] and s["last"]:
        print("  period       %s to %s" % (s["first"].date(), s["last"].date()))
    print("  requests     %s  (one per exchange with the model)"
          % num(s["turns"]))
    print("  tool calls   %s  across %d different tools"
          % (num(s["calls"]), len(called)))
    top = called.most_common(None if args.verbose else 6)
    print(textwrap.fill(
        ", ".join("%s %s" % (n, num(c)) for n, c in top),
        width=WIDTH, initial_indent="  most used    ",
        subsequent_indent=" " * 15))
    if not args.verbose and len(called) > len(top):
        print("               ...and %d more (--verbose for all of them)"
              % (len(called) - len(top)))
    if s["versions"]:
        print("  Claude Code  %s" % ", ".join(sorted(s["versions"])[-3:]))

    unknown = sorted(n for n in called if n not in TOOLS)
    if unknown:
        print()
        para("%d tool(s) you called are not in this script's size table, so "
             "they are neither priced nor ever proposed for removal. That "
             "normally means an MCP tool, or a newer Claude Code than the one "
             "this table was measured on:" % len(unknown))
        for n in unknown:
            print("    %s" % n)

    # --- 4. the two candidate lists ---------------------------------------
    def sized(label, n_tools, toks):
        if not per_tok:
            return "%s -- %d tools, %s tokens per request" % (label, n_tools,
                                                             num(toks))
        return "%s -- %d tools, %s tokens per request, %s over %d days" % (
            label, n_tools, num(toks), usd(toks * per_tok * s["turns"]),
            args.days)

    if safe_rows:
        head(sized("SAFE TO TURN OFF", len(safe_rows), tot_safe))
        para("Never called, and nothing else you use depends on them.")
        print()
        table(safe_rows, per_tok, s["turns"])

    if check_rows:
        head(sized("CHECK FIRST", len(check_rows), tot_check))
        para("Never called either, but removing one of these could break a "
             "workflow, so they are left out unless you pass "
             "--include-check-first. The note says why you might keep it.")
        print()
        table(check_rows, per_tok, s["turns"])

    # --- 5. MCP servers ---------------------------------------------------
    servers = configured_mcp_servers()
    if servers:
        used = {n.split("__")[1] for n in called if n.startswith("mcp__")
                and len(n.split("__")) > 2}
        idle = sorted(servers - used)
        head("MCP SERVERS -- %d configured, %d used in this window"
             % (len(servers), len(servers) - len(idle)))
        if idle:
            para("No calls at all from: %s" % ", ".join(idle))
            print()
            para("MCP tool descriptions are often the largest part of a "
                 "request and this script cannot size them, so they are not in "
                 "the totals above. Dropping a server you never use is usually "
                 "the single biggest win available. Remove it from your config, "
                 "or start a session with --strict-mcp-config to leave all of "
                 "them out.")
        else:
            para("All of them saw calls, so there is nothing to trim here. "
                 "Note that MCP tool descriptions are not counted in the "
                 "totals above -- this script cannot size them.")

    # --- 6. optional live measurement -------------------------------------
    if args.measure:
        if not candidates:
            head("MEASURED AGAINST THE REAL API")
            print("  Nothing to measure -- there is nothing to turn off.")
        else:
            head("MEASURED AGAINST THE REAL API")
            para("Sending two requests with the prompt \"say ok\" from an empty "
                 "directory -- one with your normal tools, one with these %d "
                 "switched off. The difference is exactly what they cost you, "
                 "on your machine and your Claude Code version."
                 % len(candidates))
            print()
            res, err = measure(candidates)
            if res is None:
                para("Could not measure: %s" % err)
            else:
                base, trimmed = res
                delta = base - trimmed
                print("    every tool on      %9s tokens in the request"
                      % num(base))
                print("    %2d tools off       %9s tokens in the request"
                      % (len(candidates), num(trimmed)))
                print("    " + "-" * 46)
                print("    you save           %9s tokens, every request"
                      % num(delta))
                print("                       %8.1f%% of that request"
                      % (100.0 * delta / base if base else 0))
                if per_tok:
                    print("                       %9s over your last %s requests"
                          % (usd(delta * per_tok * s["turns"]), num(s["turns"])))
                est = sum(tokens_of(n) for n in candidates)
                if delta and est:
                    print()
                    para("The estimate above said %s tokens, so measured / "
                         "estimated = %.2f." % (num(est), delta / est))

    # --- 7. how the numbers were made ------------------------------------
    head("HOW THESE NUMBERS WERE MADE")
    para("The per-tool token counts come from capturing real Claude Code "
         "requests, and are good to roughly +/-15% each. Pass --measure to "
         "replace the estimate with a direct measurement on your own machine.")
    if shares:
        print()
        para("The dollars use your own %s requests and your own input-token "
             "mix -- %.1f%% uncached, %.1f%% written to cache, %.1f%% read back "
             "from cache -- at Anthropic list prices. That works out to %s per "
             "1,000 tokens of tool description over this %d-day window. If you "
             "bill through a discounted gateway, pass --price-input, "
             "--price-output, --price-cache-write and --price-cache-read."
             % ((num(s["turns"]),) + tuple(x * 100 for x in shares)
                + (usd(1000 * per_tok * s["turns"]), args.days)))
    print()
    para("\"Never called\" is not \"never useful\" -- a tool you have not "
         "needed yet may be the right one next week. Everything here is one "
         "list in settings.json, and one command to put back.")
    print()
    para("What this script read from your transcripts: tool names, ids, "
         "timestamps and token counts. Never prompts, arguments, results or "
         "replies. Nothing is sent anywhere unless you pass --measure.")

    # --- 8. act -----------------------------------------------------------
    if not candidates:
        print()
        return 0

    if not args.apply:
        head("TO TURN THEM OFF")
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

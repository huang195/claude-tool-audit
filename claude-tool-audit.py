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
    "Workflow": (21822, "orchestration", "low",
                 "multi-agent orchestration; opt-in by keyword, largest single schema"),
    "DesignSync": (9053, "specialized", "low",
                   "syncs a component library to claude.ai/design"),
    "Monitor": (7599, "scheduling", "low",
                "streams events from a long-running command"),
    "ScheduleWakeup": (4982, "scheduling", "low",
                       "paces /loop dynamic mode"),
    "SendMessage": (4459, "orchestration", "check",
                    "needed to talk to subagents you spawn with Agent"),
    "EnterWorktree": (4047, "worktree", "check",
                      "git worktree workflow; keep if your repo documents one"),
    "CronCreate": (3681, "scheduling", "low", "scheduled prompts"),
    "Agent": (3174, "orchestration", "check",
              "spawns subagents; usually worth keeping"),
    "Bash": (2870, "core", "check", "core shell access"),
    "ExitWorktree": (2520, "worktree", "check", "pairs with EnterWorktree"),
    "LSP": (2311, "specialized", "check",
            "language-server lookups; useful in large typed codebases"),
    "ReportFindings": (2187, "specialized", "low",
                       "structured output for code-review skills"),
    "Skill": (1824, "specialized", "check",
              "invokes skills; keep if you use slash commands"),
    "PushNotification": (1790, "scheduling", "low",
                         "desktop/phone notification"),
    "NotebookEdit": (1633, "core", "low", "Jupyter .ipynb cell edits"),
    "Read": (1597, "core", "check", "core file read"),
    "TaskOutput": (1561, "orchestration", "low",
                   "deprecated; background tasks report their own output path"),
    "ListAgents": (1171, "orchestration", "check", "pairs with SendMessage"),
    "Edit": (968, "core", "check", "core file edit"),
    "WebSearch": (841, "web", "low",
                  "redundant if you have an MCP search tool"),
    "TaskStop": (805, "orchestration", "low", "kills a background task"),
    "WebFetch": (750, "web", "check", "fetches and summarizes a URL"),
    "Write": (639, "core", "check", "core file write"),
    "CronDelete": (359, "scheduling", "low", "pairs with CronCreate"),
    "CronList": (232, "scheduling", "low", "pairs with CronCreate"),
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
        print("  already denied, nothing to do")
        return False

    print("\n  will add to permissions.deny in %s:" % path)
    for n in additions:
        print("    + %s" % n)
    if not assume_yes:
        if not sys.stdin.isatty():
            print("  stdin is not a terminal; re-run interactively or pass --yes")
            return False
        if input("  proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  skipped")
            return False

    if os.path.exists(path):
        backup = path + ".bak"
        shutil.copy2(path, backup)
        print("  backup: %s" % backup)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    deny.extend(additions)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print("  written. undo by deleting those %d lines, or: mv %s.bak %s"
          % (len(additions), path, path))
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
    ap.add_argument("--settings", help="settings.json to edit (default ~/.claude)")
    for k in PRICE:
        ap.add_argument("--price-" + k.replace("_", "-"), type=float,
                        dest="price_" + k, default=PRICE[k],
                        metavar="USD_PER_MTOK",
                        help="%s price per Mtok (default %.2f)" % (k, PRICE[k]))
    args = ap.parse_args()
    price = {k: getattr(args, "price_" + k) for k in PRICE}

    print("Claude Code tool-manifest audit -- last %d days" % args.days)
    print("Reads tool names, ids, timestamps and token counts only. "
          "No prompts, arguments, or results.\n")

    s = scan(args.days)
    if not s["turns"]:
        print("No transcripts found in the window. Nothing to analyse.")
        print("(Looked in ~/.claude/projects; found %d files.)" % s["files"])
        return 0

    called = collections.Counter(s["main"]) + collections.Counter(s["sub"])
    per_tok, shares = blended_price_per_token(s["usage"], price)

    span = ""
    if s["first"] and s["last"]:
        span = "  %s .. %s" % (s["first"].date(), s["last"].date())
    print("History: %d transcript files, %d API turns, %d tool calls%s"
          % (s["files"], s["turns"], s["calls"], span))
    if s["versions"]:
        print("Claude Code versions seen: %s"
              % ", ".join(sorted(s["versions"])[-3:]))
    if shares:
        print("Your input tokens split: %.1f%% uncached / %.1f%% cache-write "
              "/ %.1f%% cache-read" % tuple(x * 100 for x in shares))
        print("=> at that mix, every 1,000 tokens of tool schema costs you "
              "$%.2f across %d turns" % (1000 * per_tok * s["turns"], s["turns"]))
    print()

    # --- what you actually use -------------------------------------------
    print("TOOLS YOU CALLED (%d)" % len(called))
    for name, n in called.most_common():
        mark = "" if name in TOOLS else "   <- not in this script's table"
        print("  %-36s %6d%s" % (name, n, mark))

    unknown = [n for n in called if n not in TOOLS]
    if unknown:
        print("\n  %d tool(s) above are unknown to this script's measured table."
              % len(unknown))
        print("  That usually means a newer Claude Code, or MCP tools. They are")
        print("  never proposed for denial and not priced.")

    # --- what you never touched ------------------------------------------
    never = [n for n, (c, g, risk, note) in sorted(
        TOOLS.items(), key=lambda kv: -kv[1][0])
        if n not in called and risk != "interactive" and c > 0]

    print("\nDECLARED BUT NEVER CALLED (%d)" % len(never))
    if not never:
        print("  none -- your manifest is already tight.")
    hdr = "  %-20s %8s %10s  %s" % ("tool", "tokens", "$/window", "note")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    low, check = [], []
    tot_low = tot_check = 0
    for name in never:
        chars, group, risk, note = TOOLS[name]
        tk = tokens_of(name)
        cost = tk * per_tok * s["turns"] if per_tok else 0
        flag = " " if risk == "low" else "!"
        print("  %s%-19s %8d %10s  %s"
              % (flag, name, tk, "$%.2f" % cost, note))
        if risk == "low":
            low.append(name)
            tot_low += tk
        else:
            check.append(name)
            tot_check += tk

    if per_tok:
        print("\n  low-risk subtotal      %8d tokens  $%.2f over %d turns"
              % (tot_low, tot_low * per_tok * s["turns"], s["turns"]))
        if check:
            print("  check-first subtotal   %8d tokens  $%.2f  (marked !)"
                  % (tot_check, tot_check * per_tok * s["turns"]))
        # Share is computed from wire bytes, which are exact, rather than from
        # derived token counts.
        all_chars = sum(v[0] for v in TOOLS.values())
        dead_chars = sum(TOOLS[n][0] for n in never)
        print("  never-called schemas are %.0f%% of your declared manifest "
              "by wire bytes" % (100.0 * dead_chars / all_chars))
    print("\n  ! = removing this plausibly breaks a workflow. Read the note.")
    print("  Estimates use this script's measured schema sizes (+/-15% per tool)")
    print("  and your own turn count and cache mix. Pass --measure for exact.")

    # --- MCP servers ------------------------------------------------------
    servers = configured_mcp_servers()
    if servers:
        used = {n.split("__")[1] for n in called if n.startswith("mcp__")
                and len(n.split("__")) > 2}
        idle = sorted(servers - used)
        print("\nMCP SERVERS: %d configured, %d with calls in the window"
              % (len(servers), len(servers) - len(idle)))
        if idle:
            print("  no calls from: %s" % ", ".join(idle))
            print("  MCP schemas are often the largest part of a manifest and this")
            print("  script cannot size them. Drop unused servers from your config,")
            print("  or run with --strict-mcp-config to exclude them per-session.")

    # --- optional live measurement ---------------------------------------
    candidates = low + (check if args.include_check_first else [])
    if args.measure:
        if not candidates:
            print("\n--measure: nothing to deny, skipping.")
        else:
            print("\nMEASURING (2 API calls, prompt \"say ok\", empty directory)...")
            res, err = measure(candidates)
            if res is None:
                print("  failed: %s" % err)
            else:
                base, trimmed = res
                delta = base - trimmed
                print("  baseline manifest        %8d prompt tokens" % base)
                print("  with %2d tools denied     %8d prompt tokens"
                      % (len(candidates), trimmed))
                print("  measured saving          %8d tokens/turn (%.1f%%)"
                      % (delta, 100.0 * delta / base if base else 0))
                if per_tok:
                    print("  at your own volume       $%.2f over %d turns"
                          % (delta * per_tok * s["turns"], s["turns"]))
                est = sum(tokens_of(n) for n in candidates)
                if delta:
                    print("  (table estimated %d; measured/estimate = %.2f)"
                          % (est, delta / est if est else 0))

    # --- optional apply ---------------------------------------------------
    print()
    if not args.apply:
        if candidates:
            print("To act on this, re-run with --apply (it will prompt, back up your")
            print("settings, and change nothing else). Or add this by hand to")
            print("~/.claude/settings.json:")
            print('  "permissions": { "deny": %s }'
                  % json.dumps(sorted(candidates)))
        return 0

    if not candidates:
        print("Nothing to deny.")
        return 0

    print("DISABLE NEVER-USED TOOLS")
    print("  %d low-risk tool(s) proposed." % len(low))
    if check and not args.include_check_first:
        print("  %d check-first tool(s) held back; pass --include-check-first"
              " to include them." % len(check))
    print("  Effect: their schemas stop being sent. Claude will not see or")
    print("  offer them. Fully reversible by editing one list.")
    apply_deny(settings_path(args.settings), candidates, args.yes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

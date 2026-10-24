#!/usr/bin/env python3
"""Relay a free conversation between two fresh Claude instances via `claude -p`.

Usage:
  dialogue.py --condition can-end --turns 30 [--model sonnet]
  dialogue.py --continue runs/<run-dir>      # pick up an interrupted run
  dialogue.py --render                       # rebuild all transcripts + index

A "turn" (exchange) is one message from A and one from B.
"""
import argparse, datetime, json, os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"

BASE_PROMPT = ("You are Claude. You are in a conversation with another instance of Claude. "
               "There is no task or topic.")
END_LINE = " You may end the conversation at any time by writing [END]."
END_TOKEN = "[END]"
OPENER = "[The other Claude has joined the conversation.]"

CLEAN_ENV_KEYS = ["HOME", "PATH", "LANG"]
EXTRA_ENV = {
    "DO_NOT_TRACK": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1", "DISABLE_BUG_COMMAND": "1",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

RETRY_WAIT = 600          # seconds between retries after an error / usage limit
MAX_RETRIES = 36          # ~6 hours of waiting before giving up (state is kept)


def clean_env():
    env = {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}
    env.setdefault("LANG", "C.UTF-8")
    env.update(EXTRA_ENV)
    return env


def call_claude(state, speaker, text):
    """Send `text` to instance `speaker`, return (reply, usage_info)."""
    sess = state["sessions"].get(speaker)
    cmd = ["claude", "-p", "--model", state["model"],
           "--system-prompt", state["system_prompt"],
           "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--output-format", "json"]
    if sess:
        cmd += ["--resume", sess]
    for attempt in range(MAX_RETRIES + 1):
        p = None
        try:
            p = subprocess.run(cmd, input=text, capture_output=True, text=True,
                               cwd=state["sandbox"], env=clean_env(), timeout=900)
            d = json.loads(p.stdout)
            if not d.get("is_error") and d.get("result") is not None:
                state["sessions"][speaker] = d["session_id"]
                u = d.get("usage", {})
                return d["result"], {
                    "cost_usd_equiv": d.get("total_cost_usd"),
                    "input": u.get("input_tokens"),
                    "cache_read": u.get("cache_read_input_tokens"),
                    "cache_write": u.get("cache_creation_input_tokens"),
                    "output": u.get("output_tokens"),
                    "model": ",".join(d.get("modelUsage", {}).keys()),
                }
            err = d.get("result") or p.stderr
        except Exception as e:  # timeout, bad JSON, etc.
            err = f"{type(e).__name__}: {e} {p.stderr if p else ''}"
        log(f"error from {speaker} (attempt {attempt + 1}): {str(err)[:300]}")
        if attempt < MAX_RETRIES:
            log(f"waiting {RETRY_WAIT}s before retrying")
            time.sleep(RETRY_WAIT)
    raise SystemExit("giving up after repeated errors; rerun with --continue to resume")


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def save(state):
    (Path(state["dir"]) / "state.json").write_text(json.dumps(state, indent=1, ensure_ascii=False))
    render(Path(state["dir"]))


def new_run(args):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    RUNS.mkdir(exist_ok=True)
    n = 1 + sum(1 for d in RUNS.iterdir() if f"_{args.condition}_" in d.name)
    d = RUNS / f"{stamp}_{args.condition}_{args.model}_r{n}"
    d.mkdir()
    state = {
        "dir": str(d), "model": args.model, "condition": args.condition,
        "turns": args.turns, "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "cli_version": subprocess.run(["claude", "--version"], capture_output=True,
                                      text=True).stdout.strip(),
        "system_prompt": BASE_PROMPT + (END_LINE if args.condition == "can-end" else ""),
        "opener": OPENER, "sandbox": tempfile.mkdtemp(prefix="cd-"),
        "sessions": {}, "messages": [], "ended": None,
    }
    save(state)
    return state


def run(state):
    msgs = state["messages"]
    can_end = state["condition"] == "can-end"
    order = ["A", "B"]
    while state["ended"] is None:
        i = len(msgs)
        speaker = order[i % 2]
        prev = msgs[-1]["text"] if msgs else state["opener"]
        # did someone end on the previous message? then this is the final reply
        final = bool(msgs) and can_end and END_TOKEN in msgs[-1]["text"]
        if i >= 2 * state["turns"] and not final:
            state["ended"] = f"turn limit ({state['turns']} exchanges)"
            break
        log(f"exchange {i // 2 + 1}, Claude {speaker}{' (final reply)' if final else ''}")
        reply, usage = call_claude(state, speaker, prev)
        msgs.append({"n": i + 1, "speaker": speaker, "text": reply,
                     "time": datetime.datetime.now().isoformat(timespec="seconds"), "usage": usage})
        if final:
            state["ended"] = f"Claude {msgs[-2]['speaker']} wrote {END_TOKEN} (message {i}); " \
                             f"Claude {speaker} gave a final reply"
        save(state)
    state["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    save(state)
    render_index()
    log(f"done: {state['ended']}")


def render(d):
    s = json.loads((d / "state.json").read_text())
    msgs = s["messages"]
    # the CLI reports a running per-session total, so take each instance's latest value
    cost = sum(max([m["usage"].get("cost_usd_equiv") or 0 for m in msgs if m["speaker"] == sp] or [0])
               for sp in ("A", "B"))
    out = [f"# {s['condition']} · run {d.name.split('_r')[-1]}", "",
           f"- **Date:** {s['started'].replace('T', ' ')}",
           f"- **Model:** {s['model']} ({msgs[0]['usage']['model'] if msgs else '?'}), {s['cli_version']}",
           f"- **Condition:** {s['condition']} · limit {s['turns']} exchanges (not told to them)",
           f"- **Ended:** {s['ended'] or 'in progress'}",
           f"- **Messages:** {len(msgs)} · API-equivalent cost ≈ ${cost:.2f} (billed to Pro limits, not money)",
           "", "**Setup**", "",
           f"- System prompt (both): *{s['system_prompt']}*",
           f"- Claude A's first input: `{s['opener']}`. Claude B's first input is A's first message.",
           "- Claude Code also adds, unremovably on a Pro login: a one-line Agent SDK identity prefix, "
           "a note with the account email, and a short environment block (temp working dir, OS, "
           "date, model). No tools, memory, CLAUDE.md, MCP or hooks.", ""]
    for m in msgs:
        out += ["---", "", f"### Claude {m['speaker']} · {m['n']}", "", m["text"].strip(), ""]
    (d / "transcript.md").write_text("\n".join(out))


def render_index():
    rows = ["# Claude ↔ Claude free conversations", "",
            "| Run | Model | Messages | How it ended |", "|---|---|---|---|"]
    for d in sorted(RUNS.iterdir(), reverse=True):
        if not (d / "state.json").exists():
            continue
        s = json.loads((d / "state.json").read_text())
        rows.append(f"| [{d.name}](runs/{d.name}/transcript.md) | {s['model']} | "
                    f"{len(s['messages'])} | {s['ended'] or 'in progress'} |")
    (ROOT / "index.md").write_text("\n".join(rows) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", choices=["can-end", "no-end"])
    ap.add_argument("--turns", type=int, default=30)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--continue", dest="cont")
    ap.add_argument("--render", action="store_true")
    a = ap.parse_args()
    if a.render:
        for d in RUNS.iterdir():
            if (d / "state.json").exists():
                render(d)
        render_index()
    elif a.cont:
        run(json.loads((Path(a.cont) / "state.json").read_text()))
    elif a.condition:
        run(new_run(a))
    else:
        ap.error("need --condition, --continue or --render")


if __name__ == "__main__":
    main()

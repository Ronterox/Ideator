"""ideator: describe a problem in plain words, generate unusual ideas for it, and watch the run live.

  ideator new  [DESCRIPTION] [--repo PATH] [--topic NAME] [--rounds N] [-- RUN OPTIONS]
                                                                         start a run and follow it
  ideator follow [RUN]                                                   live colored log of a run
  ideator list                                                           all runs with status and cost
  ideator report [RUN] [--page]                                          top ideas (or page the full report)
  ideator stop [RUN]                                                     stop a run (it still ranks and reports)

RUN is a run folder name, a unique prefix of one, or omitted for the latest run.
If DESCRIPTION is omitted, it is read from --file, from piped stdin, or typed interactively.
Ctrl-C while following only detaches; the run keeps going in the background.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .ui import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, fmt_elapsed, follow, pid_alive, read_meta, render

if not sys.stdout.isatty():  # no escape codes when piped or redirected
    BOLD = CYAN = DIM = GREEN = RED = RESET = YELLOW = ""

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


def last_event(run_dir: Path) -> dict | None:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return None
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 8192))
        lines = fh.read().decode(errors="replace").splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def run_status(run_dir: Path) -> str:
    ev = last_event(run_dir)
    if ev and ev["kind"] == "done":
        return ev["msg"].split()[0]  # finished / stopped / failed
    if pid_alive(read_meta(run_dir).get("pid"), run_dir):
        return "running"
    if ev is None:
        return "legacy" if (run_dir / "state.json").exists() else "empty"
    return "crashed"


def all_runs() -> list[Path]:
    if not RUNS.exists():
        return []
    return sorted((d for d in RUNS.iterdir() if d.is_dir()), key=lambda d: d.name)


def resolve_run(name: str | None) -> Path:
    runs = all_runs()
    if not runs:
        sys.exit("No runs yet. Start one with: ideator new")
    if not name:
        return runs[-1]
    if Path(name).is_dir():
        return Path(name).resolve()
    matches = [d for d in runs if d.name.startswith(name)] or [d for d in runs if name in d.name]
    if len(matches) != 1:
        sys.exit(f"'{name}' matches {len(matches)} runs; be more specific (see: ideator list)")
    return matches[0]


def read_description(args) -> str:
    if args.description:
        return args.description
    if args.file:
        return Path(args.file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print(f"{BOLD}Describe the problem the way you would explain it to a colleague.{RESET}")
    print(f"{DIM}What you are building, what fails today, constraints. Finish with Ctrl-D on an empty line.{RESET}\n")
    return sys.stdin.read()


# ---------------------------------------------------------------- commands

def cmd_new(args) -> int:
    description = read_description(args).strip()
    if len(description) < 20:
        sys.exit("The description is too short to ideate on; say a bit more about the problem.")
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    if repo and not repo.is_dir():
        sys.exit(f"--repo is not a directory: {repo}")
    topic = args.topic or (slug(repo.name) if repo else slug(" ".join(description.split()[:5])))

    run_dir = RUNS / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{topic}"
    run_dir.mkdir(parents=True)
    (run_dir / "problem.md").write_text(description + "\n")

    cmd = [sys.executable, "-m", "alien_loop.run", "--run-dir", str(run_dir), "--topic", topic,
           "--question-file", str(run_dir / "problem.md"), "--iterations", str(args.rounds)]
    if repo:
        cmd += ["--repo", str(repo)]
    else:  # no repo: give the generators an empty folder, so they rely on the description and the web
        empty = run_dir / "no-repo"
        empty.mkdir()
        cmd += ["--repo", str(empty)]
    if args.no_carry:
        cmd.append("--no-carry")
    cmd += args.extra

    with (run_dir / "stdout.log").open("w") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
    meta = {"pid": proc.pid, "started": time.time(), "topic": topic, "repo": str(repo or ""), "command": cmd}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    print(f"{GREEN}{BOLD}Started{RESET} run {BOLD}{run_dir.name}{RESET} (pid {proc.pid})")
    print(f"{DIM}Follow later: ideator follow {run_dir.name} · stop: ideator stop {run_dir.name}{RESET}\n")
    if args.no_follow:
        return 0
    return _follow(run_dir)


def _follow(run_dir: Path, from_start: bool = True) -> int:
    try:
        return follow(run_dir, from_start=from_start)
    except KeyboardInterrupt:
        sys.stdout.write("\r\033[K")
        print(f"{YELLOW}Detached.{RESET} The run keeps going: ideator follow {run_dir.name}")
        return 0


def cmd_follow(args) -> int:
    run_dir = resolve_run(args.run)
    status = run_status(run_dir)
    if status == "legacy":
        sys.exit(f"{run_dir.name} has no live log (it was made before the ideator CLI existed)")
    if status != "running" and args.tail:  # nothing new will arrive; show how it ended
        ev = last_event(run_dir)
        print(render(ev, color=sys.stdout.isatty()) if ev else f"{run_dir.name}: {status}")
        return 0 if status == "finished" else 1
    return _follow(run_dir, from_start=not args.tail)


def cmd_list(args) -> int:
    runs = all_runs()
    if not runs:
        print("No runs yet. Start one with: ideator new")
        return 0
    colors = {"running": CYAN, "finished": GREEN, "stopped": YELLOW, "failed": RED, "crashed": RED}
    print(f"{BOLD}{'RUN':<36} {'STATUS':<9} {'TIME':>8} {'COST':>7} {'IDEAS':>5}  TOP IDEA{RESET}")
    for d in runs:
        status = run_status(d)
        ev = last_event(d) or {}
        meta = read_meta(d)
        elapsed = time.time() - meta["started"] if status == "running" and meta.get("started") else ev.get("elapsed", 0)
        state = {}
        try:
            state = json.loads((d / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        cost = ev.get("cost", state.get("cost_usd", 0.0))
        ideas = state.get("ideas", [])
        top = next((i["title"] for i in sorted(ideas, key=lambda i: i.get("rank", 1e9)) if "rank" in i), "")
        when = fmt_elapsed(elapsed) if elapsed else "–"
        print(f"{d.name:<36} {colors.get(status, DIM)}{status:<9}{RESET} {when:>8} "
              f"${cost:>6.2f} {len(ideas):>5}  {top[:70]}")
    return 0


def cmd_report(args) -> int:
    run_dir = resolve_run(args.run)
    report = run_dir / "report.md"
    if not report.exists():
        sys.exit(f"{run_dir.name} has no report yet (status: {run_status(run_dir)})")
    if args.page:
        return subprocess.call(["less", "-R", str(report)])
    state = json.loads((run_dir / "state.json").read_text())
    ranked = sorted((i for i in state["ideas"] if "rank" in i), key=lambda i: i["rank"])
    av = "final_availability" if any("final_availability" in i for i in ranked) else "availability"
    co = "final_coherence" if av.startswith("final") else "coherence"
    print(f"{BOLD}{run_dir.name}{RESET} · {run_status(run_dir)} · ${state.get('cost_usd', 0):.2f} · {len(state['ideas'])} ideas\n")
    for i in ranked[: args.top]:
        print(f"{BOLD}{i['rank']}. {i['title']}{RESET}")
        print(f"   {DIM}score {i['score']:+.2f} · availability {i.get(av)} · coherence {i.get(co)} · {i['generator']} r{i['round']}{RESET}")
        print(f"   {i['summary']}\n")
    print(f"Full report: {report}\n{DIM}(ideator report --page to read it here){RESET}")
    return 0


def cmd_stop(args) -> int:
    run_dir = resolve_run(args.run)
    status = run_status(run_dir)
    if status != "running":
        sys.exit(f"{run_dir.name} is not running ({status})")
    os.kill(read_meta(run_dir)["pid"], signal.SIGTERM)
    print(f"Asked {run_dir.name} to stop. Generators already working finish their current task (can take a "
          f"minute or two); then it ranks and reports everything produced so far. Watch: ideator follow {run_dir.name}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="ideator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    n = sub.add_parser("new", help="describe a problem and start generating ideas")
    n.add_argument("description", nargs="?", help="the problem in your own words (or use --file / stdin)")
    n.add_argument("-f", "--file", help="read the description from a file")
    n.add_argument("-r", "--repo", help="your project folder; generators may read it (read-only)")
    n.add_argument("-t", "--topic", help="short name; runs with the same topic share their ideas (default: repo name)")
    n.add_argument("-n", "--rounds", type=int, default=4)
    n.add_argument("--no-carry", action="store_true", help="ignore ideas from earlier runs on this topic")
    n.add_argument("--no-follow", action="store_true", help="start in the background and return")
    n.set_defaults(func=cmd_new)

    f = sub.add_parser("follow", help="live colored log of a run")
    f.add_argument("run", nargs="?")
    f.add_argument("--tail", action="store_true", help="only show new events, not the history")
    f.set_defaults(func=cmd_follow)

    sub.add_parser("list", help="all runs with status and cost").set_defaults(func=cmd_list)

    r = sub.add_parser("report", help="top ideas of a run")
    r.add_argument("run", nargs="?")
    r.add_argument("--top", type=int, default=5)
    r.add_argument("--page", action="store_true", help="page through the full markdown report")
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("stop", help="stop a run gracefully")
    s.add_argument("run", nargs="?")
    s.set_defaults(func=cmd_stop)

    # Everything after a bare `--` goes to the run itself (see: python -m alien_loop.run -h).
    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    args = p.parse_args(argv[: argv.index("--")] if "--" in argv else argv)
    args.extra = extra
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

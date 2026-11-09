"""ideator: describe a problem in plain words, generate unusual ideas for it, and watch the run live.

  ideator new  [DESCRIPTION] [--repo PATH] [--topic NAME] [--rounds N] [-- RUN OPTIONS]
                                                                         start a run and follow it
  ideator follow [RUN]                                                   live colored log of a run
  ideator list                                                           all runs with status and cost
  ideator continue [RUN] [--like 1 3] [--note WHY]                       new run steered toward ideas you liked
  ideator report [RUN] [--explicit] [--page]                             top ideas in plain words (or the full report)
  ideator simplify [RUN]                                                 add the plain-language view to an older run
  ideator stop [RUN]                                                     stop a run (it still ranks and reports)

RUN is a run folder name, a unique prefix of one, or omitted for the latest run.
If DESCRIPTION is omitted, it is read from --file, from piped stdin, or typed interactively.
Ctrl-C while following only detaches; the run keeps going in the background.
When a run you are following finishes, ideator asks which ideas you liked and can continue from them.
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
    run_dir = launch(description, repo, topic, args.rounds, args.extra, no_carry=args.no_carry)
    return started(run_dir, args.no_follow)


def launch(description: str, repo: Path | None, topic: str, rounds: int, extra: list[str], *,
           no_carry: bool = False, liked: dict | None = None, parent: Path | None = None) -> Path:
    """Create the run folder and start alien_loop.run detached. Returns the run folder."""
    run_dir = RUNS / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{topic}"
    run_dir.mkdir(parents=True)
    (run_dir / "problem.md").write_text(description + "\n")

    cmd = [sys.executable, "-m", "alien_loop.run", "--run-dir", str(run_dir), "--topic", topic,
           "--question-file", str(run_dir / "problem.md"), "--iterations", str(rounds)]
    if liked:
        (run_dir / "liked.json").write_text(json.dumps(liked, indent=2, ensure_ascii=False))
        cmd += ["--liked-file", str(run_dir / "liked.json")]
    if parent and (parent / "state.json").exists():
        cmd += ["--resume-from", str(parent / "state.json")]
    if repo:
        cmd += ["--repo", str(repo)]
    else:  # no repo: give the generators an empty folder, so they rely on the description and the web
        empty = run_dir / "no-repo"
        empty.mkdir()
        cmd += ["--repo", str(empty)]
    if no_carry:
        cmd.append("--no-carry")
    cmd += extra

    with (run_dir / "stdout.log").open("w") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
    meta = {"pid": proc.pid, "started": time.time(), "topic": topic, "repo": str(repo or ""), "command": cmd,
            "parent": parent.name if parent else None}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return run_dir


def started(run_dir: Path, no_follow: bool) -> int:
    pid = read_meta(run_dir).get("pid")
    print(f"{GREEN}{BOLD}Started{RESET} run {BOLD}{run_dir.name}{RESET} (pid {pid})")
    print(f"{DIM}Follow later: ideator follow {run_dir.name} · stop: ideator stop {run_dir.name}{RESET}\n")
    if no_follow:
        return 0
    code = _follow(run_dir)
    if code is None:  # detached
        return 0
    if code == 0 and sys.stdin.isatty() and sys.stdout.isatty():
        return interactive(run_dir)
    return code


def _follow(run_dir: Path, from_start: bool = True) -> int | None:
    """Follow until the run ends (0 finished, 1 otherwise); None if the user detached with Ctrl-C."""
    try:
        return follow(run_dir, from_start=from_start)
    except KeyboardInterrupt:
        sys.stdout.write("\r\033[K")
        print(f"{YELLOW}Detached.{RESET} The run keeps going: ideator follow {run_dir.name}")
        return None


# ---------------------------------------------------------------- liking ideas and continuing

def load_state(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        sys.exit(f"{run_dir.name} has no results yet (status: {run_status(run_dir)})")


def ranked_ideas(state: dict) -> list[dict]:
    return sorted((i for i in state.get("ideas", []) if "rank" in i), key=lambda i: i["rank"])


def continue_run(parent: Path, picks: list[int], note: str, rounds: int, extra: list[str], no_follow: bool) -> int:
    """Start a run on the parent's problem, repo and topic, steered toward the picked ideas (by rank)."""
    state, meta = load_state(parent), read_meta(parent)
    by_rank = {i["rank"]: i for i in ranked_ideas(state)}
    missing = [n for n in picks if n not in by_rank]
    if missing:
        sys.exit(f"No idea ranked {', '.join(map(str, missing))} in {parent.name} (it has {len(by_rank)} ranked ideas)")

    liked = list(state.get("liked", []))  # likes accumulate across continuations
    seen = {i["title"] for i in liked}
    for n in picks:
        i = by_rank[n]
        if i["title"] not in seen:
            liked.append({k: i[k] for k in ("title", "summary", "key_mechanism")} | {"rank": n, "run": parent.name})
            seen.add(i["title"])
    notes = [x for x in (state.get("liked_note", ""), note.strip()) if x]

    problem_file = parent / "problem.md"
    description = problem_file.read_text().strip() if problem_file.exists() else state["question"]
    repo_str = meta["repo"] if "repo" in meta else state.get("repo", "")
    repo = Path(repo_str) if repo_str and Path(repo_str).name != "no-repo" else None
    topic = state.get("topic") or meta.get("topic") or slug(parent.name)

    run_dir = launch(description, repo, topic, rounds, extra, liked={"ideas": liked, "note": "; ".join(notes)}, parent=parent)
    print(f"Continuing {BOLD}{topic}{RESET} from {parent.name}, steered by {len(liked)} liked idea(s).")
    return started(run_dir, no_follow)


def ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def interactive(run_dir: Path) -> int:
    """After a run: show the plain-language top ideas, ask which were liked, continue from them."""
    print()
    show_simple(run_dir, top=10)
    count = len(ranked_ideas(load_state(run_dir)))
    while True:
        answer = ask(f"{BOLD}Which ideas did you like?{RESET} Numbers like '1 3', Enter to stop: ")
        if not answer:
            print(f"{DIM}Continue later with: ideator continue {run_dir.name} --like 1 3{RESET}")
            return 0
        try:
            picks = sorted({int(x) for x in answer.replace(",", " ").split()})
        except ValueError:
            print("Please type rank numbers separated by spaces.")
            continue
        if not picks or any(n < 1 or n > count for n in picks):
            print(f"Pick numbers between 1 and {count}.")
            continue
        break
    note = ask("What did you like about them? (optional, helps steer; Enter to skip): ") or ""
    rounds_answer = ask("Rounds for the next run [4]: ")
    if rounds_answer is None:
        return 0
    rounds = int(rounds_answer) if rounds_answer.isdigit() and int(rounds_answer) > 0 else 4
    return continue_run(run_dir, picks, note, rounds, [], no_follow=False)


def cmd_continue(args) -> int:
    run_dir = resolve_run(args.run)
    if run_status(run_dir) == "running":
        sys.exit(f"{run_dir.name} is still running; wait for it to finish (ideator follow {run_dir.name})")
    if not args.like:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            sys.exit("Say which ideas you liked: ideator continue RUN --like 1 3")
        return interactive(run_dir)
    return continue_run(run_dir, sorted(set(args.like)), args.note or "", args.rounds, args.extra, args.no_follow)


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
        best = next((i for i in sorted(ideas, key=lambda i: i.get("rank", 1e9)) if "rank" in i), None)
        top = best["title"] if best else ""
        when = fmt_elapsed(elapsed) if elapsed else "–"
        print(f"{d.name:<36} {colors.get(status, DIM)}{status:<9}{RESET} {when:>8} "
              f"${cost:>6.2f} {len(ideas):>5}  {top[:70]}")
    return 0


def score_keys(ranked: list[dict]) -> tuple[str, str]:
    av = "final_availability" if any("final_availability" in i for i in ranked) else "availability"
    return av, ("final_coherence" if av.startswith("final") else "coherence")


def show_simple(run_dir: Path, top: int) -> None:
    """Plain-language top ideas (from the simplifier pass); falls back to the explicit text when missing."""
    state = load_state(run_dir)
    ranked = ranked_ideas(state)
    av, co = score_keys(ranked)
    print(f"{BOLD}{run_dir.name}{RESET} · {run_status(run_dir)} · ${state.get('cost_usd', 0):.2f} · {len(state['ideas'])} ideas\n")
    print(f"{DIM}Out of 10, higher is better: uniqueness (how few experts would come up with it) · coherence{RESET}\n")
    for i in ranked[:top]:
        s = i.get("simple")
        borrowed = f" {DIM}(from {s['borrowed_from']}){RESET}" if s and s["borrowed_from"] else ""
        print(f"{BOLD}{i['rank']:>2}. {i['title']}{RESET}{borrowed}")
        print(f"    {CYAN}uniqueness {10 - i[av]} · coherence {i[co]}{RESET}")
        if s and s.get("tldr"):
            print(f"    {YELLOW}{BOLD}{s['tldr']}{RESET}")
        print(f"    {s['plain_summary'] if s else i['summary']}\n")


def cmd_report(args) -> int:
    run_dir = resolve_run(args.run)
    report = run_dir / "report.md"
    if not report.exists():
        sys.exit(f"{run_dir.name} has no report yet (status: {run_status(run_dir)})")
    if args.page:
        summary = run_dir / "summary.md"
        return subprocess.call(["less", "-R", str(report if args.explicit or not summary.exists() else summary)])
    if not args.explicit:
        show_simple(run_dir, args.top)
        print(f"{DIM}Full detail: ideator report {run_dir.name} --explicit · liked some? ideator continue {run_dir.name} --like 1 3{RESET}")
        return 0
    state = json.loads((run_dir / "state.json").read_text())
    ranked = sorted((i for i in state["ideas"] if "rank" in i), key=lambda i: i["rank"])
    av, co = score_keys(ranked)
    print(f"{BOLD}{run_dir.name}{RESET} · {run_status(run_dir)} · ${state.get('cost_usd', 0):.2f} · {len(state['ideas'])} ideas\n")
    for i in ranked[: args.top]:
        print(f"{BOLD}{i['rank']}. {i['title']}{RESET}")
        print(f"   {DIM}score {i['score']:+.2f} · availability {i.get(av)} · coherence {i.get(co)} · {i['generator']} r{i['round']}{RESET}")
        print(f"   {i['summary']}\n")
    print(f"Full report: {report}\n{DIM}(ideator report --explicit --page to read it here){RESET}")
    return 0


def cmd_simplify(args) -> int:
    """Run the plain-language pass on an existing run (e.g. one made before the pass existed)."""
    import anthropic

    from .llm import CostTracker
    from .run import load_dotenv, simplify_ideas, summary_md, write_atomic

    run_dir = resolve_run(args.run)
    if run_status(run_dir) == "running":
        sys.exit(f"{run_dir.name} is still running; it simplifies itself when it finishes")
    state = load_state(run_dir)
    top = ranked_ideas(state)[: args.top]
    if not top:
        sys.exit(f"{run_dir.name} has no ranked ideas to simplify")
    load_dotenv(ROOT / ".env")
    tracker = CostTracker()
    print(f"Simplifying the top {len(top)} ideas of {run_dir.name} with Opus…")
    simplify_ideas(anthropic.Anthropic(max_retries=4), tracker, lambda m, k=None: print(f"{DIM}{m}{RESET}"),
                   state["question"], top)
    state.pop("overview", None)
    state["cost_usd"] = round(state.get("cost_usd", 0) + tracker.cost(), 4)
    write_atomic(run_dir / "state.json", json.dumps(state, indent=2, ensure_ascii=False, default=str))
    write_atomic(run_dir / "summary.md", summary_md(state.get("topic", ""), ranked_ideas(state), *score_keys(top), len(top)))
    print(f"{DIM}cost ${tracker.cost():.2f}{RESET}\n")
    show_simple(run_dir, top=len(top))
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

    c = sub.add_parser("continue", help="new run on the same topic, steered toward ideas you liked")
    c.add_argument("run", nargs="?")
    c.add_argument("-l", "--like", type=int, nargs="+", metavar="RANK", help="ranks of the ideas you liked (asks if omitted)")
    c.add_argument("--note", help="what you liked about them; passed to the generators and the coach")
    c.add_argument("-n", "--rounds", type=int, default=4)
    c.add_argument("--no-follow", action="store_true", help="start in the background and return")
    c.set_defaults(func=cmd_continue)

    r = sub.add_parser("report", help="top ideas of a run, in plain words")
    r.add_argument("run", nargs="?")
    r.add_argument("--top", type=int, default=5)
    r.add_argument("--explicit", action="store_true", help="the full write-up of each idea instead of the plain version")
    r.add_argument("--page", action="store_true", help="page through the markdown (summary, or full report with --explicit)")
    r.set_defaults(func=cmd_report)

    sp = sub.add_parser("simplify", help="add the plain-language version to an older run")
    sp.add_argument("run", nargs="?")
    sp.add_argument("--top", type=int, default=10)
    sp.set_defaults(func=cmd_simplify)

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

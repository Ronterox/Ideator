"""Structured run events (events.jsonl) and their colored, live terminal rendering."""

import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

RESET, BOLD, DIM, INVERSE = "\033[0m", "\033[1m", "\033[2m", "\033[7m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = (f"\033[{n}m" for n in (31, 32, 33, 34, 35, 36, 37))
BRIGHT_MAGENTA, BRIGHT_BLUE = "\033[95m", "\033[94m"

TOOL_RE = re.compile(r"^(read_file|list_dir|grep) ")
SCORE_RE = re.compile(r"(availability|avail) (\d+)")


class EventLog:
    """Append-only event log shared by all threads of a run; optionally echoes rendered lines."""

    def __init__(self, run_dir: Path, tracker, echo: bool):
        self.path = run_dir / "events.jsonl"
        self.tracker = tracker
        self.echo = echo
        self.start = time.time()
        self._lock = threading.Lock()
        self._fh = self.path.open("a", buffering=1)

    def emit(self, source: str, msg: str, kind: str | None = None) -> None:
        event = {
            "t": round(time.time(), 3),
            "elapsed": round(time.time() - self.start, 1),
            "cost": round(self.tracker.cost(), 4),
            "source": source,
            "kind": kind or infer_kind(msg),
            "msg": msg,
        }
        with self._lock:
            self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            if self.echo:
                print(render(event, color=sys.stdout.isatty()), flush=True)

    def logger(self, source: str):
        return lambda msg, kind=None: self.emit(source, msg, kind)


def infer_kind(msg: str) -> str:
    if msg.startswith("FAILED"):
        return "error"
    if msg.startswith("WARNING"):
        return "warn"
    if "(server)" in msg:
        return "search"
    if TOOL_RE.match(msg):
        return "tool"
    return "info"


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def source_color(source: str) -> str:
    if "final" in source:
        return BRIGHT_MAGENTA + BOLD
    if "critic" in source:
        return MAGENTA
    if "coach" in source:
        return YELLOW
    if "sonnet" in source:
        return BRIGHT_BLUE
    if "haiku" in source:
        return GREEN
    return WHITE + BOLD


def _score_color(match: re.Match) -> str:
    value = int(match.group(2))
    color = GREEN if value <= 3 else YELLOW if value <= 6 else RED
    return f"{match.group(1)} {color}{BOLD}{value}{RESET}"


def render(event: dict, color: bool = True) -> str:
    clock = time.strftime("%H:%M:%S", time.localtime(event["t"]))
    elapsed = "+" + fmt_elapsed(event["elapsed"])
    cost = f"${event['cost']:.2f}"
    source = event["source"][:30].ljust(30)
    msg, kind = event["msg"], event["kind"]
    if not color:
        return f"{clock} {elapsed:>8} {cost:>7}  {source} {msg}"

    if kind == "round":
        return f"\n{BOLD}{INVERSE} {clock}  {elapsed}  {cost}  {msg} {RESET}"
    if kind == "done":
        return f"{BOLD}{GREEN}{clock} {elapsed:>8} {cost:>7}  {msg}{RESET}"
    body = {
        "tool": DIM + msg + RESET,
        "search": CYAN + msg + RESET,
        "error": RED + BOLD + msg + RESET,
        "warn": YELLOW + msg + RESET,
        "result": BOLD + msg + RESET,
        "summary": GREEN + BOLD + msg + RESET,
        "score": SCORE_RE.sub(_score_color, msg),
    }.get(kind, msg)
    return (f"{DIM}{clock}{RESET} {CYAN}{elapsed:>8}{RESET} {YELLOW}{cost:>7}{RESET}  "
            f"{source_color(event['source'])}{source}{RESET} {body}")


# ---------------------------------------------------------------- following a run

def pid_alive(pid: int | None, run_dir: Path | None = None) -> bool:
    if not pid:
        return False
    if run_dir is not None:  # guard against pid reuse: the process must be this run's
        try:
            if str(run_dir) not in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace"):
                return False
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited child not yet reaped) still answers kill(0); check its state.
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def read_meta(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def print_stdout_tail(run_dir: Path, lines: int = 25) -> None:
    tail = run_dir / "stdout.log"
    if tail.exists():
        print("\n".join(tail.read_text(errors="replace").splitlines()[-lines:]) or "(no output)")


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def follow(run_dir: Path, from_start: bool = True) -> int:
    """Print a run's events as they arrive, with a live status line. Returns when the run ends."""
    path = run_dir / "events.jsonl"
    meta = read_meta(run_dir)
    tty = sys.stdout.isatty()
    red, bold, reset = (RED, BOLD, RESET) if tty else ("", "", "")
    while not path.exists():  # the run process may not have created the file yet
        if not pid_alive(meta.get("pid"), run_dir):
            print(f"{red}{bold}The run exited before it started logging. Its output:{reset}")
            print_stdout_tail(run_dir)
            return 1
        time.sleep(0.1)

    last = {"cost": 0.0, "elapsed": 0.0, "t": time.time()}
    round_label, current, status_shown, tick = "starting", "", False, 0
    start = meta.get("started", time.time())

    def clear_status():
        nonlocal status_shown
        if status_shown:
            sys.stdout.write("\r\033[K")
            status_shown = False

    def show_status(state: str):
        nonlocal status_shown, tick
        if not tty:
            return
        tick += 1
        elapsed = time.time() - start if state == "running" else last["elapsed"]
        width = shutil.get_terminal_size((120, 20)).columns
        text = (f"{SPINNER[tick % len(SPINNER)] if state == 'running' else '●'} {state} · {round_label} · "
                f"{fmt_elapsed(elapsed)} elapsed · ${last['cost']:.2f} · {current}")
        sys.stdout.write("\r\033[K" + BOLD + text[: width - 1] + RESET)
        sys.stdout.flush()
        status_shown = True

    with path.open() as fh:
        if not from_start:
            fh.seek(0, os.SEEK_END)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if line and not line.endswith("\n") and pid_alive(meta.get("pid"), run_dir):
                fh.seek(pos)  # half-written event: wait for the rest of the line
                time.sleep(0.1)
                continue
            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last = event
                if event["kind"] == "round":
                    round_label = event["msg"].strip("= ").lower()
                current = f"{event['source']}: {event['msg']}"[:80]
                clear_status()
                print(render(event, color=tty), flush=True)
                if event["kind"] == "done":
                    return 0 if "finished" in event["msg"] else 1
                continue
            if not pid_alive(meta.get("pid"), run_dir):
                time.sleep(0.5)
                pos = fh.tell()
                if fh.readline():  # a final line raced in; rewind and print it
                    fh.seek(pos)
                    continue
                clear_status()
                print(f"{red}{bold}The run process exited without finishing. Last output:{reset}")
                print_stdout_tail(run_dir)
                return 1
            show_status("running")
            time.sleep(0.25)

"""Ideas -> critic -> coach loop.

Generators (Sonnet, Haiku) propose ideas for one question, researching a local repo and the web.
An Opus critic scores each idea's availability (who would have come up with it) and coherence.
A coach (Sonnet by default) rewrites each generator's prompt and tells it why its ideas were not unique.
Generators never see coherence. The final ranking fuses both scores, as in "The Alien Space of Science":
    score = (1 - beta) * z(coherence) + beta * z(unavailability)

Usually started through the `ideator` CLI (alien_loop/cli.py), which runs this module detached
and follows its events. Direct use:
    python -m alien_loop.run --question-file brief.md --topic NAME --repo path/to/repo
"""

import argparse
import json
import os
import random
import re
import signal
import statistics
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic

from . import prompts as P
from .llm import AgentError, CostTracker, run_agent
from .repo_tools import TOOL_DEFS, RepoTools
from .ui import EventLog

DEFAULT_QUESTION = "How can we automatically judge whether a research idea is genuinely novel?"
DEFAULT_REPO = Path(__file__).resolve().parent.parent / "alien-space-of-science"
CRITIC_MODEL = "claude-opus-5-5"


def load_prior_ideas(runs_dir: Path, topic: str, question: str, exclude: Path) -> list[dict]:
    """Ideas from earlier runs on the same topic (or, for runs without a topic, the same question)."""
    seen, prior = set(), []
    for state_file in sorted(runs_dir.glob("*/state.json")):
        if state_file.parent == exclude:
            continue
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if topic and state.get("topic"):
            if state["topic"] != topic:
                continue
        elif state.get("question", "").strip().lower() != question.strip().lower():
            continue
        for idea in state.get("ideas", []):
            title = idea.get("title", "").strip()
            if title and title.lower() not in seen and idea.get("key_mechanism"):
                seen.add(title.lower())
                prior.append({"title": title, "key_mechanism": idea["key_mechanism"], "run": state_file.parent.name})
    return prior

def zscores(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mean, sd = statistics.fmean(values), statistics.pstdev(values)
    return [0.0 if sd == 0 else (v - mean) / sd for v in values]


def norm_id(idea_id: str) -> str:
    return idea_id.strip().strip("[]` ").lower()


# Generators must never be steered by coherence. Kept narrow on purpose: words like "score" are
# part of many questions' own topic (e.g. scoring novelty), so they are not flagged.
LEAK_RE = re.compile(
    r"\w*coheren\w*|\w*feasib\w*|practicalit\w*|workab\w*|technically sound|\b\d+(?:\.\d+)?\s*/\s*10\b|\brigou?r\w*",
    re.IGNORECASE,
)


def leaks(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in LEAK_RE.finditer(text)})


def strip_leaks(text: str) -> str:
    """Last resort: drop sentences that still mention coherence after rewriting failed."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(s for s in sentences if not LEAK_RE.search(s))


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def idea_line(idea: dict) -> str:
    return f"- {idea['title']}: {idea['key_mechanism']}"


class Loop:
    def __init__(self, args):
        self.args = args
        self.client = anthropic.Anthropic(max_retries=4)
        self.repo = RepoTools(Path(args.repo))
        self.tracker = CostTracker()
        self.generators = [dict(g, prompts=[g["system"]], feedback="") for g in P.GENERATORS]
        self.ideas: list[dict] = []
        self.coaching: list[dict] = []
        self.out = Path(args.run_dir) if args.run_dir else Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.out.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.out, self.tracker, echo=sys.stdout.isatty())
        self.prior = [] if args.no_carry else load_prior_ideas(self.out.parent, args.topic, args.question, self.out)

    # ---------------------------------------------------------------- generate
    def generate(self, gen: dict, round_no: int) -> list[dict]:
        log = self.events.logger(f"r{round_no} {gen['id']}")
        taken = self.prior + self.ideas
        user = P.GENERATOR_TASK.format(
            question=self.args.question,
            n_ideas=self.args.ideas,
            taken_block=P.TAKEN_BLOCK.format(taken="\n".join(idea_line(i) for i in taken)) if taken else "",
            feedback_block=P.FEEDBACK_BLOCK.format(feedback=gen["feedback"]) if gen["feedback"] else "",
        )
        log(f"generating with {gen['model']}")
        result = run_agent(
            self.client, model=gen["model"], system=gen["system"], user_text=user,
            submit_tool=P.SUBMIT_IDEAS_TOOL, tracker=self.tracker, role="generator", log=log, effort=self.args.gen_effort,
            repo=self.repo, repo_tool_defs=TOOL_DEFS, web_searches=self.args.gen_searches,
            max_turns=self.args.max_turns, use_fallbacks=not self.args.no_fallbacks,
        )
        ideas = result["ideas"][: self.args.ideas]
        for n, idea in enumerate(ideas, 1):
            idea.update(id=f"r{round_no}-{gen['id']}-{n}", round=round_no, generator=gen["id"], model=gen["model"])
        log(f"submitted {len(ideas)} ideas: " + " | ".join(i["title"] for i in ideas), "result")
        return ideas

    # ---------------------------------------------------------------- critic
    def review(self, round_ideas: list[dict], round_no: int) -> None:
        log = self.events.logger(f"r{round_no} critic")
        for attempt in range(2):  # second pass only covers ideas the critic skipped
            pending = [i for i in round_ideas if "availability" not in i]
            if not pending:
                return
            if attempt:
                log(f"re-reviewing {len(pending)} skipped idea(s)")
            self._review_batch(pending, round_no, log)
        for i in round_ideas:
            if "availability" not in i:
                log(f"WARNING: no review for {i['id']}; it is left out of the ranking")

    def _review_batch(self, round_ideas: list[dict], round_no: int, log) -> None:
        scored = [i for i in self.ideas if "availability" in i]
        calibration = "\n".join(
            f"- [{i['availability']} avail / {i['coherence']} coh] {i['title']}" for i in scored
        )
        ideas_text = "\n\n".join(
            f"[{i['id']}] {i['title']}\nSummary: {i['summary']}\nKey mechanism: {i['key_mechanism']}"
            for i in round_ideas
        )
        user = P.CRITIC_TASK.format(
            question=self.args.question, round_no=round_no, ideas=ideas_text,
            calibration_block=P.CALIBRATION_BLOCK.format(calibration=calibration) if scored else "",
            prior_block=P.PRIOR_RUNS_BLOCK.format(prior="\n".join(idea_line(i) for i in self.prior)) if self.prior else "",
        )
        log(f"reviewing {len(round_ideas)} ideas")
        result = run_agent(
            self.client, model=CRITIC_MODEL, system=P.CRITIC_SYSTEM, user_text=user,
            submit_tool=P.SUBMIT_REVIEW_TOOL, tracker=self.tracker, role="critic", log=log, effort=self.args.critic_effort,
            web_searches=self.args.critic_searches, max_turns=self.args.max_turns,
            use_fallbacks=not self.args.no_fallbacks,
        )
        by_id = {norm_id(r["idea_id"]): r for r in result["reviews"]}
        for idea in round_ideas:
            r = by_id.get(norm_id(idea["id"]))
            if r is None:
                continue
            idea.update(
                availability=max(0, min(10, int(r["availability"]))),
                coherence=max(0, min(10, int(r["coherence"]))),
                closest_existing_work=r["closest_existing_work"],
                who_would_propose=r["who_would_propose"],
                uniqueness_feedback=strip_leaks(r["uniqueness_feedback"]),
            )
            log(f"availability {idea['availability']}, coherence {idea['coherence']} · {idea['title']}", "score")

    # ---------------------------------------------------------------- coach
    def coach(self, gen: dict, round_ideas: list[dict], round_no: int) -> None:
        log = self.events.logger(f"r{round_no} coach->{gen['id']}")
        mine = [i for i in round_ideas if i["generator"] == gen["id"] and "availability" in i]
        if not mine:
            log("no reviewed ideas; keeping prompt")
            return
        # Coherence is deliberately left out so the coach cannot pass it on.
        findings = "\n\n".join(
            f"Idea: {i['title']}\n{i['summary']}\n"
            f"Closest existing work: {'; '.join(i['closest_existing_work']) or 'none found'}\n"
            f"Who would propose it: {'; '.join(i['who_would_propose']) or 'unclear'}\n"
            f"Reviewer note: {i['uniqueness_feedback']}"
            for i in mine
        )
        user = P.COACH_TASK.format(gen_id=gen["id"], system_prompt=gen["system"], ideas_with_findings=findings)
        coach_args = dict(
            model=self.args.coach_model, system=P.COACH_SYSTEM, submit_tool=P.SUBMIT_COACHING_TOOL, tracker=self.tracker, role="coach",
            log=log, effort="medium", max_turns=3, use_fallbacks=not self.args.no_fallbacks,
        )
        result = run_agent(self.client, user_text=user, **coach_args)
        rewrites = 0
        while flagged := leaks(result["critique"] + "\n" + result["new_system_prompt"]):
            if rewrites == 2:  # still leaking after two rewrites: fall back to dropping the sentences
                log(f"rewrite did not remove {flagged}; dropping those sentences")
                result = {k: strip_leaks(v) for k, v in result.items()}
                break
            rewrites += 1
            log(f"coach output mentions {flagged}; asking for a rewrite ({rewrites}/2)")
            result = run_agent(self.client, user_text=P.COACH_REWRITE_TASK.format(flagged=", ".join(flagged), **result), **coach_args)
        result["rewrites"] = rewrites
        gen["system"] = result["new_system_prompt"]
        gen["feedback"] = result["critique"]
        gen["prompts"].append(gen["system"])
        self.coaching.append({"round": round_no, "generator": gen["id"], **result})
        log("prompt rewritten")

    # ---------------------------------------------------------------- final pass
    def final_pass(self) -> None:
        if not self.ideas:
            return
        self.events.emit("ideator", "FINAL PASS: rescoring every idea on one scale", "round")
        """Rescore every idea from every round in one critic call, so all scores share one scale.

        Ideas are shuffled and given anonymous ids so the critic cannot favour a round or a generator.
        """
        log = self.events.logger("final critic")
        ideas = list(self.ideas)
        random.Random(self.args.seed).shuffle(ideas)
        anon = {f"I{n:03d}": idea for n, idea in enumerate(ideas, 1)}
        for attempt in range(2):  # second attempt only covers ideas the critic skipped
            pending = {k: i for k, i in anon.items() if "final_availability" not in i}
            if not pending:
                break
            text = "\n\n".join(
                f"[{k}] {i['title']}\nSummary: {i['summary']}\nKey mechanism: {i['key_mechanism']}\n"
                f"Prior findings - closest work: {'; '.join(i.get('closest_existing_work', [])) or 'not reviewed'}\n"
                f"Prior findings - who would propose it: {'; '.join(i.get('who_would_propose', [])) or 'not reviewed'}"
                for k, i in pending.items()
            )
            log(f"rescoring {len(pending)} ideas on one scale" + (" (retry for skipped ideas)" if attempt else ""))
            try:
                result = run_agent(
                    self.client, model=CRITIC_MODEL, system=P.FINAL_SYSTEM,
                    user_text=P.FINAL_TASK.format(question=self.args.question, n=len(pending), ideas=text),
                    submit_tool=P.SUBMIT_FINAL_TOOL, tracker=self.tracker, role="final", log=log, effort=self.args.final_effort,
                    web_searches=self.args.final_searches, max_turns=6, use_fallbacks=not self.args.no_fallbacks,
                )
            except Exception as exc:
                log(f"FAILED: {type(exc).__name__}: {exc}")
                return
            by_id = {norm_id(s["idea_id"]): s for s in result["scores"]}
            for k, idea in pending.items():
                s = by_id.get(norm_id(k))
                if s:
                    idea.update(
                        final_availability=max(0, min(10, int(s["availability"]))),
                        final_coherence=max(0, min(10, int(s["coherence"]))),
                        final_rationale=s["rationale"],
                    )
        missing = [i["id"] for i in self.ideas if "final_availability" not in i]
        if missing:
            log(f"WARNING: no final score for {len(missing)} idea(s); they are left out of the ranking: {missing}")

    # ---------------------------------------------------------------- loop
    def run(self) -> None:
        a = self.args
        log = self.events.logger("ideator")
        log(f"topic '{a.topic or '-'}' · repo {self.repo.root} · {a.iterations} rounds × {len(self.generators)} generators")
        log(f"run folder {self.out}")
        log(f"{len(self.prior)} ideas carried over from earlier runs" + (" (use --no-carry to ignore them)" if self.prior else ""))
        try:
            for round_no in range(1, a.iterations + 1):
                if not self.round(round_no):
                    break
            self.final_pass()
        finally:  # whatever happened, rank and report what we paid for
            self.rank()
            self.save()
            self.report()

    def round(self, round_no: int) -> bool:
        a = self.args
        self.events.emit("ideator", f"ROUND {round_no}/{a.iterations}", "round")
        round_ideas: list[dict] = []
        pool = ThreadPoolExecutor(len(self.generators))
        futures = {pool.submit(self.generate, g, round_no): g for g in self.generators}
        try:
            for fut, g in futures.items():
                try:
                    round_ideas.extend(fut.result())
                except Exception as exc:  # one failing generator must not sink the round
                    self.events.logger(f"r{round_no} {g['id']}")(f"FAILED: {type(exc).__name__}: {exc}")
        except KeyboardInterrupt:  # stopping: keep whatever the generators finish, then let run() report
            self.events.emit("ideator", "WARNING: stop requested; waiting for generators already running")
            pool.shutdown(wait=True)
            for fut in futures:
                if not fut.cancelled() and fut.exception() is None:
                    for idea in fut.result():
                        if idea not in round_ideas:
                            round_ideas.append(idea)
            self.ideas.extend(round_ideas)
            raise
        pool.shutdown(wait=True)
        if not round_ideas:
            self.events.emit("ideator", "WARNING: no generator produced ideas this round; stopping")
            return False

        self.ideas.extend(round_ideas)  # before review: reviews update these dicts in place
        try:
            self.review(round_ideas, round_no)
        except Exception as exc:
            self.events.logger(f"r{round_no} critic")(f"FAILED: {type(exc).__name__}: {exc}")
        self.save()

        if round_no < a.iterations:
            with ThreadPoolExecutor(len(self.generators)) as pool:
                futures = {pool.submit(self.coach, g, round_ideas, round_no): g for g in self.generators}
                for fut, g in futures.items():
                    try:
                        fut.result()
                    except Exception as exc:
                        self.events.logger(f"r{round_no} coach->{g['id']}")(f"FAILED, keeping old prompt: {type(exc).__name__}: {exc}")
        self.save()
        self.events.emit("ideator", f"round {round_no} done · {len(round_ideas)} ideas · ${self.tracker.cost():.2f} so far", "result")
        return True

    def rank(self) -> None:
        # Rank on the single-scale final scores; only if the final pass produced none, use in-round scores.
        if any("final_availability" in i for i in self.ideas):
            self.avail_key, self.coh_key, self.score_source = "final_availability", "final_coherence", "final calibration pass"
        else:
            self.avail_key, self.coh_key, self.score_source = "availability", "coherence", "in-round reviews (final pass failed)"
        pool = [i for i in self.ideas if self.avail_key in i]
        z_coh = zscores([i[self.coh_key] for i in pool])
        z_unav = zscores([10 - i[self.avail_key] for i in pool])
        for idea, zc, zu in zip(pool, z_coh, z_unav):
            idea["score"] = round((1 - self.args.beta) * zc + self.args.beta * zu, 3)
        pool.sort(key=lambda i: i["score"], reverse=True)
        for rank, idea in enumerate(pool, 1):
            idea["rank"] = rank

    def save(self) -> None:
        state = {
            "topic": self.args.topic, "question": self.args.question, "repo": str(self.repo.root), "config": vars(self.args),
            "ideas": self.ideas, "coaching": self.coaching,
            "prompts": {g["id"]: g["prompts"] for g in self.generators}, "prior_ideas": len(self.prior),
            "usage": self.tracker.by_model, "cost_usd": round(self.tracker.cost(), 4),
        }
        write_atomic(self.out / "state.json", json.dumps(state, indent=2, ensure_ascii=False, default=str))

    def report(self) -> None:
        a = self.args
        ranked = sorted((i for i in self.ideas if "rank" in i), key=lambda i: i["rank"])
        md = [f"# Ideation run: {a.topic or 'untitled'}\n\n**Problem:**\n\n> " + a.question.replace("\n", "\n> ") + f"\n\n**Repo:** `{self.repo.root}`  \n"
              f"**Rounds:** {a.iterations} · **beta:** {a.beta} · **ideas/generator:** {a.ideas} · "
              f"**ideas carried over from earlier runs:** {len(self.prior)}\n"]

        av, co = self.avail_key, self.coh_key
        rounds = range(1, a.iterations + 1)
        md.append(f"Scores below come from the **{self.score_source}**: every idea from every round rescored together in one call, shuffled, with anonymous ids.\n")
        md.append("## Did the loop make ideas less available?\n\nMean availability per round (lower = fewer experts would reach it). "
                  "Coherence shown for reference; generators never saw it.\n")

        def mean_row(label, rows_for):
            cells = []
            for key in (av, co):
                for r in rounds:
                    rows = [i[key] for i in rows_for(r) if key in i]
                    cells.append(f"{statistics.fmean(rows):.1f}" if rows else "–")
            return f"| {label} | " + " | ".join(cells) + " |"

        md.append("| generator | " + " | ".join(f"r{r} avail" for r in rounds) + " | " + " | ".join(f"r{r} coh" for r in rounds) + " |")
        md.append("|---|" + "---|" * (2 * a.iterations))
        for g in self.generators:
            md.append(mean_row(g["id"], lambda r, g=g: [i for i in self.ideas if i["generator"] == g["id"] and i["round"] == r]))
        md.append(mean_row("**all**", lambda r: [i for i in self.ideas if i["round"] == r]))

        md.append(f"\n## Top {a.top_k} ideas\n")
        for i in ranked[: a.top_k]:
            md.append(
                f"### {i['rank']}. {i['title']}\n\n"
                f"`{i['id']}` · score **{i['score']}** · availability {i[av]}/10 · coherence {i[co]}/10 · {i['model']}"
                f" · in-round scores: availability {i.get('availability', '–')}, coherence {i.get('coherence', '–')}\n\n"
                + (f"**Final-pass rationale:** {i['final_rationale']}\n\n" if i.get("final_rationale") else "") +
                f"{i['summary']}\n\n**Key mechanism:** {i['key_mechanism']}\n\n"
                f"**Why the ideator thinks it's unexpected:** {i['why_unexpected']}\n\n"
                f"**Who the critic thinks would propose it:**\n" + "\n".join(f"- {w}" for w in i.get("who_would_propose", [])) +
                "\n\n**Closest existing work:**\n" + "\n".join(f"- {w}" for w in i.get("closest_existing_work", [])) +
                ("\n\n**Sources used:** " + ", ".join(i["sources"]) if i["sources"] else "") + "\n"
            )

        md.append("## Full ranking\n\n| rank | score | avail | coh | round | generator | title |\n|---|---|---|---|---|---|---|")
        for i in ranked:
            md.append(f"| {i['rank']} | {i['score']} | {i[av]} | {i[co]} | {i['round']} | {i['generator']} | {i['title']} |")

        md.append("\n## Prompt evolution\n")
        for g in self.generators:
            md.append(f"### {g['id']} ({g['model']})\n")
            for r, p in enumerate(g["prompts"], 1):
                md.append(f"**Round {r} prompt:**\n\n> " + p.replace("\n", "\n> ") + "\n")
            for c in (c for c in self.coaching if c["generator"] == g["id"]):
                md.append(f"**Critique after round {c['round']}** (coach rewrites to remove coherence wording: {c.get('rewrites', 0)}):\n\n> " + c["critique"].replace("\n", "\n> ") + "\n")

        md.append("## Cost\n\n" + self.tracker.table() + "\n")
        write_atomic(self.out / "report.md", "\n".join(md))

        if ranked:
            self.events.emit("ideator", f"TOP {min(a.top_k, len(ranked))} IDEAS", "round")
        for i in ranked[: a.top_k]:
            self.events.emit("ideator", f"{i['rank']}. [{i['score']:+.2f} · avail {i[av]} · coh {i[co]}] {i['title']}  ({i['generator']}, r{i['round']})", "summary")
        self.events.emit("ideator", f"report {self.out / 'report.md'}")


def load_dotenv(path: Path) -> None:
    """Set KEY=VALUE lines from a .env file without overriding variables already set."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--question", default=DEFAULT_QUESTION, help="The problem, inline.")
    p.add_argument("--question-file", help="Read the problem description from this file (overrides --question).")
    p.add_argument("--topic", default="", help="Short name; ideas carry over between runs with the same topic.")
    p.add_argument("--run-dir", help="Exact folder for this run (default: <out>/<timestamp>).")
    p.add_argument("--repo", default=str(DEFAULT_REPO), help="Folder the generators can read (read-only).")
    p.add_argument("--iterations", type=int, default=4)
    p.add_argument("--ideas", type=int, default=3, help="Ideas per generator per round.")
    p.add_argument("--beta", type=float, default=0.7, help="Weight on unavailability in the final ranking.")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--gen-searches", type=int, default=4, help="Max web searches per generator per round.")
    p.add_argument("--critic-searches", type=int, default=6, help="Max web searches for the critic per round.")
    p.add_argument("--final-searches", type=int, default=5, help="Max web searches in the final rescoring pass.")
    p.add_argument("--seed", type=int, default=0, help="Shuffle seed for the final pass.")
    p.add_argument("--max-turns", type=int, default=12, help="Tool-use turns before an agent is told to submit.")
    p.add_argument("--gen-effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--critic-effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"], help="Per-round critic (Opus).")
    p.add_argument("--final-effort", default="high", choices=["low", "medium", "high", "xhigh", "max"], help="Final rescoring pass (Opus); decides the ranking.")
    p.add_argument("--coach-model", default="claude-sonnet-5-5", help="Model that rewrites generator prompts.")
    p.add_argument("--no-carry", action="store_true", help="Ignore ideas from earlier runs on the same question.")
    p.add_argument("--no-fallbacks", action="store_true", help="Disable server-side refusal fallbacks.")
    p.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "runs"))
    args = p.parse_args()

    if args.question_file:
        args.question = Path(args.question_file).read_text().strip()
    if not Path(args.repo).is_dir():
        sys.exit(f"--repo is not a directory: {args.repo}")

    def stop(signum, frame):  # `ideator stop`: finish gracefully, still rank and report
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a second stop must not interrupt the final report
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)

    try:
        loop = Loop(args)
    except Exception:  # nothing to log to yet; the traceback lands in stdout.log, which `ideator follow` shows
        traceback.print_exc()
        sys.exit(1)
    status = "finished"
    try:
        loop.run()
    except KeyboardInterrupt:
        status = "stopped"
    except Exception as exc:
        status = "failed"
        loop.events.emit("ideator", f"FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    loop.events.emit("ideator", f"{status} in {(time.time() - loop.events.start) / 60:.1f} min · total ${loop.tracker.cost():.2f}", "done")


if __name__ == "__main__":
    main()

"""Claude API plumbing: an agent loop that ends when the model calls a submit tool, plus cost tracking."""

import threading

import anthropic

from .repo_tools import RepoTools, format_args

MAX_TOKENS = 48000

# $ per 1M tokens: input, output, cache read. Cache writes are billed at 1.25x input.
# Older ids are listed because a refusal fallback can serve a request with another model.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
HAIKU_LONG = (0.50, 2.50, 0.05)  # Haiku 5.5 prompts over 100K tokens
WEB_SEARCH_PRICE = 10.00 / 1000  # $ per search


def _price_key(model: str) -> str:
    if model in PRICES:
        return model
    matches = [k for k in PRICES if model.startswith(k)]
    return max(matches, key=len) if matches else "claude-opus-5"  # unknown: price conservatively

# Models without server-side refusal fallback support (Haiku has none).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


def web_search_tool(model: str, max_uses: int) -> dict:
    # The dynamic-filtering variant needs Opus/Sonnet; Haiku gets the basic one.
    kind = "web_search_20250305" if "haiku" in model else "web_search_20260209"
    return {"type": kind, "name": "web_search", "max_uses": max_uses}


class CostTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.by_model: dict[str, dict] = {}

    def add(self, model: str, usage, role: str = "") -> None:
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        inp = usage.input_tokens or 0
        out = usage.output_tokens or 0
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        key = _price_key(model)
        pin, pout, pread = HAIKU_LONG if key == "claude-haiku-5-5" and inp + read + write > 100_000 else PRICES[key]
        dollars = (inp * pin + write * pin * 1.25 + read * pread + out * pout) / 1e6 + searches * WEB_SEARCH_PRICE
        with self._lock:
            row = self.by_model.setdefault(f"{role}/{model}" if role else model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "searches": 0, "calls": 0, "usd": 0.0})
            row["input"] += inp
            row["output"] += out
            row["cache_read"] += read
            row["cache_write"] += write
            row["searches"] += searches
            row["calls"] += 1
            row["usd"] += dollars

    def cost(self, key: str | None = None) -> float:
        with self._lock:
            return sum(r["usd"] for k, r in self.by_model.items() if key is None or k == key)

    def table(self) -> str:
        lines = ["| role / model | calls | input | cache read | cache write | output | searches | cost |", "|---|---|---|---|---|---|---|---|"]
        for m, r in sorted(self.by_model.items()):
            lines.append(f"| {m} | {r['calls']} | {r['input']:,} | {r['cache_read']:,} | {r['cache_write']:,} | {r['output']:,} | {r['searches']} | ${self.cost(m):.2f} |")
        lines.append(f"| **total** | | | | | | | **${self.cost():.2f}** |")
        return "\n".join(lines)


class AgentError(RuntimeError):
    pass


def run_agent(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    user_text: str,
    submit_tool: dict,
    tracker: CostTracker,
    role: str = "",
    log,
    effort: str = "medium",
    repo: RepoTools | None = None,
    repo_tool_defs: list | None = None,
    web_searches: int = 0,
    max_turns: int = 12,
    use_fallbacks: bool = True,
) -> dict:
    """Run a tool loop until the model calls `submit_tool`; return that call's input."""
    tools = list(repo_tool_defs or [])
    if web_searches:
        tools.append(web_search_tool(model, web_searches))
    tools.append(submit_tool)
    submit_name = submit_tool["name"]

    extra = {}
    if use_fallbacks and model in FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    messages: list = [{"role": "user", "content": user_text}]
    nudges = 0
    for turn in range(1, max_turns + 4):
        # Streaming lets us allow a large output (thinking + a long submit) without HTTP timeouts.
        with client.beta.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
            **extra,
        ) as stream:
            response = stream.get_final_message()
        tracker.add(response.model, response.usage, role)

        if response.stop_reason == "refusal":
            raise AgentError(f"{model} refused: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            # Any tool_use in a truncated turn may carry partial input; never act on it.
            raise AgentError(f"{model} hit max_tokens={MAX_TOKENS}; output truncated")

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue  # server-side tool loop paused; re-send so it resumes

        for block in response.content:
            if block.type == "server_tool_use":
                log(f"{block.name} (server) {format_args(block.input)}")

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        submit = next((b for b in tool_uses if b.name == submit_name), None)
        if submit is not None:
            return submit.input

        if not tool_uses:
            nudges += 1
            if nudges > 2:
                raise AgentError(f"{model} never called {submit_name} (stop_reason={response.stop_reason})")
            messages.append({"role": "user", "content": f"Call {submit_name} now with your final answer."})
            continue

        results = []
        for block in tool_uses:
            log(f"{block.name} {format_args(block.input)}")
            try:
                if repo is None:
                    raise ValueError(f"tool {block.name} is not available")
                content = repo.execute(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            except Exception as exc:  # report tool errors back to the model instead of crashing
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {exc}", "is_error": True})
        if turn >= max_turns:
            results.append({"type": "text", "text": f"Your research budget is used up. Call {submit_name} now."})
        messages.append({"role": "user", "content": results})

    raise AgentError(f"{model} exceeded the turn limit without calling {submit_name}")

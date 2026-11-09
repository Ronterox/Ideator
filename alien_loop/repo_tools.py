"""Read-only file tools the generators use to research a local repository.

Every path is resolved against the repo root and rejected if it escapes it.
"""

import fnmatch
import json
import shutil
import subprocess
from pathlib import Path

if shutil.which("rg") is None:
    raise SystemExit("ripgrep (rg) is required for the grep tool: sudo apt install ripgrep")

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".omc", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", ".zig-cache", "zig-cache", "zig-out"}
# Never read anything that looks like a secret: file contents go to the API and into run logs.
SECRET_GLOBS = [".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
                "*credentials*", "secrets.*", "*.p12", "*.pfx", "*.jks", "*.keystore", ".netrc", ".npmrc", ".pypirc"]


def is_secret(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)
MAX_FILE_BYTES = 2_000_000
MAX_LIST_ENTRIES = 200
MAX_GREP_RESULTS = 60
GREP_SECONDS = 10


class RepoTools:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _inside(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def _resolve(self, rel: str) -> Path:
        target = (self.root / (rel or ".")).resolve()
        if not self._inside(target):
            raise ValueError(f"path escapes the repository root: {rel}")
        return target

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise ValueError(f"not a directory: {path}")
        entries = []
        for child in sorted(target.iterdir()):
            if child.name in SKIP_DIRS:
                continue
            try:
                if child.is_dir():
                    entries.append(f"{child.name}/")
                else:
                    entries.append(f"{child.name}  ({child.stat().st_size:,} bytes)")
            except OSError:
                entries.append(f"{child.name}  (broken link)")
        more = len(entries) - MAX_LIST_ENTRIES
        out = "\n".join(entries[:MAX_LIST_ENTRIES])
        if more > 0:
            out += f"\n... {more} more entries not shown"
        return out or "(empty directory)"

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 250) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        if is_secret(target):
            raise ValueError(f"refusing to read a file that may contain secrets: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"file too large ({target.stat().st_size:,} bytes); use grep instead")
        lines = target.read_text(errors="replace").splitlines()
        start = max(start_line, 1)
        max_lines = min(max(max_lines, 1), 400)
        chunk = lines[start - 1:start - 1 + max_lines]
        body = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chunk))
        end = start - 1 + len(chunk)
        if end < len(lines):
            body += f"\n[showing lines {start}-{end} of {len(lines)}; call again with start_line={end + 1} for more]"
        return body or "(empty file)"

    def grep(self, pattern: str, path: str = ".") -> str:
        base = self._resolve(path)
        if base.is_file() and is_secret(base):  # rg searches files named explicitly even if globs exclude them
            raise ValueError(f"refusing to search a file that may contain secrets: {path}")
        # ripgrep: linear-time regex (no catastrophic backtracking), skips binaries,
        # and does not follow symlinks, so nothing outside the repo is read.
        cmd = [
            "rg", "--line-number", "--no-heading", "--ignore-case", "--color=never",
            "--max-filesize", "2M", "--max-columns", "300", "--max-columns-preview",
            *[arg for d in sorted(SKIP_DIRS) for arg in ("--glob", f"!{d}/")],
            *[arg for g in SECRET_GLOBS for arg in ("--iglob", f"!{g}")],
            "-e", pattern, "--", str(base.relative_to(self.root)) if base != self.root else ".",
        ]
        try:
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=GREP_SECONDS)
        except subprocess.TimeoutExpired:
            return f"[search stopped after {GREP_SECONDS}s; narrow the pattern or path]"
        if proc.returncode == 1:
            return "(no matches)"
        if proc.returncode != 0:
            raise ValueError(proc.stderr.strip()[:500] or "ripgrep failed")
        hits = sorted(proc.stdout.splitlines())
        out = "\n".join(hits[:MAX_GREP_RESULTS])
        if len(hits) > MAX_GREP_RESULTS:
            out += f"\n[{len(hits) - MAX_GREP_RESULTS} more matches not shown; narrow the pattern or path]"
        return out

    def execute(self, name: str, args: dict) -> str:
        handlers = {"list_dir": self.list_dir, "read_file": self.read_file, "grep": self.grep}
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        return handlers[name](**args)


TOOL_DEFS = [
    {
        "name": "list_dir",
        "description": "List the files and subdirectories of a directory in the research repository. Paths are relative to the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory relative to the repo root, e.g. '.' or 'generation'."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the research repository, with line numbers. Long files are returned in pages; follow the hint at the end to read more.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the repo root."},
                "start_line": {"type": "integer", "description": "1-based line to start from."},
                "max_lines": {"type": "integer", "description": "Number of lines to return (max 400)."},
            },
            "required": ["path", "start_line", "max_lines"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grep",
        "description": "Case-insensitive regex search (ripgrep / Rust regex syntax) over text files in the research repository. Returns file:line:text matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path": {"type": "string", "description": "File or directory to search, relative to the repo root."},
            },
            "required": ["pattern", "path"],
            "additionalProperties": False,
        },
    },
]
for _t in TOOL_DEFS:
    _t["strict"] = True


def format_args(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)[:120]

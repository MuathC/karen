"""Fetch exact PR commits into a bare mirror and collect the text the model reads.

PR code is never checked out or run: only git fetch, diff and cat-file touch it.
"""

import base64
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_DIFF_CHARS = 300_000
MAX_CONTEXT_CHARS = 200_000
HUNK_HEADER = re.compile(r"@@ -\d+(?:,(?P<old>\d+))? \+(?P<start>\d+)(?:,(?P<new>\d+))? @@")


@dataclass(frozen=True)
class Snapshot:
    head: str
    diff: str
    # Full text of changed files at head, as context; files that don't fit are left out.
    files: dict[str, str]
    # Why the model can't see the whole change. Empty means it saw every changed byte.
    gaps: list[str]
    notes: list[str] = field(default_factory=list)
    guidelines: str = ""
    profile: str = "strict"
    ignored: list[str] = field(default_factory=list)
    config_error: str = ""
    previous: str = ""
    full_diff: str = ""
    rewritten: bool = False
    prior_findings: tuple[dict, ...] = ()
    # Repositories the agent may browse: name -> {"git_dir", "commit"}.
    repos: dict[str, dict] = field(default_factory=dict)


def _valid_glob(pattern: object) -> bool:
    return (isinstance(pattern, str) and bool(pattern) and "\0" not in pattern
            and not pattern.startswith("/") and ".." not in PurePosixPath(pattern).parts)


def _config(text: str) -> dict:
    config = tomllib.loads(text)
    if set(config) - {"profile", "ignore", "instructions", "path_instructions"}:
        raise ValueError("unknown top-level key")
    if config.get("profile", "strict") not in ("strict", "chill"):
        raise ValueError("profile must be strict or chill")
    if not isinstance(config.get("instructions", ""), str):
        raise ValueError("instructions must be a string")
    if not isinstance(config.get("ignore", []), list) or any(not _valid_glob(p) for p in config.get("ignore", [])):
        raise ValueError("ignore must contain nonempty relative glob strings without NUL or '..' path segments")
    paths = config.get("path_instructions", [])
    if not isinstance(paths, list):
        raise ValueError("path_instructions must be an array of tables")
    for entry in paths:
        if not isinstance(entry, dict) or set(entry) - {"paths", "instructions"}:
            raise ValueError("path_instructions entries allow only paths and instructions")
        if not isinstance(entry.get("paths", []), list) or any(not _valid_glob(p) for p in entry.get("paths", [])):
            raise ValueError("paths must contain nonempty relative glob strings without NUL or '..' path segments")
        if not isinstance(entry.get("instructions", ""), str):
            raise ValueError("path instructions must be a string")
    return config


def _base_file(mirror: Path, base: str, path: str) -> str | None:
    if not _git(mirror, "ls-tree", "--name-only", "-z", base, "--", path):
        return None
    return _git(mirror, "cat-file", "blob", f"{base}:{path}").decode("utf-8")


def fetch_default_branch(mirror: Path, remote: str, token: str) -> str:
    """Fetch a repository's default branch into its bare mirror and return the commit."""
    _init(mirror)
    _git(mirror, "fetch", "--quiet", "--no-tags", remote, "HEAD", token=token)
    return _git(mirror, "rev-parse", "FETCH_HEAD").decode().strip()


def take_snapshot(mirror: Path, remote: str, base: str, head: str, token: str, previous: str = "") -> Snapshot:
    _init(mirror)
    _git(mirror, "fetch", "--quiet", "--no-tags", remote, base, head, token=token)
    merge_base = _git(mirror, "merge-base", base, head).decode().strip()
    rewritten = False
    if previous and previous != head:
        try:
            _git(mirror, "merge-base", "--is-ancestor", previous, head)
        except subprocess.CalledProcessError:
            rewritten = True
            previous = ""
    else:
        previous = ""
    config_error = ""
    try:
        config = _config(_base_file(mirror, base, ".karen.toml") or "")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        config, config_error = {}, "must be valid UTF-8 TOML"
    except ValueError as error:
        config, config_error = {}, str(error)
    excludes = [f":(exclude,glob){pattern}" for pattern in config.get("ignore", [])]
    def changed(*pathspec: str) -> list[str]:
        raw = _git(mirror, "diff", "--name-only", "--no-renames", "-z", merge_base, head, "--", *pathspec)
        return list(map(os.fsdecode, filter(None, raw.split(b"\0"))))
    all_changed = changed()
    reviewed = changed(*excludes) if excludes else all_changed
    ignored = [path for path in all_changed if path not in set(reviewed)]
    guideline_parts = []
    gaps = []
    notes = []
    for path in ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md", "CONTRIBUTING.md", "CODING_STANDARDS.md"):
        try:
            content = _base_file(mirror, base, path)
        except UnicodeDecodeError:
            notes.append(f"Base guideline {path} is not valid UTF-8 and was not read; fix its encoding before relying on this review.")
            continue
        if content is not None:
            guideline_parts.append(f"{path}:\n{content}")
    if config.get("instructions"):
        guideline_parts.append(".karen.toml instructions:\n" + config["instructions"])
    for entry in config.get("path_instructions", []):
        paths = entry.get("paths", [])
        matches = changed(*(f":(glob){p}" for p in paths), *excludes) if paths else []
        if matches and entry.get("instructions"):
            guideline_parts.append(".karen.toml path instructions:\n" + entry["instructions"])
    guidelines = "\n\n".join(guideline_parts)
    if len(guidelines) > 40_000:
        guidelines = guidelines[:40_000] + "\n[Base branch guidelines were truncated at 40,000 characters.]"

    raw_diff = _git(mirror, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", merge_base, head, "--", *excludes)
    full_diff = raw_diff.decode("utf-8", errors="replace")
    if previous:
        raw_diff = (_git(mirror, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", previous, head,
                         "--", *(f":(literal){path}" for path in reviewed)) if reviewed else b"")
    diff = raw_diff.decode("utf-8", errors="replace")
    if diff.encode("utf-8") != raw_diff:
        gaps.append("Some changed bytes aren't valid UTF-8, so the model saw replacement characters instead.")
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        gaps.append(f"The diff is over {MAX_DIFF_CHARS:,} characters, so the model saw only the start of it.")
    numstat = _git(mirror, "diff", "--numstat", "--no-renames", "-z", merge_base, head, "--", *excludes)
    binary = [os.fsdecode(record.split(b"\t", 2)[2]) for record in numstat.split(b"\0") if record.startswith(b"-\t-\t")]
    if binary:
        # File names come from the PR author: quote them so they read as data.
        gaps.append("Binary files changed, which the model can't read: " + ", ".join(map(json.dumps, binary)))

    files = {}
    budget = MAX_CONTEXT_CHARS
    changed_files = _git(mirror, "diff", "--name-only", "--no-renames", "--diff-filter=d", "-z", merge_base, head, "--", *excludes)
    for path in map(os.fsdecode, filter(None, changed_files.split(b"\0"))):
        if path in binary:
            continue
        try:
            content = _git(mirror, "cat-file", "blob", f"{head}:{path}").decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(content) <= budget:
            files[path] = content
            budget -= len(content)
    return Snapshot(head=head, diff=diff, files=files, gaps=gaps, notes=notes, guidelines=guidelines,
                    profile=config.get("profile", "strict"), ignored=ignored, config_error=config_error,
                    previous=previous, rewritten=rewritten, full_diff=full_diff)


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """New-file line numbers per path that GitHub accepts for inline review comments:
    added and context lines inside the diff's hunks."""
    return {path: set().union(*hunks) for path, hunks in commentable_hunks(diff).items()}


def commentable_hunks(diff: str) -> dict[str, list[set[int]]]:
    hunks: dict[str, list[set[int]]] = {}
    path = None
    current: set[int] = set()
    number = old_left = new_left = 0
    for line in diff.split("\n"):
        # Inside a hunk, count lines instead of parsing them: content can look like a header.
        if old_left or new_left:
            if line.startswith("-"):
                old_left -= 1
            elif line.startswith(("+", " ")):
                if path:
                    current.add(number)
                number += 1
                new_left -= 1
                if line.startswith(" "):
                    old_left -= 1
            continue
        if line.startswith("+++ "):
            # Git ends a name containing spaces with a tab. Quoted names (other special
            # characters) and deleted files get no inline comments.
            path = line[len("+++ b/"):].removesuffix("\t") if line.startswith("+++ b/") else None
        elif hunk := HUNK_HEADER.match(line):
            current = set()
            if path:
                hunks.setdefault(path, []).append(current)
            old_left = int(hunk["old"] or 1)
            number = int(hunk["start"])
            new_left = int(hunk["new"] or 1)
    return {path: [hunk for hunk in groups if hunk] for path, groups in hunks.items() if any(groups)}


def _init(mirror: Path) -> None:
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _git(mirror.parent, "init", "--quiet", "--bare", mirror.name)


def _git(cwd: Path, *args: str, token: str = "") -> bytes:
    # Ignore user and system git config so no diff driver, hook or credential helper runs.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True, timeout=300).stdout

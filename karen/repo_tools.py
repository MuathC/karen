"""Read-only repository tools for the review agent, served over MCP on stdio.

The review sandbox runs this file as `python3 -I repo_tools.py <repos.json>`, where the
JSON maps each repository name to a bare git dir and one commit. The tools read git
objects at those commits only: nothing is checked out or run, and no other file, commit
or repository is reachable. Stdlib only, since it runs as a plain script.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path, PurePosixPath

MAX_OUTPUT = 60_000
MAX_FILES = 3_000
MAX_MATCHES = 300
MAX_FILE_BYTES = 5_000_000
MAX_ARGUMENT = 1_000
TYPES = {"string": str, "integer": int, "boolean": bool}


class ToolError(Exception):
    pass


def tools(repos: dict) -> list[dict]:
    names = ", ".join(repos)
    repo = {"type": "string", "description": f"One of: {names}"}
    path = {"type": "string", "description": "Path relative to the repository root"}
    read_only = {"readOnlyHint": True, "openWorldHint": False}
    return [
        {"name": "list_files", "annotations": read_only,
         "description": f"List the files of a repository, optionally under one directory. Repositories: {names}.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["repo"],
                         "properties": {"repo": repo, "directory": path}}},
        {"name": "read_file", "annotations": read_only,
         "description": "Read a file, with line numbers, optionally only a range of lines.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["repo", "path"],
                         "properties": {"repo": repo, "path": path,
                                        "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}}},
        {"name": "search", "annotations": read_only,
         "description": "Find lines containing a text (or an extended regex when regex is true) in one "
                        "repository or, without repo, in all of them. path_glob limits the files searched.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["pattern"],
                         "properties": {"pattern": {"type": "string"}, "repo": repo,
                                        "path_glob": {"type": "string"}, "regex": {"type": "boolean"}}}},
    ]


def call(repos: dict, name: str, arguments: dict) -> str:
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools(repos)}
    if name not in schemas:
        raise ToolError(f"unknown tool; use one of: {', '.join(schemas)}")
    arguments = _checked(schemas[name], arguments)
    if name == "list_files":
        git_dir, commit = _repo(repos, arguments["repo"])
        directory = _path(arguments.get("directory", ""))
        raw = _git(git_dir, "ls-tree", "-r", "--name-only", "-z", commit, "--", *([f":(literal){directory}"] if directory else []))
        files = [os.fsdecode(item) for item in raw.split(b"\0") if item]
        if not files:
            raise ToolError("no files there")
        more = f"\n[{len(files) - MAX_FILES} more files left out; list a directory instead]" if len(files) > MAX_FILES else ""
        return _limit("\n".join(files[:MAX_FILES]) + more, "list a directory instead")
    if name == "read_file":
        git_dir, commit = _repo(repos, arguments["repo"])
        path = _path(arguments["path"])
        if not path:
            raise ToolError("path is required")
        try:
            size = int(_git(git_dir, "cat-file", "-s", f"{commit}:{path}"))
            if size > MAX_FILE_BYTES:
                raise ToolError(f"file too large to read ({size:,} bytes); search it instead")
            raw = _git(git_dir, "cat-file", "blob", f"{commit}:{path}")
        except subprocess.CalledProcessError:
            raise ToolError("no such file") from None
        if b"\0" in raw[:8000]:
            raise ToolError("binary file")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, arguments.get("start_line", 1))
        end = min(len(lines), arguments.get("end_line", len(lines)))
        return _limit("\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1)),
                      "read a smaller line range")
    if name == "search":
        pattern = arguments["pattern"]
        if not pattern or "\0" in pattern or len(pattern) > MAX_ARGUMENT:
            raise ToolError(f"pattern must be a nonempty string of at most {MAX_ARGUMENT} characters")
        glob = _path(arguments.get("path_glob", ""))
        mode = "-E" if arguments.get("regex", False) else "-F"
        selected = [arguments["repo"]] if "repo" in arguments else list(repos)
        results = []
        for repo in selected:
            git_dir, commit = _repo(repos, repo)
            lines, failed = _grep(git_dir, MAX_MATCHES + 1 - len(results), "-n", "-I", "--no-color", mode,
                                  "-e", pattern, commit, "--", *([f":(glob){glob}"] if glob else []))
            if failed:
                raise ToolError("search failed or timed out; check the pattern")
            prefix = f"{commit}:".encode()
            results += [f"{repo}:{line.removeprefix(prefix).decode('utf-8', errors='replace')}" for line in lines]
            if len(results) > MAX_MATCHES:
                break
        if not results:
            return "no matches"
        more = "\n[more matches left out; narrow the search]" if len(results) > MAX_MATCHES else ""
        return _limit("\n".join(results[:MAX_MATCHES]) + more, "narrow the search")


def _checked(schema: dict, arguments: dict) -> dict:
    """The arguments, checked against the tool's schema. Null means absent."""
    given = {key: value for key, value in arguments.items() if value is not None}
    properties = schema["properties"]
    for key, value in given.items():
        if key not in properties:
            raise ToolError(f"unknown argument {key!r}; this tool takes {', '.join(properties)}")
        # type() rather than isinstance(), so true/false aren't integers.
        if type(value) is not TYPES[properties[key]["type"]]:
            raise ToolError(f"{key} must be a {properties[key]['type']}")
        if type(value) is str:
            try:
                value.encode()
            except UnicodeEncodeError:  # JSON can carry lone surrogates, which git can't take
                raise ToolError(f"{key} is not valid text") from None
    missing = [key for key in schema["required"] if key not in given]
    if missing:
        raise ToolError(f"missing {', '.join(missing)}")
    return given


def _repo(repos: dict, name: str) -> tuple[str, str]:
    if name not in repos:
        raise ToolError(f"unknown repository; use one of: {', '.join(repos)}")
    return repos[name]["git_dir"], repos[name]["commit"]


def _path(value: str) -> str:
    if "\0" in value or len(value) > MAX_ARGUMENT or value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise ToolError("paths are relative to the repository root, without '..'")
    return value.removeprefix("./").rstrip("/")


def _limit(text: str, hint: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n[output cut at {MAX_OUTPUT:,} characters; {hint}]"


def _env(git_dir: str) -> dict[str, str]:
    # No user or system git config, so no external diff, textconv or pager runs.
    return {"PATH": "/usr/bin:/bin", "HOME": git_dir, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def _git(git_dir: str, *args: str) -> bytes:
    return subprocess.run(["git", "--git-dir", git_dir, *args], env=_env(git_dir), capture_output=True,
                          check=True, timeout=60).stdout


def _grep(git_dir: str, limit: int, *args: str) -> tuple[list[bytes], bool]:
    """Up to `limit` matching lines, and whether git failed. Reads only what it keeps,
    so a pattern matching most of a big repository can't fill memory."""
    with subprocess.Popen(["git", "--git-dir", git_dir, "grep", *args], env=_env(git_dir),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        timer = threading.Timer(60, process.kill)
        timer.start()
        lines = []
        for line in process.stdout:
            lines.append(line.rstrip(b"\n"))
            if len(lines) >= limit:
                process.kill()
                break
        timer.cancel()
    # git grep exits 1 for no matches; a kill after enough lines isn't a failure.
    return lines, len(lines) < limit and process.returncode not in (0, 1)


def handle(repos: dict, message: object) -> dict | None:
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    if "id" not in message:  # a notification, such as notifications/initialized
        return None
    method, params = message.get("method"), message.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        return _error(message["id"], -32602, "invalid params")
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if isinstance(version, str) else "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "repo", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools(repos)}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments")
        arguments = {} if arguments is None else arguments
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(message["id"], -32602, "invalid params")
        try:
            text, failed = call(repos, name, arguments), False
        except (ToolError, subprocess.SubprocessError, OSError) as error:
            text, failed = str(error) if isinstance(error, ToolError) else "the tool failed", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping":
        result = {}
    else:
        return _error(message["id"], -32601, "method not found")
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def _error(request_id: object, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}


def main() -> None:
    repos = json.loads(Path(sys.argv[1]).read_text())
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = handle(repos, message)
        if reply is not None:
            print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()

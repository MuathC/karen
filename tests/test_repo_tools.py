import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karen import repo_tools
from tests.test_snapshot_and_assess import git


def call(repos: dict, name: str, **arguments) -> tuple[str, bool]:
    reply = repo_tools.handle(repos, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}})
    return reply["result"]["content"][0]["text"], reply["result"]["isError"]


class RepoToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.repos = {}
        for name, files in (("owner/app", {"app.py": "def total(xs):\n    return sum(xs)\n",
                                           "sub/use.py": "from app import total\nprint(total([1]))\n"}),
                            ("owner/lib", {"lib.py": "import app\napp.total([2])\n"})):
            work = root / name.replace("/", "__")
            work.mkdir()
            git(work, "init", "-q", "-b", "main")
            for path, content in files.items():
                (work / path).parent.mkdir(parents=True, exist_ok=True)
                (work / path).write_text(content)
            git(work, "add", ".")
            git(work, "commit", "-qm", "first")
            commit = git(work, "rev-parse", "HEAD")
            # A later commit the tools must not reach.
            (work / "later.py").write_text("hidden\n")
            git(work, "add", ".")
            git(work, "commit", "-qm", "later")
            self.repos[name] = {"git_dir": str(work / ".git"), "commit": commit}

    def test_lists_and_reads_files_at_the_given_commit_only(self) -> None:
        listing, failed = call(self.repos, "list_files", repo="owner/app")
        self.assertFalse(failed)
        self.assertEqual(listing.splitlines(), ["app.py", "sub/use.py"])
        self.assertEqual(call(self.repos, "list_files", repo="owner/app", directory="sub/")[0], "sub/use.py")
        self.assertEqual(call(self.repos, "read_file", repo="owner/app", path="app.py", start_line=2),
                         ("2:     return sum(xs)", False))
        self.assertEqual(call(self.repos, "read_file", repo="owner/app", path="later.py"), ("no such file", True))

    def test_search_spans_repositories_and_filters_paths(self) -> None:
        text, failed = call(self.repos, "search", pattern="total(")
        self.assertFalse(failed)
        self.assertEqual(sorted(text.splitlines()), [
            "owner/app:app.py:1:def total(xs):", "owner/app:sub/use.py:2:print(total([1]))",
            "owner/lib:lib.py:2:app.total([2])"])
        self.assertEqual(call(self.repos, "search", pattern="total(", repo="owner/app", path_glob="sub/*")[0],
                         "owner/app:sub/use.py:2:print(total([1]))")
        self.assertEqual(call(self.repos, "search", pattern="hidden"), ("no matches", False))
        self.assertTrue(call(self.repos, "search", pattern="(", regex=True)[1])

    def test_rejects_paths_and_repositories_outside_the_map(self) -> None:
        for path in ("/etc/passwd", "../config", "sub/../../config", "a\0b"):
            self.assertTrue(call(self.repos, "read_file", repo="owner/app", path=path)[1], path)
        self.assertTrue(call(self.repos, "read_file", repo="owner/other", path="app.py")[1])
        self.assertTrue(call(self.repos, "unknown")[1])

    def test_oversized_and_malformed_input_get_errors_not_a_crash(self) -> None:
        self.assertTrue(call(self.repos, "search", pattern="x" * 200_000)[1])
        self.assertTrue(call(self.repos, "read_file", repo="owner/app", path="a/" * 1_000)[1])
        self.assertTrue(call(self.repos, "list_files", repo=[])[1])
        for message in (None, [], "text"):
            self.assertEqual(repo_tools.handle(self.repos, message)["error"]["code"], -32600)

    def test_wrong_types_and_unknown_arguments_are_errors_not_defaults(self) -> None:
        self.assertEqual(call(self.repos, "search", pattern="[", regex="true"), ("regex must be a boolean", True))
        self.assertEqual(call({}, "search", pattern="[", regex="true"), ("regex must be a boolean", True))
        self.assertEqual(call(self.repos, "list_files", repo="owner/app", directory=None),
                         call(self.repos, "list_files", repo="owner/app"))
        self.assertFalse(call(self.repos, "search", pattern="total(", path_glob=None)[1])
        for arguments in ({"pattern": "\ud800"}, {"pattern": "x", "path_glob": "\ud800"}):
            self.assertEqual(call(self.repos, "search", **arguments)[1], True)
        self.assertTrue(call(self.repos, "read_file", repo="owner/app", path="app.py", start_line=True)[1])
        self.assertTrue(call(self.repos, "search", pattern="total(", glob="sub/*")[1])
        self.assertEqual(call(self.repos, "read_file", repo="owner/app", path="app.py", start_line=None, end_line=1),
                         ("1: def total(xs):", False))
        for params in ({"name": "list_files", "arguments": ["x"]}, {"name": "list_files", "arguments": []},
                       {"name": ["list_files"]}, {"name": {}}, False, [], "x"):
            reply = repo_tools.handle(self.repos, {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": params})
            self.assertEqual((reply["id"], reply["error"]["code"]), (10, -32602), params)

    def test_no_message_crashes_the_server_or_loses_its_id(self) -> None:
        values = [None, True, False, 0, 7, -1, 1.5, "", "x", "\ud800", "../x", "/etc", "owner/app", "app.py", "sub/*", "(",
                  [], ["x"], {}, {"a": 1}, 10 ** 20]
        keys = ["repo", "directory", "path", "start_line", "end_line", "pattern", "path_glob", "regex", "other"]
        methods = ["initialize", "tools/list", "tools/call", "ping", "other", None, 3]
        names = ["list_files", "read_file", "search", "other"]
        rng = random.Random(0)
        for _ in range(1_500):
            arguments = {rng.choice(keys): rng.choice(values) for _ in range(rng.randint(0, 4))}
            params = rng.choice([{"name": rng.choice(names + values), "arguments": arguments},
                                 {"name": rng.choice(names), "arguments": rng.choice(values)}, rng.choice(values)])
            message = {"jsonrpc": "2.0", "id": rng.choice([1, "a", None, [1]]), "method": rng.choice(methods),
                       "params": params}
            reply = repo_tools.handle(self.repos, message)
            json.dumps(reply)
            self.assertEqual(reply["id"], message["id"], message)

    def test_outputs_and_reads_stay_bounded(self) -> None:
        with mock.patch.object(repo_tools, "MAX_OUTPUT", 10):
            self.assertIn("[output cut at 10 characters", call(self.repos, "list_files", repo="owner/app")[0])
        with mock.patch.object(repo_tools, "MAX_FILE_BYTES", 10):
            self.assertEqual(call(self.repos, "read_file", repo="owner/app", path="app.py"),
                             ("file too large to read (34 bytes); search it instead", True))
        with mock.patch.object(repo_tools, "MAX_MATCHES", 1):
            self.assertEqual(call(self.repos, "search", pattern="total(")[0].splitlines()[1:],
                             ["[more matches left out; narrow the search]"])

    def test_runs_as_a_plain_mcp_script(self) -> None:
        repos_file = Path(tempfile.mkdtemp()) / "repos.json"
        repos_file.write_text(json.dumps(self.repos))
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_files", "arguments": ["x"]}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
        lines = "".join(json.dumps(m) + "\n" for m in messages) + "not json\n"
        result = subprocess.run([sys.executable, "-I", repo_tools.__file__, str(repos_file)], capture_output=True,
                                text=True, input=lines, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        # A bad call or line gets an error reply and the server keeps answering.
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, None])
        self.assertEqual(replies[1]["error"]["code"], -32602)
        tools = replies[2]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["list_files", "read_file", "search"])
        self.assertIn("owner/lib", tools[0]["description"])

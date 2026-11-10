import json
import re
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from karen.assess import Assessment, AssessmentError, Finding, Walkthrough
from karen.github import GitHubError
from karen.reviewer import Reviewer
from karen.snapshot import Snapshot

REPO = "owner/repo"
HEAD = "a" * 40
NEWER_HEAD = "b" * 40
BOT = "reviewer[bot]"
CLEAN = Assessment(walkthrough=[], effort=3, diagram=None, summary="Looks correct.", verdict="approve", findings=[])
NIT = Finding(severity="minor", path="app.py", line=3, title="Name", detail="Could be clearer.", fix="Rename it.", start_line=None, suggestion=None)
UNSET = object()
BUG = Finding(severity="blocking", path="app.py", line=5, title="Crash", detail="Divides by zero on empty input.",
              fix="Raise ValueError when xs is empty.", start_line=None, suggestion=None)
ELSEWHERE = Finding(severity="important", path="other.py", line=10, title="Stale cache", detail="Never invalidated.",
                    fix="Clear the cache after writes.", start_line=None, suggestion=None)
DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -4,2 +4,3 @@ def average(xs):
     total = sum(xs)
-    return total / len(xs)
+    count = len(xs)
+    return total / count
"""


def command(body: str, *, comment_id: int = 101, association: str = "OWNER",
            user_type: str = "User", created: str = "2026-11-07T12:00:00Z", pr: int = 7) -> dict:
    return {
        "id": comment_id, "body": body, "author_association": association,
        "user": {"login": "maintainer", "type": user_type},
        "html_url": f"https://github.com/{REPO}/pull/{pr}#issuecomment-{comment_id}",
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{pr}",
        "created_at": created, "updated_at": created,
    }


class FakeGitHub:
    token = "token"

    def __init__(self, comments: list[dict], *, author_association: str = "OWNER") -> None:
        self.comments = comments
        self.pr = {
            "number": 7, "state": "open", "title": "Change", "body": "", "author_association": author_association, "user": {"login": "author"},
            "head": {"sha": HEAD, "ref": "feature"},
            "base": {"sha": "c" * 40, "ref": "main", "repo": {"full_name": REPO, "clone_url": "unused"}},
        }
        self.later_reads: list[dict] = []  # PR fields that change on the second and later reads
        self.reviews: list[dict] = []
        self.posts: list[tuple[str, dict]] = []
        self.review_errors: list[Exception] = []
        self.create_despite_error = False
        self.reply: object = UNSET  # when set, the review is created but GitHub replies with this
        self.list_errors = 0  # how many review listings fail before one works
        self.comment_error: Exception | None = None
        self.reads = 0
        self.permissions: dict[str, str] = {}  # login -> repository permission, for org members

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        if method == "GET" and path.startswith(f"/repos/{REPO}/collaborators/") and path.endswith("/permission"):
            return {"permission": self.permissions.get(path.split("/")[-2], "read")}
        if method == "GET" and path == f"/repos/{REPO}/pulls/7":
            self.reads += 1
            return self.pr | (self.later_reads[0] if self.reads > 1 and self.later_reads else {})
        self.posts.append((path, body))
        if path.endswith("/reviews"):
            review = {"id": len(self.reviews) + 1, "user": {"login": BOT}, "commit_id": body["commit_id"],
                      "state": {"APPROVE": "APPROVED", "COMMENT": "COMMENTED"}[body["event"]], "body": body["body"],
                      "comments": body.get("comments", [])}
            if self.review_errors:
                if self.create_despite_error:
                    self.reviews.append(review)
                raise self.review_errors.pop(0)
            self.reviews.append(review)
            return review if self.reply is UNSET else self.reply
        if path.endswith("/issues/7/comments") and self.comment_error:
            raise self.comment_error
        return {}

    def get_all(self, path: str) -> list[dict]:
        if "/issues/comments" in path:
            return self.comments
        if self.list_errors:
            self.list_errors -= 1
            raise GitHubError(503, "Service unavailable")
        return self.reviews

    def reactions(self) -> list[str]:
        return [body["content"] for path, body in self.posts if path.endswith("/reactions")]

    def failure_notes(self) -> list[str]:
        return [body["body"] for path, body in self.posts if path.endswith("/issues/7/comments")]


class FakeAuth:
    login = BOT

    def __init__(self, github: FakeGitHub) -> None:
        self.github = github

    def client(self, repo: str) -> FakeGitHub:
        return self.github


class ReviewerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = Path(tempfile.mkdtemp())
        (self.state_dir / "state.json").write_text(json.dumps({
            "repos": {REPO: {"cutoff": "2026-11-07T00:00:00Z", "since": "2026-11-07T00:00:00Z"}}, "comments": {},
        }))
        self.assessed = 0

    def run_reviewer(self, github: FakeGitHub, assessment: Assessment | BaseException = CLEAN,
                     snapshot: Snapshot | None = None, settle_window: timedelta = timedelta(minutes=10)) -> None:
        def fake_assess(pr: dict, snap: Snapshot) -> Assessment:
            self.assessed += 1
            if isinstance(assessment, BaseException):
                raise assessment
            return assessment

        reviewer = Reviewer(
            FakeAuth(github), self.state_dir, assess=fake_assess, reconcile_delay=0, settle_window=settle_window,
            take_snapshot=lambda *args: snapshot or Snapshot(head=HEAD, diff="diff", files={}, gaps=[]),
        )
        reviewer.run_once([REPO])

    def record(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text())["comments"]["101"]

    def test_withheld_review_does_not_hide_older_open_thread_or_persist_secret(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github, replace(CLEAN, findings=[BUG]), Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        finding_id = self.record()["findings"][0]["id"]
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [{"id": "thread", "isResolved": False, "comments": {"nodes": [
                {"author": {"__typename": "Bot", "login": "reviewer"}, "body": f"<!-- karen finding={finding_id} -->\n\nBug"}]}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        secret = "ghp_" + "x" * 35
        github.comments = [command("/karen review", comment_id=102)]
        self.run_reviewer(github, replace(CLEAN, findings=[replace(ELSEWHERE, detail=secret)]), Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertNotIn(secret, (self.state_dir / "state.json").read_text())
        github.comments = [command("/karen review approve", comment_id=103)]
        self.run_reviewer(github, CLEAN, Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertEqual(github.reviews[-1]["state"], "COMMENTED")
        self.assertIn("Previous", github.reviews[-1]["body"])

    def test_uncertain_post_does_not_defer_later_command_or_rewind_cursor(self) -> None:
        github = FakeGitHub([command("/karen review"), command("/karen review approve", comment_id=102)])
        github.review_errors = [GitHubError(503, "unavailable")]
        self.run_reviewer(github)
        state = json.loads((self.state_dir / "state.json").read_text())
        self.assertEqual(state["comments"]["101"]["status"], "posting")
        self.assertEqual(state["comments"]["102"]["status"], "done")
        self.assertEqual(github.reviews[0]["state"], "COMMENTED")
        self.assertIn("An earlier review on this PR is still being confirmed", github.reviews[0]["body"])
        self.assertGreaterEqual(state["repos"][REPO]["since"], "2026-11-07T00:00:00Z")
        self.run_reviewer(github)
        self.assertEqual(len(github.reviews), 1)

    def test_unconfirmed_blocking_review_is_not_hidden_by_later_comment_after_settlement(self) -> None:
        github = FakeGitHub([command("/karen review"), command("/karen review approve", comment_id=102)])
        github.review_errors = [GitHubError(503, "unavailable")]
        github.create_despite_error = True
        github.list_errors = 1
        assessments = [replace(CLEAN, findings=[ELSEWHERE]), CLEAN, CLEAN]
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        reviewer = Reviewer(FakeAuth(github), self.state_dir, assess=lambda pr, snap: assessments.pop(0),
                            reconcile_delay=0, take_snapshot=lambda *args: Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        reviewer.run_once([REPO])
        self.assertEqual(reviewer.state["comments"]["101"]["status"], "posting")
        self.assertEqual(github.reviews[1]["state"], "COMMENTED")
        github.comments.append(command("/karen review approve", comment_id=103))
        reviewer.run_once([REPO])
        self.assertEqual(reviewer.state["comments"]["101"]["status"], "done")
        self.assertEqual(github.reviews[2]["state"], "COMMENTED")
        self.assertIn("Previous blocking or important findings remain unresolved", github.reviews[2]["body"])
        finding_id = reviewer.state["comments"]["101"]["findings"][0]["id"]
        assessments.append(replace(CLEAN, resolved=[finding_id]))
        github.comments.append(command("/karen review approve", comment_id=104))
        reviewer.run_once([REPO])
        self.assertEqual(github.reviews[3]["state"], "APPROVED")

    def test_clean_unconfirmed_review_allows_approval_after_settlement(self) -> None:
        github = FakeGitHub([command("/karen review"), command("/karen review approve", comment_id=102)])
        github.review_errors = [GitHubError(503, "unavailable")]
        github.create_despite_error = True
        github.list_errors = 1
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        self.run_reviewer(github)
        self.assertEqual(github.reviews[1]["state"], "COMMENTED")
        github.comments.append(command("/karen review approve", comment_id=103))
        self.run_reviewer(github)
        self.assertEqual(self.record()["status"], "done")
        self.assertEqual(github.reviews[2]["state"], "APPROVED")

    def test_started_record_blocks_only_its_own_pr(self) -> None:
        github = FakeGitHub([])
        reviewer = Reviewer(FakeAuth(github), self.state_dir, assess=lambda pr, snap: CLEAN,
                            take_snapshot=lambda *args: Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        reviewer.state["comments"]["99"] = {"repo": REPO, "pr": 8, "status": "posting"}
        self.assertEqual(reviewer.prepare(github, REPO, 7, True, "maintainer").event, "APPROVE")
        reviewer.state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "started"}
        self.assertEqual(reviewer.prepare(github, REPO, 7, True, "maintainer").event, "COMMENT")

    def test_old_withheld_or_unconfirmed_review_does_not_block_clean_approval(self) -> None:
        github = FakeGitHub([])
        reviewer = Reviewer(FakeAuth(github), self.state_dir, assess=lambda pr, snap: CLEAN,
                            take_snapshot=lambda *args: Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        reviewer.state["comments"]["99"] = {"repo": REPO, "pr": 7, "status": "done", "withheld_findings": True}
        reviewer.state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "failed",
            "error": "couldn't confirm the review was posted", "unsafe_findings": True}
        self.assertEqual(reviewer.prepare(github, REPO, 7, True, "maintainer").event, "APPROVE")

    def test_incremental_inline_comments_and_suggestions_use_full_pr_hunks(self) -> None:
        github = FakeGitHub([])
        incremental = "+++ b/app.py\n@@ -1,2 +1,2 @@\n-old\n+new\n context\n"
        full = "+++ b/app.py\n@@ -2 +2 @@\n-old\n+new\n"
        findings = [replace(BUG, line=2, start_line=1, suggestion="crosses PR hunk"),
                    replace(ELSEWHERE, path="app.py", line=1)]
        reviewer = Reviewer(FakeAuth(github), self.state_dir,
                            assess=lambda pr, snap: replace(CLEAN, findings=findings),
                            take_snapshot=lambda *args: Snapshot(HEAD, incremental, {}, [], previous=NEWER_HEAD, full_diff=full))
        draft = reviewer.prepare(github, REPO, 7, False, "maintainer")
        self.assertEqual([comment["line"] for comment in draft.comments()], [2])
        self.assertNotIn("suggestion\n", draft.comments()[0]["body"])
        self.assertIn(ELSEWHERE.title, draft.body())
        reviewer.take_snapshot = lambda *args: Snapshot(HEAD, incremental, {}, [], previous=NEWER_HEAD)
        self.assertEqual(reviewer.prepare(github, REPO, 7, False, "maintainer").comments(), [])
        reviewer.assess = lambda pr, snap: replace(CLEAN, findings=[replace(BUG, line=2, suggestion="replacement")])
        reviewer.take_snapshot = lambda *args: Snapshot(HEAD, "", {}, [], previous=NEWER_HEAD, full_diff=full)
        self.assertIn("```suggestion\nreplacement", reviewer.prepare(github, REPO, 7, False, "maintainer").comments()[0]["body"])

    def test_incremental_review_supplies_open_findings_and_resolves_only_bot_thread(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding_id = "0123456789ab"
        state = json.loads((self.state_dir / "state.json").read_text())
        state["comments"]["99"] = {"repo": REPO, "pr": 7, "status": "done", "head": HEAD,
                                   "at": "2026-11-07T12:00:00Z", "findings": []}
        state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD,
                                    "at": "2026-11-07T11:00:00Z", "findings": [
                                        {"id": finding_id, "inline": True, "title": "Crash", "detail": "bad", "path": "app.py"}]}
        (self.state_dir / "state.json").write_text(json.dumps(state))
        resolved = []
        def graphql(query: str, variables: dict) -> dict:
            if "resolveReviewThread" in query:
                resolved.append(variables["threadId"])
                return {"resolveReviewThread": {"thread": {"id": variables["threadId"], "isResolved": True}}}
            return {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                {"id": "bot-thread", "isResolved": False, "comments": {"nodes": [
                    {"author": {"__typename": "Bot", "login": "reviewer"}, "body": f"<!-- karen finding={finding_id} -->\n\nBug"}]}},
                # GraphQL drops the "[bot]" suffix, so a human with the bare name must not count as the bot.
                {"id": "human-thread", "isResolved": False, "comments": {"nodes": [
                    {"author": {"__typename": "User", "login": "reviewer"}, "body": f"<!-- karen finding={finding_id} -->\n\nBug"}]}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        github.graphql = graphql
        seen = []
        def snapshot(*args: str) -> Snapshot:
            self.assertEqual(args[-1], NEWER_HEAD)
            return Snapshot(HEAD, DIFF, {}, [], previous=NEWER_HEAD, full_diff=DIFF)
        def assessment(pr: dict, snap: Snapshot) -> Assessment:
            seen.extend(snap.prior_findings)
            return replace(CLEAN, resolved=[finding_id])
        Reviewer(FakeAuth(github), self.state_dir, assess=assessment, take_snapshot=snapshot).run_once([REPO])
        self.assertEqual([finding["id"] for finding in seen], [finding_id])
        self.assertEqual(resolved, ["bot-thread"])
        self.assertIn("Reviewed the changes since bbbbbbb.", github.reviews[0]["body"])
        self.assertIn("**Fixed since the last review**\n- Crash", github.reviews[0]["body"])

    def test_full_approval_is_blocked_by_existing_open_important_finding(self) -> None:
        github = FakeGitHub([])
        reviewer = Reviewer(FakeAuth(github), self.state_dir, assess=lambda pr, snap: CLEAN,
                            take_snapshot=lambda *args: Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        reviewer.state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD,
                                              "findings": [{"id": "0123456789ab", "inline": False,
                                                            "severity": "important", "title": "Bug", "path": "app.py",
                                                            "line": 5, "detail": "bad", "fix": "Fix"}]}
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        draft = reviewer.prepare(github, REPO, 7, True, "maintainer")
        self.assertEqual(draft.event, "COMMENT")
        self.assertIn("previous", draft.body().lower())

    def test_escalated_duplicate_blocks_approval_and_persists(self) -> None:
        from dataclasses import asdict
        from karen.reviewer import _finding_id
        github = FakeGitHub([])
        minor = replace(BUG, severity="minor")
        reviewer = Reviewer(FakeAuth(github), self.state_dir, assess=lambda pr, snap: replace(CLEAN, findings=[BUG]),
                            take_snapshot=lambda *args: Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        reviewer.state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD,
            "findings": [asdict(minor) | {"id": _finding_id(minor), "inline": False}]}
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        draft = reviewer.prepare(github, REPO, 7, True, "maintainer")
        self.assertEqual(draft.event, "COMMENT")
        self.assertEqual([finding["severity"] for finding in draft.findings_to_persist()], ["blocking"])

    def test_changed_pr_during_assessment_keeps_body_finding_unresolved(self) -> None:
        for changed in ("head", "base"):
            with self.subTest(changed=changed):
                self.setUp()
                github = FakeGitHub([command("/karen review")])
                state = json.loads((self.state_dir / "state.json").read_text())
                finding_id = "0123456789ab"
                state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD,
                    "findings": [{"id": finding_id, "inline": False, "severity": "important", "title": "Old bug",
                                  "detail": "still broken", "path": "app.py", "line": 5, "fix": "Fix"}]}
                (self.state_dir / "state.json").write_text(json.dumps(state))
                github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
                    "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
                if changed == "head":
                    github.later_reads = [{"head": {"sha": NEWER_HEAD, "ref": "feature"}}]
                else:
                    github.later_reads = [{"base": github.pr["base"] | {"ref": "other"}}]
                self.run_reviewer(github, replace(CLEAN, resolved=[finding_id]), Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
                self.assertEqual(self.record()["findings"][0]["id"], finding_id)
                self.assertNotIn("**Fixed", github.reviews[0]["body"])
                self.assertNotIn("Old bug", github.reviews[0]["body"])

    def test_body_finding_survives_incremental_review_until_fixed(self) -> None:
        github = FakeGitHub([])
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        assessments = [replace(CLEAN, findings=[ELSEWHERE]), CLEAN, CLEAN]
        seen = []
        def assess(pr, snapshot):
            seen.append(snapshot.prior_findings)
            return assessments.pop(0)
        def snapshot(*args):
            previous = args[5] if args[5] != github.pr["head"]["sha"] else ""
            return Snapshot(github.pr["head"]["sha"], DIFF, {}, [], previous=previous, full_diff=DIFF)
        reviewer = Reviewer(FakeAuth(github), self.state_dir, assess=assess, take_snapshot=snapshot)
        reviewer.handle(github, REPO, command("/karen review"), False, False)
        finding = reviewer.state["comments"]["101"]["findings"][0]
        self.assertIn(ELSEWHERE.title, github.reviews[0]["body"])
        github.pr["head"]["sha"] = NEWER_HEAD
        reviewer.handle(github, REPO, command("/karen review", comment_id=102), False, False)
        self.assertIn("Reviewed the changes since aaaaaaa", github.reviews[1]["body"])
        self.assertNotIn(ELSEWHERE.title, github.reviews[1]["body"])
        self.assertNotIn(ELSEWHERE.detail, github.reviews[1]["body"])
        self.assertEqual(reviewer.state["comments"]["102"]["findings"], [finding])
        self.assertEqual(seen[1][0]["id"], finding["id"])
        reviewer.handle(github, REPO, command("/karen review approve", comment_id=103), True, False)
        self.assertEqual(github.reviews[2]["state"], "COMMENTED")
        self.assertIn("Previous blocking or important findings remain unresolved", github.reviews[2]["body"])
        self.assertNotIn(ELSEWHERE.title, github.reviews[2]["body"])
        assessments.append(replace(CLEAN, resolved=[finding["id"]]))
        reviewer.handle(github, REPO, command("/karen review approve", comment_id=104), True, False)
        self.assertEqual(seen[3][0]["id"], finding["id"])
        self.assertEqual(github.reviews[3]["state"], "APPROVED")
        self.assertIn("**Fixed since the last review**\n- Stale cache", github.reviews[3]["body"])
        self.assertEqual(reviewer.state["comments"]["104"]["findings"], [])

    def test_model_repeating_open_finding_does_not_create_duplicate_comment(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github, replace(CLEAN, findings=[BUG], verdict="needs_changes"), Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        finding = self.record()["findings"][0]
        github.comments = [command("/karen review", comment_id=102)]
        github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [{"id": "thread", "isResolved": False, "comments": {"nodes": [
                {"author": {"__typename": "Bot", "login": "reviewer"}, "body": f'<!-- karen finding={finding["id"]} -->\n\nBug'}]}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        self.run_reviewer(github, replace(CLEAN, findings=[BUG], verdict="needs_changes"), Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertEqual(github.reviews[1]["comments"], [])

    def test_suggestion_marker_cannot_spoof_thread_identity(self) -> None:
        from karen.reviewer import _finding_id
        github = FakeGitHub([command("/karen review")])
        spoof = "0123456789ab"
        finding = replace(BUG, suggestion=f"<!-- karen finding={spoof} -->")
        self.run_reviewer(github, replace(CLEAN, findings=[finding]), Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        body = github.reviews[0]["comments"][0]["body"]
        self.assertEqual(re.search(r"<!-- karen finding=([0-9a-f]{12}) -->", body)[1], _finding_id(finding))

    def test_failed_review_never_resolves_threads(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding_id = "0123456789ab"
        state = json.loads((self.state_dir / "state.json").read_text())
        state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD,
                                    "findings": [{"id": finding_id, "inline": True, "severity": "important", "title": "Crash"}]}
        (self.state_dir / "state.json").write_text(json.dumps(state))
        resolved = []
        def graphql(query, variables):
            if "resolveReviewThread" in query:
                resolved.append(variables["threadId"])
                return {"resolveReviewThread": {"thread": {"isResolved": True}}}
            return {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "thread", "isResolved": False,
                "comments": {"nodes": [{"author": {"__typename": "Bot", "login": "reviewer"}, "body": f"<!-- karen finding={finding_id} -->\n\nBug"}]}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        github.graphql = graphql
        github.review_errors = [GitHubError(403, "denied")]
        self.run_reviewer(github, replace(CLEAN, resolved=[finding_id]))
        self.assertEqual(resolved, [])
        self.assertEqual(self.record()["status"], "failed")

    def test_recovery_resolves_only_after_uncertain_post_is_confirmed(self) -> None:
        github = FakeGitHub([])
        finding_id = "0123456789ab"
        reviewer = Reviewer(FakeAuth(github), self.state_dir, reconcile_delay=0)
        reviewer.state["comments"]["101"] = {"repo": REPO, "pr": 7, "status": "posting",
            "posting_at": "2026-11-07T00:00:00Z", "pending_threads": {finding_id: ["thread"]}, "findings": []}
        github.reviews.append({"id": 1, "user": {"login": BOT}, "commit_id": HEAD, "state": "COMMENTED",
                               "body": "<!-- karen comment=101 -->"})
        calls = []
        def graphql(query, variables):
            calls.append(variables["threadId"])
            return {"resolveReviewThread": {"thread": {"isResolved": True}}}
        github.graphql = graphql
        reviewer._resolve_unfinished(github, REPO)
        self.assertEqual(calls, ["thread"])
        self.assertEqual(reviewer.state["comments"]["101"]["status"], "done")
        self.assertEqual(len(github.reviews), 1)

    def test_resolution_failure_logs_after_done_and_never_updates_review_or_retries(self) -> None:
        github = FakeGitHub([])
        reviewer = Reviewer(FakeAuth(github), self.state_dir, reconcile_delay=0)
        reviewer.state["comments"]["101"] = {"repo": REPO, "pr": 7, "status": "posting",
            "posting_at": "2026-11-07T00:00:00Z", "pending_threads": {"0123456789ab": ["thread", "other"]}, "findings": []}
        github.reviews.append({"id": 1, "user": {"login": BOT}, "commit_id": HEAD, "state": "COMMENTED",
                               "body": "<!-- karen comment=101 -->"})
        calls = []
        def graphql(query, variables):
            self.assertEqual(self.record()["status"], "done")
            self.assertNotIn("pending_threads", self.record())
            calls.append(variables["threadId"])
            raise GitHubError(403, "denied")
        github.graphql = graphql
        with self.assertLogs("karen.reviewer", level="ERROR") as logs:
            reviewer.run_once([REPO])
        self.assertTrue(any("Couldn't resolve review thread" in message for message in logs.output))
        self.assertEqual(calls, ["thread", "other"])
        self.assertEqual(self.record()["status"], "done")
        self.assertNotIn("Could not resolve threads", github.reviews[0]["body"])
        self.assertEqual(github.posts, [(f"/repos/{REPO}/issues/comments/101/reactions", {"content": "+1"})])
        reviewer.run_once([REPO])
        self.assertEqual(calls, ["thread", "other"])

    def test_thread_pagination_and_failed_resolution_are_logged(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding_id = "0123456789ab"
        state = json.loads((self.state_dir / "state.json").read_text())
        state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD,
                                    "findings": [{"id": finding_id, "inline": True, "severity": "important", "title": "Crash"}]}
        (self.state_dir / "state.json").write_text(json.dumps(state))
        cursors = []
        def graphql(query: str, variables: dict) -> dict:
            if "resolveReviewThread" in query:
                raise GitHubError(403, "denied")
            cursors.append(variables["cursor"])
            nodes = [] if variables["cursor"] is None else [{"id": "thread", "isResolved": False,
                "comments": {"nodes": [{"author": {"__typename": "Bot", "login": "reviewer"},
                                         "body": f"<!-- karen finding={finding_id} -->\n\nBug"}]}}]
            return {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes,
                "pageInfo": {"hasNextPage": variables["cursor"] is None, "endCursor": "next"}}}}}
        github.graphql = graphql
        reviewer = Reviewer(FakeAuth(github), self.state_dir,
                            assess=lambda pr, snap: replace(CLEAN, resolved=[finding_id]),
                            take_snapshot=lambda *args: Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF, previous=NEWER_HEAD))
        with self.assertLogs("karen.reviewer", level="ERROR"):
            reviewer.run_once([REPO])
        self.assertEqual(cursors, [None, "next"])
        self.assertEqual(self.record()["status"], "done")
        self.assertNotIn("Could not resolve threads", github.reviews[0]["body"])
        self.assertIn("**Fixed since the last review**\n- Crash", github.reviews[0]["body"])

    def test_full_and_approval_commands_do_not_request_incremental_snapshot(self) -> None:
        for text in ("/karen full review", "/karen review approve"):
            with self.subTest(text=text):
                self.setUp()
                github = FakeGitHub([command(text)])
                state = json.loads((self.state_dir / "state.json").read_text())
                state["comments"]["100"] = {"repo": REPO, "pr": 7, "status": "done", "head": NEWER_HEAD, "findings": []}
                (self.state_dir / "state.json").write_text(json.dumps(state))
                github.graphql = lambda query, variables: {"repository": {"pullRequest": {"reviewThreads": {
                    "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
                args_seen = []
                def snapshot(*args: str) -> Snapshot:
                    args_seen.append(args)
                    return Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF)
                Reviewer(FakeAuth(github), self.state_dir, assess=lambda pr, snap: CLEAN,
                         take_snapshot=snapshot).run_once([REPO])
                self.assertEqual(len(args_seen[0]), 6)
                self.assertEqual(args_seen[0][-1], "")
                self.assertEqual(github.reviews[0]["state"], "APPROVED" if "approve" in text else "COMMENTED")

    def test_walkthrough_table_and_optional_diagram(self) -> None:
        for diagram in (None, "sequenceDiagram\nA->>B: ```` @someone <!--"):
            with self.subTest(diagram=diagram):
                self.setUp()
                github = FakeGitHub([command("/karen review")])
                assessment = Assessment(summary="Summary.", verdict="approve", findings=[], effort=4,
                                        walkthrough=[Walkthrough("a|b\n@someone", "changed | stuff\r\n<!--")],
                                        diagram=diagram)
                self.run_reviewer(github, assessment)
                body = github.reviews[0]["body"]
                self.assertIn("Summary.\n\n**Review effort:** 4/5", body)
                self.assertIn("<summary>📋 Walkthrough</summary>", body)
                self.assertIn("| a\\|b @​someone | changed \\| stuff &lt;!-- |", body)
                if diagram is None:
                    self.assertNotIn("mermaid", body)
                else:
                    self.assertIn("`````mermaid\n" + diagram + "\n`````", body)

    def test_fitting_large_walkthrough_and_diagram_are_complete(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        change = "x" * 12001
        diagram = "sequenceDiagram\n" + "y" * 12001
        assessment = Assessment("Summary", "needs_changes", [ELSEWHERE],
                                [Walkthrough("app.py", change)], 3, diagram)
        self.run_reviewer(github, assessment)
        body = github.reviews[0]["body"]
        self.assertIn(f"| app.py | {change} |", body)
        self.assertIn(f"```mermaid\n{diagram}\n```", body)
        self.assertIn("Stale cache", body)
        self.assertIn("fix all findings", body)
        self.assertLessEqual(len(body), 60000)

    def test_large_walkthrough_preserves_mixed_findings_and_notes(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        nit = Finding("minor", "elsewhere.py", None, "Small issue", "Nit detail", "Fix nit", start_line=None, suggestion=None)
        assessment = Assessment("Summary", "needs_changes", [BUG, ELSEWHERE, nit],
                                [Walkthrough("app.py", "x" * 61000)], 3,
                                "sequenceDiagram\nA->>B: ````")
        self.run_reviewer(github, assessment, Snapshot(HEAD, DIFF, {}, ["A review limit"], full_diff=DIFF))
        review = github.reviews[0]
        body = review["body"]
        self.assertEqual(review["state"], "COMMENTED")
        for text in ("Stale cache", "Never invalidated", "Small issue", "Why this wasn't approved",
                     "A review limit", "fix all findings", "Crash", "</details>"):
            self.assertIn(text, body)
        self.assertLess(len(body), 60000)
        self.assertIn("omitted", body)

    def test_partial_walkthrough_reports_omitted_rows_and_keeps_diagram(self) -> None:
        github = FakeGitHub([command("/karen review")])
        assessment = Assessment("Summary", "needs_changes", [ELSEWHERE],
                                [Walkthrough("first.py", "Changed"), Walkthrough("huge.py", "x" * 61000),
                                 Walkthrough("last.py", "Changed")], 3, "sequenceDiagram\nA->>B: Go")
        self.run_reviewer(github, assessment)
        body = github.reviews[0]["body"]
        self.assertIn("| first.py | Changed |", body)
        self.assertIn("omitted rows: 2", body)
        self.assertIn("```mermaid\nsequenceDiagram\nA->>B: Go\n```", body)
        self.assertIn("Stale cache", body)
        self.assertIn("fix all findings", body)

    def test_marker_is_included_in_body_budget(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github, Assessment("Summary", "approve", [], [Walkthrough("a.py", "x")], 3, None))
        padding = 60000 - len(github.reviews[0]["body"])
        self.setUp()
        github = FakeGitHub([command("/karen review")])
        change = "x" * (padding + 1)
        self.run_reviewer(github, Assessment("Summary", "approve", [], [Walkthrough("a.py", change)], 3, None))
        self.assertEqual(len(github.reviews[0]["body"]), 60000)
        self.assertIn(change, github.reviews[0]["body"])
        self.setUp()
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github, Assessment("Summary", "approve", [], [Walkthrough("a.py", change + "x")], 3, None))
        self.assertIn("omitted rows: 1", github.reviews[0]["body"])
        self.assertLessEqual(len(github.reviews[0]["body"]), 60000)

    def test_large_diagram_is_omitted_without_breaking_findings(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        assessment = Assessment("Summary", "needs_changes", [ELSEWHERE, NIT],
                                [Walkthrough("app.py", "Changed")], 3, "`" * 61000)
        self.run_reviewer(github, assessment)
        body = github.reviews[0]["body"]
        self.assertIn("Diagram omitted", body)
        self.assertIn("Stale cache", body)
        self.assertIn("fix all findings", body)
        self.assertEqual(body.count("<details>"), body.count("</details>"))

    def test_oversized_findings_fail_closed_before_review_post(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        finding = Finding("blocking", "other.py", None, "Critical", "x" * 61000, "Fix it", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment("Summary", "needs_changes", [finding], [], 3, None))
        self.assertEqual(github.reviews, [])
        self.assertEqual(self.record()["status"], "failed")
        self.assertIn("too large", github.failure_notes()[0])

    def test_oversized_inline_finding_fails_closed(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding = Finding("blocking", "app.py", 5, "Crash", "x" * 30000, "Fix it", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment("Summary", "needs_changes", [finding], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertEqual(github.reviews, [])
        self.assertEqual(self.record()["status"], "failed")

    def test_oversized_fallback_does_not_post_partial_review(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [GitHubError(422, "Line could not be resolved")]
        finding = Finding("blocking", "app.py", 5, "Crash", "x" * 29000, "Fix it", start_line=None, suggestion=None)
        other = Finding("important", "other.py", None, "Other", "y" * 4000, "Fix it", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment("Summary", "needs_changes", [finding, other], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertEqual(github.reviews, [])
        self.assertEqual(len([path for path, body in github.posts if path.endswith("/reviews")]), 1)
        self.assertEqual(self.record()["status"], "failed")

    def test_credential_in_omitted_diagram_is_not_posted(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        diagram = "x" * 61000 + " ghp_" + "x" * 36
        self.run_reviewer(github, Assessment("Summary", "approve", [], [], 3, diagram))
        self.assertEqual(github.reviews[0]["state"], "APPROVED")
        self.assertIn("Diagram omitted", github.reviews[0]["body"])
        self.assertNotIn("ghp_", github.reviews[0]["body"])

    def test_approval_lists_ignored_files_and_invalid_config_note(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        snapshot = Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF, ignored=["generated/@someone<!--.py"],
                            config_error="unknown key @someone<!--")
        self.run_reviewer(github, snapshot=snapshot)
        review = github.reviews[0]
        self.assertEqual(review["state"], "APPROVED")
        self.assertIn("**Not reviewed (ignored by .karen.toml)**", review["body"])
        self.assertIn("generated/@​someone&lt;!--.py", review["body"])
        self.assertIn("invalid; defaults were used: unknown key @​someone&lt;!--", review["body"])

    def test_full_review_command_is_accepted_without_approval(self) -> None:
        github = FakeGitHub([command("/karen full review")])
        self.run_reviewer(github)
        self.assertEqual(github.reviews[0]["state"], "COMMENTED")

    def test_review_command_never_approves(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github)
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertEqual(github.reviews[0]["commit_id"], HEAD)
        self.assertEqual(github.reactions(), ["eyes", "+1"])

    def test_fallback_records_findings_as_body_only(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [GitHubError(422, "invalid line")]
        self.run_reviewer(github, replace(CLEAN, verdict="needs_changes", findings=[BUG]),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertFalse(self.record()["findings"][0]["inline"])

    def test_finished_review_records_findings_and_inline_marker(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github, replace(CLEAN, verdict="needs_changes", findings=[BUG]),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        finding = self.record()["findings"][0]
        self.assertEqual(finding["title"], BUG.title)
        self.assertRegex(finding["id"], r"^[0-9a-f]{12}$")
        self.assertIn(f'<!-- karen finding={finding["id"]} -->',
                      github.reviews[0]["comments"][0]["body"])

    def test_approve_command_approves_clean_change(self) -> None:
        github = FakeGitHub([command("  /karen review approve\n")])
        nit = Finding("minor", "app.py", 6, "Name", "Could be clearer.", "Rename it.", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="Fine.", verdict="approve", findings=[nit]),
                          Snapshot(head=HEAD, diff=DIFF, files={}, gaps=[], full_diff=DIFF))
        self.assertEqual([r["state"] for r in github.reviews], ["APPROVED"])
        self.assertIn("✅ Karen approved this change", github.reviews[0]["body"])
        self.assertIn("<summary>🧹 Nitpicks (1)</summary>", github.reviews[0]["body"])
        self.assertEqual(github.reviews[0]["comments"], [])

    def test_codex_browses_the_pr_head_and_loadable_context_repos(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        seen = {}

        def fetch(mirror: Path, remote: str, token: str) -> str:
            if "gone" in remote:
                raise subprocess.CalledProcessError(128, "git")
            seen["fetch"] = (mirror.name, remote, token)
            return "d" * 40

        def fake_assess(pr: dict, snap: Snapshot) -> Assessment:
            seen["repos"] = snap.repos
            return CLEAN

        Reviewer(FakeAuth(github), self.state_dir, assess=fake_assess, reconcile_delay=0, fetch_default_branch=fetch,
                 context_repos=(REPO, "owner/lib", "owner/gone"),
                 take_snapshot=lambda *args: Snapshot(head=HEAD, diff="diff", files={}, gaps=[])).run_once([REPO])
        self.assertEqual(seen["fetch"], ("owner__lib", "https://github.com/owner/lib.git", "token"))
        self.assertEqual(seen["repos"], {
            REPO: {"git_dir": str(self.state_dir / "git" / "owner__repo"), "commit": HEAD},
            "owner/lib": {"git_dir": str(self.state_dir / "git" / "owner__lib"), "commit": "d" * 40}})
        # Missing context is worth saying but doesn't block approving what the agent did see.
        self.assertEqual([r["state"] for r in github.reviews], ["APPROVED"])
        self.assertIn("Couldn't load owner/gone for cross-repository context.", github.reviews[0]["body"])

    def test_approve_command_with_findings_only_comments(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="Bug.", verdict="approve", findings=[BUG]))
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertIn("blocking or important findings", github.reviews[0]["body"])

    def test_unsure_verdict_only_comments(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="Unclear intent.", verdict="unsure", findings=[]))
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertIn("couldn't reach a confident decision", github.reviews[0]["body"])

    def test_incomplete_snapshot_blocks_approval(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        gaps = ['Binary files changed, which the model can\'t read: "logo.png"']
        self.run_reviewer(github, snapshot=Snapshot(head=HEAD, diff="diff", files={}, gaps=gaps))
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertIn("logo.png", github.reviews[0]["body"])

    def test_org_members_need_write_access(self) -> None:
        github = FakeGitHub([command("/karen review approve", association="CONTRIBUTOR")], author_association="MEMBER")
        github.permissions = {"maintainer": "write", "author": "admin"}
        self.run_reviewer(github)
        self.assertEqual([r["state"] for r in github.reviews], ["APPROVED"])
        reader = FakeGitHub([command("/karen review", association="MEMBER")])
        self.run_reviewer(reader)
        self.assertEqual(reader.reviews, [])

    def test_untrusted_pr_author_blocks_approval(self) -> None:
        github = FakeGitHub([command("/karen review approve")], author_association="CONTRIBUTOR")
        self.run_reviewer(github)
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertIn("doesn't have write access", github.reviews[0]["body"])

    def test_head_moving_during_review_blocks_approval(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        github.later_reads = [{"head": {"sha": NEWER_HEAD}}]
        self.run_reviewer(github)
        self.assertEqual([(r["state"], r["commit_id"]) for r in github.reviews], [("COMMENTED", HEAD)])
        self.assertIn("covers aaaaaaa only", github.reviews[0]["body"])

    def test_retargeted_pr_blocks_approval(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        github.later_reads = [{"base": github.pr["base"] | {"ref": "release"}}]
        self.run_reviewer(github)
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertIn("retargeted to release", github.reviews[0]["body"])

    def test_declined_approval_is_reposted_as_an_honest_comment(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        github.review_errors = [GitHubError(422, "Can not approve your own pull request")]
        self.run_reviewer(github)
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        body = github.reviews[0]["body"]
        self.assertTrue(body.startswith("### Karen review: not approved"))
        self.assertIn("GitHub declined the approval: Can not approve your own pull request", body)

    def test_ignored_comments_never_reach_the_model(self) -> None:
        github = FakeGitHub([
            command("/karen review", association="CONTRIBUTOR", comment_id=1),
            command("/karen review", user_type="Bot", comment_id=2),
            command("/karen review please", comment_id=3),
            command("/Karen Review", comment_id=4),
            command("/codex review", comment_id=8),
            command("/karen review", created="2026-11-06T23:59:59Z", comment_id=5),
            command("/karen review", created="2026-11-07T00:00:00Z", comment_id=6),
            command("/karen review", comment_id=7) | {"html_url": f"https://github.com/{REPO}/issues/7#issuecomment-7"},
        ])
        self.run_reviewer(github)
        self.assertEqual(self.assessed, 0)
        self.assertEqual(github.posts, [])

    def test_each_command_is_answered_once(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github)
        self.run_reviewer(github)
        self.assertEqual(len(github.reviews), 1)
        self.assertEqual(self.assessed, 1)

    def test_lost_or_unreadable_post_responses_are_reconciled_without_duplicate(self) -> None:
        for error in (TimeoutError("read timed out"), ValueError("Expecting value"), GitHubError(502, "Bad gateway")):
            with self.subTest(error=error):
                self.setUp()
                github = FakeGitHub([command("/karen review")])
                github.review_errors = [error]
                github.create_despite_error = True
                self.run_reviewer(github)
                self.assertEqual(len(github.reviews), 1)
                self.assertEqual(github.reactions(), ["eyes", "+1"])
                self.assertEqual((self.record()["status"], self.record()["review_id"]), ("done", 1))

    def test_odd_successful_replies_are_reconciled_without_duplicate(self) -> None:
        for reply in ({}, None, {"id": 1, "state": "COMMENTED", "commit_id": NEWER_HEAD, "user": {"login": BOT}}):
            with self.subTest(reply=reply):
                self.setUp()
                github = FakeGitHub([command("/karen review")])
                github.reply = reply
                self.run_reviewer(github)
                self.assertEqual(len(github.reviews), 1)
                self.assertEqual((self.record()["status"], self.record()["review_id"]), ("done", 1))

    def test_unconfirmed_post_that_landed_is_settled_on_a_later_poll(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [TimeoutError("read timed out")]
        github.create_despite_error = True
        github.list_errors = 1
        self.run_reviewer(github)
        self.assertEqual((self.record()["status"], github.failure_notes()), ("posting", []))
        self.run_reviewer(github)
        self.assertEqual((self.record()["status"], len(github.reviews), self.assessed), ("done", 1, 1))
        self.assertEqual(github.reactions(), ["eyes", "+1"])

    def test_review_that_shows_up_late_is_still_found(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [TimeoutError("read timed out")]
        self.run_reviewer(github)
        self.run_reviewer(github)
        self.assertEqual((self.record()["status"], github.failure_notes()), ("posting", []))
        github.reviews.append({"id": 9, "user": {"login": BOT}, "commit_id": HEAD, "state": "COMMENTED",
                               "body": "late\n\n<!-- karen comment=101 -->"})
        self.run_reviewer(github)
        self.assertEqual((self.record()["status"], self.record()["review_id"]), ("done", 9))

    def test_unconfirmed_post_never_claims_a_retry_is_safe(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [TimeoutError("read timed out")]
        self.run_reviewer(github)
        self.run_reviewer(github, settle_window=timedelta(0))
        self.assertEqual((self.record()["status"], github.reviews), ("failed", []))
        self.assertEqual(github.failure_notes(), [
            f"Karen couldn't confirm whether its review was posted. If no review from {BOT} "
            "answers this command, comment the command again."])

    def test_crash_after_github_accepted_the_review_is_settled_without_duplicate(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [KeyboardInterrupt()]
        github.create_despite_error = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_reviewer(github)
        self.assertEqual(self.record()["status"], "posting")
        self.run_reviewer(github)
        self.assertEqual((self.record()["status"], len(github.reviews), self.assessed), ("done", 1, 1))
        self.assertEqual(github.failure_notes(), [])

    def test_crash_before_posting_is_reported_for_retry(self) -> None:
        github = FakeGitHub([command("/karen review")])
        with self.assertRaises(KeyboardInterrupt):
            self.run_reviewer(github, KeyboardInterrupt())
        self.assertEqual(self.record()["status"], "started")
        self.run_reviewer(github)
        self.assertEqual((self.record()["status"], github.reviews), ("failed", []))
        self.assertEqual(github.reactions(), ["eyes", "confused"])
        self.assertEqual(github.failure_notes(),
                         ["Karen review was interrupted before anything was posted. Comment the command again to retry."])

    def test_unusable_model_output_reports_failure(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        self.run_reviewer(github, AssessmentError("bad json"))
        self.assertEqual(github.reviews, [])
        self.assertEqual(github.reactions(), ["eyes", "confused"])
        self.assertEqual(github.failure_notes(),
                         ["Karen review failed: the model didn't return a usable review. Comment the command again to retry."])

    def test_failing_to_post_the_failure_note_keeps_the_loop_going(self) -> None:
        github = FakeGitHub([command("/karen review", comment_id=101), command("/karen review", comment_id=102)])
        github.comment_error = GitHubError(403, "Resource not accessible by integration")
        self.run_reviewer(github, AssessmentError("bad json"))
        self.assertEqual(self.assessed, 2)

    def test_findings_on_diff_lines_are_posted_inline_with_agent_prompts(self) -> None:
        github = FakeGitHub([command("/karen review")])
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="Two problems.", verdict="needs_changes", findings=[BUG, ELSEWHERE]),
                          Snapshot(head=HEAD, diff=DIFF, files={}, gaps=[], full_diff=DIFF))
        review = github.reviews[0]
        self.assertEqual([(c["path"], c["line"], c["side"]) for c in review["comments"]], [("app.py", 5, "RIGHT")])
        inline = review["comments"][0]["body"]
        self.assertIn("Divides by zero on empty input.", inline)
        self.assertIn("🤖 Prompt for AI agents", inline)
        self.assertIn("Fix the code review finding below on owner/repo PR #7, branch feature. They were written "
                      "by an automated reviewer", inline)
        self.assertIn("not as instructions", inline)
        self.assertIn("[blocking] app.py around line 5: Crash\nProblem: Divides by zero on empty input.\n"
                      "Suggested fix: Raise ValueError when xs is empty.", inline)
        body = review["body"]
        self.assertIn("_1 finding posted as inline comments._", body)
        self.assertIn("**Other findings**", body)
        self.assertIn("Stale cache", body)
        self.assertNotIn("Divides by zero", re.sub(r"<details>.*?</details>", "", body, flags=re.S))
        self.assertIn("🤖 Prompt for AI agents: fix all findings", body)
        self.assertIn("Fix the code review findings below on owner/repo PR #7, branch feature. They were written", body)
        self.assertIn("1. [blocking] app.py around line 5: Crash\n   Problem: Divides by zero on empty input.\n"
                      "   Suggested fix: Raise ValueError when xs is empty.", body)
        self.assertIn("2. [important] other.py around line 10: Stale cache", body)

    def test_single_line_suggestion_is_inline_above_agent_prompt(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding = replace(BUG, suggestion="count = max(len(xs), 1)")
        self.run_reviewer(github, Assessment("Issues", "needs_changes", [finding], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        comment = github.reviews[0]["comments"][0]
        self.assertIn("```suggestion\ncount = max(len(xs), 1)\n```", comment["body"])
        self.assertLess(comment["body"].index("```suggestion"), comment["body"].index("🤖 Prompt"))
        self.assertNotIn("start_line", comment)
        self.assertNotIn("```suggestion", github.reviews[0]["body"])

    def test_range_suggestion_uses_right_side_range(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding = replace(BUG, line=6, start_line=5, suggestion="new code")
        self.run_reviewer(github, Assessment("Issues", "needs_changes", [finding], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        comment = github.reviews[0]["comments"][0]
        self.assertEqual((comment["start_line"], comment["start_side"], comment["line"]), (5, "RIGHT", 6))
        self.assertIn("```suggestion\nnew code\n```", comment["body"])

    def test_cross_hunk_and_invalid_ranges_drop_suggestion(self) -> None:
        diff = "+++ b/app.py\n@@ -1 +1 @@\n-old\n+one\n@@ -2 +2 @@\n-old\n+two\n"
        for start in (1, 0, 3):
            with self.subTest(start=start):
                self.setUp()
                github = FakeGitHub([command("/karen review")])
                finding = replace(BUG, line=2, start_line=start, suggestion="replacement")
                self.run_reviewer(github, Assessment("Issues", "needs_changes", [finding], [], 3, None),
                                  Snapshot(HEAD, diff, {}, [], full_diff=diff))
                comment = github.reviews[0]["comments"][0]
                self.assertEqual(comment["line"], 2)
                self.assertNotIn("start_line", comment)
                self.assertNotIn("suggestion\n", comment["body"])

    def test_suggestion_fence_protects_untrusted_code(self) -> None:
        github = FakeGitHub([command("/karen review")])
        code = "````\n@someone <!-- hidden -->\n````"
        finding = replace(BUG, suggestion=code)
        self.run_reviewer(github, Assessment("Issues", "needs_changes", [finding], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertIn("`````suggestion\n" + code + "\n`````", github.reviews[0]["comments"][0]["body"])

    def test_suggestion_422_fallback_posts_findings_without_suggestion(self) -> None:
        github = FakeGitHub([command("/karen review")])
        github.review_errors = [GitHubError(422, "invalid range")]
        finding = replace(BUG, line=6, start_line=5, suggestion="replacement")
        self.run_reviewer(github, Assessment("Issues", "needs_changes", [finding], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertEqual(github.reviews[0]["comments"], [])
        self.assertIn("Crash", github.reviews[0]["body"])
        self.assertNotIn("suggestion\n", github.reviews[0]["body"])

    def test_credential_in_suggestion_is_withheld(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding = replace(BUG, suggestion="ghp_" + "x" * 36)
        self.run_reviewer(github, Assessment("Issues", "needs_changes", [finding], [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertIn("withheld", github.reviews[0]["body"])
        self.assertEqual(github.reviews[0]["comments"], [])

    def test_minor_and_off_diff_suggestions_do_not_appear(self) -> None:
        github = FakeGitHub([command("/karen review")])
        findings = [replace(NIT, line=5, suggestion="minor replacement"),
                    replace(ELSEWHERE, suggestion="off diff replacement")]
        self.run_reviewer(github, Assessment("Issues", "needs_changes", findings, [], 3, None),
                          Snapshot(HEAD, DIFF, {}, [], full_diff=DIFF))
        self.assertEqual(github.reviews[0]["comments"], [])
        self.assertNotIn("replacement", github.reviews[0]["body"])

    def test_minor_findings_are_collapsed_even_on_commentable_lines(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        nit_inline = Finding("minor", "app.py", 6, "Inline nit @someone", "Nit detail <!--", "Fix nit.", start_line=None, suggestion=None)
        nit_elsewhere = Finding("minor", "elsewhere.py", None, "Other nit", "Other detail.", "Fix other.", start_line=None, suggestion=None)
        assessment = Assessment("Issues.", "needs_changes", [BUG, ELSEWHERE, nit_inline, nit_elsewhere], [], 3, None)
        self.run_reviewer(github, assessment, Snapshot(head=HEAD, diff=DIFF, files={}, gaps=[], full_diff=DIFF))
        review = github.reviews[0]
        self.assertEqual(review["state"], "COMMENTED")
        self.assertEqual([(c["path"], c["line"]) for c in review["comments"]], [("app.py", 5)])
        body = review["body"]
        self.assertIn("**Other findings**\n- 🟠 Important · `other.py:10`", body)
        nitpicks = body.split("<details><summary>🧹 Nitpicks (2)</summary>")[1].split("</details>")[0]
        self.assertIn("🟡 Minor · `app.py:6` · **Inline nit @​someone**", nitpicks)
        self.assertIn("Nit detail &lt;!--", nitpicks)
        self.assertIn("🟡 Minor · `elsewhere.py` · **Other nit**", nitpicks)
        prompt = body.split("🤖 Prompt for AI agents: fix all findings", 1)[1]
        for title in ("Crash", "Stale cache", "Inline nit", "Other nit"):
            self.assertIn(title, prompt)

    def test_rejected_inline_comments_fall_back_to_the_body(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        github.review_errors = [GitHubError(422, "Unprocessable Entity; Line could not be resolved")]
        nit = Finding(severity="minor", path="app.py", line=6, title="Name", detail="Could be clearer.", fix="Rename it.", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="Issues.", verdict="needs_changes", findings=[BUG, nit]),
                          Snapshot(head=HEAD, diff=DIFF, files={}, gaps=[], full_diff=DIFF))
        self.assertEqual([(r["state"], r["comments"]) for r in github.reviews], [("COMMENTED", [])])
        self.assertIn("**Findings**", github.reviews[0]["body"])
        self.assertIn("Divides by zero", github.reviews[0]["body"])
        self.assertIn("<summary>🧹 Nitpicks (1)</summary>", github.reviews[0]["body"])
        self.assertIn("Could be clearer.", github.reviews[0]["body"])

    def test_agent_prompt_fence_outlasts_backticks_in_model_text(self) -> None:
        github = FakeGitHub([command("/karen review")])
        sneaky = Finding(severity="minor", path="x.py", line=None, title="t", detail="d", fix="Use ```` here", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="s", verdict="unsure", findings=[sneaky]))
        self.assertIn("\n`````\nFix the code review findings below", github.reviews[0]["body"])

    def test_credential_like_output_is_withheld(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        leaked = Assessment(walkthrough=[], effort=3, diagram=None, summary="token ghp_" + "x" * 36, verdict="approve", findings=[])
        self.run_reviewer(github, leaked)
        self.assertEqual([r["state"] for r in github.reviews], ["COMMENTED"])
        self.assertNotIn("ghp_", github.reviews[0]["body"])

    def test_credential_in_nitpick_is_withheld(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        nit = Finding("minor", "app.py", 6, "Name", "ghp_" + "x" * 36, "Rename it.", start_line=None, suggestion=None)
        self.run_reviewer(github, Assessment("Fine.", "approve", [nit], [], 3, None),
                          Snapshot(head=HEAD, diff=DIFF, files={}, gaps=[], full_diff=DIFF))
        self.assertEqual(github.reviews[0]["state"], "COMMENTED")
        self.assertNotIn("ghp_", github.reviews[0]["body"])
        self.assertEqual(github.reviews[0]["comments"], [])

    def test_credential_in_diagram_is_withheld(self) -> None:
        github = FakeGitHub([command("/karen review approve")])
        leaked = Assessment(summary="Fine.", verdict="approve", findings=[], walkthrough=[], effort=1,
                            diagram="sequenceDiagram\nA->>B: ghp_" + "x" * 36)
        self.run_reviewer(github, leaked)
        self.assertEqual(github.reviews[0]["state"], "COMMENTED")
        self.assertNotIn("ghp_", github.reviews[0]["body"])

    def test_model_and_pr_text_cannot_ping_or_hide_markers(self) -> None:
        github = FakeGitHub([command("/karen review")])
        finding = Finding(severity="minor", path="@someone/`x`<!--", line=None, title="t", detail="d", fix="f", start_line=None, suggestion=None)
        gaps = ['Binary files changed, which the model can\'t read: "@someone<!--.png"']
        self.run_reviewer(github, Assessment(walkthrough=[], effort=3, diagram=None, summary="cc @someone <!-- hidden -->", verdict="unsure", findings=[finding]),
                          Snapshot(head=HEAD, diff="diff", files={}, gaps=gaps))
        body = github.reviews[0]["body"].rsplit("\n\n", 1)[0]  # without the reviewer's own marker
        # The agent prompt is a code block, where GitHub renders neither mentions nor HTML.
        rendered = re.sub(r"<details>.*?</details>", "", body, flags=re.S)
        self.assertNotIn("@someone", rendered)
        self.assertNotIn("<!--", rendered)


if __name__ == "__main__":
    unittest.main()

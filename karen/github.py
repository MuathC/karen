"""GitHub REST access as either the reviewer GitHub App or the gh CLI user."""

import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import jwt

API = "https://api.github.com"
# Installation tokens are narrowed to this even if the App is later granted more.
# GitHub refuses resolveReviewThread without contents: write, though nothing is pushed.
TOKEN_PERMISSIONS = {"pull_requests": "write", "contents": "write"}


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        request = urllib.request.Request(
            API + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "karen",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code, _error_message(error)) from None
        return json.loads(raw) if raw else None

    def graphql(self, query: str, variables: dict) -> dict:
        response = self.call("POST", "/graphql", {"query": query, "variables": variables})
        if response.get("errors"):
            raise GitHubError(400, "; ".join(error.get("message", "GraphQL error") for error in response["errors"]))
        return response["data"]

    def get_all(self, path: str) -> list:
        separator = "&" if "?" in path else "?"
        items: list = []
        page = 1
        while True:
            batch = self.call("GET", f"{path}{separator}per_page=100&page={page}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


def _error_message(error: urllib.error.HTTPError) -> str:
    """GitHub's message plus its error details, e.g. "Can not approve your own pull request"."""
    try:
        reply = json.loads(error.read())
        details = [item if isinstance(item, str) else item.get("message") or item.get("code")
                   for item in reply.get("errors", [])]
        return "; ".join([reply["message"], *filter(None, details)])
    except (ValueError, KeyError, TypeError, AttributeError):
        return str(error.reason)


class AppAuth:
    """Acts as <slug>[bot], with installation tokens scoped to one repository each."""

    def __init__(self, app_id: int, private_key: str) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.login = GitHub(self._jwt()).call("GET", "/app")["slug"] + "[bot]"
        self._tokens: dict[str, tuple[str, float]] = {}

    def _jwt(self) -> str:
        now = int(time.time())
        claims = {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def client(self, repo: str) -> GitHub:
        token, expires_at = self._tokens.get(repo, ("", 0.0))
        # Tokens live an hour; renew early so one long review never outlives its token.
        if expires_at - time.time() < 30 * 60:
            app = GitHub(self._jwt())
            installation = app.call("GET", f"/repos/{repo}/installation")
            issued = app.call(
                "POST",
                f"/app/installations/{installation['id']}/access_tokens",
                {"repositories": [repo.split("/")[1]], "permissions": TOKEN_PERMISSIONS},
            )
            token = issued["token"]
            expires_at = datetime.fromisoformat(issued["expires_at"]).timestamp()
            self._tokens[repo] = (token, expires_at)
        return GitHub(token)


class UserAuth:
    """Acts as the gh CLI user. GitHub never lets that user approve their own PRs."""

    def __init__(self) -> None:
        token = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self._github = GitHub(token)
        self.login = self._github.call("GET", "/user")["login"]

    def client(self, repo: str) -> GitHub:
        return self._github

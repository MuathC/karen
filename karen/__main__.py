"""Command line: watch, once, preview and create-app. See README.md."""

import argparse
import fcntl
import json
import logging
import sys
import time
import tomllib
from functools import partial
from pathlib import Path

from karen.app_setup import create_app
from karen.assess import HOMES, assess, is_isolated
from karen.github import AppAuth, UserAuth
from karen.reviewer import Reviewer

CONFIG_DIR = Path.home() / ".config" / "karen"
STATE_DIR = Path.home() / ".local" / "state" / "karen"
ISOLATION_BREACH = 3  # the systemd unit doesn't restart on this exit status

logger = logging.getLogger("karen")


def load_auth() -> AppAuth | UserAuth:
    """The App identity when create-app has run, otherwise the gh CLI user."""
    app_file = CONFIG_DIR / "app.json"
    if not app_file.exists():
        return UserAuth()
    app = json.loads(app_file.read_text())
    return AppAuth(app["id"], Path(app["private_key"]).read_text())


def main() -> None:
    parser = argparse.ArgumentParser(prog="karen")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("watch", help="poll the configured repositories and answer commands")
    commands.add_parser("once", help="one polling pass, then exit")
    preview = commands.add_parser("preview", help="review a PR locally and print the result; posts nothing")
    preview.add_argument("repo")
    preview.add_argument("pr", type=int)
    preview.add_argument("--approve", action="store_true", help="decide as /karen review approve would")
    create = commands.add_parser("create-app", help="register the reviewer GitHub App")
    create.add_argument("name")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "create-app":
        create_app(args.name, CONFIG_DIR)
        return
    auth = load_auth()
    config = tomllib.loads((CONFIG_DIR / "config.toml").read_text())
    context_repos = tuple(config.get("context_repos", []))
    engine = config.get("engine", "codex")
    if engine not in HOMES:
        sys.exit(f"engine must be one of: {', '.join(HOMES)}")
    review = partial(assess, engine)
    if args.command == "preview":
        draft = Reviewer(auth, STATE_DIR, review, context_repos=context_repos).prepare(auth.client(args.repo), args.repo, args.pr, args.approve, auth.login)
        print(f"Would post {draft.event} on {draft.head} as {auth.login}:\n\n{draft.body()}")
        for comment in draft.comments():
            print(f"\n--- inline comment on {comment['path']}:{comment['line']} ---\n{comment['body']}")
        return

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(STATE_DIR / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Another reviewer is already running.")
        reviewer = Reviewer(auth, STATE_DIR, review, context_repos=context_repos)  # reads state only while holding the lock
        # Checked on every start, so an agent upgrade that adds a tool stops the reviewer.
        # A network failure here just exits; systemd restarts the service a minute later.
        if not is_isolated(engine):
            logger.critical("%s could read a local file; refusing to review until that tool is disabled", engine)
            sys.exit(ISOLATION_BREACH)
        if args.command == "once":
            reviewer.run_once(config["repos"])
            return
        logger.info("Answering commands on %s as %s with %s", ", ".join(config["repos"]), auth.login, engine)
        while True:
            try:
                reviewer.run_once(config["repos"])
            except Exception:  # keep watching through network or GitHub outages
                logger.exception("Polling failed; trying again next round")
            time.sleep(config.get("poll_seconds", 30))


if __name__ == "__main__":
    main()

# Karen

A code reviewer that only speaks when spoken to. Comment `/karen review` on a pull
request and Karen reviews it from your own machine, using your Codex or Claude Code
subscription.

| Comment | What happens |
| --- | --- |
| `/karen review` | Reviews what changed since its last review and closes the threads that were fixed. |
| `/karen full review` | Reviews the whole pull request again. |
| `/karen review approve` | Reviews the whole pull request and approves it if nothing important is wrong. |

Karen reacts 👀 when it starts and 👍 when the review is posted. Problems are posted
as inline comments, each with a one-click fix when there is one and a prompt you can
paste into a coding agent.

## How it works

```
PR comment → poll → snapshot → agent in a sandbox → review on the PR
```

- **Poll** (`reviewer.py`): every 30 seconds Karen checks the watched repos for new
  commands. Only people with write access can start a review.
- **Snapshot** (`snapshot.py`): the PR's code never runs. Karen fetches it into a bare
  git mirror and reads the diff.
- **Agent** (`assess.py`, `repo_tools.py`): Codex or Claude Code runs in a
  [bubblewrap](https://github.com/containers/bubblewrap) sandbox with its shell and file
  tools turned off, so it can only list, read and search the repository. When Karen
  starts, it asks the agent to read a file outside the sandbox and refuses to run if
  the agent can.
- **Review** (`reviewer.py`): Karen posts the findings. It approves only when the agent
  says approve, no important finding is open, it saw the whole diff, the author has
  write access, and no new commits came in.

## Setup

You need Linux, Python 3.11+, `git`, `bubblewrap`, `python3-jwt`, `python3-cryptography`,
and the `codex` CLI (with Node.js) or the `claude` CLI.

1. Clone this repo to `~/dev/karen`.
2. Create the GitHub App, then install it on your repos with the link it prints. The App
   is public so it can go on an organization, but it only reviews the repos you list below.
   ```bash
   python3 -m karen create-app my-karen
   ```
3. Sign Karen's agent in. It uses its own sign-in, separate from your personal one:
   ```bash
   mkdir -p ~/.local/state/karen/codex-home && CODEX_HOME=~/.local/state/karen/codex-home codex login
   ```
   or
   ```bash
   CLAUDE_CONFIG_DIR=~/.local/state/karen/claude-home claude auth login
   ```
4. Write `~/.config/karen/config.toml`:
   ```toml
   repos = ["owner/repo"]
   engine = "codex"   # or "claude"
   poll_seconds = 30
   ```
5. Try a review without posting it, then start the service:
   ```bash
   python3 -m karen preview owner/repo 1
   ```
   ```bash
   systemctl --user enable --now ~/dev/karen/systemd/karen.service
   ```

Logs: `journalctl --user -u karen -f`. Tests: `python3 -m unittest discover -s tests -t .`

## Per-repo rules

Karen follows `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` and `CODING_STANDARDS.md`
from the base branch. An optional `.karen.toml` adds more:

```toml
profile = "chill"          # skip minor findings
ignore = ["generated/**"]
instructions = "Check compatibility with the public API."
```

Karen's review is an AI's opinion, not proof. It never pushes or merges.

## License

MIT

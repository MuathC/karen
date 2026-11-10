"""Register the reviewer as a GitHub App through GitHub's manifest flow.

A local page hands GitHub the manifest. After you confirm on GitHub, the App's id and
private key come back to this process and are saved to the config directory, so the
key never passes through the browser's downloads.
"""

import html
import http.server
import json
import os
import secrets
import urllib.parse
import urllib.request
from pathlib import Path

PORT = 8765
FORM = """<!doctype html><title>Create reviewer App</title>
<form id="manifest" method="post" action="https://github.com/settings/apps/new?state={state}">
<input type="hidden" name="manifest" value="{manifest}">
<button>Continue to GitHub</button></form>
<script>document.getElementById("manifest").submit()</script>"""


def create_app(name: str, config_dir: Path) -> None:
    # GitHub returns the key once; make sure it can be saved before creating the App.
    key_path = config_dir / "app-private-key.pem"
    if key_path.exists():
        raise SystemExit(f"{key_path} already exists; move it away before creating another App.")
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    state = secrets.token_urlsafe(16)
    manifest = {
        "name": name,
        "url": "https://github.com",
        "redirect_url": f"http://127.0.0.1:{PORT}/created",
        # Public, so it can be installed on an organization. It only reviews the repos in config.toml.
        "public": True,
        "default_permissions": {"pull_requests": "write", "contents": "write"},
        "default_events": [],
    }
    created: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                page = FORM.format(state=state, manifest=html.escape(json.dumps(manifest)))
            elif url.path == "/created" and query.get("state") == [state] and "code" in query:
                created.update(_convert(query["code"][0]))
                page = "<p>GitHub App created. You can close this tab.</p>"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Open http://127.0.0.1:{PORT}/ in the browser that is signed in to GitHub.")
    while not created:
        server.handle_request()

    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as key_file:
        key_file.write(created["pem"])
    (config_dir / "app.json").write_text(json.dumps(
        {"id": created["id"], "slug": created["slug"], "private_key": str(key_path)}, indent=2
    ))
    print(f"Created {created['slug']} (id {created['id']}). Install it on the repositories to review:")
    print(f"{created['html_url']}/installations/new")


def _convert(code: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions", method="POST",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "karen"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())

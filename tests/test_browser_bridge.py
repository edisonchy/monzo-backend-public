import json
import select
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest


@pytest.fixture(autouse=True)
def clean_database():
    # Override the application suite's DB fixture: this tests a separate local process.
    yield


def test_idle_preconnection_does_not_block_oauth(tmp_path):
    calls = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header(
                "Set-Cookie",
                "monzo_oauth_binding=test-binding; Secure; HttpOnly; SameSite=Lax; Path=/oauth",
            )
            self.end_headers()
            self.wfile.write(
                json.dumps({"authorization_url": "https://auth.monzo.com/?state=test"}).encode()
            )

        def do_GET(self):
            calls.append((self.path, self.headers.get("Cookie"), self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    env = tmp_path / ".env"
    env.write_text("MONZO_OWNER_TOKEN=test-owner\n")
    with HTTPServer(("127.0.0.1", 0), Upstream) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "scripts/connect_browser.py"),
                "--env-file",
                str(env),
                "--port",
                "0",
                "--upstream",
                f"http://127.0.0.1:{upstream.server_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert select.select([process.stdout], [], [], 5)[0], "Helper did not start"
            url = process.stdout.readline().strip().removeprefix("Open on this Mac: ")
            parsed = urlsplit(url)
            origin = f"http://localhost:{parsed.port}"
            # Chromium can open a TCP connection and send no HTTP request yet.
            with (
                socket.create_connection(("127.0.0.1", parsed.port)),
                httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client,
            ):
                assert client.get(f"http://127.0.0.1:{parsed.port}/").status_code == 403
                result = client.get(url)
                assert result.status_code == 302
                assert result.headers["location"] == "https://auth.monzo.com/?state=test"
                assert "HttpOnly" in result.headers["set-cookie"]
                assert "Secure" not in result.headers["set-cookie"]
                assert client.get(url).status_code == 404
                result = client.get(origin + "/oauth/callback?code=test-code&state=test")
                assert result.status_code == 200
                assert "Monzo connection saved" in result.text
            process.wait(timeout=5)
            assert calls == [
                ("/v1/oauth/start", "Bearer test-owner"),
                (
                    "/oauth/callback?code=test-code&state=test",
                    "monzo_oauth_binding=test-binding",
                    None,
                ),
            ]
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
            upstream.shutdown()
            thread.join(timeout=5)

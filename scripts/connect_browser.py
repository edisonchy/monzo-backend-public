"""Short-lived loopback OAuth bridge. The owner bearer token never enters the browser."""

import argparse
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:8088")
    parser.add_argument("--port", type=int, default=8087)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)
    if upstream.scheme != "http" or upstream.hostname != "127.0.0.1":
        raise SystemExit("Upstream must be a loopback SSH tunnel")
    env = dotenv_values(args.env_file)
    owner = env["MONZO_OWNER_TOKEN"]
    start_path = "/connect/" + secrets.token_urlsafe(32)
    deadline = time.monotonic() + 1800
    completed = False
    started = False
    flow_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Callback URLs contain an authorisation code. Never log them.

        def reply(self, status, body, *, location=None, cookie=None):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
            if location:
                self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            data = body.encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # Idle browser preconnections must not block other HTTP handlers.
            # Only actual OAuth requests serialise around single-use state.
            with flow_lock:
                self.handle_get()

        def handle_get(self):
            nonlocal completed, started
            if self.headers.get("Host") != f"localhost:{server.server_port}":
                return self.reply(403, "Use the exact localhost connection link.")
            path = urlsplit(self.path)
            try:
                with httpx.Client(timeout=70, follow_redirects=False, trust_env=False) as client:
                    if path.path == start_path and not started and not path.query:
                        result = client.post(
                            args.upstream + "/v1/oauth/start",
                            headers={"Authorization": "Bearer " + owner},
                        )
                        if result.status_code != 200:
                            return self.reply(
                                502, "Connection could not start. Return to the chat."
                            )
                        location = result.json()["authorization_url"]
                        parsed = urlsplit(location)
                        if parsed.scheme != "https" or parsed.netloc != "auth.monzo.com":
                            return self.reply(502, "Unexpected authorisation destination.")
                        cookies = SimpleCookie()
                        cookies.load(result.headers["set-cookie"])
                        cookie = cookies["monzo_oauth_binding"]
                        # Only this local HTTP hop omits Secure. HttpOnly, SameSite,
                        # expiry and /oauth path remain; the upstream keeps its setting.
                        cookie["secure"] = ""
                        started = True
                        return self.reply(
                            302,
                            "Continue at Monzo.",
                            location=location,
                            cookie=cookie.OutputString(),
                        )
                    if path.path == "/oauth/callback" and started and not completed:
                        query = parse_qs(path.query)
                        if any(len(query.get(k, [])) != 1 for k in ("code", "state")):
                            return self.reply(
                                400,
                                "Authorisation was cancelled or incomplete. Return to the chat.",
                            )
                        cookies = SimpleCookie()
                        cookies.load(self.headers.get("Cookie", ""))
                        binding = cookies.get("monzo_oauth_binding")
                        if not binding:
                            return self.reply(
                                400,
                                "Connection cookie is missing. Use the same browser to start and finish.",
                            )
                        result = client.get(
                            args.upstream + "/oauth/callback",
                            params={k: query[k][0] for k in ("code", "state")},
                            headers={"Cookie": "monzo_oauth_binding=" + binding.value},
                        )
                        if result.status_code != 200:
                            return self.reply(
                                502,
                                "Monzo connection was not completed. Return to the chat; do not share this URL.",
                            )
                        print(
                            "OAuth completed; token pair stored by backend. Awaiting app approval and discovery verification.",
                            flush=True,
                        )
                        self.reply(
                            200,
                            "Monzo connection saved. Approve access in the Monzo mobile app, then return to the chat and say approved. You can close this tab.",
                            cookie="monzo_oauth_binding=; Path=/oauth; Max-Age=0; HttpOnly; SameSite=Lax",
                        )
                        completed = True
                        return
                    return self.reply(
                        404,
                        "This temporary service only handles the private Monzo connection link.",
                    )
            except Exception:
                return self.reply(
                    502, "Connection failed. Return to the chat; no credential details were logged."
                )

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.timeout = 1
        print(f"Open on this Mac: http://localhost:{server.server_port}" + start_path, flush=True)
        while time.monotonic() < deadline and not completed:
            server.handle_request()
    print("Temporary OAuth bridge stopped.", flush=True)


if __name__ == "__main__":
    main()

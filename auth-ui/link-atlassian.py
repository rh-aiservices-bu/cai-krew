#!/usr/bin/env python3
"""
Get your Atlassian OAuth tokens for Jira MCP access.

Runs a local PKCE flow (same as Claude Code) against Atlassian's MCP OAuth
server, then prints the two tokens for you to paste into the Hermie Portal.

Usage:
    python3 link-atlassian.py

No arguments needed. Your browser will open for Atlassian login.
Paste the printed tokens into the Hermie Portal Atlassian card.
No org-admin approval needed — localhost redirect is allowlisted by default.
"""

import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

ATLASSIAN_CLIENT_ID = "BHEAUa4btXKyLVTl"  # from Step 1 dynamic registration
ATLASSIAN_AUTHORIZE_URL = "https://mcp.atlassian.com/v1/authorize"
ATLASSIAN_TOKEN_URL = "https://cf.mcp.atlassian.com/v1/token"
LOCAL_PORT = 18923
REDIRECT_URI = f"http://localhost:{LOCAL_PORT}/callback"
SCOPES = "read:jira-work write:jira-work read:jira-user read:me"


def _public_ssl_ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def pkce_pair():
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(code_challenge, state):
    params = {
        "client_id": ATLASSIAN_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": SCOPES,
        "state": state,
    }
    return f"{ATLASSIAN_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code, code_verifier):
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": ATLASSIAN_CLIENT_ID,
        "code_verifier": code_verifier,
    }).encode()
    req = urllib.request.Request(
        ATLASSIAN_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "mcp-gateway-local-linker/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_public_ssl_ctx()) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  Token exchange failed: HTTP {e.code}", file=sys.stderr)
        print(f"  Response: {body}", file=sys.stderr)
        sys.exit(1)


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code = None
    state_received = None
    error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            CallbackHandler.error = params["error"][0]
            desc = params.get("error_description", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<h2>Authorization failed</h2><p>{CallbackHandler.error}: {desc}</p>"
                "<p>You can close this tab.</p>".encode()
            )
            return

        CallbackHandler.auth_code = params.get("code", [None])[0]
        CallbackHandler.state_received = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>Atlassian linked!</h2>"
            b"<p>Authorization code received. You can close this tab.</p>"
        )

    def log_message(self, fmt, *args):
        pass


def wait_for_callback(expected_state, timeout=300):
    server = http.server.HTTPServer(("127.0.0.1", LOCAL_PORT), CallbackHandler)
    server.timeout = timeout

    while CallbackHandler.auth_code is None and CallbackHandler.error is None:
        server.handle_request()

    server.server_close()

    if CallbackHandler.error:
        print(f"\nError from Atlassian: {CallbackHandler.error}", file=sys.stderr)
        sys.exit(1)

    if CallbackHandler.state_received != expected_state:
        print("\nState mismatch - possible CSRF. Aborting.", file=sys.stderr)
        sys.exit(1)

    return CallbackHandler.auth_code


def main():
    code_verifier, code_challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    authorize_url = build_authorize_url(code_challenge, state)

    print("Opening browser for Atlassian login...")
    print(f"  If the browser doesn't open, visit:\n  {authorize_url}\n")
    webbrowser.open(authorize_url)

    print(f"Waiting for callback on http://localhost:{LOCAL_PORT}/callback ...")
    code = wait_for_callback(state)
    print("  Authorization code received.\n")

    print("Exchanging code for tokens...")
    token_response = exchange_code(code, code_verifier)
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token", "")
    expires_in = token_response.get("expires_in", "unknown")

    if not access_token:
        print(f"Error: no access_token in response: {token_response}", file=sys.stderr)
        sys.exit(1)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         Copy these tokens into the Hermie Portal             ║
║   https://auth-ui.apps.cluster-9shz5.9shz5.sandbox4079      ║
║                  .opentlc.com/dashboard                      ║
╚══════════════════════════════════════════════════════════════╝

Access token  (expires in {expires_in}s):
{access_token}

Refresh token:
{refresh_token if refresh_token else "(none — re-run script to refresh)"}

Paste both into the Atlassian card in the portal and click Connect.
The portal will validate and store them automatically.
""")


if __name__ == "__main__":
    main()

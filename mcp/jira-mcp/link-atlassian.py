#!/usr/bin/env python3
"""
Link your Atlassian account to Keycloak for per-user Jira MCP tool access.

Runs a local OAuth PKCE flow against Atlassian's MCP OAuth server
(same flow Claude Code uses), then stores the tokens as Keycloak
user attributes so Authorino can inject them on Jira tool calls.

Usage:
    python3 link-atlassian.py --keycloak-user <username>

The script will open your browser for Atlassian login. After you
authorize, it captures the tokens locally and pushes them to Keycloak.
No org-admin approval needed (localhost redirect is allowlisted by
managed Atlassian orgs).
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

ATLASSIAN_CLIENT_ID = "<YOUR_CLIENT_ID>"  # from Step 1 dynamic registration
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


def get_keycloak_admin_token(kc_base, admin_user, admin_pass):
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": admin_user,
        "password": admin_pass,
    }).encode()
    req = urllib.request.Request(
        f"{kc_base}/realms/master/protocol/openid-connect/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    ctx = __import__("ssl").create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = __import__("ssl").CERT_NONE
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read())["access_token"]


def find_keycloak_user(kc_base, realm, admin_token, username):
    req = urllib.request.Request(
        f"{kc_base}/admin/realms/{realm}/users?username={username}&exact=true",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    ctx = __import__("ssl").create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = __import__("ssl").CERT_NONE
    with urllib.request.urlopen(req, context=ctx) as resp:
        users = json.loads(resp.read())
    if not users:
        return None
    return users[0]


def update_user_attributes(kc_base, realm, admin_token, user_id, attrs):
    req = urllib.request.Request(
        f"{kc_base}/admin/realms/{realm}/users/{user_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    ctx = __import__("ssl").create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = __import__("ssl").CERT_NONE
    with urllib.request.urlopen(req, context=ctx) as resp:
        user_data = json.loads(resp.read())

    existing = user_data.get("attributes", {})
    existing.update(attrs)
    user_data["attributes"] = existing

    put_data = json.dumps(user_data).encode()
    put_req = urllib.request.Request(
        f"{kc_base}/admin/realms/{realm}/users/{user_id}",
        data=put_data,
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(put_req, context=ctx) as resp:
        pass


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
    parser = argparse.ArgumentParser(
        description="Link your Atlassian account to Keycloak for MCP Gateway Jira access"
    )
    parser.add_argument("--keycloak-user", required=True, help="Keycloak username to store tokens for")
    parser.add_argument("--keycloak-url", default=os.environ.get("KEYCLOAK_URL", ""), help="Keycloak base URL (or set KEYCLOAK_URL env var)")
    parser.add_argument("--keycloak-realm", default="mcp", help="Keycloak realm (default: mcp)")
    parser.add_argument("--keycloak-admin", default="admin", help="Keycloak admin username")
    parser.add_argument("--keycloak-admin-pass", default=os.environ.get("KEYCLOAK_ADMIN_PASS", ""), help="Keycloak admin password (or set KEYCLOAK_ADMIN_PASS env var)")
    args = parser.parse_args()

    if not args.keycloak_url:
        print("Error: --keycloak-url or KEYCLOAK_URL env var required", file=sys.stderr)
        sys.exit(1)
    if not args.keycloak_admin_pass:
        print("Error: --keycloak-admin-pass or KEYCLOAK_ADMIN_PASS env var required", file=sys.stderr)
        sys.exit(1)

    kc_base = args.keycloak_url.rstrip("/")

    print(f"Verifying Keycloak user '{args.keycloak_user}' exists...")
    admin_token = get_keycloak_admin_token(kc_base, args.keycloak_admin, args.keycloak_admin_pass)
    user = find_keycloak_user(kc_base, args.keycloak_realm, admin_token, args.keycloak_user)
    if not user:
        print(f"Error: user '{args.keycloak_user}' not found in realm '{args.keycloak_realm}'", file=sys.stderr)
        sys.exit(1)
    print(f"  Found: {user['username']} ({user.get('email', 'no email')})")

    code_verifier, code_challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    authorize_url = build_authorize_url(code_challenge, state)

    print(f"\nOpening browser for Atlassian authorization...")
    print(f"  If the browser doesn't open, go to:\n  {authorize_url}\n")
    webbrowser.open(authorize_url)

    print(f"Waiting for callback on http://localhost:{LOCAL_PORT}/callback ...")
    code = wait_for_callback(state)
    print("  Authorization code received.")

    print("Exchanging code for tokens...")
    token_response = exchange_code(code, code_verifier)
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in", "unknown")

    if not access_token:
        print(f"Error: no access_token in response: {token_response}", file=sys.stderr)
        sys.exit(1)

    print(f"  access_token:  {access_token[:20]}... (expires in {expires_in}s)")
    print(f"  refresh_token: {'yes' if refresh_token else 'no'}")

    print(f"\nStoring tokens in Keycloak for user '{args.keycloak_user}'...")
    admin_token = get_keycloak_admin_token(kc_base, args.keycloak_admin, args.keycloak_admin_pass)
    attrs = {"atlassian_oauth_token": [access_token]}
    if refresh_token:
        attrs["atlassian_oauth_refresh"] = [refresh_token]
    update_user_attributes(kc_base, args.keycloak_realm, admin_token, user["id"], attrs)
    print("  Done.")

    print(f"\n--- Summary ---")
    print(f"User:          {args.keycloak_user}")
    print(f"Realm:         {args.keycloak_realm}")
    print(f"Access token:  stored as 'atlassian_oauth_token'")
    print(f"Refresh token: stored as 'atlassian_oauth_refresh'")
    print(f"Expires in:    {expires_in}s")
    print(f"\nThe user can now use Jira tools through the MCP Gateway.")
    print(f"Run the token refresh CronJob (or re-run this script) before the token expires.")


if __name__ == "__main__":
    main()

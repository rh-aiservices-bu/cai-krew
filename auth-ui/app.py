"""Hermie Portal — per-user Keycloak linking and PAT management.

Flow:
  1. User opens portal → GET /
  2. Clicks "Sign in" → GET /login → PKCE redirect to Keycloak
  3. Keycloak redirects back → GET /callback
     - Exchanges code for tokens
     - Reads slack_user_id from userinfo (set by admin via provision_users.py)
     - Writes HERMES_HOME/user-tokens/{slack_user_id}/gateway.json
     - Creates session, redirects to /dashboard
  4. Dashboard shows linked identity + PAT status
  5. User can update their Gitea PAT or Atlassian API token via POST /update-pat
  6. Sign out performs RP-initiated Keycloak logout (kills SSO session)

Required env vars:
  KEYCLOAK_BASE      Keycloak realm base URL
                     e.g. https://sso.apps.cluster-xxx.../realms/mcp
  CLIENT_ID          OAuth client ID  (mcp-gateway)
  CLIENT_SECRET      OAuth client secret
  REDIRECT_URI       Full callback URL  (https://auth-ui.apps.xxx.../callback)

Optional:
  HERMES_HOME        Path to Hermes home on the shared PVC  (default: /app/.hermes)
  GITEA_URL          Gitea instance URL for the dashboard link
  SLACK_URL          Slack workspace URL for the dashboard link
  SESSION_SECRET     Secret for signing session cookies (random default)
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import stat
import time
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KEYCLOAK_BASE  = os.environ["KEYCLOAK_BASE"].rstrip("/")
CLIENT_ID      = os.environ["CLIENT_ID"]
CLIENT_SECRET  = os.environ["CLIENT_SECRET"]
REDIRECT_URI   = os.environ["REDIRECT_URI"]
HERMES_HOME    = Path(os.environ.get("HERMES_HOME", "/app/.hermes"))
GITEA_URL      = os.environ.get("GITEA_URL", "").rstrip("/")
SLACK_URL      = os.environ.get("SLACK_URL", "").rstrip("/")

# Derive Keycloak admin API base from realm URL
_kc_parts = KEYCLOAK_BASE.split("/realms/", 1)
KC_ADMIN_BASE = _kc_parts[0] + "/admin/realms/" + _kc_parts[1] if len(_kc_parts) == 2 else ""

AUTH_URL     = f"{KEYCLOAK_BASE}/protocol/openid-connect/auth"
TOKEN_URL    = f"{KEYCLOAK_BASE}/protocol/openid-connect/token"
USERINFO_URL = f"{KEYCLOAK_BASE}/protocol/openid-connect/userinfo"

SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

# Atlassian MCP OAuth — localhost PKCE client (registered 2026-08-13)
# Users run link-atlassian.py locally, then paste tokens here for storage.
ATLASSIAN_CLIENT_ID = "BHEAUa4btXKyLVTl"
ATLASSIAN_TOKEN_URL = "https://cf.mcp.atlassian.com/v1/token"

# ---------------------------------------------------------------------------
# Embed chibi mascot image as base64 (avoids need for static file serving)
# ---------------------------------------------------------------------------

_CHIBI_B64 = ""
_chibi_path = Path(__file__).parent / "chibi-hermie.png"
if _chibi_path.exists():
    _CHIBI_B64 = base64.b64encode(_chibi_path.read_bytes()).decode()

# ---------------------------------------------------------------------------
# In-memory stores (single-pod)
# ---------------------------------------------------------------------------

# state → {"verifier": str, "created_at": float}
_pkce_store: dict[str, dict] = {}
_STATE_TTL = 600

# session_id → {access_token, refresh_token, id_token, user_info, slack_user_id,
#               sub, display_name, preferred_username, email, expires_at}
_sessions: dict[str, dict] = {}
_SESSION_TTL = 3600


def _prune(store: dict, key: str, ttl: int) -> None:
    cutoff = time.time() - ttl
    for k in [k for k, v in store.items() if v.get("created_at", 0) < cutoff]:
        store.pop(k, None)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Token / PVC helpers
# ---------------------------------------------------------------------------

def _write_token(slack_user: str, tokens: dict) -> None:
    token_dir = HERMES_HOME / "user-tokens" / slack_user
    token_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
    token_path = token_dir / "gateway.json"
    tmp = token_path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(tokens, f, indent=2)
    except Exception:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(token_path))
    logger.info("Token written for slack_user=%s", slack_user)


def _token_status(slack_user: str) -> dict:
    """Return info about the user's current token file on the PVC."""
    path = HERMES_HOME / "user-tokens" / slack_user / "gateway.json"
    if not path.exists():
        return {"linked": False}
    try:
        data = json.loads(path.read_text())
        expires_at = data.get("expires_at", 0)
        remaining = max(0, int(expires_at - time.time()))
        return {
            "linked": True,
            "expires_at": expires_at,
            "remaining_sec": remaining,
            "valid": remaining > 0,
        }
    except Exception:
        return {"linked": True, "valid": False, "error": "unreadable"}


# ---------------------------------------------------------------------------
# Keycloak Admin API helpers
# ---------------------------------------------------------------------------

async def _get_sa_admin_token() -> str:
    """Get an access token for the mcp-gateway service account (manage-users role)."""
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _update_kc_attribute(user_sub: str, attr_name: str, value: str) -> None:
    """Set a single attribute on a Keycloak user via the Admin API."""
    if not KC_ADMIN_BASE:
        raise ValueError("Cannot derive Keycloak admin URL from KEYCLOAK_BASE")
    if not user_sub:
        raise ValueError("No user sub available in session — please sign out and sign in again")
    sa_token = await _get_sa_admin_token()
    headers = {"Authorization": f"Bearer {sa_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        resp = await client.get(f"{KC_ADMIN_BASE}/users/{user_sub}", headers=headers)
        resp.raise_for_status()
        user_rep = resp.json()
        attrs = dict(user_rep.get("attributes") or {})
        attrs[attr_name] = [value]
        user_rep["attributes"] = attrs
        resp = await client.put(
            f"{KC_ADMIN_BASE}/users/{user_sub}", headers=headers, json=user_rep
        )
        resp.raise_for_status()


async def _update_gitea_pat(user_sub: str, pat: str) -> None:
    """Set the gitea_pat attribute on a Keycloak user via the Admin API."""
    await _update_kc_attribute(user_sub, "gitea_pat", pat)


# ---------------------------------------------------------------------------
# HTML / CSS
# ---------------------------------------------------------------------------

_CHIBI_TAG = (
    f'<img src="data:image/png;base64,{_CHIBI_B64}" alt="Hermie" class="mascot">'
    if _CHIBI_B64 else ""
)

_CSS = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap');

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Nunito', -apple-system, BlinkMacSystemFont, sans-serif;
    background: linear-gradient(135deg, #fdf6e3 0%, #fef3c7 50%, #fff8f0 100%);
    min-height: 100vh;
    color: #1c1917;
  }

  /* ── Header ── */
  .header {
    background: linear-gradient(135deg, #92400e 0%, #b45309 60%, #d97706 100%);
    padding: 0 32px;
    display: flex;
    align-items: center;
    gap: 16px;
    box-shadow: 0 4px 20px rgba(146, 64, 14, 0.35);
    min-height: 70px;
  }
  .mascot {
    height: 58px;
    width: 58px;
    object-fit: cover;
    border-radius: 50%;
    border: 3px solid rgba(255,255,255,0.4);
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    margin: 6px 0;
  }
  .header-text h1 {
    color: white;
    font-size: 1.3rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }
  .header-text p {
    color: rgba(255,255,255,0.75);
    font-size: 0.78rem;
    font-weight: 600;
  }
  .header-spacer { flex: 1; }
  .header-nav a {
    color: rgba(255,255,255,0.85);
    text-decoration: none;
    font-size: 0.85rem;
    font-weight: 700;
    padding: 6px 14px;
    border-radius: 20px;
    border: 1.5px solid rgba(255,255,255,0.35);
    transition: background 0.15s;
  }
  .header-nav a:hover { background: rgba(255,255,255,0.15); }

  /* ── Container ── */
  .container { max-width: 680px; margin: 36px auto; padding: 0 16px 48px; }

  /* ── Cards ── */
  .card {
    background: white;
    border-radius: 16px;
    box-shadow: 0 2px 12px rgba(146,64,14,0.08), 0 1px 3px rgba(0,0,0,0.06);
    margin-bottom: 20px;
    overflow: hidden;
    border: 1px solid rgba(217,119,6,0.12);
  }
  .card-header {
    padding: 14px 22px;
    background: linear-gradient(135deg, #fffbeb, #fef3c7);
    border-bottom: 1px solid rgba(217,119,6,0.15);
    font-size: 0.78rem;
    font-weight: 800;
    color: #92400e;
    letter-spacing: .08em;
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card-body { padding: 20px 22px; }

  /* ── Rows ── */
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid #fef3c7;
  }
  .row:last-child { border-bottom: none; }
  .label { font-size: 0.82rem; color: #78716c; font-weight: 600; }
  .value { font-size: 0.88rem; font-weight: 700; font-family: monospace; color: #1c1917; }

  /* ── Badges ── */
  .badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: .03em;
  }
  .badge-green  { background: #d1fae5; color: #065f46; }
  .badge-yellow { background: #fef3c7; color: #92400e; }
  .badge-red    { background: #fee2e2; color: #991b1b; }
  .badge-gray   { background: #f1f5f9; color: #64748b; }

  /* ── Forms ── */
  form { margin-top: 14px; }
  input[type=text], input[type=password] {
    width: 100%;
    padding: 10px 14px;
    border: 1.5px solid #d6d3d1;
    border-radius: 10px;
    font-size: 0.9rem;
    font-family: inherit;
    margin-bottom: 10px;
    transition: border-color 0.15s;
    background: #fffbf7;
  }
  input[type=text]:focus, input[type=password]:focus {
    outline: none;
    border-color: #d97706;
    box-shadow: 0 0 0 3px rgba(217,119,6,0.1);
  }

  /* ── Buttons ── */
  .btn {
    display: inline-block;
    padding: 9px 22px;
    border-radius: 10px;
    font-size: 0.88rem;
    font-weight: 800;
    cursor: pointer;
    border: none;
    text-decoration: none;
    transition: transform 0.1s, box-shadow 0.1s;
    font-family: inherit;
  }
  .btn:active { transform: scale(0.97); }
  .btn-primary {
    background: linear-gradient(135deg, #d97706, #b45309);
    color: white;
    box-shadow: 0 3px 10px rgba(180,83,9,0.35);
  }
  .btn-primary:hover { box-shadow: 0 5px 16px rgba(180,83,9,0.45); }
  .btn-secondary {
    background: #f5f5f4;
    color: #57534e;
    border: 1.5px solid #e7e5e4;
  }
  .btn-secondary:hover { background: #e7e5e4; }
  .btn-link {
    background: none;
    color: #b45309;
    border: none;
    padding: 0;
    font-size: 0.85rem;
    text-decoration: underline;
    cursor: pointer;
    font-family: inherit;
    font-weight: 600;
  }

  /* ── Alerts ── */
  .alert {
    padding: 12px 18px;
    border-radius: 12px;
    margin-bottom: 20px;
    font-size: 0.88rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .alert-success { background: #d1fae5; color: #065f46; border: 1px solid #6ee7b7; }
  .alert-error   { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }

  /* ── Hero (login page) ── */
  .hero {
    text-align: center;
    padding: 60px 20px 40px;
  }
  .hero-mascot {
    height: 140px;
    width: 140px;
    object-fit: cover;
    border-radius: 50%;
    border: 4px solid #fcd34d;
    box-shadow: 0 8px 32px rgba(180,83,9,0.25);
    margin-bottom: 20px;
  }
  .hero h2 {
    font-size: 2rem;
    font-weight: 800;
    color: #92400e;
    margin-bottom: 8px;
    letter-spacing: -0.03em;
  }
  .hero p {
    color: #78716c;
    margin-bottom: 28px;
    font-size: 1rem;
    font-weight: 600;
    line-height: 1.5;
  }

  /* ── External link ── */
  .ext-link {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #b45309;
    text-decoration: none;
    font-weight: 700;
    font-size: 0.85rem;
  }
  .ext-link:hover { text-decoration: underline; }

  /* ── Quick-link buttons ── */
  .quick-links {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }
  .btn-app {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 12px 20px;
    border-radius: 12px;
    font-size: 0.9rem;
    font-weight: 700;
    text-decoration: none;
    border: 2px solid transparent;
    transition: transform 0.1s, box-shadow 0.1s, background 0.15s;
    font-family: inherit;
    flex: 1;
    min-width: 160px;
    justify-content: center;
  }
  .btn-app:active { transform: scale(0.97); }
  .btn-app-gitea {
    background: #f0fdf4;
    color: #166534;
    border-color: #86efac;
  }
  .btn-app-gitea:hover { background: #dcfce7; box-shadow: 0 3px 10px rgba(22,101,52,0.15); }
  .btn-app-slack {
    background: #fdf4ff;
    color: #7e22ce;
    border-color: #d8b4fe;
  }
  .btn-app-slack:hover { background: #f3e8ff; box-shadow: 0 3px 10px rgba(126,34,206,0.15); }
  .btn-app-atlassian {
    background: #eff6ff;
    color: #1e40af;
    border-color: #93c5fd;
  }
  .btn-app-atlassian:hover { background: #dbeafe; box-shadow: 0 3px 10px rgba(30,64,175,0.15); }

  /* ── Actions row ── */
  .actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 10px;
  }

  /* ── PAT hint ── */
  .pat-hint {
    font-size: 0.78rem;
    color: #a8a29e;
    margin-bottom: 10px;
    font-weight: 600;
  }
</style>
"""

_SVG_LINK = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>'


def _page(title: str, body: str, show_header: bool = True) -> str:
    header = ""
    if show_header:
        header = f"""
<div class="header">
  {_CHIBI_TAG}
  <div class="header-text">
    <h1>Hermie Portal</h1>
    <p>Your AI companion's command center</p>
  </div>
  <div class="header-spacer"></div>
</div>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Hermie Portal</title>
  {_CSS}
</head>
<body>
{header}
<div class="container">{body}</div>
</body></html>"""


def _page_dashboard(title: str, body: str, username: str) -> str:
    header = f"""
<div class="header">
  {_CHIBI_TAG}
  <div class="header-text">
    <h1>Hermie Portal</h1>
    <p>Your AI companion's command center</p>
  </div>
  <div class="header-spacer"></div>
  <div class="header-nav">
    <a href="/logout">Sign out</a>
  </div>
</div>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Hermie Portal</title>
  {_CSS}
</head>
<body>
{header}
<div class="container">{body}</div>
</body></html>"""


def _pat_display(pat: str | None) -> str:
    if not pat:
        return '<span class="badge badge-gray">Not set</span>'
    masked = "●" * 8 + pat[-4:] if len(pat) > 4 else "●" * len(pat)
    return f'<span class="badge badge-green">Set</span>&nbsp;<span style="font-family:monospace;font-size:0.8rem;color:#a8a29e">{masked}</span>'


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Hermie Portal")


def _get_session(request: Request) -> dict | None:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    session = _sessions.get(sid)
    if not session:
        return None
    if session.get("expires_at", 0) < time.time():
        _sessions.pop(sid, None)
        return None
    return session


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    session = _get_session(request)
    if session:
        return RedirectResponse("/dashboard")

    hero_img = (
        f'<img src="data:image/png;base64,{_CHIBI_B64}" alt="Hermie" class="hero-mascot">'
        if _CHIBI_B64 else ""
    )
    body = f"""
    <div class="hero">
      {hero_img}
      <h2>Hey there! I'm Hermie ✨</h2>
      <p>Sign in with your account to link your Slack identity<br>
         and manage your service credentials.</p>
      <a href="/login" class="btn btn-primary">Sign in with Keycloak</a>
    </div>"""
    return HTMLResponse(_page("Welcome", body, show_header=False))


@app.get("/login")
def login():
    """Start PKCE auth flow. prompt=login forces credential entry even if KC session is active."""
    _prune(_pkce_store, "created_at", _STATE_TTL)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    _pkce_store[state] = {"verifier": verifier, "created_at": time.time()}
    auth_url = (
        f"{AUTH_URL}?client_id={CLIENT_ID}&response_type=code"
        f"&scope=openid%20profile%20email"
        f"&redirect_uri={REDIRECT_URI}&state={state}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
        f"&prompt=login"
    )
    return RedirectResponse(auth_url)


@app.get("/callback")
async def callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    if error:
        desc = error_description or error
        logger.warning("Keycloak returned error on callback: %s — %s", error, desc)
        body = f"""<div class="hero">
          <h2>Hmm, something went wrong 😕</h2>
          <p style="color:#991b1b">{desc}</p>
          <a href="/login" class="btn btn-primary">Try again</a>
        </div>"""
        return HTMLResponse(_page("Error", body), status_code=400)

    if not code or not state:
        return RedirectResponse("/login")

    _prune(_pkce_store, "created_at", _STATE_TTL)
    entry = _pkce_store.pop(state, None)
    if not entry:
        raise HTTPException(400, "Invalid or expired state. Please start over.")

    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": entry["verifier"],
        })

    if resp.status_code != 200:
        raise HTTPException(502, f"Token exchange failed ({resp.status_code}). Please try again.")

    tokens = resp.json()
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token", "")
    if not access_token:
        raise HTTPException(502, "Unexpected response from Keycloak.")

    payload = _decode_jwt_payload(access_token)
    logger.info("JWT payload keys: %s", list(payload.keys()))

    # Fetch UserInfo — authoritative source for profile claims
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        ui_resp = await client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    user_info = ui_resp.json() if ui_resp.status_code == 200 else {}
    logger.info("UserInfo keys: %s", list(user_info.keys()))

    # Prefer userinfo for profile claims; fall back to JWT payload
    slack_user_id = user_info.get("slack_user_id") or payload.get("slack_user_id", "")
    sub = user_info.get("sub") or payload.get("sub", "")
    preferred_username = user_info.get("preferred_username") or payload.get("preferred_username", "")
    email = user_info.get("email") or payload.get("email", "")
    display_name = (
        user_info.get("name")
        or f"{user_info.get('given_name', '')} {user_info.get('family_name', '')}".strip()
        or preferred_username
    )

    # Write gateway.json to PVC if user has a linked Slack account
    expires_in = tokens.get("expires_in", 1800)
    token_data = {
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
        "slack_user_id": slack_user_id,
    }
    if slack_user_id:
        try:
            _write_token(slack_user_id, token_data)
            logger.info("Linked Keycloak sub=%s to Slack user=%s", sub, slack_user_id)
        except Exception as exc:
            logger.exception("Failed to write token: %s", exc)
    else:
        logger.warning("User %s has no slack_user_id attribute — not writing token", preferred_username)

    # Create session
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {
        "access_token": access_token,
        "id_token": id_token,
        "refresh_token": tokens.get("refresh_token", ""),
        "user_info": user_info,
        "sub": sub,
        "slack_user_id": slack_user_id,
        "display_name": display_name,
        "preferred_username": preferred_username,
        "email": email,
        "created_at": time.time(),
        "expires_at": time.time() + _SESSION_TTL,
    }

    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie("session_id", sid, httponly=True, samesite="lax", max_age=_SESSION_TTL)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, msg: str = "", error: str = ""):
    session = _get_session(request)
    if not session:
        return RedirectResponse("/")

    slack_user_id = session["slack_user_id"]
    display_name  = session["display_name"]
    username      = session["preferred_username"]
    user_info     = session["user_info"]
    email          = session.get("email") or user_info.get("email") or "—"
    gitea_pat             = user_info.get("gitea_pat", "")
    atlassian_oauth_token = user_info.get("atlassian_oauth_token", "")

    # Token status on PVC
    tok_status = _token_status(slack_user_id) if slack_user_id else {"linked": False}

    # Slack section
    if slack_user_id:
        slack_html = f'<span class="badge badge-green">Linked</span>&nbsp;<span class="value">{slack_user_id}</span>'
    else:
        slack_html = '<span class="badge badge-yellow">Not configured</span>&nbsp;<span style="color:#a8a29e;font-size:0.82rem">Ask an admin to set your Slack user ID</span>'

    # PVC token status
    if not slack_user_id:
        pvc_badge = '<span class="badge badge-gray">N/A</span>'
    elif not tok_status["linked"]:
        pvc_badge = '<span class="badge badge-red">Missing</span>'
    elif not tok_status.get("valid"):
        pvc_badge = '<span class="badge badge-yellow">Expired</span>'
    else:
        rem = tok_status["remaining_sec"]
        pvc_badge = f'<span class="badge badge-green">Valid — {rem // 60}m remaining</span>'

    alert_html = ""
    if msg:
        alert_html = f'<div class="alert alert-success">✅ {msg}</div>'
    if error:
        alert_html = f'<div class="alert alert-error">⚠️ {error}</div>'

    # Quick links buttons
    quick_link_buttons = []
    if GITEA_URL:
        quick_link_buttons.append(
            f'<a href="{GITEA_URL}" target="_blank" rel="noopener" class="btn-app btn-app-gitea">'
            f'🐙 Open Gitea {_SVG_LINK}</a>'
        )
    if SLACK_URL:
        quick_link_buttons.append(
            f'<a href="{SLACK_URL}" target="_blank" rel="noopener" class="btn-app btn-app-slack">'
            f'💬 Open Slack {_SVG_LINK}</a>'
        )
    quick_link_buttons.append(
        f'<a href="/atlassian-script" class="btn-app btn-app-atlassian">'
        f'🔵 Download Jira Script {_SVG_LINK}</a>'
    )

    quick_links_card = ""
    if quick_link_buttons:
        quick_links_card = f"""
    <div class="card">
      <div class="card-header">🔗 Quick Links</div>
      <div class="card-body">
        <div class="quick-links">{''.join(quick_link_buttons)}</div>
      </div>
    </div>"""

    pat_hint = ""
    if GITEA_URL:
        pat_hint = f'<div class="pat-hint">Generate a PAT in <a href="{GITEA_URL}/user/settings/applications" target="_blank" rel="noopener" class="ext-link" style="font-size:inherit">Gitea → Settings → Applications {_SVG_LINK}</a></div>'

    body = f"""
    {alert_html}
    {quick_links_card}

    <div class="card">
      <div class="card-header">🪪 Identity</div>
      <div class="card-body">
        <div class="row"><span class="label">Name</span><span class="value">{display_name}</span></div>
        <div class="row"><span class="label">Username</span><span class="value">{username}</span></div>
        <div class="row"><span class="label">Email</span><span class="value">{email}</span></div>
        <div class="row"><span class="label">Slack account</span><div>{slack_html}</div></div>
        <div class="row"><span class="label">Hermes token</span>{pvc_badge}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">🐙 Gitea PAT</div>
      <div class="card-body">
        <div class="row">
          <span class="label">Personal Access Token</span>
          <div style="display:flex;align-items:center;gap:10px">
            <span>{_pat_display(gitea_pat)}</span>
            {'<form method="post" action="/test-connection" style="margin:0"><input type="hidden" name="service" value="gitea"><button type="submit" class="btn btn-secondary" style="padding:4px 12px;font-size:0.78rem">Test</button></form>' if gitea_pat else ''}
          </div>
        </div>
        {pat_hint}
        <form method="post" action="/update-pat">
          <input type="hidden" name="service" value="gitea">
          <input type="password" name="pat" placeholder="Paste your Gitea PAT here" autocomplete="off">
          <button type="submit" class="btn btn-primary">Update PAT</button>
        </form>
      </div>
    </div>

    <div class="card">
      <div class="card-header">🔵 Atlassian (Jira)</div>
      <div class="card-body">
        <div class="row">
          <span class="label">OAuth Status</span>
          <div style="display:flex;align-items:center;gap:10px">
            <span>{'<span class="badge badge-green">Connected</span>' if atlassian_oauth_token else '<span class="badge badge-gray">Not connected</span>'}</span>
            {'<form method="post" action="/test-connection" style="margin:0"><input type="hidden" name="service" value="atlassian"><button type="submit" class="btn btn-secondary" style="padding:4px 12px;font-size:0.78rem">Test</button></form>' if atlassian_oauth_token else ''}
          </div>
        </div>
        <div class="pat-hint" style="margin-top:10px">
          <strong>Step 1:</strong> <a href="/atlassian-script" class="ext-link">Download link-atlassian.py {_SVG_LINK}</a>
          and run it on your laptop:<br>
          <code style="display:block;background:#f5f5f4;padding:8px 12px;border-radius:8px;margin:8px 0;font-size:0.82rem">python3 link-atlassian.py</code>
          <strong>Step 2:</strong> Copy the two tokens it prints and paste them below.
        </div>
        <form method="post" action="/atlassian-connect" style="margin-top:14px">
          <input type="password" name="access_token" placeholder="Access token (starts with eyJ...)" autocomplete="off" required>
          <input type="password" name="refresh_token" placeholder="Refresh token (starts with eyJ...)" autocomplete="off" required>
          <button type="submit" class="btn btn-primary">{"Reconnect" if atlassian_oauth_token else "Connect Atlassian"}</button>
        </form>
      </div>
    </div>
    """
    return HTMLResponse(_page_dashboard(f"Dashboard — {display_name}", body, username))


@app.post("/update-pat", response_class=HTMLResponse)
async def update_pat(request: Request, service: str = Form(...), pat: str = Form(...)):
    session = _get_session(request)
    if not session:
        return RedirectResponse("/", status_code=302)

    pat = pat.strip()
    if not pat:
        return RedirectResponse("/dashboard?error=PAT+cannot+be+empty", status_code=302)

    user_sub = session["sub"]
    attr_map = {"gitea": "gitea_pat", "atlassian": "atlassian_pat"}
    if service not in attr_map:
        return RedirectResponse("/dashboard?error=Unknown+service", status_code=302)

    attr_name = attr_map[service]
    stored_value = pat
    if service == "atlassian":
        email = session.get("email", "")
        if not email:
            return RedirectResponse("/dashboard?error=No+email+in+session+—+sign+out+and+back+in", status_code=302)
        stored_value = base64.b64encode(f"{email}:{pat}".encode()).decode()
    try:
        await _update_kc_attribute(user_sub, attr_name, stored_value)
        logger.info("Updated %s for sub=%s", attr_name, user_sub)
        # Refresh session user_info so dashboard shows updated status
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            ui_resp = await client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {session['access_token']}"},
            )
        if ui_resp.status_code == 200:
            session["user_info"] = ui_resp.json()
    except Exception as exc:
        logger.exception("Failed to update %s: %s", attr_name, exc)
        return RedirectResponse(
            f"/dashboard?error=Failed+to+update+token:+{str(exc)[:80]}", status_code=302
        )

    return RedirectResponse("/dashboard?msg=Token+updated+successfully", status_code=302)


@app.get("/logout")
def logout(request: Request):
    """Clear portal session. prompt=login on next /login ensures credentials are required."""
    sid = request.cookies.get("session_id")
    if sid:
        _sessions.pop(sid, None)
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response


# ---------------------------------------------------------------------------
# Atlassian token paste + validate flow
# ---------------------------------------------------------------------------

_ATLASSIAN_SCRIPT_PATH = Path(__file__).parent / "link-atlassian.py"


@app.get("/atlassian-script")
def atlassian_script():
    """Serve link-atlassian.py as a download."""
    if not _ATLASSIAN_SCRIPT_PATH.exists():
        raise HTTPException(404, "Script not found")
    content = _ATLASSIAN_SCRIPT_PATH.read_bytes()
    return Response(
        content=content,
        media_type="text/x-python",
        headers={"Content-Disposition": "attachment; filename=link-atlassian.py"},
    )


@app.post("/atlassian-connect")
async def atlassian_connect(
    request: Request,
    access_token: str = Form(...),
    refresh_token: str = Form(...),
):
    """Validate Atlassian tokens via refresh grant, then store in Keycloak."""
    session = _get_session(request)
    if not session:
        return RedirectResponse("/", status_code=302)

    access_token  = access_token.strip()
    refresh_token = refresh_token.strip()

    if not access_token or not refresh_token:
        return RedirectResponse("/dashboard?error=Both+tokens+are+required", status_code=302)

    # Validate by calling the refresh grant — if it works, both tokens are good.
    # We store the fresh access_token from the response (guaranteed not stale).
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            ATLASSIAN_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "client_id":     ATLASSIAN_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"User-Agent": "mcp-gateway-portal/1.0"},
        )

    if resp.status_code != 200:
        logger.warning("Atlassian token validation failed: %s %s", resp.status_code, resp.text[:200])
        return RedirectResponse(
            f"/dashboard?error=Token+validation+failed+({resp.status_code})+-+did+you+copy+the+tokens+correctly%3F",
            status_code=302,
        )

    fresh = resp.json()
    new_access  = fresh.get("access_token", access_token)  # use refreshed token
    new_refresh = fresh.get("refresh_token", refresh_token)

    try:
        user_sub = session["sub"]
        await _update_kc_attribute(user_sub, "atlassian_oauth_token",   new_access)
        await _update_kc_attribute(user_sub, "atlassian_oauth_refresh",  new_refresh)
        logger.info("Stored Atlassian tokens for sub=%s", user_sub)
        # Refresh session userinfo so dashboard shows updated status immediately
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            ui_resp = await client.get(
                USERINFO_URL, headers={"Authorization": f"Bearer {session['access_token']}"}
            )
        if ui_resp.status_code == 200:
            session["user_info"] = ui_resp.json()
    except Exception as exc:
        logger.exception("Failed to store Atlassian tokens: %s", exc)
        return RedirectResponse(
            f"/dashboard?error=Failed+to+store+tokens:+{str(exc)[:80]}", status_code=302
        )

    return RedirectResponse("/dashboard?msg=Atlassian+connected+successfully", status_code=302)


# ---------------------------------------------------------------------------
# Connection test endpoints
# ---------------------------------------------------------------------------

@app.post("/test-connection")
async def test_connection(request: Request, service: str = Form(...)):
    session = _get_session(request)
    if not session:
        return RedirectResponse("/", status_code=302)

    user_info = session.get("user_info", {})

    if service == "gitea":
        gitea_pat = user_info.get("gitea_pat", "")
        if not gitea_pat:
            return RedirectResponse("/dashboard?error=No+Gitea+PAT+stored", status_code=302)
        if not GITEA_URL:
            return RedirectResponse("/dashboard?error=GITEA_URL+not+configured", status_code=302)
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as client:
                resp = await client.get(
                    f"{GITEA_URL}/api/v1/user",
                    headers={"Authorization": f"token {gitea_pat}"},
                )
            if resp.status_code == 200:
                gitea_user = resp.json().get("login", "unknown")
                return RedirectResponse(
                    f"/dashboard?msg=Gitea+PAT+valid+%E2%80%94+logged+in+as+{gitea_user}",
                    status_code=302,
                )
            else:
                return RedirectResponse(
                    f"/dashboard?error=Gitea+PAT+invalid+(HTTP+{resp.status_code})", status_code=302
                )
        except Exception as exc:
            return RedirectResponse(f"/dashboard?error=Gitea+test+failed:+{str(exc)[:60]}", status_code=302)

    elif service == "atlassian":
        atlassian_token = user_info.get("atlassian_oauth_token", "")
        atlassian_refresh = user_info.get("atlassian_oauth_refresh", "")
        if not atlassian_refresh:
            return RedirectResponse("/dashboard?error=No+Atlassian+token+stored", status_code=302)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    ATLASSIAN_TOKEN_URL,
                    data={
                        "grant_type":    "refresh_token",
                        "client_id":     ATLASSIAN_CLIENT_ID,
                        "refresh_token": atlassian_refresh,
                    },
                    headers={"User-Agent": "mcp-gateway-portal/1.0"},
                )
            if resp.status_code == 200:
                expires_in = resp.json().get("expires_in", "?")
                return RedirectResponse(
                    f"/dashboard?msg=Atlassian+token+valid+%E2%80%94+expires+in+{expires_in}s",
                    status_code=302,
                )
            else:
                return RedirectResponse(
                    f"/dashboard?error=Atlassian+token+invalid+(HTTP+{resp.status_code})+%E2%80%94+re-run+link-atlassian.py",
                    status_code=302,
                )
        except Exception as exc:
            return RedirectResponse(
                f"/dashboard?error=Atlassian+test+failed:+{str(exc)[:60]}", status_code=302
            )

    return RedirectResponse("/dashboard?error=Unknown+service", status_code=302)


# ---------------------------------------------------------------------------
# Legacy /link endpoint (backwards compat — redirects to /login)
# ---------------------------------------------------------------------------

@app.get("/link")
def link_legacy(slack_user: str = Query(None)):
    """Legacy entry point — now users log in via / and get slack_user_id from Keycloak."""
    logger.info("Legacy /link called with slack_user=%s — redirecting to /login", slack_user)
    return RedirectResponse("/login")

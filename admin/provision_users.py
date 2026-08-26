#!/usr/bin/env python3
"""provision_users.py — Bulk-provision Keycloak + Gitea users for the MCP/Hermes portal.

Creates or updates users in Keycloak (slack_user_id, gitea_pat attributes, Gitea tool
roles) and in Gitea (account + PAT).  If gitea_pat is omitted from the YAML, the PAT
is auto-generated via the Gitea admin API and stored back into Keycloak.

Keycloak SSO login into Gitea works automatically on first login because the account is
pre-created with a matching email and ACCOUNT_LINKING=auto is set in the Gitea config.

Usage:
    python3 provision_users.py users.yaml
    python3 provision_users.py users.yaml --dry-run

Dependencies:
    pip install requests pyyaml

Required env vars (or set them in the YAML under 'keycloak:' / 'gitea:'):
    KEYCLOAK_URL     Base Keycloak URL  (e.g. https://sso.apps.cluster-.../  )
    KC_REALM         Realm name         (default: mcp)
    KC_ADMIN_USER    Admin username     (default: temp-admin)
    KC_ADMIN_PASS    Admin password
    GITEA_URL        Base Gitea URL     (e.g. https://gitea-gitea.apps.cluster-.../ )
    GITEA_ADMIN_USER Gitea admin user   (default: opentlc-mgr)
    GITEA_ADMIN_PASS Gitea admin password
"""

import argparse
import json
import os
import secrets
import string
import sys
import urllib.parse

try:
    import requests
    import yaml
except ImportError:
    print("ERROR: Missing dependencies. Run:  pip install requests pyyaml")
    sys.exit(1)

requests.packages.urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# All Gitea tool roles
# ---------------------------------------------------------------------------

ALL_GITEA_ROLES = [
    "tool:actions_config_read", "tool:actions_config_write",
    "tool:actions_run_read", "tool:actions_run_write",
    "tool:attachment_read", "tool:create_branch",
    "tool:create_or_update_file", "tool:create_release",
    "tool:create_repo", "tool:create_tag",
    "tool:delete_branch", "tool:delete_file",
    "tool:delete_release", "tool:delete_tag",
    "tool:fork_repo", "tool:get_commit",
    "tool:get_dir_contents", "tool:get_file_contents",
    "tool:get_gitea_mcp_server_version", "tool:get_latest_release",
    "tool:get_me", "tool:get_release",
    "tool:get_repository_tree", "tool:get_tag",
    "tool:get_user_orgs", "tool:issue_read",
    "tool:issue_write", "tool:label_read",
    "tool:label_write", "tool:list_branches",
    "tool:list_commits", "tool:list_issues",
    "tool:list_my_repos", "tool:list_org_repos",
    "tool:list_pull_requests", "tool:list_releases",
    "tool:list_tags", "tool:milestone_read",
    "tool:milestone_write", "tool:notification_read",
    "tool:notification_write", "tool:package_read",
    "tool:package_write", "tool:pull_request_read",
    "tool:pull_request_review_write", "tool:pull_request_write",
    "tool:search_issues", "tool:search_org_teams",
    "tool:search_repos", "tool:search_users",
    "tool:timetracking_read", "tool:timetracking_write",
    "tool:wiki_read", "tool:wiki_write",
]

# Gitea PAT scopes needed for the MCP gitea-mcp-server tools
GITEA_PAT_SCOPES = [
    "read:repository", "write:repository",
    "read:issue", "write:issue",
    "read:user", "write:user",
    "read:notification", "write:notification",
    "read:package", "write:package",
    "read:organization", "write:organization",
    "read:misc",
]


# ---------------------------------------------------------------------------
# Keycloak admin client
# ---------------------------------------------------------------------------

class KeycloakAdmin:
    def __init__(self, base_url: str, realm: str, admin_user: str, admin_pass: str):
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self._token = self._get_admin_token(admin_user, admin_pass)
        self._session = requests.Session()
        self._session.verify = False
        self._session.headers["Authorization"] = f"Bearer {self._token}"
        self._session.headers["Content-Type"] = "application/json"

    def _get_admin_token(self, user: str, password: str) -> str:
        url = f"{self.base_url}/realms/master/protocol/openid-connect/token"
        resp = requests.post(url, data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": user,
            "password": password,
        }, verify=False, timeout=15)
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _admin(self, path: str) -> str:
        return f"{self.base_url}/admin/realms/{self.realm}/{path.lstrip('/')}"

    def get_user(self, username: str) -> dict | None:
        resp = self._session.get(
            self._admin("users"),
            params={"username": username, "exact": "true"},
        )
        resp.raise_for_status()
        users = resp.json()
        return users[0] if users else None

    def create_user(self, rep: dict) -> str:
        resp = self._session.post(self._admin("users"), json=rep)
        resp.raise_for_status()
        location = resp.headers.get("Location", "")
        return location.rsplit("/", 1)[-1]

    def update_user(self, user_id: str, rep: dict) -> None:
        resp = self._session.put(self._admin(f"users/{user_id}"), json=rep)
        resp.raise_for_status()

    def set_password(self, user_id: str, password: str, temporary: bool = False) -> None:
        resp = self._session.put(
            self._admin(f"users/{user_id}/reset-password"),
            json={"type": "password", "value": password, "temporary": temporary},
        )
        resp.raise_for_status()

    def get_client_id(self, client_id_str: str) -> str | None:
        """Return the internal UUID for a clientId string."""
        resp = self._session.get(
            self._admin("clients"),
            params={"clientId": client_id_str, "first": 0, "max": 1},
        )
        resp.raise_for_status()
        clients = resp.json()
        return clients[0]["id"] if clients else None

    def get_client_roles(self, client_uuid: str) -> dict:
        """Return {role_name: role_rep} for all roles of a client."""
        resp = self._session.get(self._admin(f"clients/{client_uuid}/roles"))
        resp.raise_for_status()
        return {r["name"]: r for r in resp.json()}

    def assign_client_roles(self, user_id: str, client_uuid: str, roles: list[dict]) -> None:
        resp = self._session.post(
            self._admin(f"users/{user_id}/role-mappings/clients/{client_uuid}"),
            json=roles,
        )
        resp.raise_for_status()

    def get_user_client_roles(self, user_id: str, client_uuid: str) -> list[str]:
        resp = self._session.get(
            self._admin(f"users/{user_id}/role-mappings/clients/{client_uuid}")
        )
        resp.raise_for_status()
        return [r["name"] for r in resp.json()]

    def add_user_to_group(self, user_id: str, group_name: str) -> None:
        resp = self._session.get(
            self._admin("groups"), params={"search": group_name, "exact": "true"}
        )
        resp.raise_for_status()
        groups = resp.json()
        if not groups:
            print(f"    WARNING: group '{group_name}' not found, skipping")
            return
        group_id = groups[0]["id"]
        resp = self._session.put(self._admin(f"users/{user_id}/groups/{group_id}"))
        resp.raise_for_status()

    def ensure_user_profile_attribute(self, attr_name: str) -> None:
        """Declare attr_name in user profile so Keycloak doesn't drop it."""
        resp = self._session.get(self._admin("users/profile"))
        resp.raise_for_status()
        profile = resp.json()
        existing = {a["name"] for a in profile.get("attributes", [])}
        if attr_name not in existing:
            profile.setdefault("attributes", []).append({
                "name": attr_name,
                "displayName": attr_name,
                "permissions": {"view": ["admin"], "edit": ["admin"]},
                "multivalued": False,
            })
            resp = self._session.put(self._admin("users/profile"), json=profile)
            resp.raise_for_status()
            print(f"  + Declared user profile attribute: {attr_name}")
        # Ensure unmanagedAttributePolicy allows admin editing
        if profile.get("unmanagedAttributePolicy") != "ADMIN_EDIT":
            profile["unmanagedAttributePolicy"] = "ADMIN_EDIT"
            resp = self._session.put(self._admin("users/profile"), json=profile)
            resp.raise_for_status()
            print("  + Set unmanagedAttributePolicy: ADMIN_EDIT")


# ---------------------------------------------------------------------------
# Gitea admin client
# ---------------------------------------------------------------------------

class GiteaAdmin:
    def __init__(self, base_url: str, admin_user: str, admin_pass: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.verify = False
        self._session.auth = (admin_user, admin_pass)
        self._session.headers["Content-Type"] = "application/json"

    def _api(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def get_user(self, username: str) -> dict | None:
        resp = self._session.get(self._api(f"users/{username}"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def create_user(self, username: str, email: str, full_name: str, password: str,
                    source_id: int = 0, login_name: str = "") -> dict:
        """Create a Gitea user. Set source_id to the Keycloak auth source ID and
        login_name to the Keycloak user UUID (sub) to create the external_login_user
        link automatically — SSO works on first login with no link page shown."""
        resp = self._session.post(self._api("admin/users"), json={
            "email": email,
            "full_name": full_name,
            "login_name": login_name or username,
            "must_change_password": False,
            "password": password,
            "send_notify": False,
            "source_id": source_id,
            "username": username,
            "visibility": "public",
        })
        resp.raise_for_status()
        return resp.json()

    def update_user(self, username: str, email: str, full_name: str,
                    source_id: int = 0, login_name: str = "") -> None:
        """Update profile fields for an existing user."""
        resp = self._session.patch(self._api(f"admin/users/{username}"), json={
            "email": email,
            "full_name": full_name,
            "login_name": login_name or username,
            "must_change_password": False,
            "source_id": source_id,
        })
        resp.raise_for_status()

    def list_tokens(self, username: str) -> list[dict]:
        resp = self._session.get(self._api(f"users/{username}/tokens"))
        resp.raise_for_status()
        return resp.json()

    def delete_token(self, username: str, token_name: str) -> None:
        resp = self._session.delete(self._api(f"users/{username}/tokens/{token_name}"))
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    def create_token(self, username: str, token_name: str = "mcp-gateway") -> str:
        """Create a PAT for username and return the raw token string."""
        # Delete existing token with same name to allow re-generation
        existing = [t for t in self.list_tokens(username) if t["name"] == token_name]
        if existing:
            self.delete_token(username, token_name)
            print(f"  Replaced existing Gitea token '{token_name}'")

        resp = self._session.post(self._api(f"users/{username}/tokens"), json={
            "name": token_name,
            "scopes": GITEA_PAT_SCOPES,
        })
        resp.raise_for_status()
        return resp.json()["sha1"]


# ---------------------------------------------------------------------------
# Provision logic
# ---------------------------------------------------------------------------

def _random_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def provision_user(kc: KeycloakAdmin, gitea: GiteaAdmin | None, gitea_source_id: int,
                   u: dict, dry_run: bool) -> None:
    username = u["username"]
    print(f"\n{'[DRY RUN] ' if dry_run else ''}User: {username}")

    email = u.get("email", f"{username}@example.com")
    full_name = f"{u.get('first_name', username)} {u.get('last_name', '')}".strip()
    gitea_pat = u.get("gitea_pat")  # explicit override in YAML

    if dry_run:
        print(f"  Would create/update KC user: {username} ({email})")
        if gitea is not None:
            print(f"  Would ensure Gitea account '{username}' with KC SSO link (source_id={gitea_source_id})")
            if not gitea_pat:
                print(f"  Would auto-generate Gitea PAT 'mcp-gateway' and store in Keycloak")
        if u.get("gitea_roles"):
            roles = ALL_GITEA_ROLES if u["gitea_roles"] == "all" else u["gitea_roles"]
            print(f"  Would assign {len(roles)} Gitea roles")
        return

    # ---- Keycloak user (first — we need kc_sub for Gitea linking) -----------
    user_rep = {
        "username": username,
        "email": email,
        "emailVerified": True,
        "enabled": True,
        "firstName": u.get("first_name", username),
        "lastName": u.get("last_name", ""),
        "attributes": {},
    }

    existing_kc = kc.get_user(username)
    if existing_kc:
        kc_sub = existing_kc["id"]
        merged_attrs = dict(existing_kc.get("attributes") or {})
        user_rep["attributes"] = merged_attrs
        kc.update_user(kc_sub, user_rep)
        print(f"  KC user updated (sub={kc_sub})")
    else:
        kc_sub = kc.create_user(user_rep)
        print(f"  KC user created (sub={kc_sub})")

    if u.get("password"):
        kc.set_password(kc_sub, u["password"], temporary=u.get("password_temporary", False))
        print(f"  Password set (temporary={u.get('password_temporary', False)})")

    for group in u.get("groups", ["mcp-users"]):
        kc.add_user_to_group(kc_sub, group)
        print(f"  Added to group: {group}")

    # ---- Gitea account + PAT (after KC so we have kc_sub) -------------------
    if gitea is not None:
        existing_gitea = gitea.get_user(username)
        if existing_gitea:
            gitea.update_user(username, email, full_name,
                              source_id=gitea_source_id, login_name=kc_sub)
            print(f"  Gitea account updated")
        else:
            gitea.create_user(username, email, full_name, _random_password(),
                              source_id=gitea_source_id, login_name=kc_sub)
            print(f"  Gitea account created (linked to KC sub={kc_sub[:8]}...)")

        if not gitea_pat:
            gitea_pat = gitea.create_token(username)
            print(f"  Gitea PAT generated: {'*' * 8}{gitea_pat[-4:]}")

    # ---- Update KC attributes (gitea_pat, slack_user_id) --------------------
    new_attrs: dict = {}
    if u.get("slack_user_id"):
        new_attrs["slack_user_id"] = [u["slack_user_id"]]
    if gitea_pat:
        new_attrs["gitea_pat"] = [gitea_pat]

    if new_attrs:
        existing_kc2 = kc.get_user(username)
        merged = dict((existing_kc2 or {}).get("attributes") or {})
        merged.update(new_attrs)
        user_rep2 = dict(user_rep)
        user_rep2["attributes"] = merged
        kc.update_user(kc_sub, user_rep2)
        print(f"  KC attributes updated")

    # ---- Gitea roles --------------------------------------------------------
    gitea_roles_spec = u.get("gitea_roles")
    if gitea_roles_spec:
        client_id_str = "mcp-test/gitea-mcp-server"
        client_uuid = kc.get_client_id(client_id_str)
        if not client_uuid:
            print(f"  WARNING: client '{client_id_str}' not found, skipping roles")
        else:
            available = kc.get_client_roles(client_uuid)
            wanted = ALL_GITEA_ROLES if gitea_roles_spec == "all" else gitea_roles_spec
            already = set(kc.get_user_client_roles(kc_sub, client_uuid))
            to_add = [available[r] for r in wanted if r in available and r not in already]
            if to_add:
                kc.assign_client_roles(kc_sub, client_uuid, to_add)
                print(f"  Assigned {len(to_add)} Gitea roles")
            else:
                print(f"  Gitea roles already up to date")

    if u.get("slack_user_id"):
        print(f"  slack_user_id: {u['slack_user_id']}")
    if gitea_pat:
        print(f"  gitea_pat stored: {'*' * 8}{gitea_pat[-4:]}")


def main():
    parser = argparse.ArgumentParser(description="Provision Keycloak + Gitea users for MCP portal")
    parser.add_argument("users_file", help="Path to users YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen, don't apply")
    parser.add_argument("--skip-gitea", action="store_true", help="Skip Gitea provisioning (KC only)")
    args = parser.parse_args()

    with open(args.users_file) as f:
        config = yaml.safe_load(f)

    # Keycloak config
    kc_cfg = config.get("keycloak", {})
    base_url = kc_cfg.get("url") or os.environ.get("KEYCLOAK_URL", "").rstrip("/")
    realm    = kc_cfg.get("realm") or os.environ.get("KC_REALM", "mcp")
    adm_user = kc_cfg.get("admin_user") or os.environ.get("KC_ADMIN_USER", "temp-admin")
    adm_pass = kc_cfg.get("admin_password") or os.environ.get("KC_ADMIN_PASS", "")

    if not base_url:
        print("ERROR: KEYCLOAK_URL not set (env var or keycloak.url in YAML)")
        sys.exit(1)
    if not adm_pass:
        print("ERROR: KC_ADMIN_PASS not set (env var or keycloak.admin_password in YAML)")
        sys.exit(1)

    # Gitea config
    gt_cfg = config.get("gitea", {})
    gitea_url       = gt_cfg.get("url") or os.environ.get("GITEA_URL", "")
    gitea_user      = gt_cfg.get("admin_user") or os.environ.get("GITEA_ADMIN_USER", "opentlc-mgr")
    gitea_pass      = gt_cfg.get("admin_password") or os.environ.get("GITEA_ADMIN_PASS", "")
    # auth_source_id: the Gitea login_source ID for the Keycloak auth source (default 1).
    # Check: SELECT id, name FROM login_source WHERE type=6; in Gitea's DB.
    gitea_source_id = int(gt_cfg.get("auth_source_id", 1))

    print(f"Connecting to Keycloak {base_url} realm={realm} as {adm_user}")
    kc = KeycloakAdmin(base_url, realm, adm_user, adm_pass)

    gitea: GiteaAdmin | None = None
    if not args.skip_gitea:
        if gitea_url and gitea_pass:
            print(f"Connecting to Gitea {gitea_url} as {gitea_user} (auth_source_id={gitea_source_id})")
            gitea = GiteaAdmin(gitea_url, gitea_user, gitea_pass)
        else:
            print("NOTE: No Gitea config found — skipping Gitea provisioning (use --skip-gitea to suppress this)")

    # Ensure user profile attributes are declared in Keycloak
    if not args.dry_run:
        print("\nEnsuring Keycloak user profile attributes...")
        kc.ensure_user_profile_attribute("slack_user_id")
        kc.ensure_user_profile_attribute("gitea_pat")

    users = config.get("users", [])
    print(f"\nProvisioning {len(users)} user(s)...")
    for u in users:
        try:
            provision_user(kc, gitea, gitea_source_id, u, dry_run=args.dry_run)
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print("\nDone.")


if __name__ == "__main__":
    main()

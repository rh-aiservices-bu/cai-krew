# Google IdP Integration with RHBK (Keycloak)

Step-by-step guide to configure Google Workspace as an Identity Provider in a RHBK realm, allowing users from your organization to log in with their corporate Google accounts.

## Why

The MCP Gateway authenticates users via Keycloak JWTs. Instead of managing local passwords, we configure Google Workspace as an Identity Provider so users can log in with their existing corporate accounts. Google acts as the identity provider - Keycloak handles the JWT issuance for the MCP Gateway.

This is also a prerequisite for the Atlassian OAuth flow: users authenticate via Google, then link their Atlassian account separately for per-user Jira tool access.

## Prerequisites

- RHBK 26.2 deployed (operator version `26.2.16-opr.1` or later)
- Keycloak realm already imported (e.g. `mcp` realm)
- Access to the [Google Cloud Console](https://console.cloud.google.com/) with permissions to manage OAuth credentials
- Access to the Keycloak admin console

## Architecture

```
User clicks "Sign in with Google" on Keycloak login page
  -> Keycloak redirects to Google OAuth
    -> Google detects @example.com -> redirects to corporate SSO (if configured)
      -> User authenticates
        -> Google returns OIDC token to Keycloak callback
          -> Keycloak auto-links to existing user (by email) or creates new user
            -> Keycloak issues JWT with user claims
              -> User is logged in
```

The same flow works from any app that redirects to the Keycloak login page (e.g. an auth-ui portal or MCP Inspector). Keycloak shows the Google login button regardless of where the login was initiated from.

## Step 1: Configure Google Cloud Console

### 1.1 Open the OAuth Client

Go to the Google Cloud Console OAuth credentials page:

- URL: https://console.cloud.google.com/apis/credentials
- Select your GCP project

Find your OAuth 2.0 Client ID (or create a new one if needed).

### 1.2 Add the Keycloak Callback URI

In the OAuth client settings, add this redirect URI under "Authorized redirect URIs":

```
https://<KEYCLOAK_HOST>/realms/<REALM>/broker/google/endpoint
```

For example:
```
https://sso.apps.<CLUSTER_DOMAIN>/realms/mcp/broker/google/endpoint
```

> **IMPORTANT**: The `google` part of the URI must match the IdP **alias** you set in Keycloak (Step 2). If you use a different alias (like `google-oidc`), the callback URL changes to `.../broker/google-oidc/endpoint` and you need to register THAT in Google Cloud Console instead. Mismatched alias = redirect_uri_mismatch error.

Save the changes.

## Step 2: Configure Google IdP in Keycloak

### 2.1 Navigate to Identity Providers

Go to the Keycloak admin console:

```
https://<KEYCLOAK_HOST>/admin/master/console/#/<REALM>/identity-providers
```

Make sure you select the correct realm (not `master`).

### 2.2 Add a Google Social Provider

Click "Add provider" and select **Google** from the Social providers section.

> **Note**: We use the built-in "Google" social provider, NOT "OpenID Connect v1.0". The Google social provider handles Google-specific configuration automatically (discovery URL, hosted domain restriction, etc).

### 2.3 Fill in the Provider Settings

| Setting | Value | Why |
|---------|-------|-----|
| Alias | `google` | Determines the callback URI: `/realms/<REALM>/broker/google/endpoint`. Must match what you registered in Google Cloud Console |
| Display Name | `Google` | Shows as "Sign in with Google" on the login page |
| Client ID | `<your-google-oauth-client-id>` | From Step 1.1 |
| Client Secret | `<your-google-oauth-client-secret>` | From the Google Cloud Console OAuth client |
| Hosted Domain | `example.com` | Restricts login to @example.com accounts only. Users with personal Gmail accounts will be rejected. Replace with your organization's domain |

### 2.4 Advanced Settings

| Setting | Value | Why |
|---------|-------|-----|
| Trust Email | **ON** | Google has already verified corporate emails - no need for Keycloak to re-verify |
| Store Tokens | **OFF** | We only use Google for login, not for calling Google APIs. No need to store Google access tokens |
| Request Refresh Token | **OFF** | Same reason - no Google API calls needed |
| Stored Tokens Readable | **OFF** | Nothing to read since we don't store tokens |
| Sync Mode | `legacy` | Default sync behavior for user attributes |

### 2.5 Set the First Broker Login Flow

Under "First login flow", select **auto-link-broker** instead of the default `first broker login`.

> If the `auto-link-broker` flow doesn't exist yet, create it first (Step 3 below), then come back and set it here.

## Step 3: Create the auto-link-broker Authentication Flow

### 3.1 Why This Flow is Needed

The default `first broker login` flow tries to verify the user's email when they log in for the first time via Google. This requires Keycloak to send a verification email, which needs SMTP configured.

**Problem**: If SMTP is not configured in your realm, the email verification step silently fails and the user gets stuck on an error page.

**Solution**: The `auto-link-broker` flow skips email verification entirely. Since we set `trustEmail: true` on the Google IdP (and Google has already verified corporate emails via your organization's directory), this is safe. The flow auto-links the Google identity to an existing Keycloak user that has the same email.

> If you DO have SMTP configured in your realm, you can skip this step and use the default `first broker login` flow. The auto-link flow is only needed when SMTP is unavailable.

### 3.2 Create the Flow in the Admin Console

Go to:
```
https://<KEYCLOAK_HOST>/admin/master/console/#/<REALM>/authentication
```

Click "Create flow" and configure:

- **Name**: `auto-link-broker`
- **Description**: `Auto-links broker accounts by email without password verification. Required when SMTP is not configured.`
- **Flow type**: `basic-flow`
- **Top level**: yes

Add two execution steps in this exact order:

| Step | Authenticator | Requirement | Priority |
|------|--------------|-------------|----------|
| 1 | Detect Existing Broker User (`idp-detect-existing-broker-user`) | REQUIRED | 0 |
| 2 | Automatically Set Existing User (`idp-auto-link`) | REQUIRED | 1 |

> **IMPORTANT**: Do NOT add any other steps (like "Confirm Link Existing Account" or "Verify Existing Account by Email"). Those would require user interaction or SMTP. The whole point is to auto-link silently by email match.

### 3.3 Assign the Flow to the Google IdP

Go back to the Google IdP settings (Identity Providers -> Google) and set "First login flow" to `auto-link-broker`.

## Step 4: Update the Realm Import YAML (GitOps)

To make this reproducible, the realm import YAML (`KeycloakRealmImport` CR) needs to include the Google IdP and the auto-link-broker flow.

### 4.1 Add the Identity Provider

Add this block under `spec.realm`:

```yaml
    identityProviders:
      - alias: google
        displayName: Google
        providerId: google
        enabled: true
        trustEmail: true
        storeToken: false
        addReadTokenRoleOnCreate: false
        linkOnly: false
        firstBrokerLoginFlowAlias: auto-link-broker
        config:
          clientId: "${GOOGLE_CLIENT_ID}"
          clientSecret: "${GOOGLE_CLIENT_SECRET}"
          hostedDomain: example.com
          useUserIpParam: "false"
          offlineAccess: "false"
          syncMode: LEGACY
```

> The `${GOOGLE_CLIENT_ID}` and `${GOOGLE_CLIENT_SECRET}` are Keycloak environment variable references. You need to set these as env vars on the Keycloak StatefulSet. They are NOT shell `envsubst` variables - Keycloak resolves them at import time.

### 4.2 Add the Authentication Flow

Add this block under `spec.realm`:

```yaml
    authenticationFlows:
      - alias: auto-link-broker
        description: Auto-links broker accounts by email without password verification. Required when SMTP is not configured.
        providerId: basic-flow
        topLevel: true
        builtIn: false
        authenticationExecutions:
          - authenticator: idp-detect-existing-broker-user
            authenticatorFlow: false
            requirement: REQUIRED
            priority: 0
          - authenticator: idp-auto-link
            authenticatorFlow: false
            requirement: REQUIRED
            priority: 1
```

### 4.3 Add Redirect URIs (if needed)

If your frontend apps need to initiate login flows, add their redirect URIs to your public Keycloak client:

```yaml
    clients:
      - clientId: mcp-gateway
        # ... existing config ...
        redirectUris:
          - "http://localhost:*"
          - "http://127.0.0.1:*"
          - "https://localhost:*"
          - "https://<YOUR_APP_HOST>/*"
```

### 4.4 Set the Google Credentials as Keycloak Env Vars

Create a Secret with the Google OAuth credentials and mount them as environment variables on the Keycloak pod:

```bash
oc create secret generic google-idp-credentials -n <KEYCLOAK_NAMESPACE> \
  --from-literal=GOOGLE_CLIENT_ID="<your-google-client-id>" \
  --from-literal=GOOGLE_CLIENT_SECRET="<your-google-client-secret>"
```

Then reference it in the Keycloak CR or StatefulSet as `envFrom` or individual `env` entries.

## Step 5: Verify

### 5.1 Check the IdP Shows on the Login Page

Open the Keycloak login page for your realm:

```
https://<KEYCLOAK_HOST>/realms/<REALM>/account
```

Or any app that redirects to Keycloak login. You should see a "Sign in with Google" button below the username/password form.

### 5.2 Test the Login Flow

1. Click "Sign in with Google"
2. Google shows the account picker (or redirects to your corporate SSO if a hosted domain is configured)
3. Authenticate with your corporate credentials
4. Google returns to Keycloak
5. Keycloak auto-links your identity (if a user with matching email exists) or creates a new user
6. You are logged in and redirected back to the application

### 5.3 Verify in the Admin Console

After a successful login, check the user was created/linked in the Keycloak admin console:

```
Admin Console -> <realm> -> Users -> search by email
```

The user should have a "Federated Identities" entry showing `google` as the identity provider.

### 5.4 Verify Token Claims

Get a token and check the claims (for local test users only - Google-linked users must use the browser PKCE flow):

```bash
export KEYCLOAK_URL="https://<KEYCLOAK_HOST>"

curl -sk -X POST "${KEYCLOAK_URL}/realms/<REALM>/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=mcp-gateway" \
  -d "username=<test-user>" \
  -d "password=<test-password>" \
  -d "scope=openid groups roles" | jq '.access_token' -r | cut -d'.' -f2 | base64 -d 2>/dev/null | jq '.'
```

> Google-linked users can't use the password grant (they don't have a local password). They need to go through the browser-based PKCE flow.

## Troubleshooting

### "redirect_uri_mismatch" Error from Google

The redirect URI in Google Cloud Console doesn't match the Keycloak callback URL. Check:
- The IdP alias in Keycloak (it determines the callback path)
- The redirect URI registered in Google Cloud Console
- They must match exactly: `https://<KEYCLOAK_HOST>/realms/<REALM>/broker/<ALIAS>/endpoint`

### "Something went wrong" on First Login

Usually means the first broker login flow is failing. Most common cause: the default flow tries to send a verification email but SMTP is not configured. Fix: switch the IdP's first login flow to `auto-link-broker` (Step 3).

### Account Console Shows "Something went wrong"

The React-based account console in RHBK 26.2 has known issues in certain realm configurations. This doesn't affect the login flow itself - users can still log in via applications that redirect to Keycloak. Bypass the account console and test login through your application or MCP Inspector instead.

### User Created but Not Linked

If the auto-link flow doesn't find a matching user by email, it creates a new one. If you expected it to link to an existing user, check:
- The existing user's email matches exactly (case-sensitive)
- The existing user has `emailVerified: true`
- The flow is `auto-link-broker`, not the default `first broker login`

## References

- [RHBK Identity Brokering Docs](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.0/html/server_administration_guide/identity_broker)
- [Google Cloud Console - OAuth Credentials](https://console.cloud.google.com/apis/credentials)
- [Keycloak Google Social Provider](https://www.keycloak.org/docs/latest/server_admin/#google)
- [Keycloak Authentication Flows](https://www.keycloak.org/docs/latest/server_admin/#_authentication-flows)

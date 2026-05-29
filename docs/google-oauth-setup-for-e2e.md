# Google OAuth Setup for AgentCore E2E Workshop

## What this guide is for
- setting up Google OAuth credentials for the current E2E workshop flow;
- supplying `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` for the demo notebook and runtime;
- understanding how Google Docs outbound OAuth fits into AgentCore Gateway.

Current primary flow:
- `workshop_google_docs_rag_e2e.ipynb`
- `runtime_app_agentcore_full.py`

## Current workshop flow
1. `Step 1` in the notebook creates inbound Cognito auth for bearer JWT calls.
2. `Step 2` creates an AgentCore OAuth provider, Gateway, and Google Docs OpenAPI target.
3. `Step 4` deploys `runtime_app_agentcore_full.py` through the documented `agentcore configure/deploy` CLI path.
4. The runtime uses LangChain `create_agent` with exactly one tool: `get_google_doc`.
5. That tool calls the Gateway over MCP `tools/call`, which triggers Google OAuth when needed.
6. `Step 5` performs the first runtime invoke, browser consent if required, and then a second invoke with the same OAuth session.
7. When consent completes, the browser should land on the local callback URL and show a simple `Consent complete` page before you return to the notebook.

This flow does not use a custom StateGraph in the deployed runtime, does not use Lambda for Google Docs, and does not do chunk-ranking or retrieval over multiple sources.

---

## 0) Prerequisites
- Existing AWS profile and region (`us-east-1` in your workshop).
- Existing E2E workshop repo with `.env`.
- Google Cloud project with Google Docs API enabled.
- A Google account that can open the target document.

---

## 1) Create Google OAuth client (Google Cloud Console)

### Goal
Create a Google OAuth app for a web application and get two values:
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

Do not finalize redirect URIs yet. First create the AgentCore credential provider in the notebook `Step 2`, then copy the AgentCore `callbackUrl` back into Google Cloud using Section `2` below.

### Step-by-step for juniors

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. In the top navigation bar, click the current project selector.
3. If you already have a project for the workshop, select it.
4. If you do not have a project yet:
   - click `NEW PROJECT`;
   - enter a name such as `agentcore-workshop`;
   - click `CREATE`;
   - wait until Google switches you into that project.

### 1.1 Enable Google Docs API
1. In the left menu, open `APIs & Services` -> `Library`.
2. Search for `Google Docs API`.
3. Open it and click `Enable`.

Recommended:
1. Also search for `Google Drive API`.
2. Enable it too.

Why:
- for this workshop we read Google Docs directly through the Google Docs API;
- enabling Drive API is optional but can simplify troubleshooting in some Google Workspace environments.

### 1.2 Configure OAuth consent screen
1. Open `APIs & Services` -> `OAuth consent screen`.
2. Choose user type:
   - `External` if you are using a personal Gmail account;
   - `Internal` only if you are inside a Google Workspace organization and understand the restriction.
3. Click `Create`.
4. Fill the basic fields:
   - `App name`: for example `AgentCore Workshop`;
   - `User support email`: your email;
   - `Developer contact information`: your email.
5. Save and continue.

If Google asks for extra sections such as scopes or branding:
1. Keep the form minimal.
2. Save the default configuration unless the screen explicitly requires something.

Important:
- if the app is in `Testing` mode and user type is `External`, only listed test users can sign in;
- add your own Google account under `Test users` if Google shows that step.

### 1.3 Create OAuth client credentials
1. Open `APIs & Services` -> `Credentials`.
2. Click `+ CREATE CREDENTIALS`.
3. Choose `OAuth client ID`.
4. For `Application type`, choose `Web application`.
5. Enter a name such as `agentcore-google-docs-workshop`.

For now:
- leave `Authorized JavaScript origins` empty unless your organization requires them;
- leave `Authorized redirect URIs` empty for the moment.

Why we leave redirect URIs empty:
- AgentCore generates the correct callback URL in Step 2;
- you will copy that exact value into Google Cloud in Section `2` below.

6. Click `Create`.
7. Copy the generated values:
   - `Client ID`
   - `Client secret`

### 1.4 Put the values into `.env`
Add them to your local `.env`:

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

### 1.5 Sanity check before returning to the workshop
Before moving on, verify:
1. You are in the correct Google Cloud project.
2. `Google Docs API` is enabled.
3. OAuth consent screen exists.
4. OAuth client type is `Web application`.
5. You saved `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

Do not worry yet if redirect URIs are still empty. That is expected at this stage.

---

## 2) Add the AgentCore callback URL to Google Cloud

After you run notebook `Step 2 - Outbound provider + Gateway`, the notebook prints:
- `provider_arn`
- `provider_name`
- `callback_url`

Take that exact `callback_url` value and add it to your Google OAuth client:

1. Open `Google Auth Platform` -> `Clients`.
2. Open the OAuth client you created in Section `1`.
3. In `Authorized redirect URIs`, add the exact `callback_url` printed by the notebook.
4. Save the client.

Important:
- use the exact value, character-for-character;
- if you recreate the AgentCore OAuth provider and the callback changes, update the Google client again;
- until this redirect URI is registered, the Google consent flow in notebook `Step 5` will fail.
- the localhost callback is expected for this workshop, and it should now render a success page rather than a browser connection error.

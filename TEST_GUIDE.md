# Test Guide for AgentCore E2E Deployment

## Purpose
This guide explains how to validate the production-ready AgentCore E2E solution against the updated test cases.
It covers:
- step-by-step execution of tests;
- using dataset categories for coverage;
- expected results per dataset;
- interpreting failures and deviations.

## Scope
The test guide covers the same architecture required by `HW_ASSIGNMENT.md`:
- AgentCore Runtime deployed via `BedrockAgentCoreApp`, `@app.entrypoint`, and `app.run()`;
- Inbound auth with Cognito JWT through the Gateway;
- Gateway-to-runtime proxying;
- Outbound OAuth access to Google Docs;
- Observability and tracing for runtime and tool calls.

## Prerequisites
1. Confirm the repository is checked out and active in the working directory.
2. Copy `.env.example` to `.env` and fill in required values.
   Minimum required values:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_DOC_ID`
   - `AWS_PROFILE`
   - `AWS_REGION`
   - any Cognito/JWT-related configuration used by the Gateway.
3. Install Python dependencies in the virtual environment:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```
4. Verify the Google account used for OAuth consent has access to the target Google Doc.
5. Ensure the runtime app is prepared for deploy from `local/runtime_direct_code_deploy/runtime_app_agentcore_full.py`.
6. Verify the environment includes valid Cognito JWT values or a method to generate them.

## Step-by-step test runbook

### Step-by-Step Test Execution
1. Prepare the environment: ensure `.env` is filled, dependencies are installed, and the Google Doc access is configured.
2. Deploy the runtime using `workshop_google_docs_rag_e2e.ipynb` Step 4 and confirm the runtime is accessible through the Gateway.
3. Execute the full `test_cases.md` suite in order: Authorization, Runtime tool-calling, Consent/path behavior, Failure/fallback.
4. For each case, capture the request payload, HTTP status, response body, trace ID, and relevant log entries.
5. Document test outcomes in `EVIDENCE.md` and preserve at least two distinct invocation traces: one initial consent challenge and one successful tool-backed response.

### Step 1 — Setup and deploy runtime
1. Open `workshop_google_docs_rag_e2e.ipynb` and follow the notebook setup steps.
2. Complete `Step 4` to prepare the runtime source directory and deploy the runtime using AgentCore CLI.
3. Confirm the runtime is reachable through the Gateway endpoint.
4. If OAuth consent is required, start the notebook’s local callback server and verify callback URL availability.

### Step 2 — Validate connectivity and baseline auth
1. Use the Gateway endpoint to send a simple authenticated request.
2. Confirm the request reaches the runtime and returns a basic response.
3. If the runtime is not reachable, verify Gateway configuration and `allowedClients` settings.

### Step 3 — Execute Level 1 test cases
Run the `test_cases.md` scenarios in order:
- Authorization tests;
- Runtime tool-calling tests;
- Consent/path behavior tests;
- Failure and fallback tests.

Record each test’s HTTP status, response body, trace IDs, and log evidence.

### Step 4 — Collect observability evidence
1. Inspect logs/traces for each runtime invocation.
2. For tool-calling tests, validate that a tool-call trace/span exists and contains the tool name, status, and duration.
3. For consent flows, collect evidence of the consent challenge and successful reuse or refresh.
4. Save at least two invocations showing different outcomes (first consent challenge and second successful tool call).

### Step 5 — Cleanup
1. Remove any resources created during runtime deployment.
2. Delete temporary files or local state used for consent testing.
3. Confirm the environment returns to a clean state.
4. Document cleanup commands and results in `EVIDENCE.md` or an equivalent file.

## Dataset strategy
Use dataset categories to cover each test area systematically. Each category should include a set of test vectors for the relevant scenario.

### Auth dataset
Purpose: verify inbound auth and negative auth handling.
Values:
- `valid-jwt` — valid Cognito token with correct claims.
- `expired-jwt` — token with past expiration.
- `malformed-jwt` — invalid JWT format.
- `missing-auth` — no Authorization header.

Expected behavior:
- `valid-jwt` → HTTP 200;
- `expired-jwt` → HTTP 401;
- `malformed-jwt` → HTTP 401;
- `missing-auth` → HTTP 401;

Validation:
- Check Gateway response status and error message.
- Confirm runtime is not invoked for rejected auth cases.
- Review logs for authentication decision and failure reason.

### Tool dataset
Purpose: validate tool calling and result sourcing.
Values:
- `simple-tool-call` — prompt that requires reading from Google Docs.
- `multi-step-tool-call` — prompt that exercises tool chaining or multiple internal tool calls.
- `invalid-tool-request` — malformed tool invocation, wrong tool name, or missing required parameters.

Expected behavior:
- `simple-tool-call` → success with `sources` in response;
- `multi-step-tool-call` → all tool steps completed and correct final result;
- `invalid-tool-request` → 4xx/controlled error with clear explanation.

Validation:
- Confirm response body contains `sources` when tool call succeeds.
- Inspect trace/span metadata for tool call names and results.
- Check error handling in gateway/runtime logs when the tool request is invalid.

### Consent dataset
Purpose: validate consent lifecycle and consent state handling.
Values:
- `fresh-consent` — first invoke without existing consent.
- `existing-consent` — second invoke with valid stored consent.
- `expired-consent` — invoke with expired refresh/access token.

Expected behavior:
- `fresh-consent` → consent challenge or OAuth redirect is returned;
- `existing-consent` → request processed without a new consent challenge;
- `expired-consent` → refresh flow attempted or user asked to reauthorize;

Validation:
- Record first invoke behavior and second invoke result.
- Verify `sources` appear in successful post-consent responses.
- Confirm logs report consent decision, refresh attempt, and any token error.

### Failure dataset
Purpose: verify fallback handling and error transparency.
Values:
- `google-api-down` — target unavailable or returns 5xx.
- `missing-document` — request references a non-existent Google Doc.
- `malformed-body` — invalid JSON or malformed request payload.
- `runtime-internal-error` — inject or simulate runtime internal failure.

Expected behavior:
- `google-api-down` → controlled error/fallback response, no crash;
- `missing-document` → 404/NOT_FOUND with user-friendly message;
- `malformed-body` → 400 Bad Request with validation details;
- `runtime-internal-error` → 500/502 with safe error messaging.

Validation:
- Confirm trace/logs capture the failure and fallback path.
- Ensure error responses do not leak internal stack traces.
- Validate the runtime remains responsive after fallback.

## Expected results by dataset
| Dataset | Goal | Expected result | Evidence source |
|---|---|---|---|
| Auth | Secure inbound auth | 200/401 per scenario | HTTP status + auth logs |
| Tool | Valid tool calling and sourcing | Successful `sources` responses | Response body + trace span |
| Consent | Repeatable OAuth consent flow | consent challenge + reuse | OAuth redirect + runtime logs |
| Failure | Controlled fallback behavior | 4xx/5xx with safe error | Error payload + observability |

## Error interpretation
- `401` indicates an auth token validation problem or missing auth header.
- Missing `sources` on a successful tool call indicates a runtime response formatting or tool metadata issue.
- A consent flow returned as success on the first invoke usually means the runtime already had consent cached.
- `500/502` from the gateway suggests runtime or tool execution failed; inspect runtime logs and the gateway routing policy.
- `NOT_FOUND` or `404` for Google Docs access means the document ID or permission context is invalid.

## Observability checks
1. Capture trace IDs for each test.
2. Verify tool-call spans include the tool name, status, and duration.
3. Confirm auth and consent decisions are logged with the user identity and token validation result.
4. Save at least two evidentiary runs: one before consent and one after consent.


# AgentCore E2E Test Cases

Scope: AgentCore Runtime + Inbound Cognito JWT auth + Gateway + Outbound Google Docs tool + Observability

Notes:
- These test cases cover positive and negative auth scenarios, runtime tool-calling, consent flow behavior, and basic failure/fallback cases.
- Expectation fields include observable checks (HTTP status, response fields, and trace/log entries).

---

## Authorization

- **ATC-01 — Valid Cognito JWT auth**
  - Description: Verify successful authorization using a valid Cognito JWT.
  - Preconditions: Services (Gateway, Runtime) are running; a valid Cognito JWT is available.
  - Steps:
    1. Send a request to the Gateway/Runtime with a valid Authorization: Bearer <JWT> header.
    2. Observe the runtime processing.
  - Expected result: Gateway forwards the request, Runtime processes it; HTTP 200 and a valid agent response. Trace/log shows successful authentication for the request.

- **ATC-02 — Invalid or expired JWT rejected**
  - Description: Verify rejection for malformed or expired JWTs.
  - Preconditions: Services running; an expired or malformed JWT available.
  - Steps:
    1. Send a request with an invalid/expired JWT.
    2. Observe response and logs.
  - Expected result: Gateway returns HTTP 401 with an error indicating invalid or expired token (e.g. "Invalid Bearer token" or "Token has expired"). Auth failure trace logged.

- **ATC-03 — Missing Authorization header**
  - Description: Verify behavior when Authorization header is absent.
  - Preconditions: Services running.
  - Steps:
    1. Send a request without the Authorization header.
    2. Observe response and logs.
  - Expected result: Gateway returns HTTP 401 with an error such as "Missing Bearer token" or "Missing Authentication Token". Auth failure trace logged.

- **ATC-04 — Reuse of a valid token**
  - Description: Verify behavior when the same valid token is used for multiple requests.
  - Preconditions: Services running; a valid JWT previously used in a successful request (ATC-01).
  - Steps:
    1. Send a second request with the same valid JWT.
  - Expected result: Both requests are accepted and processed (HTTP 200). Logs show repeated successful authentication for the same subject.

---

## Runtime invocation with tool-calling

- **RTC-01 — Simple tool call flow**
  - Description: Verify the runtime invokes the Google Docs tool and returns the result.
  - Preconditions: Gateway and Runtime running; tool-calling enabled and configured for Google Docs; valid consent/access token available if the flow requires it.
  - Steps:
    1. Send a prompt that triggers the Google Docs tool (e.g. request to read a specific document).
    2. Observe Runtime response and logs.
  - Expected result: Runtime calls the tool, receives a result and returns a structured response containing the answer and metadata. Tool call logged and visible in tracing.

- **RTC-02 — Tool call trace propagation**
  - Description: Verify that tool-call traces are created and available in observability outputs.
  - Preconditions: Tracing/observability enabled  and configured.
  - Steps:
    1. Execute a request that triggers a tool call.
    2. Verify traces/observability for the invocation.
  - Expected result: Traces include a tool-call span (e.g., `get_google_doc`) with success/failure status. The agent response references the trace ID.

- **RTC-03 — Response includes sources**
  - Description: Verify responses include `sources` that reference the Google Doc(s) used.
  - Preconditions: Tool returns source metadata (e.g., doc id or link).
  - Steps:
    1. Send a request that triggers Google Docs access.
    2. Inspect the response payload.
  - Expected result: Response contains a `sources` field with links or identifiers for the Google Doc(s) used.

- **RTC-04 — General chat behavior (no hallucination)**
  - Description: Verify the agent answers general prompts accurately and does not hallucinate facts when relying on external documents.
  - Preconditions: Runtime running; valid consent if external resources are needed.
  - Steps:
    1. Send a generic prompt (e.g., "Hello, how are you doing today?").
    2. Inspect response for factual accuracy and presence of unsupported claims.
  - Expected result: Runtime responds coherently. If external facts are required, the agent cites sources in `sources`; no unsupported factual claims (no hallucinations). Any uncertainty should be clearly stated.

---

## Consent / Path behavior

- **CTC-01 — Initial consent challenge**
  - Description: First invoke should trigger the OAuth consent challenge when no consent exists.
  - Preconditions: Runtime and Gateway running; no stored consent for the user.
  - Steps:
    1. Send an authenticated request that requires Google Docs access.
    2. Observe response.
  - Expected result: System returns a consent challenge or an OAuth redirect/authorization URL indicating the user must grant consent.

- **CTC-02 — Resource access after consent**
  - Description: Verify that subsequent invokes use stored consent and return resource links.
  - Preconditions: Valid stored consent; Google Docs access available.
  - Steps:
    1. Send a request (example: "Which mammal is the slowest?").
    2. Inspect response.
  - Expected result: Request processed without requiring new consent; response contains links or identifiers to the Google Doc(s) used.

- **CTC-03 — Correct output after consent**
  - Description: Verify correct answers for instrumented prompts when consent exists.
  - Preconditions: Valid stored consent; document contains the answer.
  - Steps:
    1. Send a factual prompt that can be answered from the document (e.g., "Which mammal is the slowest?").
    2. Inspect the response and sources.
  - Expected result: Correct answer is returned and `sources` point to the supporting Google Doc.

- **CTC-04 — Consent expiry handling**
  - Description: Verify behavior when consent/refresh tokens have expired.
  - Preconditions: An expired consent/refresh token recorded.
  - Steps:
    1. Send a request requiring Google Docs access.
    2. Observe how the system handles token refresh or re-authorization.
  - Expected result: The system either refreshes tokens automatically (if refresh token valid) or returns an authorization URL for the user to reauthorize (`authorization_url`). Logs show token refresh attempt and failure reason if refresh is not possible.

- **CTC-05 — User revoked consent**
  - Description: Verify handling when a user revokes access from their Google Account.
  - Preconditions: Consent was previously granted but the user revoked access in Google Account settings.
  - Steps:
    1. User revokes the app's access in Google Account.
    2. Send a request that requires Google Docs access.
    3. Observe response and logs.
  - Expected result: System detects an `invalid_grant` or revoked token error, returns an authorization URL for re-consent, and records the revocation event in logs/metrics.

---

## Failure / Fallback

- **FTC-01 — Tool target not found**
  - Description: Verify handling when the configured tool target name is incorrect or the tool is unavailable.
  - Preconditions: Runtime configured to call a tool target that does not exist/is misnamed; consent present.
  - Steps:
    1. Send a request that requires the tool (e.g., `getDocument`).
    2. Observe the response and logs.
  - Expected result: Runtime returns an error describing the missing target (HTTP 5xx or 4xx depending on implementation), `tools_used` is empty or indicates no successful tool calls, and an explanatory error is logged.

- **FTC-02 — Document not found**
  - Description: Verify behavior when the referenced Google Doc does not exist.
  - Preconditions: Consent present; a non-existent `GOOGLE_DOC_ID` is used.
  - Steps:
    1. Use a non-existent document id and send a request that accesses it.
    2. Observe response and logs.
  - Expected result: Agent/tool returns HTTP 404 with a `NOT_FOUND` error or equivalent, and the error is recorded in traces.

- **FTC-03 — Malformed request to Runtime**
  - Description: Verify the Runtime/Gateway response to invalid or malformed requests.
  - Preconditions: Runtime and Gateway running.
  - Steps:
    1. Send a request with invalid JSON or missing required fields.
    2. Observe response and logs.
  - Expected result: Gateway/Runtime return HTTP 4xx with a clear error message describing the problem; request rejected and logged.

- **FTC-04 — Incorrect allowedClients configuration**
  - Description: Verify behavior when `allowedClients` is misconfigured so the runtime rejects requests from the Gateway.
  - Preconditions: Runtime running with an invalid `allowedClients` value that does not match Gateway configuration; consent present.
  - Steps:
    1. Configure `allowedClients` to a value not recognized by the Gateway.
    2. Send an authenticated request.
    3. Observe response.
  - Expected result: Gateway or Runtime returns HTTP 403 (Forbidden) with an explanatory message; the failed authorization is visible in logs/metrics.

- **FTC-05 — Out-of-scope prompt handling**
  - Description: Verify agent behavior for prompts that cannot be satisfied from available documents.
  - Preconditions: Consent present; available documents do not contain information for the prompt.
  - Steps:
    1. Send an out-of-scope prompt (e.g., "Give me a recipe for cooking pancakes" if documents are unrelated).
    2. Observe response.
  - Expected result: Agent returns a clear message stating the document(s) do not contain the requested information and optionally suggests alternatives. No hallucinated facts.

---

## Observability and Acceptance Criteria

- Each test should record the relevant traces/logs and include trace IDs in test output where applicable.
- For tool-calling tests, ensure a tool-call span is present in traces and that `tools_used` metadata (or equivalent) is included in the response or runtime logs.
- For auth tests, confirm the authentication decision is recorded in logs with subject claim and token validation reason on failure.

---

## Issues and Corrections Applied (summary)

- Fixed typos and inconsistent field names (e.g., `authorization_utl` → `authorization_url`).
- Clarified expected HTTP status codes and observability checks.
- Reworded vague expectations (e.g., "no hallucinated / biased output") into verifiable criteria: cite sources or state uncertainty.
- Added explicit checks for traces and `tools_used`/tool-call spans.
- Ensured at least one negative auth test (ATC-02) is present and clearly specified.

---

*File generated from the provided Word document and reviewed for logical completeness.*

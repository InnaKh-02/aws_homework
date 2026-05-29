from dataclasses import dataclass
import json
import os
import re
import urllib.parse
from typing import Any, Literal

import boto3
import requests
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.exceptions import ClientError
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.tools import ToolRuntime
from pydantic import BaseModel, Field

app = BedrockAgentCoreApp()
APP_VERSION = "2026-04-15-runtime-minimal-deps-v10"
RUNTIME_DEPLOY_REQUIREMENTS = (
    "bedrock-agentcore==1.6.2",
    "langchain==1.2.15",
    "langchain-core==1.2.29",
    "langchain-google-genai==4.2.1",
    "pydantic==2.13.1",
    "requests==2.33.1",
    "boto3==1.42.89",
    "botocore==1.42.89",
)
WORKSHOP_TOOL_NAME = "get_google_doc"

_SETTINGS: dict[str, Any] | None = None
_AC_RUNTIME = None
_LLM = None
_AGENT = None

STOPWORDS = {
    "about",
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "does",
    "document",
    "for",
    "from",
    "how",
    "in",
    "include",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "say",
    "source",
    "sources",
    "that",
    "the",
    "this",
    "to",
    "what",
    "with",
}

AGENT_SYSTEM_PROMPT = """You are a Google Docs workshop assistant running inside AgentCore Runtime.

You have exactly one tool: get_google_doc.
Always call get_google_doc before answering any user question about the document.
Do not invent document content.
If the tool says consent is required, stop and explain that consent is required.
If the tool says the document is empty or unavailable, reflect that status and stop.
If the tool returns document text, answer only from that document and cite the provided source URL.
Use the structured response schema for document-grounded answers.
"""


@dataclass(frozen=True)
class AgentRuntimeContext:
    doc_id: str
    access_token: str
    oauth_session_uri: str
    mcp_session_id: str
    oauth_return_url: str
    force_authentication: bool
    max_doc_calls: int


class GoogleDocToolArtifact(BaseModel):
    kind: Literal["document", "consent", "error", "empty"]
    authorization_url: str = ""
    oauth_session_uri: str = ""
    document_text: str = ""
    source_url: str = ""
    error_message: str = ""


class WorkshopStructuredResponse(BaseModel):
    kind: Literal["bullet_summary", "not_found"] = Field(
        description="Use bullet_summary when the document contains the answer, else not_found."
    )
    bullets: list[str] = Field(
        default_factory=list,
        description="Short factual bullets grounded only in the Google Doc.",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Source links used for the answer.",
    )
    message: str = Field(
        default="",
        description="Short fallback message, mainly for not_found.",
    )


def get_settings() -> dict[str, Any]:
    global _SETTINGS
    if _SETTINGS is None:
        google_api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not google_api_key:
            raise RuntimeError("Missing GOOGLE_API_KEY environment variable.")
        _SETTINGS = {
            "GATEWAY_URL": os.environ["GATEWAY_URL"],
            "GOOGLE_DOCS_TOOL_NAME": os.environ["GOOGLE_DOCS_TOOL_NAME"],
            "MCP_VERSION": os.environ.get("GATEWAY_MCP_VERSION", "2025-11-25"),
            "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
            "GOOGLE_MODEL_ID": os.environ.get("GOOGLE_MODEL_ID", "gemini-3-flash-preview"),
            "GOOGLE_API_KEY": google_api_key,
            "DOC_CONTEXT_MAX_CHARS": int(os.environ.get("DOC_CONTEXT_MAX_CHARS", "12000")),
            "GOOGLE_MAX_OUTPUT_TOKENS": int(os.environ.get("GOOGLE_MAX_OUTPUT_TOKENS", "512")),
        }
    return _SETTINGS


def get_ac_runtime():
    global _AC_RUNTIME
    if _AC_RUNTIME is None:
        _AC_RUNTIME = boto3.client(
            "bedrock-agentcore",
            region_name=get_settings()["AWS_REGION"],
        )
    return _AC_RUNTIME


def get_llm():
    global _LLM
    if _LLM is None:
        settings = get_settings()
        _LLM = ChatGoogleGenerativeAI(
            model=settings["GOOGLE_MODEL_ID"],
            api_key=settings["GOOGLE_API_KEY"],
            temperature=0,
            max_tokens=settings["GOOGLE_MAX_OUTPUT_TOKENS"],
        )
    return _LLM


@tool(WORKSHOP_TOOL_NAME, response_format="content_and_artifact")
def get_google_doc_tool(runtime: ToolRuntime[AgentRuntimeContext]) -> tuple[str, dict[str, Any]]:
    """Fetch the configured Google Doc through AgentCore Gateway."""
    return get_google_doc(runtime)


def get_agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = create_agent(
            model=get_llm(),
            tools=[get_google_doc_tool],
            system_prompt=AGENT_SYSTEM_PROMPT,
            response_format=ToolStrategy(
                WorkshopStructuredResponse,
                tool_message_content="Structured workshop answer captured.",
            ),
            context_schema=AgentRuntimeContext,
            name="agentcore_google_docs_agent",
        )
    return _AGENT


def mcp_request(
    bearer_token: str,
    method: str,
    params: dict[str, Any],
    mcp_session_id: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": settings["MCP_VERSION"],
    }
    if mcp_session_id and not mcp_session_id.startswith("urn:ietf:params:oauth:request_uri:"):
        headers["x-mcp-session-id"] = mcp_session_id

    response = requests.post(
        settings["GATEWAY_URL"],
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def extract_mcp_text(payload: dict[str, Any]) -> str:
    content = payload.get("result", {}).get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts).strip()


def parse_google_doc_payload(mcp_payload: dict[str, Any]) -> dict[str, Any]:
    merged = extract_mcp_text(mcp_payload)
    if not merged:
        return {}
    try:
        obj = json.loads(merged)
    except Exception:
        return {}

    if isinstance(obj, dict) and "body" in obj and isinstance(obj["body"], str):
        try:
            return json.loads(obj["body"])
        except Exception:
            return {"raw_body": obj["body"]}
    return obj if isinstance(obj, dict) else {}


def _collect_text_runs(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        text_run = node.get("textRun")
        if isinstance(text_run, dict):
            content = str(text_run.get("content", ""))
            if content:
                out.append(content)
        for value in node.values():
            _collect_text_runs(value, out)
        return

    if isinstance(node, list):
        for item in node:
            _collect_text_runs(item, out)


def extract_google_doc_text(doc: dict[str, Any]) -> str:
    chunks: list[str] = []
    # Parse across the whole document object, not only body.content,
    # because newer Google Docs payloads can keep text under tabs.
    _collect_text_runs(doc, chunks)
    merged = "".join(chunks).replace("\r\n", "\n").replace("\r", "\n")

    normalized_lines: list[str] = []
    previous_blank = False
    for raw_line in merged.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        is_blank = not line
        if is_blank and previous_blank:
            continue
        normalized_lines.append(line)
        previous_blank = is_blank

    return "\n".join(normalized_lines).strip()


def extract_elicitation_url(payload: dict[str, Any]) -> str | None:
    try:
        return payload["error"]["data"]["elicitations"][0]["url"]
    except Exception:
        return None


def extract_request_uri_from_url(url: str | None) -> str | None:
    if not url:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    values = query.get("request_uri")
    if not values:
        return None
    return urllib.parse.unquote(values[0])


def build_authorization_url(request_uri: str | None) -> str:
    if not request_uri:
        return ""
    encoded = urllib.parse.quote(request_uri, safe="")
    return (
        "https://bedrock-agentcore."
        f"{get_settings()['AWS_REGION']}.amazonaws.com/identities/oauth2/authorize?request_uri={encoded}"
    )


def complete_oauth_session(access_token: str, oauth_session_uri: str) -> None:
    get_ac_runtime().complete_resource_token_auth(
        userIdentifier={"userToken": access_token},
        sessionUri=oauth_session_uri,
    )


def message_to_text(msg: Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def preview_text(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def tool_artifact_to_content(artifact: GoogleDocToolArtifact) -> str:
    if artifact.kind == "consent":
        return (
            "Consent is required before the Google Doc can be accessed.\n"
            "Stop and tell the user to complete browser authorization."
        )
    if artifact.kind == "error":
        return artifact.error_message or "The Google Docs request failed."
    if artifact.kind == "empty":
        source_line = f"\nSource URL: {artifact.source_url}" if artifact.source_url else ""
        return f"The document is empty.{source_line}"
    source_line = f"\nSource URL: {artifact.source_url}" if artifact.source_url else ""
    return f"Use this Google Doc content to answer the question.{source_line}\n\n{artifact.document_text}"


def extract_tool_trace(messages: list[Any]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    step = 1
    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in getattr(message, "tool_calls", []) or []:
                if str(tool_call.get("name", "")) != WORKSHOP_TOOL_NAME:
                    continue
                trace.append(
                    {
                        "step": step,
                        "event": "tool_call",
                        "tool": str(tool_call.get("name", "")),
                        "args": dict(tool_call.get("args", {}) or {}),
                    }
                )
                step += 1
        elif isinstance(message, ToolMessage):
            if str(getattr(message, "name", "") or "") != WORKSHOP_TOOL_NAME:
                continue
            trace.append(
                {
                    "step": step,
                    "event": "tool_result",
                    "tool": str(getattr(message, "name", "") or ""),
                    "preview": preview_text(message_to_text(message)),
                }
            )
            step += 1
    return trace


def summarize_tool_usage(messages: list[Any]) -> tuple[list[str], dict[str, int]]:
    counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tool_name = str(getattr(message, "name", "") or "")
        if tool_name != WORKSHOP_TOOL_NAME:
            continue
        counts[tool_name] = counts.get(tool_name, 0) + 1
    return list(counts), counts


def extract_last_tool_result(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and str(getattr(message, "name", "") or "") == WORKSHOP_TOOL_NAME:
            return message_to_text(message)
    return ""


def previous_tool_results(messages: list[Any], tool_name: str = WORKSHOP_TOOL_NAME) -> list[str]:
    results: list[str] = []
    for message in messages:
        if isinstance(message, ToolMessage) and str(getattr(message, "name", "") or "") == tool_name:
            results.append(message_to_text(message))
    return results


def tool_artifact_from_legacy_text(tool_text: str) -> GoogleDocToolArtifact:
    parsed = parse_tool_output(tool_text)
    if parsed["kind"] == "consent":
        return GoogleDocToolArtifact(
            kind="consent",
            authorization_url=parsed.get("authorization_url", ""),
            oauth_session_uri=parsed.get("oauth_session_uri", ""),
        )
    if parsed["kind"] == "error":
        return GoogleDocToolArtifact(kind="error", error_message=tool_text)
    if parsed["kind"] == "empty":
        return GoogleDocToolArtifact(kind="empty", source_url=parsed.get("source_url", ""))
    if parsed["kind"] == "document":
        return GoogleDocToolArtifact(
            kind="document",
            document_text=parsed.get("document_text", ""),
            source_url=parsed.get("source_url", ""),
        )
    return GoogleDocToolArtifact(kind="error", error_message="Unexpected tool output format.")


def normalize_tool_artifact(raw_artifact: Any, fallback_text: str = "") -> GoogleDocToolArtifact:
    if isinstance(raw_artifact, GoogleDocToolArtifact):
        return raw_artifact
    if isinstance(raw_artifact, dict):
        try:
            return GoogleDocToolArtifact.model_validate(raw_artifact)
        except Exception:
            pass
    if fallback_text:
        return tool_artifact_from_legacy_text(fallback_text)
    return GoogleDocToolArtifact(kind="error", error_message="Missing tool artifact.")


def extract_last_tool_artifact(messages: list[Any]) -> GoogleDocToolArtifact:
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and str(getattr(message, "name", "") or "") == WORKSHOP_TOOL_NAME:
            return normalize_tool_artifact(getattr(message, "artifact", None), message_to_text(message))
    return GoogleDocToolArtifact(kind="error", error_message="Missing get_google_doc tool result.")


def run_agent(prompt: str, recursion_limit: int, runtime_context: AgentRuntimeContext) -> dict[str, Any]:
    result = get_agent().invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        context=runtime_context,
        config={"recursion_limit": recursion_limit},
    )
    messages = list(result.get("messages", []))
    final_ai_text = ""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            final_ai_text = message_to_text(message)
            break
    return {
        "messages": messages,
        "final_ai_text": final_ai_text,
        "structured_response": result.get("structured_response"),
    }


def parse_tool_output(tool_text: str) -> dict[str, str]:
    out = {
        "kind": "other",
        "authorization_url": "",
        "oauth_session_uri": "",
        "document_text": "",
        "source_url": "",
    }

    if tool_text.startswith("CONSENT_REQUIRED"):
        out["kind"] = "consent"
        auth = re.search(r"authorization_url:\s*(https?://\S+)", tool_text)
        sess = re.search(r"oauth_session_uri:\s*(\S+)", tool_text)
        out["authorization_url"] = auth.group(1) if auth else ""
        out["oauth_session_uri"] = sess.group(1) if sess else ""
        return out

    if tool_text.startswith("ERROR:"):
        out["kind"] = "error"
        return out

    if tool_text.startswith("EMPTY_DOCUMENT"):
        out["kind"] = "empty"
        src = re.search(r"SOURCE:\s*(https?://\S+)", tool_text)
        out["source_url"] = src.group(1) if src else ""
        return out

    if tool_text.startswith("DOCUMENT_TEXT:"):
        out["kind"] = "document"
        source_split = tool_text.split("\n\nSOURCE:", 1)
        body = source_split[0].replace("DOCUMENT_TEXT:\n", "", 1)
        out["document_text"] = body.strip()
        if len(source_split) > 1:
            out["source_url"] = source_split[1].strip()
        return out

    return out


def _candidate_bullets_from_text(doc_text: str) -> list[str]:
    bullets: list[str] = []
    seen: set[str] = set()

    for raw_line in doc_text.split("\n"):
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", raw_line).strip()
        if not line:
            continue
        if len(line.split()) < 5:
            continue
        if line.lower().startswith("sources:"):
            continue
        if not re.search(r"[.!?]$", line):
            line = f"{line}."
        if line not in seen:
            bullets.append(line)
            seen.add(line)

    if bullets:
        return bullets

    for sentence in re.split(r"(?<=[.!?])\s+", doc_text.replace("\n", " ")):
        sentence = sentence.strip()
        if len(sentence.split()) < 5:
            continue
        if sentence not in seen:
            bullets.append(sentence)
            seen.add(sentence)

    return bullets


def extract_query_terms(prompt: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", prompt.lower())
    return {word for word in words if word not in STOPWORDS}


def is_summary_prompt(prompt: str) -> bool:
    text = prompt.lower()
    markers = (
        "summarize",
        "summary",
        "summarise",
        "key points",
        "overview",
        "6 bullets",
        "bullets",
    )
    return any(marker in text for marker in markers)


def candidate_sentences(doc_text: str) -> list[str]:
    lines = _candidate_bullets_from_text(doc_text)
    if lines:
        return lines

    sentences: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", doc_text.replace("\n", " ")):
        cleaned = re.sub(r"\s+", " ", sentence).strip(" -*")
        if len(cleaned.split()) < 5:
            continue
        if cleaned not in seen:
            sentences.append(cleaned)
            seen.add(cleaned)
    return sentences


def build_structured_answer(
    prompt: str,
    doc_text: str,
    source_url: str,
    max_bullets: int = 6,
) -> dict[str, Any]:
    candidates = candidate_sentences(doc_text)
    sources = [source_url] if source_url else []
    query_terms = extract_query_terms(prompt)
    summary_prompt = is_summary_prompt(prompt)

    if not candidates:
        return {
            "kind": "not_found",
            "query": prompt,
            "bullets": [],
            "sources": sources,
            "message": "Not found in document.",
        }

    scored: list[tuple[int, int, str]] = []
    for idx, candidate in enumerate(candidates):
        lowered = candidate.lower()
        score = sum(1 for term in query_terms if term in lowered)
        scored.append((score, idx, candidate))

    relevant = [item for item in scored if item[0] > 0]
    if query_terms and not summary_prompt and not relevant:
        return {
            "kind": "not_found",
            "query": prompt,
            "bullets": [],
            "sources": sources,
            "message": "Not found in document.",
        }

    if summary_prompt:
        bullets = candidates[:max_bullets]
    else:
        ranked = relevant if relevant else scored
        ranked = sorted(ranked, key=lambda item: (-item[0], item[1]))
        selected = sorted(ranked[:max_bullets], key=lambda item: item[1])
        bullets = [candidate for _, _, candidate in selected]

    return {
        "kind": "bullet_summary",
        "query": prompt,
        "bullets": bullets,
        "sources": sources,
        "message": "",
    }


def render_structured_answer(answer: dict[str, Any]) -> str:
    kind = answer.get("kind")
    if kind == "not_found":
        body = str(answer.get("message") or "Not found in document.")
    else:
        bullets = [str(item).strip() for item in answer.get("bullets", []) if str(item).strip()]
        if not bullets:
            body = "Not found in document."
        else:
            body = "\n".join(f"- {bullet}" for bullet in bullets)

    sources = [str(item).strip() for item in answer.get("sources", []) if str(item).strip()]
    if sources:
        body = f"{body}\n\nSources:\n" + "\n".join(f"- {item}" for item in sources)
    return body


def normalize_structured_response(
    structured_response: Any,
    prompt: str,
    source_url: str,
) -> dict[str, Any]:
    if isinstance(structured_response, BaseModel):
        payload = structured_response.model_dump()
    elif isinstance(structured_response, dict):
        payload = dict(structured_response)
    else:
        payload = {}

    kind = str(payload.get("kind") or "not_found")
    bullets = [str(item).strip() for item in payload.get("bullets", []) if str(item).strip()]
    sources = [str(item).strip() for item in payload.get("sources", []) if str(item).strip()]
    message = str(payload.get("message") or "").strip()

    if source_url and source_url not in sources:
        sources.append(source_url)

    if kind not in {"bullet_summary", "not_found"}:
        kind = "not_found"
    if kind == "bullet_summary" and not bullets:
        kind = "not_found"
        message = message or "Not found in document."
    if kind == "not_found":
        bullets = []
        message = message or "Not found in document."

    return {
        "kind": kind,
        "query": prompt,
        "bullets": bullets,
        "sources": sources,
        "message": message,
    }


def get_google_doc(runtime: ToolRuntime[AgentRuntimeContext]) -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    ctx = runtime.context
    state = runtime.state or {}
    messages = list(state.get("messages", []))
    doc_id = str(ctx.doc_id).strip()
    token = str(ctx.access_token).strip()
    oauth_session_uri = str(ctx.oauth_session_uri).strip()
    mcp_session_id = str(ctx.mcp_session_id).strip()
    oauth_return_url = str(ctx.oauth_return_url).strip()
    force_authentication = bool(ctx.force_authentication)
    max_doc_calls = max(1, int(ctx.max_doc_calls))
    prior_tool_results = previous_tool_results(messages)
    doc_call_count = len(prior_tool_results)
    cached_artifact = extract_last_tool_artifact(messages) if prior_tool_results else None

    if not doc_id:
        artifact = GoogleDocToolArtifact(kind="error", error_message="doc_id is empty in agent context.")
        return tool_artifact_to_content(artifact), artifact.model_dump()
    if not token:
        artifact = GoogleDocToolArtifact(kind="error", error_message="user_access_token is empty in agent context.")
        return tool_artifact_to_content(artifact), artifact.model_dump()

    if doc_call_count >= max_doc_calls:
        if cached_artifact is not None:
            return tool_artifact_to_content(cached_artifact), cached_artifact.model_dump()
        artifact = GoogleDocToolArtifact(
            kind="error",
            error_message=f"get_google_doc call budget reached ({max_doc_calls}).",
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()

    if oauth_session_uri:
        try:
            complete_oauth_session(token, oauth_session_uri)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            artifact = GoogleDocToolArtifact(
                kind="error",
                error_message=(
                    "complete_resource_token_auth failed.\n"
                    f"code: {code}\n"
                    f"message: {msg}"
                ),
            )
            return tool_artifact_to_content(artifact), artifact.model_dump()

    params: dict[str, Any] = {
        "name": settings["GOOGLE_DOCS_TOOL_NAME"],
        "arguments": {"documentId": doc_id},
    }
    meta_cfg: dict[str, Any] = {}
    if oauth_return_url:
        meta_cfg["returnUrl"] = oauth_return_url
    if force_authentication:
        meta_cfg["forceAuthentication"] = True
    # Always request USER_FEDERATION metadata for OAuth targets.
    # If returnUrl is omitted, Gateway uses target defaultReturnUrl.
    params["_meta"] = {
        "aws.bedrock-agentcore.gateway/credentialProviderConfiguration": {
            "oauthCredentialProvider": meta_cfg
        }
    }

    try:
        payload = mcp_request(
            token,
            "tools/call",
            params,
            mcp_session_id=mcp_session_id,
        )
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        body = (getattr(exc.response, "text", "") or "")[:800]
        artifact = GoogleDocToolArtifact(
            kind="error",
            error_message=(
                "MCP HTTP failure while calling Google Docs tool.\n"
                f"status: {status}\n"
                f"body: {body}"
            ),
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()
    except requests.RequestException as exc:
        artifact = GoogleDocToolArtifact(
            kind="error",
            error_message=f"MCP network failure while calling Google Docs tool: {exc}",
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()

    if "error" in payload and payload["error"].get("code") == -32042:
        auth_url = extract_elicitation_url(payload) or ""
        req_uri = extract_request_uri_from_url(auth_url) or ""
        artifact = GoogleDocToolArtifact(
            kind="consent",
            authorization_url=auth_url,
            oauth_session_uri=req_uri,
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()

    if "error" in payload:
        artifact = GoogleDocToolArtifact(
            kind="error",
            error_message=f"MCP get_google_doc failed: {payload['error']}",
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()

    raw_text = extract_mcp_text(payload)
    if bool((payload.get("result") or {}).get("isError")):
        artifact = GoogleDocToolArtifact(
            kind="error",
            error_message=(
                "MCP get_google_doc returned isError=true.\n"
                f"message: {raw_text[:800]}"
            ),
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()

    doc_payload = parse_google_doc_payload(payload)
    if not doc_payload:
        artifact = GoogleDocToolArtifact(
            kind="error",
            error_message=(
                "Could not parse Google Docs tool response.\n"
                f"raw: {raw_text[:800]}"
            ),
        )
        return tool_artifact_to_content(artifact), artifact.model_dump()

    doc_text = extract_google_doc_text(doc_payload)
    source_url = f"https://docs.google.com/document/d/{doc_id}/edit"

    if not doc_text:
        artifact = GoogleDocToolArtifact(kind="empty", source_url=source_url)
        return tool_artifact_to_content(artifact), artifact.model_dump()

    artifact = GoogleDocToolArtifact(
        kind="document",
        document_text=doc_text,
        source_url=source_url,
    )
    return tool_artifact_to_content(artifact), artifact.model_dump()


def _session_from_context(context: Any) -> str:
    if context is None:
        return ""
    if isinstance(context, dict):
        return str(context.get("session_id") or context.get("sessionId") or "")
    return str(getattr(context, "session_id", None) or getattr(context, "sessionId", None) or "")


@app.entrypoint
def invoke(payload: dict, context=None):
    thread_id = str(payload.get("thread_id") or "").strip() or _session_from_context(context) or "runtime-default-thread"
    prompt_text = str(payload.get("prompt", "")).strip()

    try:
        max_doc_calls = max(1, min(3, int(payload.get("max_doc_calls", 1))))
    except (TypeError, ValueError):
        max_doc_calls = 1

    try:
        max_steps = int(payload.get("max_steps", 5))
    except (TypeError, ValueError):
        max_steps = 5
    recursion_limit = max(2, min(8, max_steps))

    runtime_context = AgentRuntimeContext(
        doc_id=str(payload.get("doc_id", "")).strip(),
        access_token=str(payload.get("user_access_token", "")).strip(),
        oauth_session_uri=str(payload.get("oauth_session_uri", "")).strip(),
        mcp_session_id=str(payload.get("mcp_session_id", "")).strip() or thread_id,
        oauth_return_url=str(payload.get("oauth_return_url", "")).strip(),
        force_authentication=bool(payload.get("force_authentication", False)),
        max_doc_calls=max_doc_calls,
    )

    try:
        agent_state = run_agent(prompt_text or "Summarize the document.", recursion_limit, runtime_context)
        messages = list(agent_state.get("messages", []))
        tool_text = extract_last_tool_result(messages)
        tool_artifact = extract_last_tool_artifact(messages)
        trace = extract_tool_trace(messages)
        tools_used, tool_call_counts = summarize_tool_usage(messages)
        structured_response = agent_state.get("structured_response")
    except Exception as exc:
        messages = []
        tool_text = f"ERROR: LangChain agent invoke failed: {exc}"
        tool_artifact = GoogleDocToolArtifact(kind="error", error_message=tool_text)
        trace = []
        tools_used = []
        tool_call_counts = {}
        structured_response = None

    authorization_url = ""
    oauth_session_uri = ""
    consent_required = False
    answer_mode = "tool_only"
    answer_payload: dict[str, Any] = {
        "kind": "tool_only",
        "query": prompt_text,
        "bullets": [],
        "sources": [],
        "message": "",
    }

    if tool_artifact.kind == "consent":
        oauth_session_uri = tool_artifact.oauth_session_uri
        raw_auth = tool_artifact.authorization_url
        if not oauth_session_uri:
            oauth_session_uri = extract_request_uri_from_url(raw_auth) or ""
        authorization_url = build_authorization_url(oauth_session_uri) if oauth_session_uri else raw_auth
        consent_required = bool(authorization_url)
        answer = (
            "Google consent required.\n"
            f"authorization_url: {authorization_url}\n"
            f"oauth_session_uri: {oauth_session_uri}\n"
            "Complete consent in browser, then re-run with the same oauth_session_uri."
        )
        answer_payload = {
            "kind": "consent",
            "query": prompt_text,
            "bullets": [],
            "sources": [],
            "message": answer,
        }
    elif tool_artifact.kind == "error":
        answer = tool_artifact.error_message or tool_text
        answer_mode = "error"
        answer_payload = {
            "kind": "error",
            "query": prompt_text,
            "bullets": [],
            "sources": [],
            "message": answer,
        }
    elif tool_artifact.kind == "empty":
        src = tool_artifact.source_url
        answer = "The document is empty."
        if src:
            answer += f"\n\nSources:\n- {src}"
        answer_mode = "empty"
        answer_payload = {
            "kind": "empty",
            "query": prompt_text,
            "bullets": [],
            "sources": [src] if src else [],
            "message": "The document is empty.",
        }
    elif tool_artifact.kind == "document":
        doc_text = tool_artifact.document_text
        source_url = tool_artifact.source_url
        if not doc_text:
            answer = "ERROR: Document text is empty after parsing tool result."
            answer_mode = "error"
            answer_payload = {
                "kind": "error",
                "query": prompt_text,
                "bullets": [],
                "sources": [source_url] if source_url else [],
                "message": answer,
            }
        else:
            doc_for_answer = doc_text[: get_settings()["DOC_CONTEXT_MAX_CHARS"]]
            if structured_response:
                answer_payload = normalize_structured_response(
                    structured_response=structured_response,
                    prompt=prompt_text,
                    source_url=source_url,
                )
            else:
                answer_payload = build_structured_answer(
                    prompt=prompt_text,
                    doc_text=doc_for_answer,
                    source_url=source_url,
                )
            answer = render_structured_answer(answer_payload)
            answer_mode = "langchain_structured" if structured_response else "deterministic_extractive"
    else:
        answer = "ERROR: Unexpected tool output format."
        answer_mode = "error"
        answer_payload = {
            "kind": "error",
            "query": prompt_text,
            "bullets": [],
            "sources": [],
            "message": answer,
        }

    return {
        "app_version": APP_VERSION,
        "recursion_limit": recursion_limit,
        "response": answer,
        "answer": answer_payload,
        "tool_trace": trace,
        "tools_used": tools_used,
        "tool_call_counts": tool_call_counts,
        "tool_call_limits": {
            "get_google_doc": runtime_context.max_doc_calls,
        },
        "answer_mode": answer_mode,
        "consent_required": consent_required,
        "authorization_url": authorization_url,
        "oauth_session_uri": oauth_session_uri,
        "thread_id": thread_id,
    }


if __name__ == "__main__":
    app.run()

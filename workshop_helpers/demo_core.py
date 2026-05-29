from __future__ import annotations

import json
import logging
import os
import re
import webbrowser
from pathlib import Path
from typing import Any

import boto3

from .callback_server import (
    callback_endpoint,
    callback_server_info,
    reset_callback_server_state,
    start_local_callback_server,
    stop_local_callback_server,
    wait_for_local_callback,
)

LOG = logging.getLogger(__name__)

DESIRED_MCP_VERSION = "2025-11-25"
DEFAULT_RETURN_URL = "http://localhost:8081/oauth2/callback"
DEFAULT_RUNTIME_THREAD_ID = "m11-runtime-react-demo-000000000000001"
DEFAULT_RUNTIME_PROMPT_1 = (
    "Summarize incident response from this document in 6 bullets and include source."
)
DEFAULT_RUNTIME_PROMPT_2 = (
    "Answer in 6 bullets: summarize incident response from the document and cite source link from the document."
)

def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    markers = ("runtime_app_agentcore_full.py", "requirements.txt", ".git")
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    raise FileNotFoundError("Could not locate project root from current working directory.")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _compact_name(prefix: str) -> tuple[str, str]:
    base = re.sub(r"[^a-zA-Z0-9_]", "_", prefix)
    base = re.sub(r"_+", "_", base).strip("_") or "acwslite"
    dns = re.sub(r"[^a-z0-9-]", "-", base.lower().replace("_", "-")).strip("-") or "acwslite"
    return base, dns


class WorkshopE2EDemoBase:
    def __init__(self, root: Path | None = None) -> None:
        self.root = find_project_root(root)
        load_env_file(self.root / ".env")

        self.aws_profile = os.getenv("AWS_PROFILE", "workshop")
        self.aws_region = os.getenv("AWS_REGION", "us-east-1")
        self.workshop_prefix = os.getenv("WORKSHOP_PREFIX", "acwslite")
        self.oauth_return_url = os.getenv("OAUTH_RETURN_URL", DEFAULT_RETURN_URL)
        self.google_model_id = os.getenv("GOOGLE_MODEL_ID", "gemini-2.5-flash")
        self.runtime_file = self.root / "runtime_app_agentcore_full.py"
        self.requirements_file = self.root / "requirements.txt"

        self._normalize_aws_env()
        self._require_env(
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_DOC_ID",
            "GOOGLE_API_KEY",
        )

        self.session = boto3.Session(
            profile_name=self.aws_profile,
            region_name=self.aws_region,
        )
        self.sts = self.session.client("sts")
        self.cognito = self.session.client("cognito-idp")
        self.ac_control = self.session.client("bedrock-agentcore-control")
        self.ac_data = self.session.client("bedrock-agentcore", region_name=self.aws_region)
        self.iam = self.session.client("iam")
        self.s3 = self.session.client("s3", region_name=self.aws_region)

        self.account_id = self.sts.get_caller_identity()["Account"]
        base, dns = _compact_name(self.workshop_prefix)
        self.names = {
            "user_pool_name": f"{base}_runtime_pool",
            "user_client_name": f"{base}_runtime_user_client",
            "provider_name": f"{base}_google_provider",
            "gateway_name": f"{dns}-gateway",
            "target_name": f"{dns}-google-docs-target",
            "runtime_agent_name": f"{base}_runtime_agent",
        }
        self.state: dict[str, Any] = {
            "names": self.names,
            "aws_region": self.aws_region,
            "account_id": self.account_id,
        }
        self._callback_server = None
        self._callback_thread = None

    def _normalize_aws_env(self) -> None:
        os.environ["AWS_PROFILE"] = self.aws_profile
        os.environ["AWS_REGION"] = self.aws_region
        os.environ["AWS_DEFAULT_REGION"] = self.aws_region
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            os.environ.pop(key, None)

    def _require_env(self, *keys: str) -> None:
        missing = [key for key in keys if not os.getenv(key)]
        if missing:
            raise ValueError(f"Missing required environment variables: {missing}")

    def _callback_endpoint(self) -> tuple[str, int, str]:
        return callback_endpoint(self.oauth_return_url)

    def _callback_server_info(
        self,
        host: str,
        port: int,
        path: str,
        *,
        status: str = "running",
    ) -> dict[str, Any]:
        return callback_server_info(self.oauth_return_url, host, port, path, status=status)

    def _reset_callback_server_state(self, server: Any) -> None:
        reset_callback_server_state(server)

    def start_callback_server(self) -> dict[str, Any]:
        host, port, path = self._callback_endpoint()
        server, thread, info = start_local_callback_server(
            self.oauth_return_url,
            current_server=self._callback_server,
            current_thread=self._callback_thread,
        )
        self._callback_server = server
        self._callback_thread = thread
        self.state["callback_server"] = info
        return dict(self.state["callback_server"])

    def stop_callback_server(self) -> None:
        self.state["callback_server"] = stop_local_callback_server(
            self.oauth_return_url,
            self._callback_server,
            self._callback_thread,
        )
        self._callback_server = None
        self._callback_thread = None

    def wait_for_callback(self, timeout_sec: int = 180) -> dict[str, Any]:
        if self._callback_server is None:
            self.start_callback_server()
        assert self._callback_server is not None
        return wait_for_local_callback(
            self.oauth_return_url,
            self._callback_server,
            timeout_sec=timeout_sec,
        )

    def open_consent_in_browser(self, authorization_url: str) -> None:
        if not authorization_url:
            raise ValueError("authorization_url is required.")
        self.start_callback_server()
        opened = webbrowser.open(authorization_url, new=1, autoraise=True)
        self.state["last_browser_open"] = {
            "authorization_url": authorization_url,
            "opened": bool(opened),
        }

    def complete_live_consent(
        self,
        first_result: dict[str, Any],
        *,
        timeout_sec: int = 180,
        open_browser: bool = True,
    ) -> dict[str, Any]:
        authorization_url = str(first_result.get("authorization_url") or "").strip()
        if open_browser:
            self.open_consent_in_browser(authorization_url)
        callback = self.wait_for_callback(timeout_sec=timeout_sec)
        session_uri = self.complete_runtime_consent(
            str(first_result.get("oauth_session_uri") or "").strip()
        )
        return {
            "authorization_url": authorization_url,
            "oauth_session_uri": session_uri,
            "callback": callback,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
            "account_id": self.account_id,
            "oauth_return_url": self.oauth_return_url,
            "names": self.names,
        }

    def _list_all(
        self,
        method: Any,
        result_key: str,
        *,
        limit_key: str = "maxResults",
        token_key: str = "nextToken",
        limit: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_token = None
        while True:
            request = dict(kwargs)
            request[limit_key] = limit
            if next_token:
                request[token_key] = next_token
            response = method(**request)
            items.extend(response.get(result_key, []))
            next_token = response.get(token_key)
            if not next_token:
                break
        return items

    def _find_user_pool_id(self, pool_name: str) -> str | None:
        for pool in self._list_all(
            self.cognito.list_user_pools,
            "UserPools",
            limit_key="MaxResults",
            limit=60,
        ):
            if pool.get("Name") == pool_name:
                return pool.get("Id")
        return None

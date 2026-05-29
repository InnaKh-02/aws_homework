from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from bedrock_agentcore_starter_toolkit.operations.runtime.create_role import (
    get_or_create_runtime_execution_role,
)
from bedrock_agentcore_starter_toolkit.services.s3 import get_or_create_s3_bucket
from bedrock_agentcore_starter_toolkit.utils.runtime.config import (
    get_agentcore_directory,
)
from bedrock_agentcore_starter_toolkit.utils.runtime.create_with_iam_eventual_consistency import (
    retry_create_with_eventual_iam_consistency,
)
from bedrock_agentcore_starter_toolkit.utils.runtime.entrypoint import build_entrypoint_array
from bedrock_agentcore_starter_toolkit.utils.runtime.package import CodeZipPackager

from .demo_core import (
    DEFAULT_RUNTIME_PROMPT_1,
    DEFAULT_RUNTIME_PROMPT_2,
    DEFAULT_RUNTIME_THREAD_ID,
    DESIRED_MCP_VERSION,
)


LOG = logging.getLogger(__name__)


class WorkshopE2ERuntimeMixin:
    def _runtime_authorizer(self) -> dict[str, Any]:
        if "inbound" not in self.state:
            self.ensure_inbound_auth()
        return {
            "customJWTAuthorizer": {
                "discoveryUrl": self.state["inbound"]["discovery_url"],
                "allowedClients": [self.state["inbound"]["user_client_id"]],
            }
        }

    def _runtime_network_configuration(self) -> dict[str, Any]:
        network_mode = os.getenv("RUNTIME_NETWORK_MODE", "PUBLIC").strip().upper() or "PUBLIC"
        if network_mode == "VPC":
            subnets = [
                item.strip()
                for item in os.getenv("RUNTIME_SUBNETS", "").split(",")
                if item.strip()
            ]
            security_groups = [
                item.strip()
                for item in os.getenv("RUNTIME_SECURITY_GROUPS", "").split(",")
                if item.strip()
            ]
            if not subnets or not security_groups:
                raise ValueError(
                    "RUNTIME_NETWORK_MODE=VPC requires both RUNTIME_SUBNETS and "
                    "RUNTIME_SECURITY_GROUPS."
                )
            return {
                "networkMode": "VPC",
                "networkModeConfig": {
                    "subnets": subnets,
                    "securityGroups": security_groups,
                },
            }

        return {"networkMode": "PUBLIC"}

    def _find_runtime_by_name(self) -> dict[str, Any] | None:
        matches = [
            item
            for item in self._list_all(
                self.ac_control.list_agent_runtimes, "agentRuntimes"
            )
            if item.get("agentRuntimeName") == self.names["runtime_agent_name"]
        ]
        matches.sort(key=lambda item: str(item.get("lastUpdatedAt", "")), reverse=True)
        return matches[0] if matches else None

    def wait_runtime_ready(self, runtime_id: str, timeout_sec: int = 1200) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            runtime = self.ac_control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = runtime.get("status")
            if status == "READY":
                return runtime
            if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "FAILED"}:
                raise RuntimeError(f"Runtime entered failure status: {status}")
            time.sleep(10)
        raise TimeoutError(f"Runtime {runtime_id} did not reach READY in time.")

    def _ensure_deploy_prereqs(self) -> None:
        if not self.runtime_file.exists():
            raise FileNotFoundError(self.runtime_file)
        if not self.requirements_file.exists():
            raise FileNotFoundError(self.requirements_file)
        if not shutil.which("uv"):
            raise RuntimeError("uv is required for direct_code_deploy packaging.")
        if not shutil.which("zip"):
            raise RuntimeError("zip is required for direct_code_deploy packaging.")

    def _build_runtime_stage_dir(self, stage_dir: Path) -> None:
        stage_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.runtime_file, stage_dir / self.runtime_file.name)
        shutil.copy2(self.requirements_file, stage_dir / self.requirements_file.name)

    def _package_runtime(self) -> tuple[str, str, list[str]]:
        self._ensure_deploy_prereqs()
        cache_dir = get_agentcore_directory(
            self.root,
            self.names["runtime_agent_name"],
            source_path=".",
        )
        packager = CodeZipPackager()
        with tempfile.TemporaryDirectory(
            prefix=f"{self.names['runtime_agent_name']}_src_"
        ) as tmp_dir:
            stage_dir = Path(tmp_dir)
            self._build_runtime_stage_dir(stage_dir)
            deployment_zip, has_otel_distro = packager.create_deployment_package(
                source_dir=stage_dir,
                agent_name=self.names["runtime_agent_name"],
                cache_dir=cache_dir,
                runtime_version="PYTHON_3_12",
                requirements_file=self.requirements_file,
                force_rebuild_deps=False,
            )
        bucket_name = get_or_create_s3_bucket(
            self.names["runtime_agent_name"],
            self.account_id,
            self.aws_region,
        )
        s3_key = f"{self.names['runtime_agent_name']}/deployment.zip"
        self.s3.upload_file(
            str(deployment_zip),
            bucket_name,
            s3_key,
            ExtraArgs={"ExpectedBucketOwner": self.account_id},
        )
        entrypoint_array = build_entrypoint_array(
            self.runtime_file.name,
            has_otel_distro,
            observability_enabled=False,
        )
        return bucket_name, s3_key, entrypoint_array

    def deploy_runtime(self) -> dict[str, Any]:
        if "gateway" not in self.state:
            self.ensure_gateway_stack()

        bucket_name, s3_key, entrypoint_array = self._package_runtime()
        role_arn = get_or_create_runtime_execution_role(
            session=self.session,
            logger=LOG,
            region=self.aws_region,
            account_id=self.account_id,
            agent_name=self.names["runtime_agent_name"],
            agent_config=None,
        )
        params = {
            "agentRuntimeArtifact": {
                "codeConfiguration": {
                    "code": {"s3": {"bucket": bucket_name, "prefix": s3_key}},
                    "runtime": "PYTHON_3_12",
                    "entryPoint": entrypoint_array,
                }
            },
            "roleArn": role_arn,
            "networkConfiguration": self._runtime_network_configuration(),
            "authorizerConfiguration": self._runtime_authorizer(),
            "environmentVariables": {
                "GATEWAY_URL": self.state["gateway"]["gateway_url"],
                "GOOGLE_DOCS_TOOL_NAME": self.state["gateway"]["google_docs_tool_name"],
                "GATEWAY_MCP_VERSION": self.state["gateway"].get(
                    "mcp_version", DESIRED_MCP_VERSION
                ),
                "AWS_REGION": self.aws_region,
                "GOOGLE_MODEL_ID": self.google_model_id,
                "GOOGLE_API_KEY": os.environ["GOOGLE_API_KEY"],
            },
        }
        existing = self._find_runtime_by_name()
        if existing and existing.get("status") in {"CREATING", "UPDATING"}:
            self.wait_runtime_ready(existing["agentRuntimeId"])

        if existing:
            response = retry_create_with_eventual_iam_consistency(
                lambda: self.ac_control.update_agent_runtime(
                    agentRuntimeId=existing["agentRuntimeId"],
                    **params,
                ),
                role_arn,
            )
            runtime_id = existing["agentRuntimeId"]
        else:
            response = retry_create_with_eventual_iam_consistency(
                lambda: self.ac_control.create_agent_runtime(
                    agentRuntimeName=self.names["runtime_agent_name"],
                    **params,
                ),
                role_arn,
            )
            runtime_id = response["agentRuntimeId"]

        runtime = self.wait_runtime_ready(runtime_id)
        self.state["runtime"] = {
            "runtime_id": runtime["agentRuntimeId"],
            "runtime_arn": runtime["agentRuntimeArn"],
            "runtime_status": runtime.get("status"),
            "runtime_role_arn": role_arn,
            "artifact_s3_uri": f"s3://{bucket_name}/{s3_key}",
        }
        return self.state["runtime"]

    def _runtime_url(self) -> str:
        if "runtime" not in self.state:
            runtime = self._find_runtime_by_name()
            if not runtime:
                raise RuntimeError("No runtime found. Run deploy step first.")
            self.state["runtime"] = {
                "runtime_id": runtime["agentRuntimeId"],
                "runtime_arn": runtime["agentRuntimeArn"],
                "runtime_status": runtime.get("status"),
            }
        runtime_arn = self.state["runtime"]["runtime_arn"]
        return (
            f"https://bedrock-agentcore.{self.aws_region}.amazonaws.com/runtimes/"
            f"{urllib.parse.quote(runtime_arn, safe='')}/invocations"
        )

    def detect_expected_app_version(self) -> str:
        configured = os.getenv("EXPECTED_APP_VERSION", "").strip()
        if configured:
            return configured
        match = re.search(
            r"^APP_VERSION\\s*=\\s*['\\\"]([^'\\\"]+)['\\\"]",
            self.runtime_file.read_text(),
            flags=re.MULTILINE,
        )
        return match.group(1) if match else ""

    def build_runtime_payload(
        self,
        *,
        prompt: str | None = None,
        thread_id: str | None = None,
        max_steps: int = 5,
        max_doc_calls: int = 1,
        oauth_session_uri: str | None = None,
        force_authentication: bool = False,
    ) -> dict[str, Any]:
        runtime_thread_id = thread_id or os.getenv(
            "RUNTIME_THREAD_ID",
            DEFAULT_RUNTIME_THREAD_ID,
        )
        if len(runtime_thread_id) < 33:
            raise ValueError(
                "RUNTIME_THREAD_ID must be at least 33 characters long."
            )

        payload = {
            "prompt": prompt or os.getenv("RUNTIME_PROMPT_1", DEFAULT_RUNTIME_PROMPT_1),
            "doc_id": os.environ["GOOGLE_DOC_ID"],
            "thread_id": runtime_thread_id,
            "mcp_session_id": runtime_thread_id,
            "max_steps": max_steps,
            "max_doc_calls": max_doc_calls,
        }
        if oauth_session_uri:
            payload["oauth_session_uri"] = oauth_session_uri.strip()
        if force_authentication:
            payload["force_authentication"] = True
        return payload

    def invoke_runtime(
        self,
        *,
        payload: dict[str, Any] | None = None,
        prompt: str | None = None,
        thread_id: str | None = None,
        max_steps: int = 5,
        max_doc_calls: int = 1,
    ) -> dict[str, Any]:
        runtime_payload = dict(
            payload
            or self.build_runtime_payload(
                prompt=prompt,
                thread_id=thread_id,
                max_steps=max_steps,
                max_doc_calls=max_doc_calls,
            )
        )
        runtime_thread_id = str(
            runtime_payload.get("thread_id")
            or thread_id
            or os.getenv("RUNTIME_THREAD_ID", DEFAULT_RUNTIME_THREAD_ID)
        ).strip()
        if len(runtime_thread_id) < 33:
            raise ValueError(
                "RUNTIME_THREAD_ID must be at least 33 characters long."
            )
        runtime_payload["thread_id"] = runtime_thread_id
        runtime_payload.setdefault("prompt", prompt or os.getenv("RUNTIME_PROMPT_1", DEFAULT_RUNTIME_PROMPT_1))
        runtime_payload.setdefault("doc_id", os.environ["GOOGLE_DOC_ID"])
        runtime_payload.setdefault("mcp_session_id", runtime_thread_id)
        runtime_payload.setdefault("max_steps", max_steps)
        runtime_payload.setdefault("max_doc_calls", max_doc_calls)
        runtime_url = self._runtime_url()

        def do_request(token: str):
            request_payload = dict(runtime_payload)
            request_payload["user_access_token"] = token
            return requests.post(
                runtime_url,
                params={"qualifier": "DEFAULT"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtime_thread_id,
                },
                json=request_payload,
                timeout=(20, 90),
            )

        token = self.get_user_access_token()
        response = do_request(token)
        if response.status_code == 401 and "Token has expired" in response.text:
            token = self.get_user_access_token()
            response = do_request(token)

        if not response.ok:
            raise RuntimeError(
                f"Runtime invoke failed: {response.status_code} {response.text[:500]}"
            )
        result = response.json()
        self.state["last_runtime_payload"] = runtime_payload
        self.state["runtime_user_access_token"] = token
        self.state["runtime_thread_id"] = runtime_thread_id
        self.state["runtime_oauth_session_uri"] = (
            result.get("oauth_session_uri") or ""
        ).strip()
        return result

    def complete_runtime_consent(self, oauth_session_uri: str | None = None) -> str:
        session_uri = (
            oauth_session_uri
            or self.state.get("runtime_oauth_session_uri", "")
            or os.getenv("RUNTIME_OAUTH_SESSION_URI", "")
        ).strip()
        if not session_uri:
            return ""
        token = (
            self.state.get("runtime_user_access_token")
            or self.state.get("user_access_token")
            or self.get_user_access_token()
        )
        self.ac_data.complete_resource_token_auth(
            userIdentifier={"userToken": token},
            sessionUri=session_uri,
        )
        self.state["runtime_oauth_session_uri"] = session_uri
        return session_uri

    def invoke_after_consent(
        self,
        first_result: dict[str, Any] | None = None,
        *,
        payload: dict[str, Any] | None = None,
        prompt: str | None = None,
        max_steps: int = 5,
        max_doc_calls: int = 1,
    ) -> dict[str, Any]:
        session_uri = ""
        if first_result:
            session_uri = (first_result.get("oauth_session_uri") or "").strip()
        self.complete_runtime_consent(session_uri)
        next_payload = payload or self.build_runtime_payload(
            prompt=prompt or os.getenv("RUNTIME_PROMPT_2", DEFAULT_RUNTIME_PROMPT_2),
            thread_id=self.state.get("runtime_thread_id"),
            max_steps=max_steps,
            max_doc_calls=max_doc_calls,
        )
        return self.invoke_runtime(payload=next_payload)

    def run_runtime_demo_step(
        self,
        label: str,
        *,
        payload: dict[str, Any] | None = None,
        prompt: str | None = None,
        thread_id: str | None = None,
        max_steps: int = 5,
        max_doc_calls: int = 1,
    ) -> dict[str, Any]:
        result = self.invoke_runtime(
            payload=payload,
            prompt=prompt,
            thread_id=thread_id,
            max_steps=max_steps,
            max_doc_calls=max_doc_calls,
        )
        self.assert_runtime_version(result)
        self.print_runtime_result(label, result)
        return result

    def print_runtime_result(self, label: str, payload: dict[str, Any]) -> None:
        print(f"\n=== {label} ===")
        answer = payload.get("answer") or {}
        assistant_lines: list[str] = []
        bullets = answer.get("bullets") or []
        if bullets:
            assistant_lines.extend(f"- {item}" for item in bullets if str(item).strip())
        elif answer.get("message"):
            assistant_lines.append(str(answer.get("message")).strip())
        else:
            response_text = str(payload.get("response") or "").strip()
            if response_text:
                if "\n\nSources:\n" in response_text:
                    response_text = response_text.split("\n\nSources:\n", 1)[0].rstrip()
                assistant_lines.append(response_text)

        print("ASSISTANT RESPONSE:")
        if assistant_lines:
            for line in assistant_lines:
                print(line)
        else:
            print("<empty>")

        sources = answer.get("sources") or []
        if sources:
            print("\nSOURCES:")
            for item in sources:
                print(f"- {item}")

        trace = payload.get("tool_trace") or []
        if trace:
            print("\nTOOL TRACE:")
            for row in trace:
                print(
                    f"- step={row.get('step')} event={row.get('event')} tool={row.get('tool')}"
                )

    def assert_runtime_version(self, payload: dict[str, Any]) -> None:
        expected = self.detect_expected_app_version()
        actual = str(payload.get("app_version") or "")
        if expected and actual != expected:
            raise RuntimeError(
                f"Stale runtime detected: app_version={actual!r}, expected={expected!r}."
            )

    def cleanup(self) -> None:
        self.stop_callback_server()
        runtime = self._find_runtime_by_name()
        if runtime:
            try:
                self.ac_control.delete_agent_runtime(
                    agentRuntimeId=runtime["agentRuntimeId"]
                )
            except Exception as exc:
                print(f"Warning deleting runtime: {exc}")

        gateway_id = self.state.get("gateway", {}).get("gateway_id")
        if gateway_id:
            try:
                for target in self._list_all(
                    self.ac_control.list_gateway_targets,
                    "items",
                    gatewayIdentifier=gateway_id,
                ):
                    self.ac_control.delete_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=target["targetId"],
                    )
            except Exception as exc:
                print(f"Warning deleting targets: {exc}")
            try:
                self.ac_control.delete_gateway(gatewayIdentifier=gateway_id)
            except Exception as exc:
                print(f"Warning deleting gateway: {exc}")

        if self.names["provider_name"]:
            try:
                self.ac_control.delete_oauth2_credential_provider(
                    name=self.names["provider_name"]
                )
            except Exception as exc:
                print(f"Warning deleting provider: {exc}")

        pool_id = self.state.get("inbound", {}).get("user_pool_id")
        if pool_id:
            try:
                self.cognito.delete_user_pool(UserPoolId=pool_id)
            except Exception as exc:
                print(f"Warning deleting user pool: {exc}")

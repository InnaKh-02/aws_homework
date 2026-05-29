from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from .demo_core import DESIRED_MCP_VERSION


class WorkshopE2EGatewayMixin:
    def get_user_access_token(self) -> str:
        if "inbound" not in self.state:
            self.ensure_inbound_auth()
        response = self.cognito.initiate_auth(
            ClientId=self.state["inbound"]["user_client_id"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": self.state["inbound"]["demo_username"],
                "PASSWORD": self.state["inbound"]["demo_password"],
            },
        )
        token = response["AuthenticationResult"]["AccessToken"]
        self.state["user_access_token"] = token
        return token

    def ensure_inbound_auth(self) -> dict[str, Any]:
        pool_id = self._find_user_pool_id(self.names["user_pool_name"])
        if not pool_id:
            created = self.cognito.create_user_pool(
                PoolName=self.names["user_pool_name"],
                Policies={
                    "PasswordPolicy": {
                        "MinimumLength": 10,
                        "RequireUppercase": True,
                        "RequireLowercase": True,
                        "RequireNumbers": True,
                        "RequireSymbols": False,
                        "TemporaryPasswordValidityDays": 7,
                    }
                },
                AutoVerifiedAttributes=["email"],
            )
            pool_id = created["UserPool"]["Id"]

        clients = self.cognito.list_user_pool_clients(
            UserPoolId=pool_id,
            MaxResults=60,
        ).get("UserPoolClients", [])
        client = next(
            (item for item in clients if item.get("ClientName") == self.names["user_client_name"]),
            None,
        )
        if not client:
            client = self.cognito.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName=self.names["user_client_name"],
                GenerateSecret=False,
                ExplicitAuthFlows=[
                    "ALLOW_USER_PASSWORD_AUTH",
                    "ALLOW_REFRESH_TOKEN_AUTH",
                    "ALLOW_USER_SRP_AUTH",
                ],
            )["UserPoolClient"]

        demo_username = os.getenv("DEMO_USERNAME", "workshop.user")
        demo_password = os.getenv("DEMO_PASSWORD", "DemoPassw0rd2026!")

        try:
            self.cognito.admin_get_user(UserPoolId=pool_id, Username=demo_username)
        except self.cognito.exceptions.UserNotFoundException:
            self.cognito.admin_create_user(
                UserPoolId=pool_id,
                Username=demo_username,
                TemporaryPassword=demo_password,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": "workshop.user@example.com"}
                ],
            )

        self.cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=demo_username,
            Password=demo_password,
            Permanent=True,
        )

        inbound = {
            "user_pool_id": pool_id,
            "user_client_id": client["ClientId"],
            "discovery_url": (
                f"https://cognito-idp.{self.aws_region}.amazonaws.com/"
                f"{pool_id}/.well-known/openid-configuration"
            ),
            "demo_username": demo_username,
            "demo_password": demo_password,
        }
        self.state["inbound"] = inbound
        self.state["user_access_token"] = self.get_user_access_token()
        return inbound

    def ensure_google_provider(self) -> dict[str, Any]:
        provider_exists = any(
            item.get("name") == self.names["provider_name"]
            for item in self._list_all(
                self.ac_control.list_oauth2_credential_providers,
                "credentialProviders",
                limit=20,
            )
        )
        request = {
            "name": self.names["provider_name"],
            "credentialProviderVendor": "GoogleOauth2",
            "oauth2ProviderConfigInput": {
                "googleOauth2ProviderConfig": {
                    "clientId": os.environ["GOOGLE_CLIENT_ID"],
                    "clientSecret": os.environ["GOOGLE_CLIENT_SECRET"],
                }
            },
        }

        if provider_exists:
            operation = self.ac_control.update_oauth2_credential_provider(**request)
        else:
            operation = self.ac_control.create_oauth2_credential_provider(**request)

        provider = self.ac_control.get_oauth2_credential_provider(
            name=self.names["provider_name"]
        )
        data = {
            "provider_name": self.names["provider_name"],
            "provider_arn": provider["credentialProviderArn"],
            "callback_url": operation.get("callbackUrl")
            or provider.get("callbackUrl", ""),
        }
        self.state["provider"] = data
        return data

    def _gateway_authorizer(self) -> dict[str, Any]:
        if "inbound" not in self.state:
            self.ensure_inbound_auth()
        return {
            "customJWTAuthorizer": {
                "discoveryUrl": self.state["inbound"]["discovery_url"],
                "allowedClients": [self.state["inbound"]["user_client_id"]],
            }
        }

    def _gateway_has_version(self, gateway: dict[str, Any], version: str) -> bool:
        supported = (
            ((gateway.get("protocolConfiguration") or {}).get("mcp") or {}).get(
                "supportedVersions"
            )
            or []
        )
        return version in supported

    def _ensure_gateway_role(self) -> str:
        role_name = f"{self.names['gateway_name']}-role"[:64]
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        inline_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "lambda:InvokeFunction",
                        "execute-api:Invoke",
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                    ],
                    "Resource": "*",
                },
                {
                    'Sid': 'GetResourceOauth2Token',
                    'Effect': 'Allow',
                    'Action': [
                        'bedrock-agentcore:GetResourceOauth2Token'
                    ],
                    'Resource': [
                        'arn:aws:bedrock-agentcore:us-east-1:501578625560:token-vault/*/oauth2credentialprovider/*',
                        'arn:aws:bedrock-agentcore:us-east-1:501578625560:token-vault/*',
                        'arn:aws:bedrock-agentcore:us-east-1:501578625560:workload-identity-directory/*/workload-identity/*',
                        'arn:aws:bedrock-agentcore:us-east-1:501578625560:workload-identity-directory/*',
                        ]
                    },
                {
                    'Sid': 'GetWorkloadAccessTokenForJWT',
                    'Effect': 'Allow',
                    'Action': [
                        'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
                    ],
                    'Resource': [
                        'arn:aws:bedrock-agentcore:us-east-1:501578625560:workload-identity-directory/*',
                        'arn:aws:bedrock-agentcore:us-east-1:501578625560:workload-identity-directory/*/workload-identity/*',
                    ]
                },
                {
                    'Sid': 'GetSecretValue',
                    'Effect': 'Allow',
                    'Action': [
                        'secretsmanager:GetSecretValue',
                    ],
                    'Resource': [
                        'arn:aws:secretsmanager:us-east-1:501578625560:secret:*'
                    ]
                }
            ],
        }
        try:
            role_arn = self.iam.get_role(RoleName=role_name)["Role"]["Arn"]
        except self.iam.exceptions.NoSuchEntityException:
            created = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="AgentCore Workshop Gateway execution role",
            )
            role_arn = created["Role"]["Arn"]
        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName="AgentCoreWorkshopGatewayInlinePolicy",
            PolicyDocument=json.dumps(inline_policy),
        )
        time.sleep(5)
        return role_arn

    def ensure_gateway(self) -> dict[str, Any]:
        selected = None
        for gateway in sorted(
            (
                item
                for item in self._list_all(self.ac_control.list_gateways, "items")
                if (item.get("name") or "") == self.names["gateway_name"]
            ),
            key=lambda item: str(item.get("updatedAt", "")),
            reverse=True,
        ):
            full = self.ac_control.get_gateway(gatewayIdentifier=gateway["gatewayId"])
            if full.get("status") == "READY" and self._gateway_has_version(
                full, DESIRED_MCP_VERSION
            ):
                selected = full
                break

        if not selected:
            role_arn = self._ensure_gateway_role()
            created = self.ac_control.create_gateway(
                name=self.names["gateway_name"],
                roleArn=role_arn,
                protocolType="MCP",
                protocolConfiguration={
                    "mcp": {
                        "searchType": "SEMANTIC",
                        "supportedVersions": [DESIRED_MCP_VERSION],
                    }
                },
                authorizerType="CUSTOM_JWT",
                authorizerConfiguration=self._gateway_authorizer(),
            )
            gateway_id = created["gatewayId"]
            for _ in range(60):
                full = self.ac_control.get_gateway(gatewayIdentifier=gateway_id)
                if full.get("status") == "READY":
                    selected = full
                    break
                time.sleep(3)
            if not selected:
                raise TimeoutError("Gateway did not reach READY state.")

        self.state["gateway"] = {
            "gateway_id": selected["gatewayId"],
            "gateway_arn": selected["gatewayArn"],
            "gateway_url": selected["gatewayUrl"],
            "gateway_role_arn": selected["roleArn"],
            "mcp_supported_versions": (
                ((selected.get("protocolConfiguration") or {}).get("mcp") or {}).get(
                    "supportedVersions",
                    [],
                )
            ),
            "workload_identity_arn": (
                (selected.get("workloadIdentityDetails") or {}).get(
                    "workloadIdentityArn"
                )
                or ""
            ),
            "mcp_version": DESIRED_MCP_VERSION,
        }
        return self.state["gateway"]

    def _google_docs_openapi(self) -> dict[str, Any]:
        return {
            "openapi": "3.0.1",
            "info": {"title": "Google Docs Minimal API", "version": "1.0.0"},
            "servers": [{"url": "https://docs.googleapis.com"}],
            "paths": {
                "/v1/documents/{documentId}": {
                    "get": {
                        "operationId": "getDocument",
                        "summary": "Get Google Doc by ID",
                        "parameters": [
                            {
                                "name": "documentId",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {
                            "200": {"description": "Google Docs document payload"}
                        },
                    }
                }
            },
        }

    def _ensure_target(
        self,
        gateway_id: str,
        target_name: str,
        inline_openapi: dict[str, Any],
        credential_config: list[dict[str, Any]],
    ) -> dict[str, Any]:
        target_cfg = {
            "mcp": {
                "openApiSchema": {"inlinePayload": json.dumps(inline_openapi)}
            }
        }
        existing = next(
            (
                item
                for item in self._list_all(
                    self.ac_control.list_gateway_targets,
                    "items",
                    gatewayIdentifier=gateway_id,
                )
                if item.get("name") == target_name
            ),
            None,
        )
        if existing:
            target_id = existing["targetId"]
            self.ac_control.update_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
                name=target_name,
                targetConfiguration=target_cfg,
                credentialProviderConfigurations=credential_config,
            )
        else:
            created = self.ac_control.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=target_name,
                targetConfiguration=target_cfg,
                credentialProviderConfigurations=credential_config,
            )
            target_id = created["targetId"]

        for _ in range(40):
            target = self.ac_control.get_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
            )
            if target.get("status") == "READY":
                return target
            time.sleep(3)
        raise TimeoutError(f"Target {target_name} did not reach READY state.")

    def _ensure_workload_identity_return_url(
        self, workload_identity_arn: str, return_url: str
    ) -> dict[str, Any] | None:
        if not workload_identity_arn or not return_url:
            return None
        workload_name = workload_identity_arn.rsplit("/", 1)[-1]
        current = self.ac_control.get_workload_identity(name=workload_name)
        urls = list(current.get("allowedResourceOauth2ReturnUrls") or [])
        if return_url in urls:
            return current
        urls.append(return_url)
        return self.ac_control.update_workload_identity(
            name=workload_name,
            allowedResourceOauth2ReturnUrls=sorted(set(urls)),
        )

    def ensure_google_docs_target(self) -> dict[str, Any]:
        if "provider" not in self.state:
            self.ensure_google_provider()
        if "gateway" not in self.state:
            self.ensure_gateway()

        credential_cfg = [
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": self.state["provider"]["provider_arn"],
                        "scopes": [
                            "https://www.googleapis.com/auth/documents.readonly","https://www.googleapis.com/auth/drive.readonly"
                        ],
                        "grantType": "AUTHORIZATION_CODE",
                        "defaultReturnUrl": self.oauth_return_url,
                    }
                },
            }
        ]

        target = self._ensure_target(
            gateway_id=self.state["gateway"]["gateway_id"],
            target_name=self.names["target_name"],
            inline_openapi=self._google_docs_openapi(),
            credential_config=credential_cfg,
        )
        self.state["gateway"]["target_id"] = target["targetId"]
        self.state["gateway"]["google_docs_tool_name"] = (
            f"{self.names['target_name']}___getDocument"
        )
        self.state["gateway"]["default_return_url"] = self.oauth_return_url

        workload = self._ensure_workload_identity_return_url(
            self.state["gateway"].get("workload_identity_arn", ""),
            self.oauth_return_url,
        )
        if workload is not None:
            self.state["gateway"]["workload_identity_name"] = workload.get("name", "")
            self.state["gateway"]["allowed_return_urls"] = workload.get(
                "allowedResourceOauth2ReturnUrls",
                [],
            )
        return self.state["gateway"]

    def ensure_gateway_stack(self) -> dict[str, Any]:
        self.ensure_inbound_auth()
        self.ensure_google_provider()
        self.ensure_gateway()
        self.ensure_google_docs_target()
        self.health_gate()
        return {
            "inbound": self.state["inbound"],
            "provider": self.state["provider"],
            "gateway": self.state["gateway"],
        }

    def health_gate(self) -> None:
        errors = []
        gateway = self.state.get("gateway", {})
        provider = self.state.get("provider", {})
        if DESIRED_MCP_VERSION not in gateway.get("mcp_supported_versions", []):
            errors.append(
                f"Gateway MCP version mismatch: {gateway.get('mcp_supported_versions', [])}"
            )
        if not gateway.get("google_docs_tool_name"):
            errors.append("google_docs_tool_name missing")
        if not provider.get("callback_url"):
            errors.append("provider.callback_url missing")
        if errors:
            raise RuntimeError("Health gate failed: " + "; ".join(errors))

    def smoke_test_gateway(self) -> dict[str, Any]:
        if "gateway" not in self.state:
            self.ensure_gateway_stack()
        self.health_gate()

        def do_request(token: str):
            return requests.post(
                self.state["gateway"]["gateway_url"],
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": self.state["gateway"].get(
                        "mcp_version", DESIRED_MCP_VERSION
                    ),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": self.state["gateway"]["google_docs_tool_name"],
                        "arguments": {"documentId": os.environ["GOOGLE_DOC_ID"]},
                    },
                },
                timeout=(10, 60),
            )

        token = self.get_user_access_token()
        response = do_request(token)
        if response.status_code == 401 and "Token has expired" in response.text:
            token = self.get_user_access_token()
            response = do_request(token)
        if response.status_code != 200:
            raise RuntimeError(
                f"Gateway smoke-test failed: {response.status_code} {response.text[:300]}"
            )
        payload = response.json()
        error = payload.get("error") or {}
        result = payload.get("result") or {}
        text = "".join(
            str(block.get("text", ""))
            for block in result.get("content", []) or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        status = "healthy"
        authorization_url = ""
        if error.get("code") == -32042:
            status = "oauth_challenge"
            authorization_url = (
                ((error.get("data") or {}).get("elicitations") or [{}])[0].get("url", "")
            )
        elif result.get("isError") and "internal error" in text.lower():
            raise RuntimeError(
                "Gateway smoke-test failed: Google Docs tool returned generic internal error."
            )
        return {
            "status": status,
            "authorization_url": authorization_url,
            "payload": payload,
        }

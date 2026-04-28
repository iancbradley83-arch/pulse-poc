"""
Async client for the Railway GraphQL API.

Methods:
  latest_deployment(project_id, service_id)
      -> {"id": str, "status": str, "createdAt": str, "commitHash": str}

  variables(project_id, environment_id, service_id)
      -> dict[str, str]  (env var name -> value)

  recent_logs(project_id, service_id, n=20)
      -> list[{"timestamp": str, "severity": str, "message": str}]
         filtered to WARN/ERROR severity, most-recent N entries.

Raises RailwayError on network failures or unexpected responses.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

RAILWAY_GRAPHQL_URL = "https://backboard.railway.app/graphql/v2"
REQUEST_TIMEOUT = 10.0


class RailwayError(Exception):
    """Raised when a Railway request fails or returns an unexpected response."""


class RailwayClient:
    def __init__(self, api_token: str) -> None:
        self._token = api_token
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            resp = await self._client.post(RAILWAY_GRAPHQL_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                errs = data["errors"]
                raise RailwayError(f"GraphQL errors: {errs}")
            return data.get("data", {})
        except httpx.TimeoutException as exc:
            raise RailwayError("unreachable") from exc
        except httpx.HTTPStatusError as exc:
            raise RailwayError(f"http {exc.response.status_code}") from exc
        except RailwayError:
            raise
        except Exception as exc:
            raise RailwayError(f"request failed: {exc}") from exc

    async def latest_deployment(
        self, project_id: str, service_id: str
    ) -> Dict[str, Any]:
        """
        Return the most recent deployment for the given service.
        Shape: {"id": str, "status": str, "createdAt": str, "commitHash": str}
        """
        query = """
        query LatestDeployment($projectId: String!, $serviceId: String!) {
          deployments(
            first: 1
            input: { projectId: $projectId, serviceId: $serviceId }
          ) {
            edges {
              node {
                id
                status
                createdAt
                meta
              }
            }
          }
        }
        """
        data = await self._query(query, {"projectId": project_id, "serviceId": service_id})
        try:
            edges = data["deployments"]["edges"]
            if not edges:
                raise RailwayError("no deployments found")
            node = edges[0]["node"]
            meta = node.get("meta") or {}
            commit_hash = meta.get("commitHash", "")
            return {
                "id": node["id"],
                "status": node["status"],
                "createdAt": node["createdAt"],
                "commitHash": commit_hash,
            }
        except RailwayError:
            raise
        except Exception as exc:
            raise RailwayError(f"couldn't parse deployment response: {exc}") from exc

    async def variables(
        self, project_id: str, environment_id: str, service_id: str
    ) -> Dict[str, str]:
        """
        Return env var name -> value dict for the given service + environment.
        """
        query = """
        query Variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
          variables(
            projectId: $projectId
            environmentId: $environmentId
            serviceId: $serviceId
          )
        }
        """
        data = await self._query(
            query,
            {
                "projectId": project_id,
                "environmentId": environment_id,
                "serviceId": service_id,
            },
        )
        try:
            raw = data.get("variables", {})
            if not isinstance(raw, dict):
                raise RailwayError(f"unexpected variables shape: {type(raw)}")
            return {str(k): str(v) for k, v in raw.items()}
        except RailwayError:
            raise
        except Exception as exc:
            raise RailwayError(f"couldn't parse variables response: {exc}") from exc

    async def recent_logs(
        self,
        project_id: str,
        service_id: str,
        n: int = 20,
    ) -> List[Dict[str, str]]:
        """
        Return the most-recent N WARN/ERROR log lines from the latest deployment.

        Fetches up to 500 runtime log entries, filters to severity in
        {WARNING, WARN, ERROR}, then returns the last N entries (most-recent last).

        Shape of each entry: {"timestamp": str, "severity": str, "message": str}
        """
        # Step 1: get the latest deployment ID.
        deployment = await self.latest_deployment(project_id, service_id)
        deployment_id = deployment["id"]

        # Step 2: fetch deployment logs (up to 500, Railway default order newest first).
        query = """
        query DeploymentLogs($deploymentId: String!, $limit: Int!) {
          deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
            message
            severity
            timestamp
          }
        }
        """
        data = await self._query(query, {"deploymentId": deployment_id, "limit": 500})
        try:
            raw_logs = data.get("deploymentLogs", [])
            if not isinstance(raw_logs, list):
                raise RailwayError(f"unexpected deploymentLogs shape: {type(raw_logs)}")

            # Filter to WARN/ERROR severities (Railway uses uppercase strings).
            filtered = [
                {
                    "timestamp": str(entry.get("timestamp", "")),
                    "severity": str(entry.get("severity", "")),
                    "message": str(entry.get("message", "")),
                }
                for entry in raw_logs
                if str(entry.get("severity", "")).upper() in ("WARNING", "WARN", "ERROR")
            ]

            # Railway returns logs newest-first; reverse for chronological output,
            # then take the last N (most recent).
            filtered.reverse()
            return filtered[-n:]
        except RailwayError:
            raise
        except Exception as exc:
            raise RailwayError(f"couldn't parse deploymentLogs response: {exc}") from exc

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

FRAMEIO_BASE_URL = "https://api.frame.io/v4"
TARGET_PROJECT_NAME = "Projects for Review"


@dataclass
class Resolution:
    status: str
    account_id: str = ""
    project_id: str = ""
    version_stack_id: str = ""
    asset_name: str = ""
    method: str = ""
    note: str = ""


class FrameIOClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})

    def _request(self, method: str, path: str, *, params: Any = None, json_body: dict[str, Any] | None = None, experimental: bool = False, retry: int = 3) -> requests.Response:
        url = path if path.startswith("http") else f"{FRAMEIO_BASE_URL}{path}"
        headers = {"api-version": "experimental"} if experimental else None
        last: requests.Response | None = None
        for attempt in range(retry):
            response = self.session.request(method, url, params=params, json=json_body, headers=headers, timeout=60)
            last = response
            if response.status_code != 429:
                return response
            time.sleep(min(5, 1 + attempt * 2))
        assert last is not None
        return last

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        return []

    def _paged_get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("page_size", 100)
        rows: list[dict[str, Any]] = []
        next_path: str | None = path
        first = True
        while next_path:
            response = self._request("GET", next_path, params=params if first else None)
            response.raise_for_status()
            payload = response.json()
            rows.extend(self._rows(payload))
            links = payload.get("links") if isinstance(payload, dict) else None
            next_path = links.get("next") if isinstance(links, dict) else None
            first = False
        return rows

    def accounts(self) -> list[dict[str, Any]]:
        return self._paged_get("/accounts")

    def projects_by_account(self, account_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/accounts/{account_id}/projects", params={"page_size": 100, "sort": "name_asc"})
        if response.status_code == 404:
            return self._projects_via_workspaces(account_id)
        response.raise_for_status()
        payload = response.json()
        rows = self._rows(payload)
        links = payload.get("links") if isinstance(payload, dict) else None
        next_path = links.get("next") if isinstance(links, dict) else None
        while next_path:
            nxt = self._request("GET", next_path)
            nxt.raise_for_status()
            p = nxt.json()
            rows.extend(self._rows(p))
            links = p.get("links") if isinstance(p, dict) else None
            next_path = links.get("next") if isinstance(links, dict) else None
        return rows

    def _projects_via_workspaces(self, account_id: str) -> list[dict[str, Any]]:
        workspaces = self._paged_get(f"/accounts/{account_id}/workspaces", {"sort": "name_asc"})
        projects: list[dict[str, Any]] = []
        for workspace in workspaces:
            workspace_id = workspace.get("id")
            if workspace_id:
                projects.extend(self._paged_get(f"/accounts/{account_id}/workspaces/{workspace_id}/projects", {"sort": "name_asc"}))
        return projects

    def find_review_projects(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for account in self.accounts():
            account_id = account.get("id")
            if not account_id:
                continue
            for project in self.projects_by_account(account_id):
                if (project.get("name") or "").strip().casefold() == TARGET_PROJECT_NAME.casefold():
                    found.append({"account_id": account_id, "project": project})
        return found

    @staticmethod
    def normalize_share_url(url: str) -> str:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
        return f"{host}{path}" if host else ""

    def list_shares(self, account_id: str, project_id: str) -> list[dict[str, Any]]:
        return self._paged_get(f"/accounts/{account_id}/projects/{project_id}/shares", {"sort": "created_at_desc"})

    def list_share_assets(self, account_id: str, share_id: str) -> list[dict[str, Any]]:
        return self._paged_get(f"/accounts/{account_id}/shares/{share_id}/assets", {"include": "project", "page_size": 100})

    @staticmethod
    def _name(asset: dict[str, Any]) -> str:
        if asset.get("name"):
            return str(asset["name"])
        head = asset.get("head_version")
        if isinstance(head, dict):
            return str(head.get("name") or "")
        result = asset.get("result")
        if isinstance(result, dict):
            return str(result.get("name") or "")
        return ""

    @staticmethod
    def _has_article_id(name: str, article_id: str) -> bool:
        return bool(article_id and re.search(rf"(?<!\d){re.escape(article_id)}(?!\d)", name or ""))

    def _version_stack_from_asset(self, account_id: str, asset: dict[str, Any]) -> tuple[str, str] | None:
        asset_type = str(asset.get("type") or "")
        if asset_type == "version_stack":
            return str(asset.get("id") or ""), self._name(asset)
        if asset_type == "file":
            parent_id = str(asset.get("parent_id") or "")
            if parent_id:
                response = self._request("GET", f"/accounts/{account_id}/version_stacks/{parent_id}")
                if response.ok:
                    payload = response.json()
                    stack = payload.get("data", payload) if isinstance(payload, dict) else {}
                    return parent_id, self._name(stack) or self._name(asset)
        return None

    def _choose_candidate(self, account_id: str, assets: list[dict[str, Any]], article_id: str) -> tuple[str, str] | None:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for asset in assets:
            name = self._name(asset)
            if article_id and not self._has_article_id(name, article_id):
                continue
            resolved = self._version_stack_from_asset(account_id, asset)
            if resolved and resolved[0] and resolved[0] not in seen:
                seen.add(resolved[0])
                candidates.append(resolved)

        if not candidates and len(assets) == 1:
            resolved = self._version_stack_from_asset(account_id, assets[0])
            if resolved:
                candidates.append(resolved)

        if len(candidates) == 1:
            return candidates[0]
        preview = [c for c in candidates if "preview" in c[1].casefold()]
        return preview[0] if len(preview) == 1 else None

    def search_article(self, account_id: str, article_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "engine": "lexical",
            "query": article_id,
            "filters": {"files_and_version_stacks": True, "folders": False, "projects": False},
        }
        if project_id:
            body["scope"] = {"project_id": project_id}

        response = self._request("POST", f"/accounts/{account_id}/search", json_body=body, experimental=True)
        if response.status_code in (400, 404, 422):
            response = self._request("POST", f"/accounts/{account_id}/search", json_body=body)
        return self._rows(response.json()) if response.ok else []

    def resolve_project(self, project: dict[str, str], review_projects: list[dict[str, Any]], share_maps: dict[tuple[str, str], dict[str, dict[str, Any]]]) -> Resolution:
        article_id = project.get("article_id", "")
        normalized_link = self.normalize_share_url(project.get("frameio_review_link", ""))

        for entry in review_projects:
            account_id = entry["account_id"]
            project_obj = entry["project"]
            project_id = str(project_obj.get("id") or "")
            share = share_maps.get((account_id, project_id), {}).get(normalized_link)
            if share:
                assets = self.list_share_assets(account_id, str(share.get("id") or ""))
                candidate = self._choose_candidate(account_id, assets, article_id)
                if candidate:
                    return Resolution(status="resolved", account_id=account_id, project_id=project_id, version_stack_id=candidate[0], asset_name=candidate[1], method="monday_share_link")
                return Resolution(status="needs_review", account_id=account_id, project_id=project_id, method="monday_share_link", note="Share was found but the linked version stack was ambiguous.")

        scoped_candidates: list[tuple[str, str, str, str]] = []
        for entry in review_projects:
            account_id = entry["account_id"]
            project_id = str(entry["project"].get("id") or "")
            for row in self.search_article(account_id, article_id, project_id):
                asset = row.get("result") if isinstance(row.get("result"), dict) else row
                name = self._name(asset)
                if not self._has_article_id(name, article_id):
                    continue
                resolved = self._version_stack_from_asset(account_id, asset)
                if resolved:
                    scoped_candidates.append((account_id, project_id, resolved[0], resolved[1]))

        unique = []
        seen: set[tuple[str, str]] = set()
        for candidate in scoped_candidates:
            key = (candidate[0], candidate[2])
            if key not in seen:
                seen.add(key)
                unique.append(candidate)

        if len(unique) > 1:
            preview = [row for row in unique if "preview" in row[3].casefold()]
            if len(preview) == 1:
                unique = preview

        if len(unique) == 1:
            account_id, project_id, stack_id, asset_name = unique[0]
            return Resolution(status="resolved", account_id=account_id, project_id=project_id, version_stack_id=stack_id, asset_name=asset_name, method="article_id_fallback", note="Monday link was not a V4 share; resolved by exact Article ID inside Projects for Review.")

        return Resolution(status="needs_review", method="unresolved", note="Could not uniquely resolve the Monday Frame.io link to a version stack.")

    def build_share_maps(self, review_projects: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        result: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for entry in review_projects:
            account_id = entry["account_id"]
            project_id = str(entry["project"].get("id") or "")
            mapping: dict[str, dict[str, Any]] = {}
            for share in self.list_shares(account_id, project_id):
                key = self.normalize_share_url(str(share.get("short_url") or ""))
                if key:
                    mapping[key] = share
            result[(account_id, project_id)] = mapping
        return result

    def version_children(self, account_id: str, version_stack_id: str) -> list[dict[str, Any]]:
        path = f"/accounts/{account_id}/version_stacks/{version_stack_id}/children"
        response = self._request("GET", path, params={"page_size": 100, "sort": "created_at_asc"})
        if response.status_code == 422:
            response = self._request("GET", path, params={"page_size": 100})
        response.raise_for_status()
        payload = response.json()
        rows = self._rows(payload)
        links = payload.get("links") if isinstance(payload, dict) else None
        next_path = links.get("next") if isinstance(links, dict) else None
        while next_path:
            nxt = self._request("GET", next_path)
            nxt.raise_for_status()
            p = nxt.json()
            rows.extend(self._rows(p))
            links = p.get("links") if isinstance(p, dict) else None
            next_path = links.get("next") if isinstance(links, dict) else None
        rows.sort(key=lambda row: str(row.get("created_at") or ""))
        return rows

    def comments_for_file(self, account_id: str, file_id: str) -> list[dict[str, Any]]:
        path = f"/accounts/{account_id}/files/{file_id}/comments"
        params: Any = [("page_size", 100), ("include", "owner"), ("include", "replies"), ("sort", "created_at_asc"), ("timestamp_as_timecode", "true")]
        response = self._request("GET", path, params=params)
        if response.status_code in (400, 422):
            # Some API deployments accept only one include enum at a time.
            # Replies are more important than expanded owner metadata because the
            # workflow must analyze every QC comment/reply.
            response = self._request(
                "GET",
                path,
                params={
                    "page_size": 100,
                    "include": "replies",
                    "sort": "created_at_asc",
                    "timestamp_as_timecode": "true",
                },
            )
        response.raise_for_status()
        payload = response.json()
        rows = self._rows(payload)
        links = payload.get("links") if isinstance(payload, dict) else None
        next_path = links.get("next") if isinstance(links, dict) else None
        while next_path:
            nxt = self._request("GET", next_path)
            nxt.raise_for_status()
            p = nxt.json()
            rows.extend(self._rows(p))
            links = p.get("links") if isinstance(p, dict) else None
            next_path = links.get("next") if isinstance(links, dict) else None
        return rows

    @staticmethod
    def flatten_comments(comments: list[dict[str, Any]], *, article_id: str, version_number: int, version_name: str, file_id: str) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []

        def owner_name(comment: dict[str, Any]) -> str:
            owner = comment.get("owner")
            if isinstance(owner, dict):
                return str(owner.get("name") or owner.get("email") or "")
            return str(comment.get("owner_name") or comment.get("owner_id") or "")

        def add(comment: dict[str, Any], is_reply: bool, parent_id: str = "") -> None:
            text = str(comment.get("text") or "").strip()
            if text:
                flattened.append({
                    "article_id": article_id,
                    "version_number": version_number,
                    "version_name": version_name,
                    "file_id": file_id,
                    "comment_id": str(comment.get("id") or ""),
                    "parent_comment_id": parent_id,
                    "is_reply": is_reply,
                    "commenter": owner_name(comment),
                    "created_at": str(comment.get("created_at") or ""),
                    "timestamp": str(comment.get("timestamp") or ""),
                    "text": text,
                })

        for comment in comments:
            parent_id = str(comment.get("id") or "")
            add(comment, False)
            replies = comment.get("replies")
            if isinstance(replies, list):
                for reply in replies:
                    if isinstance(reply, dict):
                        add(reply, True, parent_id)
        return flattened

    def extract_intermediate_comments(self, project: dict[str, str], resolution: Resolution) -> dict[str, Any]:
        if resolution.status != "resolved":
            return {"project": project, "resolution": resolution, "versions": [], "comments": []}

        versions = self.version_children(resolution.account_id, resolution.version_stack_id)
        annotated_versions: list[dict[str, Any]] = []
        all_comments: list[dict[str, Any]] = []

        for index, version in enumerate(versions, start=1):
            row = dict(version)
            row["version_number"] = index
            row["included_for_qc"] = 1 < index < len(versions)
            annotated_versions.append(row)

            if not row["included_for_qc"]:
                continue
            file_id = str(version.get("id") or "")
            if not file_id:
                continue
            comments = self.comments_for_file(resolution.account_id, file_id)
            all_comments.extend(self.flatten_comments(comments, article_id=project.get("article_id", ""), version_number=index, version_name=str(version.get("name") or ""), file_id=file_id))

        return {"project": project, "resolution": resolution, "versions": annotated_versions, "comments": all_comments}

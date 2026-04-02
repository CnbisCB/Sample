import os
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


COMMENT_COMPONENT_ID_RE = re.compile(r"CB_COMPONENT_ID:\s*([A-Za-z0-9_./-]+)")
COMMENT_SCOPE_RE = re.compile(r"CB_SCOPE:\s*([A-Za-z0-9_./-]+)")
SUPPORTED_SUFFIXES = {".java", ".c", ".cpp", ".cc", ".h", ".hpp"}
ANNOTATION_WINDOW = 6


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value.strip()


def optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def debug(title: str, data: Any) -> None:
    print(f"=== {title} ===")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(data)


def request(method: str, url: str, auth: tuple[str, str], **kwargs) -> requests.Response:
    response = requests.request(method, url, auth=auth, timeout=60, **kwargs)
    print(f"=== {method} {url} - STATUS ===")
    print(response.status_code)
    print(f"=== {method} {url} - BODY ===")
    print(response.text)

    response.raise_for_status()
    return response


def request_json(method: str, url: str, auth: tuple[str, str], **kwargs) -> Any:
    response = request(method, url, auth, **kwargs)
    if not response.text.strip():
        return None
    try:
        return response.json()
    except Exception:
        return response.text


def get_auth() -> tuple[str, str]:
    return require_env("CB_USERNAME"), require_env("CB_PASSWORD")


def get_api_base_url() -> str:
    return require_env("CB_BASE_URL").rstrip("/") + "/api/v3"


def resolve_field_id(env_name: str) -> int:
    raw = require_env(env_name)
    if not raw.isdigit():
        raise RuntimeError(f"{env_name} must be numeric. Current value: {raw}")
    return int(raw)


def cbql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
        or stripped.startswith("*/")
    )


def find_annotation_at(lines: List[str], start_index: int) -> Optional[Dict[str, Any]]:
    component_token = None
    scope_name = None
    last_annotation_index = None

    for idx in range(start_index, min(start_index + ANNOTATION_WINDOW, len(lines))):
        component_match = COMMENT_COMPONENT_ID_RE.search(lines[idx])
        scope_match = COMMENT_SCOPE_RE.search(lines[idx])

        if component_match:
            component_token = component_match.group(1).strip()
            last_annotation_index = idx

        if scope_match:
            scope_name = scope_match.group(1).strip()
            last_annotation_index = idx

        if component_token and scope_name:
            return {
                "component_token": component_token,
                "scope_name": scope_name,
                "annotation_end_index": last_annotation_index,
            }

    return None


def find_block_after_annotation(lines: List[str], annotation_end_index: int) -> Optional[Dict[str, int]]:
    start_index = None
    first_open_brace_seen = False
    brace_balance = 0

    for idx in range(annotation_end_index + 1, len(lines)):
        stripped = lines[idx].strip()

        if not stripped:
            continue

        if is_comment_line(stripped):
            continue

        start_index = idx
        break

    if start_index is None:
        return None

    for idx in range(start_index, len(lines)):
        line = lines[idx]
        open_count = line.count("{")
        close_count = line.count("}")

        if open_count > 0:
            first_open_brace_seen = True

        if first_open_brace_seen:
            brace_balance += open_count
            brace_balance -= close_count

            if brace_balance == 0:
                return {
                    "start_line": start_index + 1,
                    "end_line": idx + 1,
                }

    return None


def collect_targets(repo_root: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    for file_path in repo_root.rglob("*"):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue

        text = file_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        relative_path = file_path.relative_to(repo_root).as_posix()

        line_index = 0
        while line_index < len(lines):
            annotation = find_annotation_at(lines, line_index)
            if not annotation:
                line_index += 1
                continue

            block = find_block_after_annotation(lines, annotation["annotation_end_index"])
            if block:
                found.append(
                    {
                        "component_token": annotation["component_token"],
                        "scope_name": annotation["scope_name"],
                        "file_path": relative_path,
                        "start_line": block["start_line"],
                        "end_line": block["end_line"],
                    }
                )
                line_index = block["end_line"]
            else:
                line_index = annotation["annotation_end_index"] + 1

    if not found:
        raise RuntimeError("CB_COMPONENT_ID / CB_SCOPE annotation pair was not found.")

    return found


def build_permalink(
    server_url: str,
    repository: str,
    sha: str,
    file_path: str,
    start_line: int,
    end_line: int,
) -> str:
    return f"{server_url}/{repository}/blob/{sha}/{file_path}#L{start_line}-L{end_line}"


def get_item(auth: tuple[str, str], api_base_url: str, item_id: int) -> Any:
    return request_json("GET", f"{api_base_url}/items/{item_id}", auth=auth)


def query_items(auth: tuple[str, str], api_base_url: str, query_string: str) -> List[Dict[str, Any]]:
    url = f"{api_base_url}/items/query"
    body = {
        "page": 1,
        "pageSize": 100,
        "queryString": query_string,
    }
    result = request_json("POST", url, auth=auth, json=body)

    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected query response: {result}")

    items = result.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected query items payload: {result}")

    return items


def resolve_component_item_id(
    auth: tuple[str, str],
    api_base_url: str,
    component_token: str,
) -> int:
    if component_token.isdigit():
        component_item_id = int(component_token)
        get_item(auth, api_base_url, component_item_id)
        return component_item_id

    component_tracker_id = int(require_env("CB_COMPONENT_TRACKER_ID"))
    query = (
        f"tracker.id = {component_tracker_id} "
        f"AND summary = '{cbql_escape(component_token)}'"
    )
    items = query_items(auth, api_base_url, query)

    if len(items) == 1:
        return int(items[0]["id"])

    if len(items) == 0:
        raise RuntimeError(
            "Component item was not found. "
            f"component_token={component_token}, component_tracker_id={component_tracker_id}"
        )

    raise RuntimeError(
        "More than one component item matched the token. "
        f"component_token={component_token}, matched_count={len(items)}"
    )


def find_existing_evidence_item_id(
    auth: tuple[str, str],
    api_base_url: str,
    tracker_id: int,
    evidence_name: str,
) -> Optional[int]:
    query = (
        f"tracker.id = {tracker_id} "
        f"AND summary = '{cbql_escape(evidence_name)}'"
    )
    items = query_items(auth, api_base_url, query)

    if not items:
        return None

    if len(items) > 1:
        print(
            "=== WARNING === More than one existing evidence item matched. "
            f"Using the first one. evidence_name={evidence_name}, matched_count={len(items)}"
        )

    return int(items[0]["id"])


def build_create_custom_fields(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    field_repository = resolve_field_id("CB_FIELD_REPOSITORY")
    field_file_path = resolve_field_id("CB_FIELD_FILE_PATH")
    field_start_line = resolve_field_id("CB_FIELD_START_LINE")
    field_end_line = resolve_field_id("CB_FIELD_END_LINE")
    field_commit_sha = resolve_field_id("CB_FIELD_COMMIT_SHA")
    field_permalink = resolve_field_id("CB_FIELD_PERMALINK")
    field_linked_component = resolve_field_id("CB_FIELD_LINKED_COMPONENT")

    custom_fields: List[Dict[str, Any]] = [
        {
            "fieldId": field_repository,
            "name": "Repository Name",
            "type": "TextFieldValue",
            "value": data["repository"],
        },
        {
            "fieldId": field_file_path,
            "name": "File Path",
            "type": "TextFieldValue",
            "value": data["file_path"],
        },
        {
            "fieldId": field_start_line,
            "name": "Start Line",
            "type": "IntegerFieldValue",
            "value": data["start_line"],
        },
        {
            "fieldId": field_end_line,
            "name": "End Line",
            "type": "IntegerFieldValue",
            "value": data["end_line"],
        },
        {
            "fieldId": field_commit_sha,
            "name": "Commit SHA",
            "type": "TextFieldValue",
            "value": data["commit_sha"],
        },
        {
            "fieldId": field_permalink,
            "name": "GitHub Permalink",
            "type": "TextFieldValue",
            "value": data["permalink"],
        },
        {
            "fieldId": field_linked_component,
            "name": "Linked Component",
            "type": "ChoiceFieldValue",
            "values": [
                {
                    "id": data["component_item_id"],
                    "name": str(data["component_item_id"]),
                    "type": "TrackerItemReference",
                }
            ],
        },
    ]

    raw_scope_field_id = optional_env("CB_FIELD_SCOPE_NAME")
    if raw_scope_field_id:
        scope_field_id = int(raw_scope_field_id)
        if scope_field_id == 3:
            print("CB_FIELD_SCOPE_NAME=3 detected. Field 3 is Summary, so separate scope field create is skipped.")
        else:
            custom_fields.append(
                {
                    "fieldId": scope_field_id,
                    "name": "Scope Name",
                    "type": "TextFieldValue",
                    "value": data["scope_name"],
                }
            )

    return custom_fields


def build_update_field_values(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    field_repository = resolve_field_id("CB_FIELD_REPOSITORY")
    field_file_path = resolve_field_id("CB_FIELD_FILE_PATH")
    field_start_line = resolve_field_id("CB_FIELD_START_LINE")
    field_end_line = resolve_field_id("CB_FIELD_END_LINE")
    field_commit_sha = resolve_field_id("CB_FIELD_COMMIT_SHA")
    field_permalink = resolve_field_id("CB_FIELD_PERMALINK")
    field_linked_component = resolve_field_id("CB_FIELD_LINKED_COMPONENT")

    field_values: List[Dict[str, Any]] = [
        {
            "fieldId": field_repository,
            "name": "Repository Name",
            "type": "TextFieldValue",
            "value": data["repository"],
        },
        {
            "fieldId": field_file_path,
            "name": "File Path",
            "type": "TextFieldValue",
            "value": data["file_path"],
        },
        {
            "fieldId": field_start_line,
            "name": "Start Line",
            "type": "IntegerFieldValue",
            "value": data["start_line"],
        },
        {
            "fieldId": field_end_line,
            "name": "End Line",
            "type": "IntegerFieldValue",
            "value": data["end_line"],
        },
        {
            "fieldId": field_commit_sha,
            "name": "Commit SHA",
            "type": "TextFieldValue",
            "value": data["commit_sha"],
        },
        {
            "fieldId": field_permalink,
            "name": "GitHub Permalink",
            "type": "TextFieldValue",
            "value": data["permalink"],
        },
        {
            "fieldId": field_linked_component,
            "name": "Linked Component",
            "type": "ChoiceFieldValue",
            "values": [
                {
                    "id": data["component_item_id"],
                    "name": str(data["component_item_id"]),
                    "type": "TrackerItemReference",
                }
            ],
        },
    ]

    raw_scope_field_id = optional_env("CB_FIELD_SCOPE_NAME")
    if raw_scope_field_id:
        scope_field_id = int(raw_scope_field_id)
        if scope_field_id == 3:
            print("CB_FIELD_SCOPE_NAME=3 detected. Field 3 is Summary, so separate scope field update is skipped.")
        else:
            field_values.append(
                {
                    "fieldId": scope_field_id,
                    "name": "Scope Name",
                    "type": "TextFieldValue",
                    "value": data["scope_name"],
                }
            )

    return field_values


def create_evidence_item(
    auth: tuple[str, str],
    api_base_url: str,
    tracker_id: int,
    data: Dict[str, Any],
) -> int:
    url = f"{api_base_url}/trackers/{tracker_id}/items"
    payload = {
        "name": data["evidence_name"],
        "description": "Generated by GitHub Actions",
        "descriptionFormat": "PlainText",
        "customFields": build_create_custom_fields(data),
    }

    debug("CREATE URL", url)
    debug("CREATE PAYLOAD", payload)

    created = request_json("POST", url, auth=auth, json=payload)
    if not isinstance(created, dict) or "id" not in created:
        raise RuntimeError(f"Created item ID not found in response: {created}")

    return int(created["id"])


def update_evidence_item_fields(
    auth: tuple[str, str],
    api_base_url: str,
    item_id: int,
    data: Dict[str, Any],
) -> None:
    url = f"{api_base_url}/items/{item_id}/fields"
    payload = {
        "fieldValues": build_update_field_values(data)
    }

    debug("UPDATE FIELDS URL", url)
    debug("UPDATE FIELDS PAYLOAD", payload)

    request_json("PUT", url, auth=auth, json=payload)


def has_linked_component(item_data: Any, linked_field_id: int, component_item_id: int) -> bool:
    if not isinstance(item_data, dict):
        return False

    custom_fields = item_data.get("customFields", [])
    for field in custom_fields:
        if int(field.get("fieldId", -1)) != linked_field_id:
            continue

        for value in field.get("values", []):
            if int(value.get("id", -1)) == component_item_id:
                return True

    return False


def verify_item(
    auth: tuple[str, str],
    api_base_url: str,
    item_id: int,
    component_item_id: int,
) -> None:
    linked_field_id = resolve_field_id("CB_FIELD_LINKED_COMPONENT")
    item_data = get_item(auth, api_base_url, item_id)
    debug("VERIFY ITEM RESPONSE", item_data)

    if not has_linked_component(item_data, linked_field_id, component_item_id):
        raise RuntimeError(
            f"Linked Component verification failed. item_id={item_id}, component_item_id={component_item_id}"
        )


def main() -> None:
    repo_root = Path(".").resolve()
    api_base_url = get_api_base_url()
    auth = get_auth()
    tracker_id = int(require_env("CB_TRACKER_ID"))

    server_url = require_env("GITHUB_SERVER_URL")
    repository = require_env("GITHUB_REPOSITORY")
    sha = require_env("GITHUB_SHA")

    debug(
        "DEBUG START",
        {
            "repo_root": str(repo_root),
            "repository": repository,
            "sha": sha,
            "tracker_id": tracker_id,
        },
    )

    targets = collect_targets(repo_root)
    debug("FOUND TARGET COUNT", len(targets))
    debug("FOUND TARGETS", targets)

    created_count = 0
    updated_count = 0

    for target in targets:
        component_item_id = resolve_component_item_id(auth, api_base_url, target["component_token"])
        evidence_name = f'{target["scope_name"]} @ {target["file_path"]}'
        permalink = build_permalink(
            server_url=server_url,
            repository=repository,
            sha=sha,
            file_path=target["file_path"],
            start_line=target["start_line"],
            end_line=target["end_line"],
        )

        payload_data = {
            "component_item_id": component_item_id,
            "scope_name": target["scope_name"],
            "file_path": target["file_path"],
            "start_line": target["start_line"],
            "end_line": target["end_line"],
            "repository": repository,
            "commit_sha": sha,
            "permalink": permalink,
            "evidence_name": evidence_name,
        }

        debug("PROCESSING TARGET", payload_data)

        existing_item_id = find_existing_evidence_item_id(
            auth=auth,
            api_base_url=api_base_url,
            tracker_id=tracker_id,
            evidence_name=evidence_name,
        )

        if existing_item_id is None:
            item_id = create_evidence_item(
                auth=auth,
                api_base_url=api_base_url,
                tracker_id=tracker_id,
                data=payload_data,
            )
            created_count += 1
            print(f"CREATED: {item_id} / {evidence_name}")
        else:
            update_evidence_item_fields(
                auth=auth,
                api_base_url=api_base_url,
                item_id=existing_item_id,
                data=payload_data,
            )
            item_id = existing_item_id
            updated_count += 1
            print(f"UPDATED: {item_id} / {evidence_name}")

        verify_item(
            auth=auth,
            api_base_url=api_base_url,
            item_id=item_id,
            component_item_id=component_item_id,
        )

    print("=== SYNC RESULT ===")
    print(json.dumps({"created": created_count, "updated": updated_count}, ensure_ascii=False))


if __name__ == "__main__":
    main()

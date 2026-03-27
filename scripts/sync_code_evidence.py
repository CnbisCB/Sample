import os
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


COMMENT_COMPONENT_ID_RE = re.compile(r"CB_COMPONENT_ID:\s*(\d+)")
COMMENT_SCOPE_RE = re.compile(r"CB_SCOPE:\s*([A-Za-z0-9_./-]+)")
SUPPORTED_SUFFIXES = {".java", ".c", ".cpp", ".cc", ".h", ".hpp"}


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def debug(title: str, data: Any) -> None:
    print(f"=== {title} ===")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(data)


def request_json(method: str, url: str, auth: tuple[str, str], **kwargs) -> Any:
    resp = requests.request(method, url, auth=auth, timeout=60, **kwargs)

    print(f"=== {method} {url} - STATUS ===")
    print(resp.status_code)
    print(f"=== {method} {url} - BODY ===")
    print(resp.text)

    resp.raise_for_status()

    if resp.text.strip():
        try:
            return resp.json()
        except Exception:
            return resp.text
    return None


def is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
        or stripped.startswith("*/")
    )


def find_annotation_block(lines: List[str]) -> Optional[Dict[str, Any]]:
    for i in range(len(lines)):
        component_item_id = None
        scope_value = None
        last_annotation_idx = None

        for j in range(i, min(i + 6, len(lines))):
            component_match = COMMENT_COMPONENT_ID_RE.search(lines[j])
            scope_match = COMMENT_SCOPE_RE.search(lines[j])

            if component_match:
                component_item_id = int(component_match.group(1).strip())
                last_annotation_idx = j

            if scope_match:
                scope_value = scope_match.group(1).strip()
                last_annotation_idx = j

            if component_item_id is not None and scope_value:
                return {
                    "component_item_id": component_item_id,
                    "scope_name": scope_value,
                    "annotation_end_index": last_annotation_idx,
                }

    return None


def find_block_after_annotation(lines: List[str], annotation_end_index: int) -> Optional[Dict[str, int]]:
    start_idx = None
    first_open_brace_seen = False
    brace_balance = 0

    for j in range(annotation_end_index + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()

        if not stripped:
            continue

        if is_comment_line(stripped):
            continue

        start_idx = j
        break

    if start_idx is None:
        return None

    for k in range(start_idx, len(lines)):
        line = lines[k]
        open_count = line.count("{")
        close_count = line.count("}")

        if open_count > 0:
            first_open_brace_seen = True

        if first_open_brace_seen:
            brace_balance += open_count
            brace_balance -= close_count

            if brace_balance == 0:
                return {
                    "start_line": start_idx + 1,
                    "end_line": k + 1,
                }

    return None


def find_target_comment_and_block(repo_root: Path) -> Dict[str, Any]:
    for file_path in repo_root.rglob("*"):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue

        text = file_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()

        annotation = find_annotation_block(lines)
        if not annotation:
            continue

        block = find_block_after_annotation(lines, annotation["annotation_end_index"])
        if not block:
            continue

        relative_path = file_path.relative_to(repo_root).as_posix()

        return {
            "component_item_id": annotation["component_item_id"],
            "scope_name": annotation["scope_name"],
            "file_path": relative_path,
            "start_line": block["start_line"],
            "end_line": block["end_line"],
        }

    raise RuntimeError("CB_COMPONENT_ID / CB_SCOPE 주석 쌍을 찾지 못했습니다.")


def build_permalink(
    server_url: str,
    repository: str,
    sha: str,
    file_path: str,
    start_line: int,
    end_line: int,
) -> str:
    return f"{server_url}/{repository}/blob/{sha}/{file_path}#L{start_line}-L{end_line}"


def get_auth() -> tuple[str, str]:
    username = require_env("CB_USERNAME")
    password = require_env("CB_PASSWORD")
    return username, password


def get_api_base_url() -> str:
    return require_env("CB_BASE_URL").rstrip("/") + "/api/v3"


def resolve_field_id(env_name: str) -> int:
    raw = require_env(env_name).strip()

    if not raw.isdigit():
        raise RuntimeError(
            f"{env_name} must be a numeric field ID in this environment. Current value: {raw}"
        )

    return int(raw)


def get_item(auth: tuple[str, str], api_base_url: str, item_id: int) -> Any:
    url = f"{api_base_url}/items/{item_id}"
    return request_json("GET", url, auth=auth)


def has_linked_component(item_data: Any, field_id: int, component_item_id: int) -> bool:
    if not isinstance(item_data, dict):
        return False

    custom_fields = item_data.get("customFields", [])
    for field in custom_fields:
        if int(field.get("fieldId", -1)) != field_id:
            continue

        values = field.get("values", [])
        for value in values:
            if int(value.get("id", -1)) == component_item_id:
                return True

    return False


def update_linked_component(
    auth: tuple[str, str],
    api_base_url: str,
    item_id: int,
    linked_field_id: int,
    component_item_id: int,
) -> Any:
    url = f"{api_base_url}/items/{item_id}"

    payload = {
        "customFields": [
            {
                "fieldId": linked_field_id,
                "name": "Linked Component",
                "type": "ChoiceFieldValue",
                "values": [
                    {
                        "id": component_item_id,
                        "name": str(component_item_id),
                        "type": "TrackerItemReference",
                    }
                ],
            }
        ]
    }

    debug("Codebeamer Update URL", url)
    debug("Codebeamer Update Payload", payload)

    resp = requests.put(url, auth=auth, json=payload, timeout=60)

    print("=== PUT UPDATE ITEM STATUS ===")
    print(resp.status_code)
    print("=== PUT UPDATE ITEM BODY ===")
    print(resp.text)

    resp.raise_for_status()

    if resp.text.strip():
        try

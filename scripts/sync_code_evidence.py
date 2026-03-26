import os
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests


COMMENT_COMPONENT_TRACKER_RE = re.compile(r"CB_COMPONENT_TRACKER:\s*([A-Za-z0-9._-]+)")
COMMENT_COMPONENT_RE = re.compile(r"CB_COMPONENT:\s*([A-Za-z0-9._-]+)")
COMMENT_SCOPE_RE = re.compile(r"CB_SCOPE:\s*([A-Za-z0-9_./-]+)")

SUPPORTED_SUFFIXES = {".java", ".c", ".cpp", ".cc", ".h", ".hpp"}


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
        or stripped.startswith("*/")
    )


def debug(title: str, value: Any) -> None:
    print(f"=== {title} ===")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print(value)


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def request_json(method: str, url: str, *, auth: tuple[str, str], **kwargs) -> Any:
    resp = requests.request(method, url, auth=auth, timeout=60, **kwargs)
    debug(f"{method} {url} - STATUS", resp.status_code)
    debug(f"{method} {url} - BODY", safe_json(resp))
    resp.raise_for_status()
    return safe_json(resp)


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def collect_items_from_response(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if not isinstance(data, dict):
        return []

    for key in ("items", "itemRefs", "trackerItems", "references", "content", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    return []


def collect_trackers_from_response(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if not isinstance(data, dict):
        return []

    for key in ("trackers", "items", "content", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    return []


def find_annotation_block(lines: List[str]) -> Optional[Dict[str, Any]]:
    for i in range(len(lines)):
        tracker_value = None
        component_value = None
        scope_value = None
        last_annotation_idx = None

        for j in range(i, min(i + 8, len(lines))):
            line = lines[j]

            tracker_match = COMMENT_COMPONENT_TRACKER_RE.search(line)
            component_match = COMMENT_COMPONENT_RE.search(line)
            scope_match = COMMENT_SCOPE_RE.search(line)

            if tracker_match:
                tracker_value = tracker_match.group(1).strip()
                last_annotation_idx = j

            if component_match:
                component_value = component_match.group(1).strip()
                last_annotation_idx = j

            if scope_match:
                scope_value = scope_match.group(1).strip()
                last_annotation_idx = j

            if tracker_value and component_value and scope_value:
                return {
                    "component_tracker_key": tracker_value,
                    "component_key": component_value,
                    "scope_name": scope_value,
                    "annotation_end_index": last_annotation_idx,
                }

    return None


def find_block_after_annotation(lines: List[str], annotation_end_index: int) -> Optional[Dict[str, int]]:
    start_idx = None
    first_open_brace_seen = False
    brace_balance = 0

    for j in range(annotation_end_index + 1, len(lines)):
        stripped = lines[j].strip()

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
            "component_tracker_key": annotation["component_tracker_key"],
            "component_key": annotation["component_key"],
            "scope_name": annotation["scope_name"],
            "file_path": relative_path,
            "start_line": block["start_line"],
            "end_line": block["end_line"],
        }

    raise RuntimeError("CB_COMPONENT_TRACKER / CB_COMPONENT / CB_SCOPE 주석 쌍을 찾지 못했습니다.")


def build_permalink(server_url: str, repository: str, sha: str, file_path: str, start_line: int, end_line: int) -> str:
    return f"{server_url}/{repository}/blob/{sha}/{file_path}#L{start_line}-L{end_line}"


def get_auth() -> tuple[str, str]:
    username = require_env("CB_USERNAME")
    password = require_env("CB_PASSWORD")
    return username, password


def get_base_url() -> str:
    return require_env("CB_BASE_URL").rstrip("/")


def resolve_component_tracker_id(component_tracker_key: str) -> int:
    base_url = get_base_url()
    auth = get_auth()

    manual_tracker_id = get_env("CB_COMPONENT_TRACKER_ID")
    if manual_tracker_id:
        debug("USING ENV CB_COMPONENT_TRACKER_ID", manual_tracker_id)
        return int(manual_tracker_id)

    url = f"{base_url}/v3/trackers"
    data = request_json("GET", url, auth=auth)
    trackers = collect_trackers_from_response(data)

    if not trackers:
        raise RuntimeError("No trackers returned from Codebeamer /v3/trackers")

    normalized_target = component_tracker_key.strip().lower()

    for tracker in trackers:
        tracker_id = tracker.get("id")
        name = normalize_text(tracker.get("name")).lower()
        key = normalize_text(tracker.get("key")).lower()
        tracker_type = normalize_text(tracker.get("type")).lower()

        candidates: Set[str] = {name, key, tracker_type}
        if normalized_target in candidates:
            if not tracker_id:
                raise RuntimeError(f"Tracker found but id missing for tracker key: {component_tracker_key}")
            return int(tracker_id)

    raise RuntimeError(f"Component tracker not found in Codebeamer: {component_tracker_key}")


def resolve_component_item_id(component_tracker_id: int, component_key: str) -> int:
    base_url = get_base_url()
    auth = get_auth()

    url = f"{base_url}/v3/trackers/{component_tracker_id}/items"
    data = request_json("GET", url, auth=auth)
    items = collect_items_from_response(data)

    if not items:
        raise RuntimeError(f"No items returned from tracker: {component_tracker_id}")

    normalized_target = component_key.strip().lower()

    for item in items:
        item_id = item.get("id")
        name = normalize_text(item.get("name"))
        key = normalize_text(item.get("key"))
        item_no = item.get("itemNo")

        candidates = {
            name.lower(),
            key.lower(),
        }

        if item_no is not None:
            try:
                item_no_int = int(item_no)
                prefix = component_key.split("-")[0] if "-" in component_key else ""
                if prefix:
                    candidates.add(f"{prefix.lower()}-{item_no_int}")
                    candidates.add(f"{prefix.lower()}-{item_no_int:03d}")
            except Exception:
                pass

        if normalized_target in candidates:
            if not item_id:
                raise RuntimeError(f"Component found but item id missing: {component_key}")
            return int(item_id)

    raise RuntimeError(f"Component not found in tracker {component_tracker_id}: {component_key}")


def create_codebeamer_item(data: Dict[str, Any]) -> None:
    base_url = get_base_url()
    auth = get_auth()

    tracker_id = int(require_env("CB_TRACKER_ID"))

    field_repository = int(require_env("CB_FIELD_REPOSITORY"))
    field_file_path = int(require_env("CB_FIELD_FILE_PATH"))
    field_start_line = int(require_env("CB_FIELD_START_LINE"))
    field_end_line = int(require_env("CB_FIELD_END_LINE"))
    field_scope_name = int(require_env("CB_FIELD_SCOPE_NAME"))
    field_commit_sha = int(require_env("CB_FIELD_COMMIT_SHA"))
    field_permalink = int(require_env("CB_FIELD_PERMALINK"))
    field_linked_component = int(require_env("CB_FIELD_LINKED_COMPONENT"))

    component_tracker_id = resolve_component_tracker_id(data["component_tracker_key"])
    debug("RESOLVED COMPONENT TRACKER ID", component_tracker_id)

    component_item_id = resolve_component_item_id(component_tracker_id, data["component_key"])
    debug("RESOLVED COMPONENT ITEM ID", component_item_id)

    payload = {
        "name": f'{data["scope_name"]} @ {data["file_path"]}',
        "description": "Generated by GitHub Actions",
        "descriptionFormat": "PlainText",
        "customFields": [
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
                "fieldId": field_scope_name,
                "name": "Scope Name",
                "type": "TextFieldValue",
                "value": data["scope_name"],
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
                        "id": component_item_id,
                        "name": data["component_key"],
                        "type": "TrackerItemReference",
                    }
                ],
            },
        ],
    }

    url = f"{base_url}/v3/trackers/{tracker_id}/items"
    debug("CREATE ITEM URL", url)
    debug("CREATE ITEM PAYLOAD", payload)

    resp = requests.post(url, auth=auth, json=payload, timeout=60)
    debug("CREATE ITEM STATUS", resp.status_code)
    debug("CREATE ITEM BODY", safe_json(resp))
    resp.raise_for_status()

    print("Created Codebeamer item successfully.")


def main() -> None:
    repo_root = Path(".").resolve()

    debug("DEBUG START", {
        "Repository root": str(repo_root),
        "GITHUB_REPOSITORY": os.environ.get("GITHUB_REPOSITORY"),
        "GITHUB_SHA": os.environ.get("GITHUB_SHA"),
        "CB_BASE_URL exists": bool(os.environ.get("CB_BASE_URL")),
        "CB_USERNAME exists": bool(os.environ.get("CB_USERNAME")),
        "CB_PASSWORD exists": bool(os.environ.get("CB_PASSWORD")),
        "CB_TRACKER_ID exists": bool(os.environ.get("CB_TRACKER_ID")),
        "CB_COMPONENT_TRACKER_ID exists": bool(os.environ.get("CB_COMPONENT_TRACKER_ID")),
    })

    found = find_target_comment_and_block(repo_root)
    debug("FOUND TARGET", found)

    server_url = require_env("GITHUB_SERVER_URL")
    repository = require_env("GITHUB_REPOSITORY")
    sha = require_env("GITHUB_SHA")

    permalink = build_permalink(
        server_url=server_url,
        repository=repository,
        sha=sha,
        file_path=found["file_path"],
        start_line=found["start_line"],
        end_line=found["end_line"],
    )

    payload_data = {
        "component_tracker_key": found["component_tracker_key"],
        "component_key": found["component_key"],
        "scope_name": found["scope_name"],
        "file_path": found["file_path"],
        "start_line": found["start_line"],
        "end_line": found["end_line"],
        "repository": repository,
        "commit_sha": sha,
        "permalink": permalink,
    }

    debug("FINAL DATA", payload_data)
    create_codebeamer_item(payload_data)


if __name__ == "__main__":
    main()

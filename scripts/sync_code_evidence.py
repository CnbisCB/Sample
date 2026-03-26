import os
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


COMMENT_COMPONENT_RE = re.compile(r"CB_COMPONENT:\s*([A-Za-z0-9._-]+)")
COMMENT_SCOPE_RE = re.compile(r"CB_SCOPE:\s*([A-Za-z0-9_./-]+)")
SUPPORTED_SUFFIXES = {".java", ".c", ".cpp", ".cc", ".h", ".hpp"}


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def optional_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
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
        return resp.json()
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
    """
    CB_COMPONENT, CB_SCOPE 주석 블록 찾기
    """
    for i in range(len(lines)):
        component_value = None
        scope_value = None
        last_annotation_idx = None

        for j in range(i, min(i + 6, len(lines))):
            component_match = COMMENT_COMPONENT_RE.search(lines[j])
            scope_match = COMMENT_SCOPE_RE.search(lines[j])

            if component_match:
                component_value = component_match.group(1).strip()
                last_annotation_idx = j

            if scope_match:
                scope_value = scope_match.group(1).strip()
                last_annotation_idx = j

            if component_value and scope_value:
                component_tracker_key = component_value.split("-")[0] if "-" in component_value else None
                return {
                    "component_tracker_key": component_tracker_key,
                    "component_key": component_value,
                    "scope_name": scope_value,
                    "annotation_end_index": last_annotation_idx,
                }

    return None


def find_block_after_annotation(lines: List[str], annotation_end_index: int) -> Optional[Dict[str, int]]:
    """
    주석 아래 첫 코드 블록(메서드/생성자/클래스 등)의 시작/끝 라인 계산
    """
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
                    "start_line": start_idx + 1,  # 1-based
                    "end_line": k + 1,            # 1-based
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

    raise RuntimeError("CB_COMPONENT / CB_SCOPE 주석 쌍을 찾지 못했습니다.")


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


def get_base_url() -> str:
    return require_env("CB_BASE_URL").rstrip("/")


def get_tracker_fields(tracker_id: int) -> List[Dict[str, Any]]:
    base_url = get_base_url()
    auth = get_auth()
    url = f"{base_url}/v3/trackers/{tracker_id}/fields"
    return request_json("GET", url, auth=auth)
    debug("Component tracker response keys", list(data.keys()) if isinstance(data, dict) else type(data))


def resolve_field_id(tracker_id: int, env_name: str) -> int:
    """
    Secret 값이 숫자면 그대로 fieldId로 사용.
    숫자가 아니면 field name으로 간주하고 /fields에서 찾아서 id로 변환.
    """
    raw = require_env(env_name).strip()

    if raw.isdigit():
        return int(raw)

    fields = get_tracker_fields(tracker_id)

    for field in fields:
        if field.get("name") == raw:
            debug(f"Resolved field name '{raw}' to fieldId", field.get("id"))
            return int(field["id"])

    available = [f.get("name") for f in fields]
    raise RuntimeError(
        f"Field not found in tracker {tracker_id}: {raw}. "
        f"Available field names: {available}"
    )


def resolve_component_item_id(component_tracker_id: int, component_key: str) -> int:
    """
    component_key 예: SWDD-001
    /v3/trackers/{trackerId}/items 로 목록을 가져와서 key/name 에서 찾음
    """
    base_url = get_base_url()
    auth = get_auth()

    page = 1
    page_size = 500

    while True:
        url = f"{base_url}/v3/trackers/{component_tracker_id}/items"
        data = request_json("GET", url, auth=auth)

        item_refs = []
        if isinstance(data, dict):
            # 환경별로 items 또는 itemRefs 형태가 다를 수 있어 둘 다 대응
            item_refs = data.get("items") or data.get("itemRefs") or []

        debug(f"Component tracker page {page} item count", len(item_refs))

        if not item_refs:
            break

        for item in item_refs:
            item_id = item.get("id")
            item_name = item.get("name", "")
            item_key = item.get("key", "")

            # key가 내려오면 key 우선 비교
            if item_key == component_key:
                return int(item_id)

            # key가 안 내려오는 환경이면 name에 SWDD-001 이 포함되는지 확인
            if item_name == component_key or component_key in item_name:
                return int(item_id)

        total = data.get("total") if isinstance(data, dict) else None
        if total is not None and page * page_size >= total:
            break

        page += 1

    raise RuntimeError(
        f"Component item not found in tracker {component_tracker_id}: {component_key}"
    )


def create_codebeamer_item(data: Dict[str, Any]) -> None:
    base_url = get_base_url()
    auth = get_auth()

    tracker_id = int(require_env("CB_TRACKER_ID"))
    component_tracker_id = int(require_env("CB_COMPONENT_TRACKER_ID"))

    debug("USING ENV CB_COMPONENT_TRACKER_ID", component_tracker_id)

    component_item_id = resolve_component_item_id(component_tracker_id, data["component_key"])

    debug("RESOLVED COMPONENT ITEM ID", component_item_id)

    field_repository = resolve_field_id(tracker_id, "CB_FIELD_REPOSITORY")
    field_file_path = resolve_field_id(tracker_id, "CB_FIELD_FILE_PATH")
    field_start_line = resolve_field_id(tracker_id, "CB_FIELD_START_LINE")
    field_end_line = resolve_field_id(tracker_id, "CB_FIELD_END_LINE")
    field_scope_name = resolve_field_id(tracker_id, "CB_FIELD_SCOPE_NAME")
    field_commit_sha = resolve_field_id(tracker_id, "CB_FIELD_COMMIT_SHA")
    field_permalink = resolve_field_id(tracker_id, "CB_FIELD_PERMALINK")
    field_linked_component = resolve_field_id(tracker_id, "CB_FIELD_LINKED_COMPONENT")

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

    debug("Codebeamer Create URL", url)
    debug("Codebeamer Create Payload", payload)

    resp = requests.post(url, auth=auth, json=payload, timeout=60)

    print("=== POST CREATE ITEM STATUS ===")
    print(resp.status_code)
    print("=== POST CREATE ITEM BODY ===")
    print(resp.text)

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

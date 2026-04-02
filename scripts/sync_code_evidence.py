import os
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


COMMENT_COMPONENT_LINE_RE = re.compile(r"CB_COMPONENT_ID:\s*([^\n\r]+)")
COMMENT_SCOPE_RE = re.compile(r"CB_SCOPE:\s*([A-Za-z0-9_./-]+)")
COMPONENT_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+")
SUPPORTED_SUFFIXES = {".java", ".c", ".cpp", ".cc", ".h", ".hpp"}
ANNOTATION_WINDOW = 8


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value.strip()


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


def unique_keep_order(values: List[Any]) -> List[Any]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_component_tokens(raw_value: str) -> List[str]:
    cleaned = raw_value
    cleaned = cleaned.replace("/*", " ")
    cleaned = cleaned.replace("*/", " ")
    cleaned = cleaned.replace("//", " ")
    cleaned = cleaned.replace("*", " ")
    cleaned = cleaned.replace(",", " ")
    cleaned = cleaned.replace(";", " ")
    cleaned = cleaned.replace("|", " ")

    tokens = COMPONENT_TOKEN_RE.findall(cleaned)
    return unique_keep_order(tokens)


def find_annotation_at(lines: List[str], start_index: int) -> Optional[Dict[str, Any]]:
    component_tokens: List[str] = []
    scope_name = None
    last_annotation_index = None

    for idx in range(start_index, min(start_index + ANNOTATION_WINDOW, len(lines))):
        component_matches = COMMENT_COMPONENT_LINE_RE.findall(lines[idx])
        for raw_match in component_matches:
            component_tokens.extend(parse_component_tokens(raw_match))
            last_annotation_index = idx

        scope_match = COMMENT_SCOPE_RE.search(lines[idx])
        if scope_match:
            scope_name = scope_match.group(1).strip()
            last_annotation_index = idx

    component_tokens = unique_keep_order(component_tokens)

    if component_tokens and scope_name:
        return {
            "component_tokens": component_tokens,
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
                        "component_tokens": annotation["component_tokens"],
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


def get_field_info(
    auth: tuple[str, str],
    api_base_url: str,
    tracker_id: int,
    field_id: int,
) -> Dict[str, Any]:
    url = f"{api_base_url}/trackers/{tracker_id}/fields/{field_id}"
    result = request_json("GET", url, auth=auth)

    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected field info response for field {field_id}: {result}")

    return result


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


def resolve_component_item_ids(
    auth: tuple[str, str],
    api_base_url: str,
    component_tokens: List[str],
) -> List[int]:
    resolved_ids: List[int] = []
    seen = set()

    for token in component_tokens:
        component_item_id = resolve_component_item_id(auth, api_base_url, token)
        if component_item_id not in seen:
            seen.add(component_item_id)
            resolved_ids.append(component_item_id)

    return resolved_ids


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


def build_text_field_value(field_id: int, field_name: str, value: str) -> Dict[str, Any]:
    return {
        "fieldId": field_id,
        "name": field_name,
        "type": "TextFieldValue",
        "value": value,
    }


def build_integer_field_value(field_id: int, field_name: str, value: int) -> Dict[str, Any]:
    return {
        "fieldId": field_id,
        "name": field_name,
        "type": "IntegerFieldValue",
        "value": value,
    }


def build_url_field_value(
    tracker_id: int,
    field_info: Dict[str, Any],
    url_value: str,
) -> Dict[str, Any]:
    value_model = field_info.get("valueModel")
    if value_model != "UrlFieldValue":
        raise RuntimeError(
            f"Field {field_info.get('id')} ({field_info.get('name')}) in tracker {tracker_id} "
            f"is not a Url field. Current valueModel={value_model}."
        )

    return {
        "fieldId": int(field_info["id"]),
        "name": field_info["name"],
        "type": "UrlFieldValue",
        "value": url_value,
    }


def build_wikitext_field_value(
    tracker_id: int,
    field_info: Dict[str, Any],
    text_value: str,
) -> Dict[str, Any]:
    value_model = field_info.get("valueModel")
    if value_model != "WikiTextFieldValue":
        raise RuntimeError(
            f"Field {field_info.get('id')} ({field_info.get('name')}) in tracker {tracker_id} "
            f"is not a Wikitext field. Current valueModel={value_model}."
        )

    return {
        "fieldId": int(field_info["id"]),
        "name": field_info["name"],
        "type": "WikiTextFieldValue",
        "value": text_value,
    }


def build_linked_component_field_value(
    tracker_id: int,
    field_info: Dict[str, Any],
    component_item_ids: List[int],
) -> Dict[str, Any]:
    value_model = field_info.get("valueModel", "")
    reference_type = field_info.get("referenceType")
    multiple_values = bool(field_info.get("multipleValues", False))

    if not str(value_model).startswith("ChoiceFieldValue"):
        raise RuntimeError(
            f"Field {field_info.get('id')} ({field_info.get('name')}) in tracker {tracker_id} "
            f"does not accept ChoiceFieldValue. Current valueModel={value_model}"
        )

    if reference_type != "TrackerItemReference":
        raise RuntimeError(
            f"Field {field_info.get('id')} ({field_info.get('name')}) in tracker {tracker_id} "
            f"is not a TrackerItem reference field. referenceType={reference_type}"
        )

    if len(component_item_ids) > 1 and not multiple_values:
        raise RuntimeError(
            f"Linked Component field {field_info.get('id')} ({field_info.get('name')}) "
            "is not configured as multi-value, but multiple CB_COMPONENT_ID values were found."
        )

    return {
        "fieldId": int(field_info["id"]),
        "name": field_info["name"],
        "type": "ChoiceFieldValue",
        "values": [
            {
                "id": int(component_item_id),
                "name": str(component_item_id),
                "type": "TrackerItemReference",
            }
            for component_item_id in component_item_ids
        ],
    }


def build_custom_fields_for_create(
    auth: tuple[str, str],
    api_base_url: str,
    tracker_id: int,
    data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    field_repository = resolve_field_id("CB_FIELD_REPOSITORY")
    field_file_path = resolve_field_id("CB_FIELD_FILE_PATH")
    field_start_line = resolve_field_id("CB_FIELD_START_LINE")
    field_end_line = resolve_field_id("CB_FIELD_END_LINE")
    field_commit_sha = resolve_field_id("CB_FIELD_COMMIT_SHA")
    field_permalink = resolve_field_id("CB_FIELD_PERMALINK")
    field_permalink_label = resolve_field_id("CB_FIELD_PERMALINK_LABEL")
    field_linked_component = resolve_field_id("CB_FIELD_LINKED_COMPONENT")

    permalink_field_info = get_field_info(auth, api_base_url, tracker_id, field_permalink)
    permalink_label_field_info = get_field_info(auth, api_base_url, tracker_id, field_permalink_label)
    linked_component_field_info = get_field_info(auth, api_base_url, tracker_id, field_linked_component)

    return [
        build_text_field_value(field_repository, "Repository Name", data["repository"]),
        build_text_field_value(field_file_path, "File Path", data["file_path"]),
        build_integer_field_value(field_start_line, "Start Line", data["start_line"]),
        build_integer_field_value(field_end_line, "End Line", data["end_line"]),
        build_text_field_value(field_commit_sha, "Commit SHA", data["commit_sha"]),
        build_url_field_value(tracker_id, permalink_field_info, data["permalink"]),
        build_wikitext_field_value(
            tracker_id,
            permalink_label_field_info,
            f"[GITHUB LINK|{data['permalink']}]",
        ),
        build_linked_component_field_value(
            tracker_id,
            linked_component_field_info,
            data["component_item_ids"],
        ),
    ]


def build_field_values_for_update(
    auth: tuple[str, str],
    api_base_url: str,
    tracker_id: int,
    data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    field_repository = resolve_field_id("CB_FIELD_REPOSITORY")
    field_file_path = resolve_field_id("CB_FIELD_FILE_PATH")
    field_start_line = resolve_field_id("CB_FIELD_START_LINE")
    field_end_line = resolve_field_id("CB_FIELD_END_LINE")
    field_commit_sha = resolve_field_id("CB_FIELD_COMMIT_SHA")
    field_permalink = resolve_field_id("CB_FIELD_PERMALINK")
    field_permalink_label = resolve_field_id("CB_FIELD_PERMALINK_LABEL")
    field_linked_component = resolve_field_id("CB_FIELD_LINKED_COMPONENT")

    permalink_field_info = get_field_info(auth, api_base_url, tracker_id, field_permalink)
    permalink_label_field_info = get_field_info(auth, api_base_url, tracker_id, field_permalink_label)
    linked_component_field_info = get_field_info(auth, api_base_url, tracker_id, field_linked_component)

    return [
        build_text_field_value(field_repository, "Repository Name", data["repository"]),
        build_text_field_value(field_file_path, "File Path", data["file_path"]),
        build_integer_field_value(field_start_line, "Start Line", data["start_line"]),
        build_integer_field_value(field_end_line, "End Line", data["end_line"]),
        build_text_field_value(field_commit_sha, "Commit SHA", data["commit_sha"]),
        build_url_field_value(tracker_id, permalink_field_info, data["permalink"]),
        build_wikitext_field_value(
            tracker_id,
            permalink_label_field_info,
            f"[GITHUB LINK|{data['permalink']}]",
        ),
        build_linked_component_field_value(
            tracker_id,
            linked_component_field_info,
            data["component_item_ids"],
        ),
    ]


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
        "customFields": build_custom_fields_for_create(auth, api_base_url, tracker_id, data),
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
    tracker_id: int,
    item_id: int,
    data: Dict[str, Any],
) -> None:
    url = f"{api_base_url}/items/{item_id}/fields"
    payload = {
        "fieldValues": build_field_values_for_update(auth, api_base_url, tracker_id, data)
    }

    debug("UPDATE FIELDS URL", url)
    debug("UPDATE FIELDS PAYLOAD", payload)

    request_json("PUT", url, auth=auth, json=payload)


def has_all_linked_components(
    item_data: Any,
    linked_field_id: int,
    component_item_ids: List[int],
) -> bool:
    if not isinstance(item_data, dict):
        return False

    expected_ids = {int(x) for x in component_item_ids}
    actual_ids = set()

    custom_fields = item_data.get("customFields", [])
    for field in custom_fields:
        if int(field.get("fieldId", -1)) != linked_field_id:
            continue

        for value in field.get("values", []):
            actual_ids.add(int(value.get("id", -1)))

    return expected_ids.issubset(actual_ids)


def verify_item(
    auth: tuple[str, str],
    api_base_url: str,
    item_id: int,
    component_item_ids: List[int],
) -> None:
    linked_field_id = resolve_field_id("CB_FIELD_LINKED_COMPONENT")
    item_data = get_item(auth, api_base_url, item_id)
    debug("VERIFY ITEM RESPONSE", item_data)

    if not has_all_linked_components(item_data, linked_field_id, component_item_ids):
        raise RuntimeError(
            "Linked Component verification failed. "
            f"item_id={item_id}, component_item_ids={component_item_ids}"
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
        component_item_ids = resolve_component_item_ids(auth, api_base_url, target["component_tokens"])

        permalink = build_permalink(
            server_url=server_url,
            repository=repository,
            sha=sha,
            file_path=target["file_path"],
            start_line=target["start_line"],
            end_line=target["end_line"],
        )

        payload_data = {
            "component_item_ids": component_item_ids,
            "scope_name": target["scope_name"],
            "file_path": target["file_path"],
            "start_line": target["start_line"],
            "end_line": target["end_line"],
            "repository": repository,
            "commit_sha": sha,
            "permalink": permalink,
            "evidence_name": target["scope_name"],
        }

        debug("PROCESSING TARGET", payload_data)

        existing_item_id = find_existing_evidence_item_id(
            auth=auth,
            api_base_url=api_base_url,
            tracker_id=tracker_id,
            evidence_name=payload_data["evidence_name"],
        )

        if existing_item_id is None:
            item_id = create_evidence_item(
                auth=auth,
                api_base_url=api_base_url,
                tracker_id=tracker_id,
                data=payload_data,
            )
            created_count += 1
            print(f"CREATED: {item_id} / {payload_data['evidence_name']}")
        else:
            update_evidence_item_fields(
                auth=auth,
                api_base_url=api_base_url,
                tracker_id=tracker_id,
                item_id=existing_item_id,
                data=payload_data,
            )
            updated_count += 1
            item_id = existing_item_id
            print(f"UPDATED: {item_id} / {payload_data['evidence_name']}")

        verify_item(
            auth=auth,
            api_base_url=api_base_url,
            item_id=item_id,
            component_item_ids=component_item_ids,
        )

    print("=== SYNC RESULT ===")
    print(json.dumps({"created": created_count, "updated": updated_count}, ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple

import requests


# =========================================================
# ENV
# =========================================================
CB_BASE_URL = os.getenv("CB_BASE_URL", "http://218.237.27.234:8080/cb").rstrip("/")
CB_API_BASE = f"{CB_BASE_URL}/api/v3"

CB_USERNAME = os.getenv("CB_USERNAME", "")
CB_PASSWORD = os.getenv("CB_PASSWORD", "")
CB_TOKEN = os.getenv("CB_TOKEN", "")

CB_TRACKER_ID = int(os.getenv("CB_TRACKER_ID", "146182"))
CB_FIELD_LINKED_COMPONENT = int(os.getenv("CB_FIELD_LINKED_COMPONENT", "1002"))

# 필요 시 숫자 fieldId 넣어서 사용
CB_FIELD_SCOPE = os.getenv("CB_FIELD_SCOPE", "")                 # 예: "1234"
CB_FIELD_REPOSITORY_NAME = os.getenv("CB_FIELD_REPOSITORY_NAME", "")
CB_FIELD_START_LINE = os.getenv("CB_FIELD_START_LINE", "")
CB_FIELD_END_LINE = os.getenv("CB_FIELD_END_LINE", "")
CB_FIELD_FILE_PATH = os.getenv("CB_FIELD_FILE_PATH", "")
CB_FIELD_COMMIT_SHA = os.getenv("CB_FIELD_COMMIT_SHA", "")

SCAN_ROOT = os.getenv("SCAN_ROOT", ".")
FILE_EXTENSIONS = os.getenv(
    "FILE_EXTENSIONS",
    ".c,.cc,.cpp,.cxx,.h,.hpp,.java,.kt,.py,.js,.ts,.tsx,.jsx"
)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
DEBUG = os.getenv("DEBUG", "true").lower() == "true"


# GitHub Actions context
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_SHA = os.getenv("GITHUB_SHA", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "")
GITHUB_SERVER_URL = os.getenv("GITHUB_SERVER_URL", "https://github.com")


# =========================================================
# SESSION
# =========================================================
session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
})

if CB_TOKEN:
    session.headers.update({"Authorization": f"Bearer {CB_TOKEN}"})
elif CB_USERNAME and CB_PASSWORD:
    session.auth = (CB_USERNAME, CB_PASSWORD)
else:
    print("ERROR: Set CB_TOKEN or CB_USERNAME/CB_PASSWORD.", file=sys.stderr)
    sys.exit(1)


# =========================================================
# LOG
# =========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# =========================================================
# HTTP
# =========================================================
def request_json(method: str, url: str, expected: Tuple[int, ...] = (200, 201), **kwargs) -> Any:
    resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    debug(f"{method} {url} -> {resp.status_code}")
    if resp.text:
        debug(resp.text[:4000])

    if resp.status_code not in expected:
        raise requests.HTTPError(
            f"{method} {url} failed: {resp.status_code}\n{resp.text}",
            response=resp
        )

    if resp.text.strip():
        return resp.json()
    return None


def safe_request_json(method: str, url: str, expected: Tuple[int, ...] = (200, 201), **kwargs) -> Tuple[bool, Optional[Any], Optional[str]]:
    try:
        data = request_json(method, url, expected=expected, **kwargs)
        return True, data, None
    except Exception as e:
        return False, None, str(e)


# =========================================================
# UTILS
# =========================================================
def normalize_path(p: str) -> str:
    return str(pathlib.Path(p).as_posix())


def count_line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def github_commit_url() -> str:
    if GITHUB_REPOSITORY and GITHUB_SHA:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/commit/{GITHUB_SHA}"
    return ""


def github_file_url(file_path: str) -> str:
    if GITHUB_REPOSITORY and GITHUB_SHA:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/blob/{GITHUB_SHA}/{normalize_path(file_path)}"
    return ""


def build_item_name(scope: str) -> str:
    # Scope가 같으면 같은 아이템으로 보도록 이름 자체를 scope로 고정
    return scope.strip()


def build_description(entry: Dict[str, Any]) -> str:
    lines = [
        f"Scope: {entry['scope']}",
        f"Source File: {entry['file_path']}",
        f"Start Line: {entry['start_line']}",
        f"End Line: {entry['end_line']}",
        f"Linked Component Candidate ID: {entry['component_id']}",
    ]

    if GITHUB_REPOSITORY:
        lines.append(f"Repository: {GITHUB_REPOSITORY}")
    if GITHUB_REF_NAME:
        lines.append(f"Branch/Ref: {GITHUB_REF_NAME}")
    if GITHUB_SHA:
        lines.append(f"Commit: {GITHUB_SHA}")

    commit_url = github_commit_url()
    if commit_url:
        lines.append(f"Commit URL: {commit_url}")

    file_url = github_file_url(entry["file_path"])
    if file_url:
        lines.append(f"File URL: {file_url}")

    return "\n".join(lines)


def make_ref_obj(item_id: int) -> Dict[str, Any]:
    return {
        "id": int(item_id),
        "type": "TrackerItemReference"
    }


# =========================================================
# PARSER
# =========================================================
PAIR_PATTERN = re.compile(
    r"/\*\s*CB_COMPONENT_ID\s*:\s*(\d+)\s*\*/\s*/\*\s*CB_SCOPE\s*:\s*([^\*]+?)\s*\*/",
    re.DOTALL
)


def scan_source_files(root: str, extensions: List[str]) -> List[str]:
    root_path = pathlib.Path(root)
    results: List[str] = []

    for p in root_path.rglob("*"):
        if p.is_file() and p.suffix.lower() in extensions:
            results.append(str(p))

    return results


def parse_codebeamer_comments(file_path: str) -> List[Dict[str, Any]]:
    text = pathlib.Path(file_path).read_text(encoding="utf-8", errors="ignore")
    entries: List[Dict[str, Any]] = []

    for m in PAIR_PATTERN.finditer(text):
        component_id = int(m.group(1).strip())
        scope = m.group(2).strip()
        start_line = count_line(text, m.start())
        end_line = count_line(text, m.end())

        entries.append({
            "component_id": component_id,
            "scope": scope,
            "file_path": normalize_path(file_path),
            "start_line": start_line,
            "end_line": end_line,
        })

    return entries


# =========================================================
# PAYLOAD
# =========================================================
def add_simple_field(field_values: List[Dict[str, Any]], field_id: str, value: Any) -> None:
    if not field_id:
        return
    field_values.append({
        "fieldId": int(field_id),
        "values": [value]
    })


def build_core_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    field_values: List[Dict[str, Any]] = []

    add_simple_field(field_values, CB_FIELD_SCOPE, entry["scope"])
    add_simple_field(field_values, CB_FIELD_REPOSITORY_NAME, GITHUB_REPOSITORY)
    add_simple_field(field_values, CB_FIELD_START_LINE, entry["start_line"])
    add_simple_field(field_values, CB_FIELD_END_LINE, entry["end_line"])
    add_simple_field(field_values, CB_FIELD_FILE_PATH, entry["file_path"])
    add_simple_field(field_values, CB_FIELD_COMMIT_SHA, GITHUB_SHA)

    payload: Dict[str, Any] = {
        "name": build_item_name(entry["scope"]),
        "description": build_description(entry),
    }

    if field_values:
        payload["fieldValues"] = field_values

    return payload


def build_linked_component_candidate_payloads(component_ids: List[int]) -> List[Dict[str, Any]]:
    refs = [make_ref_obj(x) for x in component_ids]
    id_only_refs = [{"id": int(x)} for x in component_ids]

    return [
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": refs
                }
            ]
        },
        {
            "customFields": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": refs
                }
            ]
        },
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "value": refs
                }
            ]
        },
        {
            "customFields": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "value": refs
                }
            ]
        },
        {
            "fieldValues": [
                {
                    "id": CB_FIELD_LINKED_COMPONENT,
                    "values": refs
                }
            ]
        },
        {
            "customFields": [
                {
                    "id": CB_FIELD_LINKED_COMPONENT,
                    "values": refs
                }
            ]
        },
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": id_only_refs
                }
            ]
        },
        {
            "customFields": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": id_only_refs
                }
            ]
        },
    ]


# =========================================================
# API
# =========================================================
def create_work_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{CB_API_BASE}/trackers/{CB_TRACKER_ID}/items"
    return request_json("POST", url, expected=(200, 201), data=json.dumps(payload))


def patch_work_item(item_id: int, payload: Dict[str, Any]) -> Tuple[bool, Optional[Any], Optional[str]]:
    url = f"{CB_API_BASE}/items/{item_id}"
    return safe_request_json("PATCH", url, expected=(200, 201), data=json.dumps(payload))


def put_work_item(item_id: int, payload: Dict[str, Any]) -> Tuple[bool, Optional[Any], Optional[str]]:
    url = f"{CB_API_BASE}/items/{item_id}"
    return safe_request_json("PUT", url, expected=(200, 201), data=json.dumps(payload))


def get_work_item(item_id: int) -> Dict[str, Any]:
    url = f"{CB_API_BASE}/items/{item_id}"
    return request_json("GET", url, expected=(200,), params={"include": "fieldValues,customFields"})


def list_tracker_items() -> List[Dict[str, Any]]:
    url = f"{CB_API_BASE}/trackers/{CB_TRACKER_ID}/items"

    # page 방식 시도
    ok, data, err = safe_request_json(
        "GET",
        url,
        expected=(200,),
        params={"page": 1, "pageSize": 500}
    )

    if not ok:
        debug(f"Paged list failed, fallback simple GET: {err}")
        data = request_json("GET", url, expected=(200,))

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("results"), list):
            return data["results"]

    return []


# =========================================================
# FIELD EXTRACTION
# =========================================================
def extract_field_values(item_data: Dict[str, Any], target_field_id: int) -> List[Any]:
    result: List[Any] = []

    for key in ("fieldValues", "customFields"):
        arr = item_data.get(key, [])
        if not isinstance(arr, list):
            continue

        for field in arr:
            field_id = field.get("fieldId", field.get("id"))
            if field_id is None:
                continue

            try:
                if int(field_id) != int(target_field_id):
                    continue
            except Exception:
                continue

            values = field.get("values")
            if values is None:
                single_value = field.get("value")
                if single_value is not None:
                    values = single_value if isinstance(single_value, list) else [single_value]

            if isinstance(values, list):
                result.extend(values)

    return result


def extract_linked_component_ids(item_data: Dict[str, Any]) -> List[int]:
    result: List[int] = []
    values = extract_field_values(item_data, CB_FIELD_LINKED_COMPONENT)

    for v in values:
        if isinstance(v, dict) and "id" in v:
            try:
                result.append(int(v["id"]))
            except Exception:
                pass
        elif isinstance(v, int):
            result.append(v)

    return sorted(set(result))


def extract_scope_from_name(name: str) -> List[str]:
    candidates = []
    raw = (name or "").strip()
    if not raw:
        return candidates

    candidates.append(raw)

    prefix = "[Code Evidence]"
    if raw.startswith(prefix):
        rest = raw[len(prefix):].strip()
        if rest:
            candidates.append(rest)
        if " - " in rest:
            candidates.append(rest.split(" - ", 1)[0].strip())

    return sorted(set([x for x in candidates if x]))


def extract_scope_from_description(description: str) -> Optional[str]:
    if not description:
        return None
    m = re.search(r"^Scope:\s*(.+)$", description, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


# =========================================================
# SCOPE INDEX
# =========================================================
def build_scope_index() -> Dict[str, int]:
    index: Dict[str, int] = {}
    items = list_tracker_items()

    for item in items:
        item_id = item.get("id")
        name = item.get("name", "")

        if not item_id:
            continue

        for scope_key in extract_scope_from_name(name):
            key = scope_key.lower()
            if key not in index:
                index[key] = int(item_id)

    # name에서 못 찾는 경우 description / custom field 기반 보완
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue

        try:
            detail = get_work_item(int(item_id))
        except Exception as e:
            debug(f"Failed to read item {item_id} detail while building scope index: {e}")
            continue

        # scope custom field
        if CB_FIELD_SCOPE:
            values = extract_field_values(detail, int(CB_FIELD_SCOPE))
            for v in values:
                if isinstance(v, str) and v.strip():
                    key = v.strip().lower()
                    if key not in index:
                        index[key] = int(item_id)

        # description
        scope_from_desc = extract_scope_from_description(detail.get("description", ""))
        if scope_from_desc:
            key = scope_from_desc.lower()
            if key not in index:
                index[key] = int(item_id)

    return index


# =========================================================
# UPSERT LOGIC
# =========================================================
def update_core_fields(item_id: int, payload: Dict[str, Any]) -> None:
    ok, _, err = patch_work_item(item_id, payload)
    if ok:
        return

    debug(f"PATCH core update failed for item {item_id}: {err}")
    ok, _, err = put_work_item(item_id, payload)
    if ok:
        return

    raise RuntimeError(f"Core update failed for item {item_id}: {err}")


def set_linked_component(item_id: int, component_id: int) -> bool:
    try:
        detail = get_work_item(item_id)
        existing_ids = extract_linked_component_ids(detail)
    except Exception as e:
        debug(f"Failed to read existing linked components for item {item_id}: {e}")
        existing_ids = []

    merged_ids = sorted(set(existing_ids + [int(component_id)]))
    candidates = build_linked_component_candidate_payloads(merged_ids)

    for i, payload in enumerate(candidates, start=1):
        debug(f"Trying Linked Component payload #{i} for item {item_id}: {json.dumps(payload, ensure_ascii=False)}")

        ok, _, err = patch_work_item(item_id, payload)
        if not ok:
            debug(f"PATCH failed #{i}: {err}")
            ok, _, err = put_work_item(item_id, payload)
            if not ok:
                debug(f"PUT failed #{i}: {err}")
                continue

        try:
            verify = get_work_item(item_id)
            linked_ids = extract_linked_component_ids(verify)
            if int(component_id) in linked_ids:
                return True
        except Exception as e:
            debug(f"Verification failed after payload #{i}: {e}")

    return False


def upsert_by_scope(entry: Dict[str, Any], scope_index: Dict[str, int]) -> Dict[str, Any]:
    scope_key = entry["scope"].strip().lower()
    payload = build_core_payload(entry)

    if scope_key in scope_index:
        item_id = int(scope_index[scope_key])
        update_core_fields(item_id, payload)
        action = "updated"
    else:
        created = create_work_item(payload)
        item_id = int(created["id"])
        scope_index[scope_key] = item_id
        action = "created"

    linked_ok = set_linked_component(item_id, int(entry["component_id"]))

    return {
        "action": action,
        "item_id": item_id,
        "scope": entry["scope"],
        "component_id": int(entry["component_id"]),
        "file_path": entry["file_path"],
        "start_line": entry["start_line"],
        "end_line": entry["end_line"],
        "linked_component_set": linked_ok,
    }


# =========================================================
# MAIN
# =========================================================
def main() -> None:
    extensions = [x.strip().lower() for x in FILE_EXTENSIONS.split(",") if x.strip()]
    if not extensions:
        fail("FILE_EXTENSIONS is empty.")

    files = scan_source_files(SCAN_ROOT, extensions)
    if not files:
        log("No source files found.")
        return

    entries: List[Dict[str, Any]] = []
    for file_path in files:
        try:
            entries.extend(parse_codebeamer_comments(file_path))
        except Exception as e:
            debug(f"Parse failed: {file_path} / {e}")

    if not entries:
        log("No CB_COMPONENT_ID / CB_SCOPE comments found.")
        return

    log(f"Found {len(entries)} code evidence entries.")

    scope_index = build_scope_index()
    debug(f"Scope index size: {len(scope_index)}")

    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for entry in entries:
        try:
            result = upsert_by_scope(entry, scope_index)
            results.append(result)

            log(
                f"SUCCESS | action={result['action']} | "
                f"item_id={result['item_id']} | "
                f"scope={result['scope']} | "
                f"component_id={result['component_id']} | "
                f"linked_component_set={result['linked_component_set']} | "
                f"file={result['file_path']}:{result['start_line']}-{result['end_line']}"
            )
        except Exception as e:
            failed.append({
                "entry": entry,
                "error": str(e)
            })
            log(
                f"FAILED | scope={entry['scope']} | "
                f"component_id={entry['component_id']} | "
                f"file={entry['file_path']}:{entry['start_line']}-{entry['end_line']} | "
                f"error={e}"
            )

    summary = {
        "created_or_updated_count": len(results),
        "failed_count": len(failed),
        "results": results,
        "failed": failed
    }

    print("\n===== SUMMARY =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    github_output = os.getenv("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"processed_count={len(results)}\n")
            f.write(f"failed_count={len(failed)}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

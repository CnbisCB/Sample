#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import base64
import pathlib
from typing import Any, Dict, List, Optional, Tuple

import requests


# =========================================================
# Environment Variables
# =========================================================
CB_BASE_URL = os.getenv("CB_BASE_URL", "http://218.237.27.234:8080/cb").rstrip("/")
CB_API_BASE = f"{CB_BASE_URL}/api/v3"

CB_USERNAME = os.getenv("CB_USERNAME", "")
CB_PASSWORD = os.getenv("CB_PASSWORD", "")
CB_TOKEN = os.getenv("CB_TOKEN", "")

CB_TRACKER_ID = int(os.getenv("CB_TRACKER_ID", "146182"))  # Code Evidence Tracker ID
CB_FIELD_LINKED_COMPONENT = int(os.getenv("CB_FIELD_LINKED_COMPONENT", "1002"))

# Optional additional custom field IDs if needed
CB_FIELD_SCOPE = os.getenv("CB_FIELD_SCOPE", "")  # ex: "1234" if you have a dedicated custom field for scope
CB_FIELD_SOURCE_PATH = os.getenv("CB_FIELD_SOURCE_PATH", "")  # ex: "1235"

# GitHub Actions / repo context
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_SHA = os.getenv("GITHUB_SHA", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "")
GITHUB_SERVER_URL = os.getenv("GITHUB_SERVER_URL", "https://github.com")

# Source scan config
SCAN_ROOT = os.getenv("SCAN_ROOT", ".")
FILE_EXTENSIONS = os.getenv(
    "FILE_EXTENSIONS",
    ".c,.cc,.cpp,.cxx,.h,.hpp,.java,.kt,.py,.js,.ts,.tsx,.jsx"
)

# Behavior
VERIFY_AFTER_UPDATE = os.getenv("VERIFY_AFTER_UPDATE", "true").lower() == "true"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
DEBUG = os.getenv("DEBUG", "true").lower() == "true"


# =========================================================
# HTTP Session
# =========================================================
session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
})

if CB_TOKEN:
    session.headers.update({
        "Authorization": f"Bearer {CB_TOKEN}"
    })
elif CB_USERNAME and CB_PASSWORD:
    session.auth = (CB_USERNAME, CB_PASSWORD)
else:
    print("ERROR: Set either CB_TOKEN or CB_USERNAME/CB_PASSWORD.", file=sys.stderr)
    sys.exit(1)


# =========================================================
# Utils
# =========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def request_json(method: str, url: str, expected: Tuple[int, ...] = (200, 201), **kwargs) -> Any:
    resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    debug(f"{method} {url} -> {resp.status_code}")
    if resp.text:
        debug(f"Response: {resp.text[:4000]}")
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


def normalize_path(p: str) -> str:
    return str(pathlib.Path(p).as_posix())


def github_commit_url() -> str:
    if GITHUB_REPOSITORY and GITHUB_SHA:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/commit/{GITHUB_SHA}"
    return ""


def github_file_url(file_path: str) -> str:
    if GITHUB_REPOSITORY and GITHUB_SHA:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/blob/{GITHUB_SHA}/{normalize_path(file_path)}"
    return normalize_path(file_path)


def build_description(file_path: str, scope: str, component_id: int) -> str:
    lines = [
        f"Source File: {normalize_path(file_path)}",
        f"Scope: {scope}",
        f"Linked Component Candidate ID: {component_id}",
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
    file_url = github_file_url(file_path)
    if file_url:
        lines.append(f"File URL: {file_url}")
    return "\n".join(lines)


def build_item_name(scope: str, file_path: str) -> str:
    base = pathlib.Path(file_path).name
    return f"[Code Evidence] {scope} - {base}"


# =========================================================
# Parser
# =========================================================
COMMENT_PATTERN = re.compile(
    r"/\*\s*CB_COMPONENT_ID\s*:\s*(\d+)\s*\*/.*?/\*\s*CB_SCOPE\s*:\s*([^\*]+?)\s*\*/",
    re.DOTALL
)

SINGLE_COMMENT_COMPONENT = re.compile(r"/\*\s*CB_COMPONENT_ID\s*:\s*(\d+)\s*\*/")
SINGLE_COMMENT_SCOPE = re.compile(r"/\*\s*CB_SCOPE\s*:\s*([^\*]+?)\s*\*/")


def scan_source_files(root: str, extensions: List[str]) -> List[str]:
    found: List[str] = []
    root_path = pathlib.Path(root)
    for p in root_path.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in extensions:
            found.append(str(p))
    return found


def parse_codebeamer_comments(file_path: str) -> List[Dict[str, Any]]:
    text = pathlib.Path(file_path).read_text(encoding="utf-8", errors="ignore")
    matches: List[Dict[str, Any]] = []

    # 1) paired pattern
    for m in COMMENT_PATTERN.finditer(text):
        component_id = int(m.group(1).strip())
        scope = m.group(2).strip()
        matches.append({
            "component_id": component_id,
            "scope": scope,
            "file_path": file_path
        })

    # 2) fallback if comments are not adjacent
    if not matches:
        components = SINGLE_COMMENT_COMPONENT.findall(text)
        scopes = SINGLE_COMMENT_SCOPE.findall(text)
        if components and scopes:
            pair_count = min(len(components), len(scopes))
            for i in range(pair_count):
                matches.append({
                    "component_id": int(components[i].strip()),
                    "scope": scopes[i].strip(),
                    "file_path": file_path
                })

    return matches


# =========================================================
# Codebeamer Payload Builders
# =========================================================
def make_ref_obj(item_id: int) -> Dict[str, Any]:
    return {
        "id": int(item_id),
        "type": "TrackerItemReference"
    }


def build_create_payload(file_path: str, scope: str, component_id: int) -> Dict[str, Any]:
    """
    기본 생성 payload
    - name / description 으로 먼저 생성
    - linked component는 생성 후 별도 update 시도
    """
    payload: Dict[str, Any] = {
        "name": build_item_name(scope, file_path),
        "description": build_description(file_path, scope, component_id),
    }

    field_values: List[Dict[str, Any]] = []

    if CB_FIELD_SCOPE:
        field_values.append({
            "fieldId": int(CB_FIELD_SCOPE),
            "values": [scope]
        })

    if CB_FIELD_SOURCE_PATH:
        field_values.append({
            "fieldId": int(CB_FIELD_SOURCE_PATH),
            "values": [normalize_path(file_path)]
        })

    if field_values:
        payload["fieldValues"] = field_values

    return payload


def linked_component_candidate_payloads(component_id: int) -> List[Dict[str, Any]]:
    ref = make_ref_obj(component_id)

    candidates = [
        # Candidate 1
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": [ref]
                }
            ]
        },
        # Candidate 2
        {
            "customFields": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": [ref]
                }
            ]
        },
        # Candidate 3
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "value": [ref]
                }
            ]
        },
        # Candidate 4
        {
            "fieldValues": [
                {
                    "id": CB_FIELD_LINKED_COMPONENT,
                    "values": [ref]
                }
            ]
        },
        # Candidate 5
        {
            "customFields": [
                {
                    "id": CB_FIELD_LINKED_COMPONENT,
                    "values": [ref]
                }
            ]
        },
        # Candidate 6
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "value": ref
                }
            ]
        },
        # Candidate 7
        {
            "customFields": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "value": ref
                }
            ]
        },
        # Candidate 8
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": [{"id": int(component_id)}]
                }
            ]
        },
        # Candidate 9
        {
            "customFields": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": [{"id": int(component_id)}]
                }
            ]
        },
        # Candidate 10
        {
            "fieldValues": [
                {
                    "fieldId": CB_FIELD_LINKED_COMPONENT,
                    "values": [
                        {
                            "id": int(component_id),
                            "name": str(component_id),
                            "type": "TrackerItemReference"
                        }
                    ]
                }
            ]
        },
    ]
    return candidates


# =========================================================
# Codebeamer API
# =========================================================
def create_work_item(tracker_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{CB_API_BASE}/trackers/{tracker_id}/items"
    return request_json("POST", url, expected=(200, 201), data=json.dumps(payload))


def get_work_item(item_id: int) -> Dict[str, Any]:
    url = f"{CB_API_BASE}/items/{item_id}"
    return request_json("GET", url, expected=(200,), params={"include": "fieldValues,customFields"})


def update_work_item_put(item_id: int, payload: Dict[str, Any]) -> Tuple[bool, Optional[Any], Optional[str]]:
    url = f"{CB_API_BASE}/items/{item_id}"
    return safe_request_json("PUT", url, expected=(200, 201), data=json.dumps(payload))


def update_work_item_patch(item_id: int, payload: Dict[str, Any]) -> Tuple[bool, Optional[Any], Optional[str]]:
    url = f"{CB_API_BASE}/items/{item_id}"
    return safe_request_json("PATCH", url, expected=(200, 201), data=json.dumps(payload))


def extract_linked_component_values(item_data: Dict[str, Any]) -> List[int]:
    result: List[int] = []

    for key in ("fieldValues", "customFields"):
        arr = item_data.get(key, [])
        if not isinstance(arr, list):
            continue

        for field in arr:
            field_id = field.get("fieldId", field.get("id"))
            if int(field_id) != CB_FIELD_LINKED_COMPONENT:
                continue

            values = field.get("values")
            if values is None:
                single_value = field.get("value")
                if single_value is not None:
                    values = single_value if isinstance(single_value, list) else [single_value]

            if not isinstance(values, list):
                continue

            for v in values:
                if isinstance(v, dict) and "id" in v:
                    try:
                        result.append(int(v["id"]))
                    except Exception:
                        pass
                elif isinstance(v, int):
                    result.append(v)

    return result


def set_linked_component(item_id: int, component_id: int) -> bool:
    """
    Linked Component 필드는 payload shape 차이로 무시될 수 있어서
    여러 후보 payload를 PUT/PATCH로 순차 시도하고
    마지막에 실제 GET으로 반영 여부 검증
    """
    candidates = linked_component_candidate_payloads(component_id)

    for idx, payload in enumerate(candidates, start=1):
        debug(f"Trying linked component payload candidate #{idx}: {json.dumps(payload, ensure_ascii=False)}")

        ok, _, err = update_work_item_patch(item_id, payload)
        if not ok:
            debug(f"PATCH candidate #{idx} failed: {err}")
            ok, _, err = update_work_item_put(item_id, payload)
            if not ok:
                debug(f"PUT candidate #{idx} failed: {err}")
                continue

        if VERIFY_AFTER_UPDATE:
            try:
                item_data = get_work_item(item_id)
                linked_ids = extract_linked_component_values(item_data)
                debug(f"Verified linked component IDs after candidate #{idx}: {linked_ids}")
                if int(component_id) in linked_ids:
                    return True
            except Exception as e:
                debug(f"Verification failed after candidate #{idx}: {e}")
        else:
            return True

    return False


# =========================================================
# Main Flow
# =========================================================
def process_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    file_path = entry["file_path"]
    scope = entry["scope"]
    component_id = int(entry["component_id"])

    create_payload = build_create_payload(file_path, scope, component_id)
    created = create_work_item(CB_TRACKER_ID, create_payload)

    item_id = created.get("id")
    if not item_id:
        raise RuntimeError(f"Created item response does not include 'id': {created}")

    linked_ok = set_linked_component(int(item_id), component_id)

    return {
        "item_id": int(item_id),
        "scope": scope,
        "component_id": component_id,
        "file_path": normalize_path(file_path),
        "linked_component_set": linked_ok,
    }


def main() -> None:
    exts = [e.strip().lower() for e in FILE_EXTENSIONS.split(",") if e.strip()]
    if not exts:
        fail("FILE_EXTENSIONS is empty.")

    files = scan_source_files(SCAN_ROOT, exts)
    if not files:
        log("No source files found.")
        return

    parsed_entries: List[Dict[str, Any]] = []
    for file_path in files:
        try:
            parsed_entries.extend(parse_codebeamer_comments(file_path))
        except Exception as e:
            debug(f"Skipping file due to parse error: {file_path} / {e}")

    if not parsed_entries:
        log("No CB_COMPONENT_ID / CB_SCOPE comments found.")
        return

    log(f"Found {len(parsed_entries)} code evidence entries.")

    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for entry in parsed_entries:
        try:
            result = process_entry(entry)
            results.append(result)
            log(
                f"SUCCESS | item_id={result['item_id']} | "
                f"component_id={result['component_id']} | "
                f"scope={result['scope']} | "
                f"linked_component_set={result['linked_component_set']} | "
                f"file={result['file_path']}"
            )
        except Exception as e:
            failed_entry = {
                "entry": entry,
                "error": str(e)
            }
            failed.append(failed_entry)
            log(
                f"FAILED | component_id={entry.get('component_id')} | "
                f"scope={entry.get('scope')} | "
                f"file={entry.get('file_path')} | "
                f"error={e}"
            )

    summary = {
        "created_count": len(results),
        "failed_count": len(failed),
        "results": results,
        "failed": failed,
    }

    print("\n===== SUMMARY =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # GitHub Actions summary / output
    github_output = os.getenv("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"created_count={len(results)}\n")
            f.write(f"failed_count={len(failed)}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

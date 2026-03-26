import os
import re
import json
from pathlib import Path

import requests


COMMENT_COMPONENT_RE = re.compile(r"CB_COMPONENT:\s*([A-Za-z0-9._-]+)")
COMMENT_SCOPE_RE = re.compile(r"CB_SCOPE:\s*([A-Za-z0-9_./-]+)")

SUPPORTED_SUFFIXES = {".java", ".c", ".cpp", ".cc", ".h", ".hpp"}


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
        or stripped.startswith("*/")
    )


def find_annotation_block(lines):
    """
    주석 2줄(CB_COMPONENT, CB_SCOPE)을 찾고,
    그 주석 블록이 끝나는 line index를 반환.
    """
    for i in range(len(lines)):
        component_value = None
        scope_value = None
        last_annotation_idx = None

        # 주석 2줄이 꼭 바로 붙어있지 않아도 되도록, 최대 5줄 정도 탐색
        for j in range(i, min(i + 5, len(lines))):
            component_match = COMMENT_COMPONENT_RE.search(lines[j])
            scope_match = COMMENT_SCOPE_RE.search(lines[j])

            if component_match:
                component_value = component_match.group(1).strip()
                last_annotation_idx = j

            if scope_match:
                scope_value = scope_match.group(1).strip()
                last_annotation_idx = j

            if component_value and scope_value:
                return {
                    "component_key": component_value,
                    "scope_name": scope_value,
                    "annotation_end_index": last_annotation_idx,
                }

    return None


def find_block_after_annotation(lines, annotation_end_index):
    """
    주석 아래 첫 코드 블록(메서드/생성자/클래스 등)의 시작~끝 라인을 계산
    """
    start_idx = None
    first_open_brace_seen = False
    brace_balance = 0

    # 주석 아래로 내려가면서 첫 선언 시작 찾기
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

    # 시작 줄부터 블록 닫힐 때까지 brace 계산
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


def find_target_comment_and_function(repo_root: Path):
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
            "component_key": annotation["component_key"],
            "scope_name": annotation["scope_name"],
            "file_path": relative_path,
            "start_line": block["start_line"],
            "end_line": block["end_line"],
        }

    raise RuntimeError("CB_COMPONENT / CB_SCOPE 주석 쌍을 찾지 못했습니다.")


def build_permalink(server_url: str, repository: str, sha: str, file_path: str, start_line: int, end_line: int) -> str:
    return f"{server_url}/{repository}/blob/{sha}/{file_path}#L{start_line}-L{end_line}"


def create_codebeamer_item(data: dict):
    base_url = require_env("CB_BASE_URL").rstrip("/")
    username = require_env("CB_USERNAME")
    password = require_env("CB_PASSWORD")
    tracker_id = require_env("CB_TRACKER_ID")

    field_repository = int(require_env("CB_FIELD_REPOSITORY"))
    field_file_path = int(require_env("CB_FIELD_FILE_PATH"))
    field_start_line = int(require_env("CB_FIELD_START_LINE"))
    field_end_line = int(require_env("CB_FIELD_END_LINE"))
    field_scope_name = int(require_env("CB_FIELD_SCOPE_NAME"))
    field_commit_sha = int(require_env("CB_FIELD_COMMIT_SHA"))
    field_permalink = int(require_env("CB_FIELD_PERMALINK"))
    field_linked_component = int(require_env("CB_FIELD_LINKED_COMPONENT"))

    # 현재는 가장 단순 테스트 버전:
    # Linked Component의 실제 item id는 secret으로 직접 넣음
    component_item_id = int(require_env("CB_COMPONENT_ITEM_ID"))

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

    print("=== Codebeamer Request URL ===")
    print(url)

    print("=== Codebeamer Request Payload ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    resp = requests.post(url, auth=(username, password), json=payload, timeout=60)

    print("=== Codebeamer Response Status ===")
    print(resp.status_code)

    print("=== Codebeamer Response Body ===")
    print(resp.text)

    resp.raise_for_status()

    print("Created Codebeamer item successfully.")


def main():
    repo_root = Path(".").resolve()

    print("=== DEBUG START ===")
    print("Repository root:", repo_root)
    print("GITHUB_REPOSITORY:", os.environ.get("GITHUB_REPOSITORY"))
    print("GITHUB_SHA:", os.environ.get("GITHUB_SHA"))
    print("CB_BASE_URL exists:", bool(os.environ.get("CB_BASE_URL")))
    print("CB_USERNAME exists:", bool(os.environ.get("CB_USERNAME")))
    print("CB_PASSWORD exists:", bool(os.environ.get("CB_PASSWORD")))
    print("CB_TRACKER_ID exists:", bool(os.environ.get("CB_TRACKER_ID")))
    print("=== DEBUG END ===")

    found = find_target_comment_and_function(repo_root)

    print("=== FOUND TARGET ===")
    print(json.dumps(found, indent=2, ensure_ascii=False))

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
        "component_key": found["component_key"],
        "scope_name": found["scope_name"],
        "file_path": found["file_path"],
        "start_line": found["start_line"],
        "end_line": found["end_line"],
        "repository": repository,
        "commit_sha": sha,
        "permalink": permalink,
    }

    print("=== FINAL DATA ===")
    print(json.dumps(payload_data, indent=2, ensure_ascii=False))

    create_codebeamer_item(payload_data)


if __name__ == "__main__":
    main()

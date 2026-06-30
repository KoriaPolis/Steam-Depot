from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_API_BASE = "https://stea-provider-api.steamidra.workers.dev"

API_BASE = (os.environ.get("STEAMIDRA_API_BASE") or DEFAULT_API_BASE).strip().rstrip("/")
ADMIN_TOKEN = (os.environ.get("STEAMIDRA_ADMIN_TOKEN") or "").strip()

if not API_BASE.startswith(("http://", "https://")):
    raise SystemExit(f"Bad STEAMIDRA_API_BASE: {API_BASE!r}")

PROVIDER_FILE = Path("fallback_depotkeys.json")
PROCESSED_LOG = Path("merged_submission_ids.json")
REPORT_FILE = Path("submission_merge_report.json")

MAX_ITEMS_PER_ACCEPTED_FILE = 100_000
MAX_TEXT_LEN = 240

ID_RE = re.compile(r"^[0-9]{1,12}$")
KEY_RE = re.compile(r"^[a-fA-F0-9]{64}$")

KIND_VALUES = {"game", "software", "dlc", "depot", "dlc_depot", "unknown"}
KIND_ALIASES = {
    "tool": "software",
    "tools": "software",
    "application": "software",
    "app": "software",
    "dlc depot": "dlc_depot",
    "dlc-depot": "dlc_depot",
}

ROOT_KINDS = {"game", "software"}
ITEM_FIELDS = {"id", "key", "name", "kind", "parent_appid", "parent_name"}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return default

    return json.loads(text)


def save_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def as_id(value: Any) -> str:
    if isinstance(value, int) and value >= 0:
        return str(value)

    if isinstance(value, str):
        value = value.strip()
        if ID_RE.fullmatch(value):
            return str(int(value))

    return ""


def clean_text(value: Any, max_len: int = MAX_TEXT_LEN) -> str:
    if not isinstance(value, str):
        return ""

    value = value.replace("\u3000", " ").replace("\xa0", " ")
    value = value.replace("\r", " ").replace("\n", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:max_len]


def clean_kind(value: Any) -> str:
    value = clean_text(value).lower()

    if value in KIND_VALUES:
        return value

    if value in KIND_ALIASES:
        return KIND_ALIASES[value]

    return "unknown"


def is_allowed_raw_kind(value: Any) -> bool:
    if value is None:
        return True

    raw = clean_text(value).lower()
    return raw in KIND_VALUES or raw in KIND_ALIASES


def normalize_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    value = value.strip().lower()
    return value if KEY_RE.fullmatch(value) else ""


def validate_item(item: Any) -> tuple[bool, str]:
    if not isinstance(item, dict):
        return False, "item is not object"

    extra = set(item) - ITEM_FIELDS
    if extra:
        return False, f"invalid fields: {sorted(extra)}"

    item_id = as_id(item.get("id"))
    if not item_id:
        return False, "bad id"

    if not normalize_key(item.get("key")):
        return False, f"{item_id}: bad key"

    name = item.get("name", "")
    if "name" in item and (
        not isinstance(name, str)
        or len(name) > MAX_TEXT_LEN
        or "\r" in name
        or "\n" in name
    ):
        return False, f"{item_id}: bad name"

    if "kind" in item and not is_allowed_raw_kind(item.get("kind")):
        return False, f"{item_id}: bad kind"

    parent_appid = item.get("parent_appid", "")
    if "parent_appid" in item and str(parent_appid).strip() and not as_id(parent_appid):
        return False, f"{item_id}: bad parent_appid"

    parent_name = item.get("parent_name", "")
    if "parent_name" in item and (
        not isinstance(parent_name, str)
        or len(parent_name) > MAX_TEXT_LEN
        or "\r" in parent_name
        or "\n" in parent_name
    ):
        return False, f"{item_id}: bad parent_name"

    return True, ""


def validate_submission(body: Any) -> tuple[bool, str]:
    if not isinstance(body, dict):
        return False, "body is not object"

    if body.get("type") != "tool_keys":
        return False, "type is not tool_keys"

    if not isinstance(body.get("tool_version"), str) or not (1 <= len(body["tool_version"]) <= 32):
        return False, "bad tool_version"

    if not isinstance(body.get("items"), list) or not (1 <= len(body["items"]) <= MAX_ITEMS_PER_ACCEPTED_FILE):
        return False, "bad items"

    for item in body["items"]:
        ok, err = validate_item(item)
        if not ok:
            return False, err

    return True, ""


def normalize_entry(entry: Any) -> dict[str, Any]:
    # Supports both provider formats:
    #   old: "123": "64hexkey"
    #   new: "123": {"key":"64hexkey", "name":"...", "kind":"..."}
    if isinstance(entry, str):
        return {
            "key": normalize_key(entry),
            "name": "",
            "kind": "unknown",
        }

    if not isinstance(entry, dict):
        entry = {}

    out: dict[str, Any] = {
        "key": normalize_key(entry.get("key", "")),
        "name": clean_text(entry.get("name", "")),
        "kind": clean_kind(entry.get("kind", "unknown")),
    }

    # Final provider rule:
    # root game/software rows must never contain parent_appid or parent_name.
    if out["kind"] in ROOT_KINDS:
        return out

    parent_appid = as_id(entry.get("parent_appid", ""))
    parent_name = clean_text(entry.get("parent_name", ""))

    if parent_appid:
        out["parent_appid"] = parent_appid

    if parent_name:
        out["parent_name"] = parent_name

    return out


def make_incoming_entry(item: dict[str, Any]) -> dict[str, Any]:
    incoming_kind = clean_kind(item.get("kind", "unknown"))

    entry: dict[str, Any] = {
        "key": normalize_key(item.get("key")),
        "name": clean_text(item.get("name", "")),
        "kind": incoming_kind,
    }

    if incoming_kind not in ROOT_KINDS:
        parent_appid = as_id(item.get("parent_appid", ""))
        parent_name = clean_text(item.get("parent_name", ""))

        if parent_appid:
            entry["parent_appid"] = parent_appid

        if parent_name:
            entry["parent_name"] = parent_name

    return normalize_entry(entry)


def merge_item(provider: dict[str, Any], item: dict[str, Any], report: dict[str, Any]) -> None:
    item_id = as_id(item.get("id"))
    incoming_entry = make_incoming_entry(item)
    incoming_key = incoming_entry["key"]
    incoming_name = incoming_entry.get("name", "")
    incoming_kind = incoming_entry.get("kind", "unknown")
    incoming_parent_appid = incoming_entry.get("parent_appid", "")
    incoming_parent_name = incoming_entry.get("parent_name", "")

    if item_id not in provider:
        provider[item_id] = incoming_entry
        report["new_entries"] += 1
        return

    raw_existing = provider.get(item_id)
    entry = normalize_entry(raw_existing)
    old_key = normalize_key(entry.get("key", ""))

    if not old_key:
        entry["key"] = incoming_key
        report["keys_filled"] += 1
    elif old_key == incoming_key:
        report["same_key_existing"] += 1
    else:
        report["conflicts"].append(
            {
                "id": item_id,
                "existing": old_key,
                "new": incoming_key,
                "name": incoming_name,
            }
        )

        # Preserve old legacy string entry on conflict.
        provider[item_id] = raw_existing if isinstance(raw_existing, str) else entry
        return

    # Accepted verifier metadata is allowed to fix old provider metadata when the key matches.
    if incoming_name and entry.get("name") != incoming_name:
        entry["name"] = incoming_name
        report["metadata_filled"] += 1

    if incoming_kind != "unknown" and entry.get("kind", "unknown") != incoming_kind:
        entry["kind"] = incoming_kind
        report["metadata_filled"] += 1

    if entry.get("kind") in ROOT_KINDS:
        removed_parent = False

        if "parent_appid" in entry:
            entry.pop("parent_appid", None)
            removed_parent = True

        if "parent_name" in entry:
            entry.pop("parent_name", None)
            removed_parent = True

        if removed_parent:
            report["metadata_filled"] += 1
    else:
        if incoming_parent_appid and entry.get("parent_appid") != incoming_parent_appid:
            entry["parent_appid"] = incoming_parent_appid
            report["metadata_filled"] += 1

        if incoming_parent_name and entry.get("parent_name") != incoming_parent_name:
            entry["parent_name"] = incoming_parent_name
            report["metadata_filled"] += 1

    provider[item_id] = normalize_entry(entry)


def sorted_provider(provider: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}

    for item_id in sorted(provider.keys(), key=lambda x: int(x) if str(x).isdigit() else 10**18):
        sid = as_id(item_id)
        if not sid:
            if report is not None:
                report["invalid_existing_entries_skipped"] += 1
            continue

        raw_entry = provider[item_id]

        # Preserve old legacy provider format instead of deleting or rewriting everything.
        if isinstance(raw_entry, str):
            key = normalize_key(raw_entry)
            if not key:
                if report is not None:
                    report["invalid_existing_entries_skipped"] += 1
                continue

            out[sid] = key
            continue

        entry = normalize_entry(raw_entry)
        if not entry.get("key"):
            if report is not None:
                report["invalid_existing_entries_skipped"] += 1
            continue

        out[sid] = entry

    return out


def api_get_json(url: str) -> Any:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "SteaMidra-GitHubAction-Merge/1.0",
            "x-admin-token": ADMIN_TOKEN,
        },
    )

    with urlopen(req, timeout=120) as res:
        return json.loads(res.read().decode("utf-8-sig"))


def list_accepted() -> list[str]:
    keys: list[str] = []
    cursor: str | None = None

    while True:
        qs = {"limit": "100000"}
        if cursor:
            qs["cursor"] = cursor

        data = api_get_json(API_BASE.rstrip("/") + "/admin/accepted?" + urlencode(qs))

        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"admin list failed: {data}")

        for obj in data.get("objects", []):
            key = obj.get("key") if isinstance(obj, dict) else None
            if isinstance(key, str) and key.startswith("accepted/") and key.endswith(".json"):
                keys.append(key)

        if not data.get("truncated"):
            break

        cursor = data.get("cursor")
        if not cursor:
            break

    return sorted(set(keys))


def unwrap_submission_response(data: Any) -> Any:
    # Supports both styles:
    #   raw accepted JSON:
    #     {"tool_version":"...", "type":"tool_keys", "items":[...]}
    #   wrapped admin JSON:
    #     {"ok":true, "submission": {...}}
    #     {"ok":true, "body": {...}}
    #     {"ok":true, "data": {...}}
    if isinstance(data, dict) and data.get("ok") is True:
        for key in ("submission", "body", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                return nested

    return data


def download_submission(key: str) -> Any:
    url = API_BASE.rstrip("/") + "/admin/submission?key=" + quote(key, safe="")
    return unwrap_submission_response(api_get_json(url))


def main() -> None:
    if not ADMIN_TOKEN:
        raise SystemExit("Missing STEAMIDRA_ADMIN_TOKEN GitHub secret.")

    if not PROVIDER_FILE.exists():
        raise SystemExit("Missing fallback_depotkeys.json in repository root.")

    provider = load_json(PROVIDER_FILE, {})
    if not isinstance(provider, dict):
        raise SystemExit("fallback_depotkeys.json root is not an object.")

    processed = load_json(PROCESSED_LOG, [])
    if not isinstance(processed, list):
        processed = []

    processed_set = set(str(x) for x in processed)
    object_keys = list_accepted()

    report = {
        "started_at": int(time.time()),
        "objects_found": len(object_keys),
        "objects_processed": 0,
        "objects_skipped_already_processed": 0,
        "bad_submissions": [],
        "items_seen": 0,
        "new_entries": 0,
        "keys_filled": 0,
        "same_key_existing": 0,
        "metadata_filled": 0,
        "invalid_existing_entries_skipped": 0,
        "conflicts": [],
    }

    newly_processed: list[str] = []

    for key in object_keys:
        if key in processed_set:
            report["objects_skipped_already_processed"] += 1
            continue

        try:
            body = download_submission(key)
        except Exception as e:
            report["bad_submissions"].append({"object": key, "error": f"download error: {e}"})
            continue

        ok, err = validate_submission(body)
        if not ok:
            report["bad_submissions"].append({"object": key, "error": err})
            continue

        report["objects_processed"] += 1
        newly_processed.append(key)

        for item in body["items"]:
            report["items_seen"] += 1
            merge_item(provider, item, report)

    provider = sorted_provider(provider, report)

    print("Merge summary")
    for key in (
        "objects_found",
        "objects_processed",
        "objects_skipped_already_processed",
        "items_seen",
        "new_entries",
        "keys_filled",
        "same_key_existing",
        "metadata_filled",
        "invalid_existing_entries_skipped",
    ):
        print(f"{key}: {report[key]}")

    print(f"conflicts: {len(report['conflicts'])}")
    print(f"bad_submissions: {len(report['bad_submissions'])}")

    if report["conflicts"]:
        print("Conflicts were NOT overwritten. First 20:")
        for conflict in report["conflicts"][:20]:
            print(
                f"  {conflict['id']}: "
                f"existing={conflict['existing']} "
                f"new={conflict['new']} "
                f"name={conflict.get('name', '')}"
            )

    save_json(REPORT_FILE, report)

    if newly_processed:
        processed.extend(newly_processed)
        save_json(PROCESSED_LOG, sorted(set(processed)))

    save_json(PROVIDER_FILE, provider)

    print("Done.")


if __name__ == "__main__":
    main()

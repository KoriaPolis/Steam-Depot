from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_BASE = os.environ.get("STEAMIDRA_API_BASE", "https://stea-provider-api.steamidra.workers.dev")
ADMIN_TOKEN = os.environ.get("STEAMIDRA_ADMIN_TOKEN", "")
PROVIDER_FILE = Path("fallback_depotkeys.json")
PROCESSED_LOG = Path("merged_submission_ids.json")
REPORT_FILE = Path("submission_merge_report.json")

ID_RE = re.compile(r"^[0-9]{1,12}$")
KEY_RE = re.compile(r"^[a-fA-F0-9]{64}$")
KIND_VALUES = {"game", "software", "dlc", "depot", "dlc_depot", "unknown"}
ITEM_FIELDS = {"id", "key", "name", "kind", "parent_appid", "parent_name"}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return default
    return json.loads(text)


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def as_id(value: Any) -> str:
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str):
        value = value.strip()
        if ID_RE.fullmatch(value):
            return str(int(value))
    return ""


def clean_text(value: Any, max_len: int = 240) -> str:
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
    if value in {"tool", "tools", "application"}:
        return "software"
    if value in {"dlc depot", "dlc-depot"}:
        return "dlc_depot"
    return "unknown"


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
    if "name" in item and (not isinstance(name, str) or len(name) > 200 or "\r" in name or "\n" in name):
        return False, f"{item_id}: bad name"

    if "kind" in item and clean_kind(item.get("kind")) not in KIND_VALUES:
        return False, f"{item_id}: bad kind"

    parent_appid = item.get("parent_appid", "")
    if "parent_appid" in item and str(parent_appid).strip() and not as_id(parent_appid):
        return False, f"{item_id}: bad parent_appid"

    parent_name = item.get("parent_name", "")
    if "parent_name" in item and (not isinstance(parent_name, str) or len(parent_name) > 200 or "\r" in parent_name or "\n" in parent_name):
        return False, f"{item_id}: bad parent_name"

    return True, ""


def validate_submission(body: Any) -> tuple[bool, str]:
    if not isinstance(body, dict):
        return False, "body is not object"
    if body.get("type") != "tool_keys":
        return False, "type is not tool_keys"
    if not isinstance(body.get("tool_version"), str) or not (1 <= len(body["tool_version"]) <= 32):
        return False, "bad tool_version"
    if not isinstance(body.get("items"), list) or not (1 <= len(body["items"]) <= 1000):
        return False, "bad items"
    for item in body["items"]:
        ok, err = validate_item(item)
        if not ok:
            return False, err
    return True, ""


def normalize_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        entry = {}
    out = {
        "key": normalize_key(entry.get("key", "")),
        "name": clean_text(entry.get("name", "")),
        "kind": clean_kind(entry.get("kind", "unknown")),
    }
    pa = as_id(entry.get("parent_appid", ""))
    pn = clean_text(entry.get("parent_name", ""))
    if pa:
        out["parent_appid"] = pa
    if pn:
        out["parent_name"] = pn
    return out


def merge_item(provider: dict[str, dict[str, Any]], item: dict[str, Any], report: dict[str, Any]) -> None:
    item_id = as_id(item.get("id"))
    incoming_key = normalize_key(item.get("key"))
    incoming_name = clean_text(item.get("name", ""))
    incoming_kind = clean_kind(item.get("kind", "unknown"))
    incoming_parent_appid = as_id(item.get("parent_appid", ""))
    incoming_parent_name = clean_text(item.get("parent_name", ""))

    if item_id not in provider or not isinstance(provider.get(item_id), dict):
        entry = {"key": incoming_key, "name": incoming_name, "kind": incoming_kind}
        if incoming_parent_appid:
            entry["parent_appid"] = incoming_parent_appid
        if incoming_parent_name:
            entry["parent_name"] = incoming_parent_name
        provider[item_id] = entry
        report["new_entries"] += 1
        return

    entry = normalize_entry(provider[item_id])
    old_key = normalize_key(entry.get("key", ""))

    if not old_key:
        entry["key"] = incoming_key
        report["keys_filled"] += 1
    elif old_key == incoming_key:
        report["same_key_existing"] += 1
    else:
        report["conflicts"].append({"id": item_id, "existing": old_key, "new": incoming_key, "name": incoming_name})
        provider[item_id] = entry
        return

    if incoming_name and not entry.get("name"):
        entry["name"] = incoming_name
        report["metadata_filled"] += 1
    if incoming_kind != "unknown" and entry.get("kind", "unknown") == "unknown":
        entry["kind"] = incoming_kind
        report["metadata_filled"] += 1
    if incoming_parent_appid and not entry.get("parent_appid"):
        entry["parent_appid"] = incoming_parent_appid
        report["metadata_filled"] += 1
    if incoming_parent_name and not entry.get("parent_name"):
        entry["parent_name"] = incoming_parent_name
        report["metadata_filled"] += 1

    provider[item_id] = entry


def sorted_provider(provider: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for item_id in sorted(provider.keys(), key=lambda x: int(x) if str(x).isdigit() else 10**18):
        sid = as_id(item_id)
        if sid:
            out[sid] = normalize_entry(provider[item_id])
    return out


def api_get_json(url: str) -> Any:
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "SteaMidra-GitHubAction-Merge/1.0",
        "x-admin-token": ADMIN_TOKEN,
    })
    with urlopen(req, timeout=120) as res:
        return json.loads(res.read().decode("utf-8-sig"))


def list_accepted() -> list[str]:
    keys = []
    cursor = None

    while True:
        qs = {"limit": "1000"}
        if cursor:
            qs["cursor"] = cursor

        data = api_get_json(API_BASE.rstrip("/") + "/admin/accepted?" + urlencode(qs))

        if not data.get("ok"):
            raise RuntimeError(f"admin list failed: {data}")

        for obj in data.get("objects", []):
            key = obj.get("key")
            if isinstance(key, str) and key.startswith("accepted/") and key.endswith(".json"):
                keys.append(key)

        if not data.get("truncated"):
            break

        cursor = data.get("cursor")
        if not cursor:
            break

    return sorted(set(keys))


def download_submission(key: str) -> Any:
    url = API_BASE.rstrip("/") + "/admin/submission?key=" + quote(key, safe="")
    return api_get_json(url)


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
        "conflicts": [],
    }

    newly_processed = []

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

    provider = sorted_provider(provider)

    print("Merge summary")
    for k in ("objects_found", "objects_processed", "objects_skipped_already_processed", "items_seen", "new_entries", "keys_filled", "same_key_existing", "metadata_filled"):
        print(f"{k}: {report[k]}")
    print(f"conflicts: {len(report['conflicts'])}")
    print(f"bad_submissions: {len(report['bad_submissions'])}")

    if report["conflicts"]:
        print("Conflicts were NOT overwritten. First 20:")
        for c in report["conflicts"][:20]:
            print(f"  {c['id']}: existing={c['existing']} new={c['new']} name={c.get('name','')}")

    save_json(REPORT_FILE, report)

    if newly_processed:
        processed.extend(newly_processed)
        save_json(PROCESSED_LOG, sorted(set(processed)))

    save_json(PROVIDER_FILE, provider)

    print("Done.")


if __name__ == "__main__":
    main()

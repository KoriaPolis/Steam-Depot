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

MAX_ITEMS_PER_ACCEPTED_FILE = 200_000
MAX_TEXT_LEN = 240
MAX_ITEM_ID = int((os.environ.get("STEAMIDRA_MAX_ITEM_ID") or "50000000").strip() or "0")

# accepted/ must ONLY contain verifier bulk output:
#   accepted/gha-YYYYMMDD-HHMMSS-RUNID.json
# Old accepted/<uuid>.json files are intentionally ignored forever.
ACCEPTED_KEY_RE = re.compile(r"^accepted/gha-[0-9]{8}-[0-9]{6}-[0-9]+\.json$")

DEFAULT_ACCEPTED_MIN_RUN_ID = "gha-20260630-180233-28465451350"
ACCEPTED_MIN_RUN_ID = (os.environ.get("STEAMIDRA_ACCEPTED_MIN_RUN_ID") or DEFAULT_ACCEPTED_MIN_RUN_ID).strip()

# Optional exact-run debug mode. NORMAL MODE MUST LEAVE THIS EMPTY.
ACCEPTED_ONLY_RUN_ID = (os.environ.get("STEAMIDRA_ACCEPTED_ONLY_RUN_ID") or "").strip()

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
    "depot_dlc": "dlc_depot",
}

# Only these are root rows without parent fields. DLC can have a parent app.
ROOT_KINDS = {"game", "software"}
ITEM_FIELDS = {"id", "key", "name", "kind", "parent_appid", "parent_name"}

ACCEPTED_LIST_STATS = {
    "objects_seen_total": 0,
    "objects_selected_gha": 0,
    "objects_ignored_not_gha": 0,
    "objects_ignored_before_min": 0,
    "objects_ignored_not_exact": 0,
}


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
        newline="\n",
    )


def as_id(value: Any) -> str:
    if isinstance(value, int) and value >= 0:
        return str(value)

    if isinstance(value, str):
        value = value.strip()
        if ID_RE.fullmatch(value):
            return str(int(value))

    return ""


def normalize_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    value = value.strip().lower()
    return value if KEY_RE.fullmatch(value) and value.strip("0") != "" else ""


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


def normalize_run_key(value: str) -> str:
    value = value.strip()

    if not value:
        return ""

    if not value.startswith("accepted/"):
        value = "accepted/" + value

    if not value.endswith(".json"):
        value += ".json"

    return value


def should_merge_accepted_key(key: str) -> bool:
    ACCEPTED_LIST_STATS["objects_seen_total"] += 1

    if not ACCEPTED_KEY_RE.fullmatch(key):
        ACCEPTED_LIST_STATS["objects_ignored_not_gha"] += 1
        return False

    only_key = normalize_run_key(ACCEPTED_ONLY_RUN_ID)
    if only_key and key != only_key:
        ACCEPTED_LIST_STATS["objects_ignored_not_exact"] += 1
        return False

    min_key = normalize_run_key(ACCEPTED_MIN_RUN_ID)
    if min_key and key < min_key:
        ACCEPTED_LIST_STATS["objects_ignored_before_min"] += 1
        return False

    ACCEPTED_LIST_STATS["objects_selected_gha"] += 1
    return True


def validate_submission_shape(body: Any) -> tuple[bool, str]:
    if not isinstance(body, dict):
        return False, "body is not object"

    if body.get("type") != "tool_keys":
        return False, "type is not tool_keys"

    if not isinstance(body.get("tool_version"), str) or not (1 <= len(body["tool_version"]) <= 64):
        return False, "bad tool_version"

    if not isinstance(body.get("items"), list) or not (0 <= len(body["items"]) <= MAX_ITEMS_PER_ACCEPTED_FILE):
        return False, "bad items"

    return True, ""


def validate_item_basic(item: Any) -> tuple[bool, str, str, str]:
    if not isinstance(item, dict):
        return False, "item is not object", "", ""

    extra = set(item) - ITEM_FIELDS
    if extra:
        return False, f"invalid fields: {sorted(extra)}", "", ""

    item_id = as_id(item.get("id"))
    if not item_id:
        return False, "bad id", "", ""

    if MAX_ITEM_ID > 0 and int(item_id) > MAX_ITEM_ID:
        return False, f"{item_id}: id above max allowed {MAX_ITEM_ID}", item_id, ""

    item_key = normalize_key(item.get("key"))
    if not item_key:
        return False, f"{item_id}: bad key", item_id, ""

    name = item.get("name", "")
    if "name" in item and (
        not isinstance(name, str)
        or len(name) > MAX_TEXT_LEN
        or "\r" in name
        or "\n" in name
    ):
        return False, f"{item_id}: bad name", item_id, item_key

    raw_kind = item.get("kind", "unknown")
    if "kind" in item and clean_kind(raw_kind) == "unknown" and clean_text(raw_kind).lower() not in {"", "unknown"}:
        return False, f"{item_id}: bad kind", item_id, item_key

    parent_appid = item.get("parent_appid", "")
    if "parent_appid" in item and str(parent_appid).strip() and not as_id(parent_appid):
        return False, f"{item_id}: bad parent_appid", item_id, item_key

    parent_name = item.get("parent_name", "")
    if "parent_name" in item and (
        not isinstance(parent_name, str)
        or len(parent_name) > MAX_TEXT_LEN
        or "\r" in parent_name
        or "\n" in parent_name
    ):
        return False, f"{item_id}: bad parent_name", item_id, item_key

    return True, "", item_id, item_key


def make_provider_entry(item: dict[str, Any]) -> dict[str, Any]:
    kind = clean_kind(item.get("kind", "unknown"))

    entry: dict[str, Any] = {
        "key": normalize_key(item.get("key")),
        "name": clean_text(item.get("name", "")),
        "kind": kind,
    }

    if kind not in ROOT_KINDS:
        parent_appid = as_id(item.get("parent_appid", ""))
        parent_name = clean_text(item.get("parent_name", ""))

        if parent_appid:
            entry["parent_appid"] = parent_appid

        if parent_name:
            entry["parent_name"] = parent_name

    return entry


def existing_key(value: Any) -> str:
    if isinstance(value, str):
        return normalize_key(value)

    if isinstance(value, dict):
        return normalize_key(value.get("key"))

    return ""


def is_empty_placeholder(value: Any) -> bool:
    if value is None or value == "" or value == {} or value == []:
        return True

    return False


def fill_existing_object(existing: dict[str, Any], item: dict[str, Any]) -> bool:
    changed = False

    new_key = normalize_key(item.get("key"))
    if new_key and not normalize_key(existing.get("key")):
        existing["key"] = new_key
        changed = True

    # Fill blank metadata only. Do not overwrite existing non-empty provider metadata.
    if not clean_text(existing.get("name", "")):
        name = clean_text(item.get("name", ""))
        if name:
            existing["name"] = name
            changed = True

    existing_kind = clean_kind(existing.get("kind", "unknown"))
    if existing_kind == "unknown":
        kind = clean_kind(item.get("kind", "unknown"))
        if kind != "unknown":
            existing["kind"] = kind
            existing_kind = kind
            changed = True

    if existing_kind in ROOT_KINDS:
        # Root rows should not carry parent fields.
        if "parent_appid" in existing:
            existing.pop("parent_appid", None)
            changed = True
        if "parent_name" in existing:
            existing.pop("parent_name", None)
            changed = True
    else:
        parent_appid = as_id(item.get("parent_appid", ""))
        parent_name = clean_text(item.get("parent_name", ""))

        if parent_appid and not as_id(str(existing.get("parent_appid", ""))):
            existing["parent_appid"] = parent_appid
            changed = True

        if parent_name and not clean_text(existing.get("parent_name", "")):
            existing["parent_name"] = parent_name
            changed = True

    return changed


def merge_item(provider: dict[str, Any], item: Any, report: dict[str, Any]) -> bool:
    ok, err, item_id, item_key = validate_item_basic(item)
    if not ok:
        if "id above max allowed" in err:
            report["high_id_items_skipped"] += 1
        else:
            report["bad_items_skipped"] += 1
            report["bad_items"].append({"id": item_id, "error": err})
        return False

    assert isinstance(item, dict)

    if item_id not in provider:
        provider[item_id] = make_provider_entry(item)
        report["new_entries"] += 1
        return True

    raw_existing = provider.get(item_id)
    old_key = existing_key(raw_existing)

    if isinstance(raw_existing, dict):
        if old_key and old_key != item_key:
            report["conflicts"].append({
                "id": item_id,
                "existing": old_key,
                "new": item_key,
                "name": clean_text(item.get("name", "")),
                "kind": clean_kind(item.get("kind", "unknown")),
            })
            return False

        changed = fill_existing_object(raw_existing, item)
        if old_key == item_key:
            report["same_key_existing"] += 1
        if changed:
            report["keys_or_metadata_filled"] += 1
        else:
            report["existing_unchanged"] += 1
        return changed

    if isinstance(raw_existing, str):
        if old_key and old_key != item_key:
            report["conflicts"].append({
                "id": item_id,
                "existing": old_key,
                "new": item_key,
                "name": clean_text(item.get("name", "")),
                "kind": clean_kind(item.get("kind", "unknown")),
            })
            return False

        provider[item_id] = make_provider_entry(item)
        report["legacy_string_converted"] += 1
        return True

    if is_empty_placeholder(raw_existing):
        provider[item_id] = make_provider_entry(item)
        report["keys_or_metadata_filled"] += 1
        return True

    # Unknown non-empty shape. Preserve it.
    report["existing_unrecognized_preserved"] += 1
    return False


def api_get_json(url: str) -> Any:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "SteaMidra-GitHubAction-Merge/3.0",
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

        data = api_get_json(API_BASE + "/admin/accepted?" + urlencode(qs))

        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"admin accepted list failed: {data}")

        for obj in data.get("objects", []):
            key = obj.get("key") if isinstance(obj, dict) else None
            if isinstance(key, str) and should_merge_accepted_key(key):
                keys.append(key)

        if not data.get("truncated"):
            break

        cursor = data.get("cursor")
        if not cursor:
            break

    return sorted(set(keys))


def unwrap_submission_response(data: Any) -> Any:
    if isinstance(data, dict) and data.get("ok") is True:
        for key in ("submission", "body", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                return nested

    return data


def download_submission(key: str) -> Any:
    url = API_BASE + "/admin/submission?key=" + quote(key, safe="")
    return unwrap_submission_response(api_get_json(url))


def main() -> None:
    if not ADMIN_TOKEN:
        raise SystemExit("Missing STEAMIDRA_ADMIN_TOKEN GitHub secret.")

    if not PROVIDER_FILE.exists():
        raise SystemExit("Missing fallback_depotkeys.json in repository root.")

    provider = load_json(PROVIDER_FILE, {})
    if not isinstance(provider, dict):
        raise SystemExit("fallback_depotkeys.json root is not an object.")

    provider_count_before = len(provider)

    processed = load_json(PROCESSED_LOG, [])
    if not isinstance(processed, list):
        processed = []

    processed_set = set(str(x) for x in processed)
    object_keys = list_accepted()

    report = {
        "started_at": int(time.time()),
        "api_base": API_BASE,
        "accepted_min_run_id": ACCEPTED_MIN_RUN_ID,
        "accepted_only_run_id": ACCEPTED_ONLY_RUN_ID,
        "max_item_id": MAX_ITEM_ID,
        "objects_seen_total": ACCEPTED_LIST_STATS["objects_seen_total"],
        "objects_selected_gha": ACCEPTED_LIST_STATS["objects_selected_gha"],
        "objects_ignored_not_gha": ACCEPTED_LIST_STATS["objects_ignored_not_gha"],
        "objects_ignored_before_min": ACCEPTED_LIST_STATS["objects_ignored_before_min"],
        "objects_ignored_not_exact": ACCEPTED_LIST_STATS["objects_ignored_not_exact"],
        "objects_found": len(object_keys),
        "objects_processed": 0,
        "objects_skipped_already_processed": 0,
        "bad_submissions": [],
        "items_seen": 0,
        "new_entries": 0,
        "keys_or_metadata_filled": 0,
        "legacy_string_converted": 0,
        "same_key_existing": 0,
        "existing_unchanged": 0,
        "bad_items_skipped": 0,
        "high_id_items_skipped": 0,
        "existing_unrecognized_preserved": 0,
        "conflicts": [],
        "bad_items": [],
        "provider_count_before": provider_count_before,
        "provider_count_after": provider_count_before,
        "provider_changed": False,
    }

    newly_processed: list[str] = []
    provider_changed = False

    for key in object_keys:
        if key in processed_set:
            report["objects_skipped_already_processed"] += 1
            continue

        try:
            body = download_submission(key)
        except Exception as e:
            report["bad_submissions"].append({"object": key, "error": f"download error: {e}"})
            continue

        ok, err = validate_submission_shape(body)
        if not ok:
            report["bad_submissions"].append({"object": key, "error": err})
            continue

        report["objects_processed"] += 1

        for item in body["items"]:
            report["items_seen"] += 1
            changed = merge_item(provider, item, report)
            provider_changed = provider_changed or changed

        # Mark the GHA file processed even if some individual fake/bad items were skipped.
        newly_processed.append(key)

    report["provider_count_after"] = len(provider)
    report["provider_changed"] = provider_changed

    if report["provider_count_after"] < report["provider_count_before"]:
        raise SystemExit(
            f"Safety stop: provider count decreased from "
            f"{report['provider_count_before']} to {report['provider_count_after']}."
        )

    print("Merge summary")
    for key in (
        "api_base",
        "accepted_min_run_id",
        "accepted_only_run_id",
        "max_item_id",
        "objects_seen_total",
        "objects_selected_gha",
        "objects_ignored_not_gha",
        "objects_ignored_before_min",
        "objects_ignored_not_exact",
        "objects_found",
        "objects_processed",
        "objects_skipped_already_processed",
        "items_seen",
        "new_entries",
        "keys_or_metadata_filled",
        "legacy_string_converted",
        "same_key_existing",
        "existing_unchanged",
        "bad_items_skipped",
        "high_id_items_skipped",
        "existing_unrecognized_preserved",
        "provider_count_before",
        "provider_count_after",
        "provider_changed",
    ):
        print(f"{key}: {report[key]}")

    print(f"conflicts: {len(report['conflicts'])}")
    print(f"bad_submissions: {len(report['bad_submissions'])}")
    print(f"bad_items: {len(report['bad_items'])}")

    if report["conflicts"]:
        print("Conflicts were NOT overwritten. First 20:")
        for conflict in report["conflicts"][:20]:
            print(
                f"  {conflict['id']}: "
                f"existing={conflict['existing']} "
                f"new={conflict['new']} "
                f"name={conflict.get('name', '')}"
            )

    if report["bad_submissions"]:
        print("Bad submissions were skipped. First 20:")
        for bad in report["bad_submissions"][:20]:
            print(f"  {bad.get('object')}: {bad.get('error')}")

    if report["bad_items"]:
        print("Bad items were skipped. First 20:")
        for bad in report["bad_items"][:20]:
            print(f"  {bad.get('id')}: {bad.get('error')}")

    if provider_changed:
        save_json(PROVIDER_FILE, provider)

    if newly_processed:
        processed.extend(newly_processed)
        save_json(PROCESSED_LOG, sorted(set(processed)))

    if provider_changed or newly_processed:
        save_json(REPORT_FILE, report)

    print("Done.")


if __name__ == "__main__":
    main()

"""Load locations, location groups, and Deliveroo Retail channel links, then publish inventory."""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://api.deliverect.io"
OPERATION_REPORTS_URL = "https://api.deliverect.com/operationReports"
DELIVEROO_RETAIL_CHANNEL = 6002
LISTING_UPDATE_OPERATION_TYPE = 1101
PAGE_SIZE = 500
SYNC_BATCH_SIZE = 25
RUNNING_OPERATION_STATUSES = {20, 24, 25, 26, 27, 28, 29}
RECENT_LISTING_HOURS = 3
LISTING_POLL_HOURS = 2
ERROR_LOG_LEVEL = 40
OPERATION_STATUS_LABELS = {
    30: "Await",
    90: "Success",
    110: "Aborted",
    120: "Failed",
}

CHANNEL_LINK_STATUS_LABELS = {
    0: "Inactive",
    1: "Suspended",
    2: "Testing",
    3: "Subscribed",
    4: "Onboarding",
}


def expected_brand_id() -> str:
    """Optional exact brand from DELIVEROO_BRAND_ID. Empty means any non-empty brand."""
    return (os.getenv("DELIVEROO_BRAND_ID") or "").strip()


def get_configured_account_id() -> str:
    return (os.getenv("ACCOUNT_ID") or "").strip()


def _ids(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        item_id = value.get("_id") or value.get("id")
        return [item_id] if item_id else []
    ids = []
    for item in value:
        if isinstance(item, str):
            if item:
                ids.append(item)
        else:
            ids.extend(_ids(item))
    return ids


def channel_link_ids_from_location(location: dict) -> list[str]:
    raw = location.get("channelLinks")
    if raw:
        return _ids(raw)

    links = (((location.get("_links") or {}).get("related") or {}).get("channelLinks") or [])
    ids = []
    for item in links:
        if isinstance(item, str):
            ids.append(item.rstrip("/").split("/")[-1])
            continue
        if not isinstance(item, dict):
            continue
        href = (item.get("href") or "").strip()
        if href:
            ids.append(href.rstrip("/").split("/")[-1])
    return ids


def _assert_unique_ids(items: list, resource: str) -> None:
    seen = set()
    duplicates = []
    for item in items:
        item_id = item.get("_id")
        if not item_id or item_id in seen:
            if item_id:
                duplicates.append(item_id)
            continue
        seen.add(item_id)
    if duplicates:
        sample = ", ".join(duplicates[:5])
        raise ValueError(
            f"Paginated {resource} response returned duplicate _id(s): {sample}"
        )


def _response_detail(response: requests.Response) -> str:
    text = (response.text or "").strip()
    if len(text) > 400:
        text = text[:400] + "..."
    return f"HTTP {response.status_code} — {text}"


def list_resource(resource: str, account: str, extra_where: dict | None = None) -> list:
    from auth import getHeaders

    where = {"account": account}
    if extra_where:
        where.update(extra_where)

    items = []
    page = 1
    while True:
        response = requests.get(
            f"{API_BASE}/{resource}",
            headers=getHeaders(),
            params={
                "where": json.dumps(where),
                "page": page,
                "max_results": PAGE_SIZE,
                "sort": "_id",
            },
            timeout=90,
        )
        if not response.ok:
            raise RuntimeError(f"GET {resource} page {page}: {_response_detail(response)}")

        batch = response.json().get("_items") or []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1

    _assert_unique_ids(items, resource)
    return items


def groups_from_location_tags(locations: list) -> list[dict]:
    """Location tags are the groups on this account (there is no locationGroups resource)."""
    members: dict[str, list[str]] = {}
    for location in locations:
        loc_id = location.get("_id")
        if not loc_id:
            continue
        for tag in location.get("tags") or []:
            name = str(tag).strip()
            if not name:
                continue
            bucket = members.setdefault(name, [])
            if loc_id not in bucket:
                bucket.append(loc_id)
    return [
        {"_id": name, "name": name, "locations": loc_ids}
        for name, loc_ids in sorted(members.items(), key=lambda item: item[0].lower())
    ]


def fetch_location_groups(account: str, locations: list) -> tuple[list, str]:
    """Prefer the locationGroups API. This account has none, so groups come from location tags."""
    try:
        groups = list_resource("locationGroups", account)
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        groups = []
    if groups:
        return groups, "locationGroups"
    return groups_from_location_tags(locations), "location tags"


def location_group_index(groups: list, locations: list) -> dict[str, list[dict]]:
    """Map location id -> [{id, name}] from group.locations and location.locationGroup(s)."""
    by_id = {group["_id"]: group for group in groups if group.get("_id")}
    loc_groups: dict[str, list[dict]] = {}

    def add(loc_id: str, group_id: str) -> None:
        if not loc_id or not group_id:
            return
        group = by_id.get(group_id) or {}
        entry = {"id": group_id, "name": group.get("name") or group_id}
        bucket = loc_groups.setdefault(loc_id, [])
        if not any(item["id"] == group_id for item in bucket):
            bucket.append(entry)

    for group in groups:
        group_id = group.get("_id")
        for loc_id in _ids(group.get("locations")):
            add(loc_id, group_id)

    for location in locations:
        loc_id = location.get("_id")
        group_ids = _ids(location.get("locationGroup")) + _ids(location.get("locationGroups"))
        for group_id in group_ids:
            add(loc_id, group_id)

    return loc_groups


def check_deliveroo_link(
    location: dict | None,
    link: dict,
    groups: list,
    expected_brand: str,
) -> list[str]:
    """Return blocking issues. An empty list means the link is ready to sync."""
    issues = []
    if link.get("channel") != DELIVEROO_RETAIL_CHANNEL:
        issues.append("Not Deliveroo Retail (6002)")

    if location is None:
        issues.append("Location not found")
    else:
        status = str(location.get("status") or "").upper()
        if status != "SUBSCRIBED":
            issues.append(f"Location is {status or 'missing status'}")

    if not groups:
        issues.append("No location group")

    link_status = link.get("status")
    if link_status != 3:
        label = CHANNEL_LINK_STATUS_LABELS.get(link_status, link_status)
        issues.append(f"Channel link is {label}")

    settings = link.get("channelSettings") or {}
    brand = (settings.get("brandId") or "").strip()
    if not brand:
        issues.append("Missing brand ID")
    elif expected_brand and brand != expected_brand:
        issues.append("Brand ID does not match expected Deliveroo brand")

    if settings.get("isMenuPushRestricted"):
        issues.append("Menu push is restricted")

    return issues


def _yn(value) -> str:
    return "Yes" if value else "No"


def build_snapshot(
    locations: list,
    groups: list,
    channel_links: list,
    expected_brand: str,
    group_source: str = "location tags",
) -> dict:
    loc_by_id = {loc["_id"]: loc for loc in locations if loc.get("_id")}
    groups_for_loc = location_group_index(groups, locations)
    links_by_id = {
        link["_id"]: link
        for link in channel_links
        if link.get("_id") and link.get("channel") == DELIVEROO_RETAIL_CHANNEL
    }
    links_by_location: dict[str, list] = {}
    for link in links_by_id.values():
        loc_id = link.get("location")
        if loc_id:
            links_by_location.setdefault(loc_id, []).append(link)

    used_link_ids = set()
    rows = []

    def add_row(location: dict | None, link: dict) -> None:
        link_id = link.get("_id")
        if not link_id or link_id in used_link_ids:
            return
        used_link_ids.add(link_id)

        loc_id = (location or {}).get("_id") or link.get("location") or ""
        if location is None and loc_id:
            location = loc_by_id.get(loc_id)
        group_entries = groups_for_loc.get(loc_id, []) if loc_id else []
        issues = check_deliveroo_link(location, link, group_entries, expected_brand)
        settings = link.get("channelSettings") or {}
        group_names = [entry["name"] for entry in group_entries]
        group_name = ", ".join(group_names) or "No location group"
        rows.append(
            {
                "group_name": group_name,
                "group_names": group_names,
                "group_ids": [entry["id"] for entry in group_entries],
                "location_name": (location or {}).get("name") or loc_id or "Unknown location",
                "location_id": loc_id,
                "location_status": (location or {}).get("status") or "—",
                "link_name": link.get("name") or link_id,
                "link_id": link_id,
                "store_id": (settings.get("storeId") or "").strip() or "—",
                "link_status": CHANNEL_LINK_STATUS_LABELS.get(
                    link.get("status"), str(link.get("status"))
                ),
                "brand_id": (settings.get("brandId") or "").strip() or "—",
                "catalog_push": _yn(settings.get("catalogPushEnabled")),
                "menu_push_restricted": _yn(settings.get("isMenuPushRestricted")),
                "checks": "Ready" if not issues else "; ".join(issues),
                "ready": not issues,
            }
        )

    for location in locations:
        loc_id = location.get("_id")
        attached = set()
        for link_id in channel_link_ids_from_location(location):
            link = links_by_id.get(link_id)
            if not link:
                continue
            add_row(location, link)
            attached.add(link_id)
        for link in links_by_location.get(loc_id, []):
            if link.get("_id") not in attached:
                add_row(location, link)

    for link in links_by_id.values():
        if link.get("_id") not in used_link_ids:
            add_row(loc_by_id.get(link.get("location")), link)

    locations_with_link = {row["location_id"] for row in rows if row["location_id"]}
    locations_without_deliveroo = [
        {
            "name": loc.get("name") or loc.get("_id"),
            "id": loc.get("_id"),
            "status": loc.get("status") or "—",
            "group_name": ", ".join(
                entry["name"] for entry in groups_for_loc.get(loc.get("_id"), [])
            )
            or "No location group",
        }
        for loc in locations
        if loc.get("_id") not in locations_with_link
    ]

    group_ids_with_links = set()
    for row in rows:
        group_ids_with_links.update(row["group_ids"])
    groups_without_deliveroo = [
        {"name": group.get("name") or group.get("_id"), "id": group.get("_id")}
        for group in groups
        if group.get("_id") and group.get("_id") not in group_ids_with_links
    ]

    rows.sort(
        key=lambda row: (
            row["group_name"].lower(),
            row["location_name"].lower(),
            row["link_name"].lower(),
        )
    )
    locations_without_deliveroo.sort(key=lambda row: (row["group_name"].lower(), row["name"].lower()))
    groups_without_deliveroo.sort(key=lambda row: row["name"].lower())

    return {
        "locations": locations,
        "groups": groups,
        "group_source": group_source,
        "expected_brand": expected_brand,
        "channel_links": list(links_by_id.values()),
        "rows": rows,
        "locations_without_deliveroo": locations_without_deliveroo,
        "groups_without_deliveroo": groups_without_deliveroo,
    }


def fetch_account(account: str, progress=None) -> dict:
    def report(message: str) -> None:
        if progress:
            progress(message)

    report("Fetching locations")
    locations = list_resource("locations", account)
    report(f"Locations: {len(locations)}")

    report("Fetching location groups")
    groups, group_source = fetch_location_groups(account, locations)
    report(f"Location groups: {len(groups)} ({group_source})")

    report("Fetching Deliveroo Retail (6002) channel links")
    links = list_resource(
        "channelLinks",
        account,
        extra_where={"channel": DELIVEROO_RETAIL_CHANNEL},
    )
    report(f"Deliveroo Retail channel links: {len(links)}")

    snapshot = build_snapshot(locations, groups, links, expected_brand_id(), group_source)
    return snapshot


def _response_text(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def connection_status(health: dict) -> tuple[str, str]:
    """Whether the channel link health check says the connection is OK."""
    if not isinstance(health, dict):
        return "Not OK", "No health check response"
    if health.get("requestError") or health.get("error"):
        return "Not OK", str(health.get("error") or "Request failed")
    if health.get("connectionOK") is True:
        return "OK", ""
    detail = health.get("connectionResponse") or "Connection is not OK"
    return "Not OK", _response_text(detail)


def credentials_response(health: dict) -> str:
    """Partner message from the health check, such as an invalid site location ID."""
    if not isinstance(health, dict):
        return ""
    return _response_text(health.get("credentialsResponse"))


def check_channel_link_health(channel_link_id: str) -> dict:
    from auth import getHeaders

    url = f"{API_BASE}/channelLinks/{channel_link_id}/health"
    try:
        response = requests.get(url, headers=getHeaders(), timeout=30)
    except requests.RequestException as exc:
        return {"error": str(exc), "requestError": True}
    text = (response.text or "").strip()
    if not text:
        return {"error": f"Empty response (HTTP {response.status_code})", "requestError": True}
    try:
        data = response.json()
    except ValueError:
        return {"error": f"Invalid JSON (HTTP {response.status_code})", "requestError": True}
    if not isinstance(data, dict):
        return {"error": f"Unexpected response (HTTP {response.status_code})", "requestError": True}
    if not response.ok and "error" not in data:
        data["error"] = f"HTTP {response.status_code}"
        data["requestError"] = True
    return data


def check_connections(links: list[dict], workers: int = 8) -> list[dict]:
    """Run a health check for each channel link and report connectionOK."""
    if not links:
        return []

    def one(link: dict) -> dict:
        health = check_channel_link_health(link["id"])
        status, detail = connection_status(health)
        return {
            "Location": link.get("location_name") or "—",
            "Channel link ID": link["id"],
            "Store ID": link.get("store_id") or "—",
            "Brand ID": link.get("brand_id") or "—",
            "Connection": status,
            "Credentials response": credentials_response(health),
            "Detail": detail,
        }

    results = []
    with ThreadPoolExecutor(max_workers=min(workers, len(links))) as pool:
        futures = [pool.submit(one, link) for link in links]
        for future in as_completed(futures):
            results.append(future.result())
    order = {link["id"]: index for index, link in enumerate(links)}
    results.sort(key=lambda row: order.get(row["Channel link ID"], 0))
    return results


def publish_inventory(account: str, channel_link_ids: list[str], batch_size: int = SYNC_BATCH_SIZE) -> list[dict]:
    from auth import getHeaders

    ids = list(dict.fromkeys(link_id for link_id in channel_link_ids if link_id))
    if not ids:
        return []

    url = f"{API_BASE}/catalog/accounts/{account}/inventory/publish"
    results = []
    total_batches = (len(ids) + batch_size - 1) // batch_size
    for index in range(0, len(ids), batch_size):
        batch = ids[index : index + batch_size]
        batch_number = index // batch_size + 1
        response = requests.post(
            url,
            headers={**getHeaders(), "Content-Type": "application/json"},
            json={"channelLinkIds": batch, "bypassSafetyRules": True},
            timeout=120,
        )
        try:
            body = response.json()
        except ValueError:
            body = (response.text or "").strip()
        results.append(
            {
                "batch": batch_number,
                "batches": total_batches,
                "count": len(batch),
                "channel_link_ids": batch,
                "ok": response.ok,
                "status": response.status_code,
                "body": body,
            }
        )
        if not response.ok:
            break
    return results


def sync_started_at() -> str:
    """UTC timestamp slightly before now, so a report created during the call is included."""
    started = datetime.now(timezone.utc) - timedelta(seconds=15)
    return started.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def recent_listing_window(hours: int = RECENT_LISTING_HOURS) -> str:
    """UTC timestamp for listing updates from the last few hours."""
    started = datetime.now(timezone.utc) - timedelta(hours=hours)
    return started.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def report_error_message(report: dict) -> str:
    """Error lines from the operation report log (level 40 and above)."""
    messages = []
    for entry in report.get("log") or []:
        if not isinstance(entry, dict):
            continue
        try:
            level = int(entry.get("level"))
        except (TypeError, ValueError):
            continue
        if level < ERROR_LOG_LEVEL:
            continue
        message = (entry.get("message") or "").strip()
        if message and message not in messages:
            messages.append(message)
    return " | ".join(messages)


def operation_status_label(status) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return "Waiting" if status in (None, "") else str(status)
    if code in RUNNING_OPERATION_STATUSES:
        return "Running"
    return OPERATION_STATUS_LABELS.get(code, f"Unknown ({code})")


def fetch_documents_by_ids(resource: str, ids: list[str], account: str = "") -> list[dict]:
    """Load locations or channel links by id, in small batches."""
    from auth import getHeaders

    unique = list(dict.fromkeys(item_id for item_id in ids if item_id))
    if not unique:
        return []

    items = []
    for start in range(0, len(unique), 50):
        chunk = unique[start : start + 50]
        where: dict = {"_id": {"$in": chunk}}
        if account:
            where["account"] = account
        response = requests.get(
            f"{API_BASE}/{resource}",
            headers=getHeaders(),
            params={"where": json.dumps(where), "max_results": PAGE_SIZE},
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(f"GET {resource}: {_response_detail(response)}")
        items.extend(response.json().get("_items") or [])
    return items


def link_display_from_documents(links: list[dict], locations: list[dict]) -> dict[str, dict]:
    """Channel link id -> location name and Deliveroo admin id (channelSettings.storeId)."""
    names = {
        location["_id"]: location.get("name") or location["_id"]
        for location in locations
        if location.get("_id")
    }
    display = {}
    for link in links:
        link_id = link.get("_id")
        if not link_id:
            continue
        settings = link.get("channelSettings") or {}
        loc_id = link.get("location") or ""
        display[link_id] = {
            "location_name": names.get(loc_id) or "—",
            "admin_id": (settings.get("storeId") or "").strip() or "—",
        }
    return display


def channel_link_from_report(report: dict) -> tuple[str, str]:
    link = (report.get("properties") or {}).get("channelLink") or {}
    if isinstance(link, str):
        return link, ""
    if not isinstance(link, dict):
        return "", ""
    return link.get("_id") or "", link.get("name") or ""


def listing_update_rows(
    links: list[dict],
    reports: list[dict],
    context: dict[str, dict] | None = None,
) -> list[dict]:
    """One row per synced channel link. Latest type-1101 report wins."""
    wanted = {link["id"] for link in links}
    latest: dict[str, dict] = {}
    for report in reports:
        operation_type = report.get("operationType")
        if operation_type not in (None, LISTING_UPDATE_OPERATION_TYPE):
            continue
        link_id, link_name = channel_link_from_report(report)
        if link_id not in wanted:
            continue
        created = report.get("_created") or ""
        current = latest.get(link_id)
        if current is None or created >= (current.get("_created") or ""):
            latest[link_id] = {**report, "_link_name": link_name}

    rows = []
    for link in links:
        report = latest.get(link["id"])
        if not report:
            info = (context or {}).get(link["id"]) or {}
            rows.append(
                {
                    "Location": link.get("location_name") or info.get("location_name") or "—",
                    "Admin ID": link.get("admin_id") or info.get("admin_id") or "—",
                    "Channel link ID": link["id"],
                    "Status": "Waiting",
                    "Error": "",
                    "Report ID": "",
                    "Created": "",
                }
            )
            continue
        rows.append(_operation_report_row(report, context=context))
    return rows


def operation_report_rows(reports: list[dict], context: dict[str, dict] | None = None) -> list[dict]:
    """One row per listing-update report, newest first."""
    ordered = sorted(reports, key=lambda report: report.get("_created") or "", reverse=True)
    return [_operation_report_row(report, context=context) for report in ordered]


def combined_listing_rows(
    links: list[dict],
    reports: list[dict],
    context: dict[str, dict] | None = None,
) -> list[dict]:
    """Published links that have no report yet, then every report in the window."""
    seen_links = {channel_link_from_report(report)[0] for report in reports}
    waiting = [link for link in links if link["id"] not in seen_links]
    return listing_update_rows(waiting, [], context) + operation_report_rows(reports, context)


def _listing_place(link_id: str, context: dict[str, dict] | None) -> tuple[str, str]:
    info = (context or {}).get(link_id) or {}
    return info.get("location_name") or "—", info.get("admin_id") or "—"


def _operation_report_row(
    report: dict,
    context: dict[str, dict] | None = None,
) -> dict:
    link_id, _link_name = channel_link_from_report(report)
    location_name, admin_id = _listing_place(link_id, context)
    return {
        "Location": location_name,
        "Admin ID": admin_id,
        "Channel link ID": link_id,
        "Status": operation_status_label(report.get("operationStatus")),
        "Error": report_error_message(report),
        "Report ID": report.get("_id") or "",
        "Created": report.get("_created") or "",
    }


def fetch_listing_updates(
    account: str,
    created_after: str,
    channel_link_ids: list[str] | None = None,
    channel: int | None = None,
) -> list[dict]:
    """Type 1101 operation reports since ``created_after``.

    Pass channel link ids to keep only those links. Pass ``channel`` to keep
    one channel, such as Deliveroo Retail (6002). Omit both to return every
    listing update in the window.
    """
    from auth import getHeaders

    ids = None
    if channel_link_ids is not None:
        ids = list(dict.fromkeys(link_id for link_id in channel_link_ids if link_id))
        if not ids:
            return []

    # Nested properties.channelLink._id is not filtered by the operationReports API,
    # so match the channel link after fetching type 1101 reports from this sync onward.
    where = {
        "operationType": LISTING_UPDATE_OPERATION_TYPE,
        "account": {"$in": [account]},
        "_created": {"$gt": created_after},
    }
    if channel is not None:
        where["channel"] = channel
    wanted = set(ids or [])
    items = []
    page = 1
    while True:
        response = requests.get(
            OPERATION_REPORTS_URL,
            headers=getHeaders(),
            params={
                "where": json.dumps(where),
                "page": page,
                "max_results": PAGE_SIZE,
                "sort": "-_created",
            },
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(f"GET operationReports page {page}: {_response_detail(response)}")
        batch = response.json().get("_items") or []
        if not batch:
            break
        for report in batch:
            if channel is not None and report.get("channel") != channel:
                continue
            if ids is not None and channel_link_from_report(report)[0] not in wanted:
                continue
            items.append(report)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return items

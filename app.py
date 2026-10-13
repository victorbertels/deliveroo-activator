"""
Streamlit wizard: load the account, pick a location group, then publish a listing update.

Run from this directory:
  streamlit run app.py
"""

import os
import secrets
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from deliverect import (
    DELIVEROO_RETAIL_CHANNEL,
    LISTING_POLL_HOURS,
    RECENT_LISTING_HOURS,
    channel_link_from_report,
    check_connections,
    combined_listing_rows,
    fetch_account,
    fetch_documents_by_ids,
    fetch_listing_updates,
    link_display_from_documents,
    get_configured_account_id,
    operation_report_rows,
    publish_inventory,
    recent_listing_window,
    sync_started_at,
)

load_dotenv()

st.set_page_config(
    page_title="Deliveroo Activator",
    layout="wide",
    initial_sidebar_state="expanded",
)

_STREAMLIT_SECRET_KEYS = (
    "ACCOUNT_ID",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "APP_PASSWORD",
    "DELIVEROO_BRAND_ID",
)


def _hydrate_env_from_streamlit_secrets() -> None:
    candidates = (
        Path(__file__).resolve().parent / ".streamlit" / "secrets.toml",
        Path.cwd() / ".streamlit" / "secrets.toml",
    )
    if not any(path.is_file() for path in candidates):
        return
    try:
        streamlit_secrets = st.secrets
    except Exception:
        return
    for key in _STREAMLIT_SECRET_KEYS:
        if (os.getenv(key) or "").strip():
            continue
        try:
            value = streamlit_secrets[key]
        except Exception:
            continue
        if value is not None and str(value).strip():
            os.environ[key] = str(value).strip()


_hydrate_env_from_streamlit_secrets()

APP_PASSWORD = os.getenv("APP_PASSWORD", "")

DISPLAY_COLUMNS = [
    "Sync",
    "Location",
    "Channel link",
    "Link ID",
    "Store ID",
    "Brand ID",
]


def _require_password() -> None:
    if st.session_state.get("authenticated"):
        return

    st.title("Deliveroo Activator")
    st.caption("Enter the password to continue.")

    if not APP_PASSWORD:
        st.error("APP_PASSWORD is not configured. Set it in the environment or .env file.")
        st.stop()

    with st.form("login", clear_on_submit=False):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if secrets.compare_digest(password, APP_PASSWORD):
                st.session_state["authenticated"] = True
                st.rerun()
            st.error("Incorrect password.")

    st.stop()


def _rows_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=DISPLAY_COLUMNS)
    return pd.DataFrame(
        {
            "Sync": True,
            "Location": frame["location_name"],
            "Channel link": frame["link_name"],
            "Link ID": frame["link_id"],
            "Store ID": frame["store_id"] if "store_id" in frame.columns else "—",
            "Brand ID": frame["brand_id"],
        }
    )


_NAV_STYLE = """
<style>
.st-key-wizard_nav {
    position: sticky;
    bottom: 0;
    z-index: 999;
    background: var(--background-color, #ffffff);
    padding: 0.75rem 0 0.25rem 0;
    border-top: 1px solid rgba(128, 128, 128, 0.35);
}
</style>
"""


def _wizard_nav(
    *,
    back_to: int | None,
    forward_label: str | None = None,
    forward_disabled: bool = False,
    key: str,
) -> str | None:
    """Back and the forward button, after the step content."""
    st.markdown(_NAV_STYLE, unsafe_allow_html=True)
    with st.container(key="wizard_nav"):
        left, right = st.columns(2)
        went_back = False
        went_forward = False
        if back_to is not None:
            went_back = left.button("Back", key=f"{key}_back")
        if forward_label:
            went_forward = right.button(
                forward_label,
                type="primary",
                disabled=forward_disabled,
                key=f"{key}_forward",
            )
    if went_back:
        return "back"
    if went_forward:
        return "forward"
    return None


def _step_header(current: int) -> None:
    labels = ("1. Load account", "2. Rollout batch", "3. Health check", "4. Listing update")
    columns = st.columns(4)
    for column, label, number in zip(columns, labels, range(1, 5)):
        if number == current:
            column.markdown(f"**{label}**")
        else:
            column.write(label)


def _load_account(account_id: str) -> None:
    log: list[str] = []

    def progress(message: str) -> None:
        log.append(message)

    with st.status("Loading account", expanded=True) as status:
        snapshot = fetch_account(account_id, progress=progress)
        for line in log:
            status.write(line)
        ready = sum(1 for row in snapshot["rows"] if row["ready"])
        status.update(
            label=(
                f"Loaded {len(snapshot['locations'])} locations, "
                f"{len(snapshot['groups'])} groups, "
                f"{len(snapshot['rows'])} Deliveroo Retail links ({ready} ready)"
            ),
            state="complete",
        )
    st.session_state["snapshot"] = snapshot
    st.session_state.pop("chosen_links", None)
    st.session_state.pop("sync_results", None)
    st.session_state.pop("tracked_sync", None)


def _group_options(snapshot: dict) -> list[str]:
    names = sorted({group["name"] for group in snapshot["groups"] if group.get("name")})
    if any(not row["group_names"] for row in snapshot["rows"]):
        names.append("No location group")
    return names


def _rows_in_group(snapshot: dict, group: str) -> list[dict]:
    matched = []
    for row in snapshot["rows"]:
        names = row["group_names"] or ["No location group"]
        if group in names:
            matched.append(row)
    return matched


def _selected_link_ids(edited: pd.DataFrame) -> list[str]:
    if edited.empty or "Sync" not in edited.columns:
        return []
    return edited.loc[edited["Sync"] == True, "Link ID"].astype(str).tolist()


def _step_load(account_id: str) -> None:
    st.subheader("Load account")
    st.caption(f"Account `{account_id}`")
    if st.button("Load account", type="primary"):
        try:
            _load_account(account_id)
        except Exception as exc:
            st.error(str(exc))
            return
        st.session_state["wizard_step"] = 2
        st.rerun()

    st.subheader("Find operation reports")
    st.caption(
        f"Deliveroo Retail (channel {DELIVEROO_RETAIL_CHANNEL}) listing updates "
        f"from the last {RECENT_LISTING_HOURS} hours. "
        "You can look these up without publishing again."
    )
    if st.button("Find operation reports"):
        st.session_state["report_lookup"] = {"account_id": account_id}
        st.rerun()
    if st.session_state.get("report_lookup"):
        _operation_report_table("report_lookup")


def _step_group(snapshot: dict) -> None:
    st.subheader("Select rollout batch")
    st.caption("Select the correct batch (today's date) to activate them.")

    options = _group_options(snapshot)
    if not options:
        st.info("This account has no rollout batches.")
        return

    group = st.selectbox(
        "Rollout batch",
        options,
        index=None,
        placeholder="Choose today's batch",
        key="location_group",
    )
    if not group:
        return

    visible_rows = _rows_in_group(snapshot, group)
    frame = _rows_frame(visible_rows)
    if frame.empty:
        st.info("No Deliveroo Retail (6002) channel links in this rollout batch.")
        return

    version = st.session_state.get("link_selection_version", 0)
    edited = st.data_editor(
        frame,
        hide_index=True,
        use_container_width=True,
        disabled=[column for column in DISPLAY_COLUMNS if column != "Sync"],
        column_config={
            "Sync": st.column_config.CheckboxColumn(
                "Sync",
                help="Untick a link to leave it out of the listing update.",
            ),
            "Link ID": st.column_config.TextColumn("Link ID", width="medium"),
        },
        key=f"editor-{group}-{version}",
    )
    selected_ids = _selected_link_ids(edited)
    st.write(f"{len(selected_ids)} channel link{'s' if len(selected_ids) != 1 else ''} selected.")
    if st.button("Select all"):
        st.session_state["link_selection_version"] = version + 1
        st.rerun()

    action = _wizard_nav(
        back_to=1,
        forward_label="Next",
        forward_disabled=not selected_ids,
        key="group_nav",
    )
    if action == "back":
        st.session_state["wizard_step"] = 1
        st.rerun()
    if action == "forward":
        st.session_state["chosen_links"] = selected_ids
        st.session_state["chosen_group"] = group
        st.session_state.pop("health_results", None)
        st.session_state.pop("health_key", None)
        st.session_state.pop("sync_results", None)
        st.session_state.pop("report_lookup", None)
        st.session_state.pop("tracked_sync", None)
        st.session_state["wizard_step"] = 3
        st.rerun()


def _chosen_link_details(link_ids: list[str]) -> list[dict]:
    by_id = {
        row["link_id"]: row
        for row in st.session_state.get("snapshot", {}).get("rows", [])
    }
    details = []
    for link_id in link_ids:
        row = by_id.get(link_id) or {}
        details.append(
            {
                "id": link_id,
                "name": row.get("link_name") or link_id,
                "store_id": row.get("store_id") or "—",
                "location_name": row.get("location_name") or "—",
                "brand_id": row.get("brand_id") or "—",
            }
        )
    return details


def _connection_cell_style(value: str) -> str:
    if value == "OK":
        return "background-color: #15803D; color: white; font-weight: 600"
    if value == "Not OK":
        return "background-color: #DC2626; color: white; font-weight: 600"
    return ""


def _show_health_table(results: list[dict]) -> None:
    ok = sum(1 for row in results if row.get("Connection") == "OK")
    st.write(f"{ok} of {len(results)} connections OK.")
    table = pd.DataFrame(results)
    if not table.empty:
        table = table.style.map(_connection_cell_style, subset=["Connection"])
    st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Credentials response": st.column_config.TextColumn("Credentials response", width="large"),
        },
    )


def _step_health() -> None:
    st.subheader("Health check")
    chosen = st.session_state.get("chosen_links") or []
    if not chosen:
        st.info("No channel links selected.")
        if _wizard_nav(back_to=2, key="health_empty_nav") == "back":
            st.session_state["wizard_step"] = 2
            st.rerun()
        return

    selection_key = tuple(chosen)
    if st.session_state.get("health_key") != selection_key:
        links = _chosen_link_details(chosen)
        with st.spinner(f"Checking connection for {len(links)} channel links…"):
            st.session_state["health_results"] = check_connections(links)
        st.session_state["health_key"] = selection_key

    _show_health_table(st.session_state.get("health_results") or [])
    if st.button("Check again"):
        st.session_state.pop("health_results", None)
        st.session_state.pop("health_key", None)
        st.rerun()

    action = _wizard_nav(back_to=2, forward_label="Next", key="health_nav")
    if action == "back":
        st.session_state["wizard_step"] = 2
        st.rerun()
    if action == "forward":
        st.session_state["wizard_step"] = 4
        st.rerun()


def _publish_chosen(account_id: str, link_ids: list[str]) -> None:
    started_at = sync_started_at()
    by_link = {
        row["link_id"]: row
        for row in st.session_state.get("snapshot", {}).get("rows", [])
    }
    with st.spinner("Publishing inventory…"):
        results = publish_inventory(account_id, link_ids)
    processed_ids = [
        link_id
        for result in results
        if result.get("ok")
        for link_id in result.get("channel_link_ids") or []
    ]
    st.session_state["sync_results"] = results
    if processed_ids:
        st.session_state["tracked_sync"] = {
            "account_id": account_id,
            "started_at": started_at,
            "links": [
                {
                    "id": link_id,
                    "name": (by_link.get(link_id) or {}).get("link_name") or link_id,
                    "location_name": (by_link.get(link_id) or {}).get("location_name") or "—",
                    "admin_id": (by_link.get(link_id) or {}).get("store_id") or "—",
                }
                for link_id in processed_ids
            ],
        }


def _step_publish(account_id: str) -> None:
    st.subheader("Listing update")
    chosen = st.session_state.get("chosen_links") or []
    if not chosen:
        st.info("No channel links selected.")
        if _wizard_nav(back_to=3, key="publish_empty_nav") == "back":
            st.session_state["wizard_step"] = 3
            st.rerun()
        return

    _show_health_table(st.session_state.get("health_results") or [])

    results = st.session_state.get("sync_results")
    if results:
        failed = [result for result in results if not result["ok"]]
        if failed:
            st.error(f"Inventory publish stopped on batch {failed[0]['batch']} (HTTP {failed[0]['status']}).")
        else:
            published = sum(result["count"] for result in results)
            st.success(f"Published inventory for {published} channel links.")

    if st.session_state.get("tracked_sync"):
        _operation_report_table("tracked_sync")

    st.warning(
        f"Publish inventory for {len(chosen)} Deliveroo Retail channel link"
        f"{'s' if len(chosen) != 1 else ''}?"
    )
    action = _wizard_nav(back_to=3, forward_label="Publish", key="publish_nav")
    if action == "back":
        st.session_state["wizard_step"] = 3
        st.rerun()
    if action == "forward":
        _publish_chosen(account_id, chosen)
        st.rerun()


def _known_listing_context() -> dict[str, dict]:
    context = {}
    for row in (st.session_state.get("snapshot") or {}).get("rows") or []:
        link_id = row.get("link_id")
        if not link_id:
            continue
        context[link_id] = {
            "location_name": row.get("location_name") or "—",
            "admin_id": row.get("store_id") or "—",
        }
    return context


def _listing_context(account_id: str, reports: list[dict], links: list[dict] | None = None) -> dict[str, dict]:
    """Location name and Deliveroo admin id for each channel link in the table."""
    context = _known_listing_context()
    for link in links or []:
        link_id = link.get("id")
        if not link_id or link_id in context:
            continue
        if link.get("location_name") or link.get("admin_id"):
            context[link_id] = {
                "location_name": link.get("location_name") or "—",
                "admin_id": link.get("admin_id") or "—",
            }

    needed = []
    for link in links or []:
        if link.get("id"):
            needed.append(link["id"])
    for report in reports:
        link_id, _name = channel_link_from_report(report)
        if link_id:
            needed.append(link_id)
    missing = [link_id for link_id in dict.fromkeys(needed) if link_id not in context]
    if not missing:
        return context

    channel_links = fetch_documents_by_ids("channelLinks", missing, account_id)
    location_ids = [link.get("location") for link in channel_links if link.get("location")]
    location_ids.extend(report.get("location") for report in reports if report.get("location"))
    locations = fetch_documents_by_ids("locations", location_ids, account_id)
    context.update(link_display_from_documents(channel_links, locations))
    names = {
        location["_id"]: location.get("name") or location["_id"]
        for location in locations
        if location.get("_id")
    }
    for report in reports:
        link_id, _name = channel_link_from_report(report)
        if not link_id:
            continue
        info = context.setdefault(link_id, {"location_name": "—", "admin_id": "—"})
        loc_name = names.get(report.get("location"))
        if loc_name and info.get("location_name") in (None, "", "—"):
            info["location_name"] = loc_name
    return context


def _status_cell_style(value: str) -> str:
    colours = {
        "Running": "#D97706",
        "Await": "#0284C7",
        "Waiting": "#64748B",
        "Success": "#15803D",
        "Failed": "#DC2626",
        "Aborted": "#C2410C",
    }
    background = colours.get(value)
    if background is None and str(value).startswith("Unknown"):
        background = "#64748B"
    if not background:
        return ""
    return f"background-color: {background}; color: white; font-weight: 600"


@st.fragment(run_every="10s")
def _operation_report_table(source: str) -> None:
    tracked = st.session_state.get(source)
    if not tracked:
        return

    lookup = source == "report_lookup"
    st.subheader("Listing updates")
    try:
        if lookup:
            reports = fetch_listing_updates(
                tracked["account_id"],
                recent_listing_window(),
                channel=DELIVEROO_RETAIL_CHANNEL,
            )
            context = _listing_context(tracked["account_id"], reports)
            rows = operation_report_rows(reports, context)
        else:
            recent = fetch_listing_updates(
                tracked["account_id"],
                recent_listing_window(LISTING_POLL_HOURS),
                channel=DELIVEROO_RETAIL_CHANNEL,
            )
            published = fetch_listing_updates(
                tracked["account_id"],
                tracked["started_at"],
                [link["id"] for link in tracked["links"]],
            )
            by_id = {report.get("_id"): report for report in recent}
            for report in published:
                by_id[report.get("_id")] = report
            reports = list(by_id.values())
            context = _listing_context(tracked["account_id"], reports, tracked["links"])
            rows = combined_listing_rows(tracked["links"], reports, context)
    except Exception as exc:
        st.error(str(exc))
        return

    waiting = sum(1 for row in rows if row["Status"] == "Waiting")
    running = sum(1 for row in rows if row["Status"] in {"Running", "Await"})
    note, refresh = st.columns([4, 1])
    if lookup:
        note.caption(
            f"Deliveroo Retail (channel {DELIVEROO_RETAIL_CHANNEL}) listing updates "
            f"from the last {RECENT_LISTING_HOURS} hours. Checking every 10 seconds."
        )
    elif waiting or running:
        note.caption(
            f"Checking operation reports every 10 seconds. "
            f"Also showing Deliveroo Retail listing updates from the last {LISTING_POLL_HOURS} hours."
        )
    else:
        note.caption(
            f"Listing updates finished. Still checking every 10 seconds, "
            f"including the last {LISTING_POLL_HOURS} hours."
        )
    refresh.button("Refresh", key=f"refresh_{source}")
    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.style.map(_status_cell_style, subset=["Status"])
    st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        column_config={"Error": st.column_config.TextColumn("Error", width="large")},
    )


def main() -> None:
    _require_password()

    account_id = get_configured_account_id()
    with st.sidebar:
        st.header("Deliveroo Activator")
        if account_id:
            st.caption(f"Account `{account_id}`")
        if st.button("Sign out"):
            st.session_state.pop("authenticated", None)
            st.rerun()

    st.title("Deliveroo Boots activator")
    step = int(st.session_state.get("wizard_step") or 1)
    _step_header(step)

    if not account_id:
        st.error("ACCOUNT_ID is not configured. Set it in the environment or .env file.")
        st.stop()

    if step == 1:
        _step_load(account_id)
    elif step == 2:
        snapshot = st.session_state.get("snapshot")
        if not snapshot:
            st.session_state["wizard_step"] = 1
            st.rerun()
        _step_group(snapshot)
    elif step == 3:
        _step_health()
    else:
        _step_publish(account_id)


main()

import os
import unittest

from deliverect import (
    build_snapshot,
    check_deliveroo_link,
    connection_status,
    credentials_response,
    combined_listing_rows,
    link_display_from_documents,
    email_from_token,
    listing_update_rows,
    notify_zapier,
    operation_report_rows,
    zapier_notification,
    zapier_webhook_url,
    report_error_message,
)

BRAND = "brand-a"


def _location(loc_id="loc1", name="High Street", status="SUBSCRIBED", links=None, group=None):
    location = {
        "_id": loc_id,
        "name": name,
        "status": status,
        "channelLinks": links or [],
    }
    if group:
        location["locationGroup"] = group
    return location


def _link(
    link_id="link1",
    location="loc1",
    status=3,
    brand=BRAND,
    restricted=False,
    channel=6002,
):
    return {
        "_id": link_id,
        "name": "Deliveroo",
        "location": location,
        "channel": channel,
        "status": status,
        "channelSettings": {
            "brandId": brand,
            "catalogPushEnabled": True,
            "isMenuPushRestricted": restricted,
        },
    }


def _group(group_id="g1", name="Wave 1", locations=None):
    return {"_id": group_id, "name": name, "locations": locations or []}


class CheckTests(unittest.TestCase):
    def test_ready_link_has_no_issues(self):
        issues = check_deliveroo_link(
            _location(),
            _link(),
            [{"id": "g1", "name": "Wave 1"}],
            BRAND,
        )
        self.assertEqual(issues, [])

    def test_blocks_missing_group_status_brand_and_menu_push(self):
        issues = check_deliveroo_link(
            _location(status="SUSPENDED"),
            _link(status=1, brand="", restricted=True),
            [],
            BRAND,
        )
        self.assertIn("Location is SUSPENDED", issues)
        self.assertIn("No location group", issues)
        self.assertIn("Channel link is Suspended", issues)
        self.assertIn("Missing brand ID", issues)
        self.assertIn("Menu push is restricted", issues)

    def test_blocks_unexpected_brand(self):
        issues = check_deliveroo_link(
            _location(),
            _link(brand="some-other-brand"),
            [{"id": "g1", "name": "Wave 1"}],
            BRAND,
        )
        self.assertEqual(issues, ["Brand ID does not match expected Deliveroo brand"])

    def test_empty_expected_brand_allows_any_brand(self):
        issues = check_deliveroo_link(
            _location(),
            _link(brand="some-other-brand"),
            [{"id": "g1", "name": "Wave 1"}],
            "",
        )
        self.assertEqual(issues, [])


class SnapshotTests(unittest.TestCase):
    def test_joins_group_from_group_locations_and_from_location_field(self):
        locations = [
            _location("loc1", "Alpha", links=["link1"]),
            _location("loc2", "Beta", links=["link2"], group="g2"),
        ]
        groups = [
            _group("g1", "North", ["loc1"]),
            _group("g2", "South", []),
        ]
        links = [_link("link1", "loc1"), _link("link2", "loc2")]
        snapshot = build_snapshot(locations, groups, links, BRAND)
        by_location = {row["location_name"]: row for row in snapshot["rows"]}
        self.assertEqual(by_location["Alpha"]["group_name"], "North")
        self.assertEqual(by_location["Beta"]["group_name"], "South")
        self.assertTrue(all(row["ready"] for row in snapshot["rows"]))
        self.assertEqual(snapshot["locations_without_deliveroo"], [])
        self.assertEqual(snapshot["groups_without_deliveroo"], [])

    def test_reports_locations_and_groups_without_deliveroo(self):
        locations = [
            _location("loc1", "Alpha", links=["link1"]),
            _location("loc2", "Beta"),
        ]
        groups = [_group("g1", "North", ["loc1"]), _group("g2", "Empty", [])]
        snapshot = build_snapshot(locations, groups, [_link()], BRAND)
        self.assertEqual(
            [row["name"] for row in snapshot["locations_without_deliveroo"]],
            ["Beta"],
        )
        self.assertEqual(
            [row["name"] for row in snapshot["groups_without_deliveroo"]],
            ["Empty"],
        )

    def test_ignores_non_deliveroo_channels_and_flags_orphan_links(self):
        locations = [_location("loc1", "Alpha", links=["uber"])]
        links = [
            _link("uber", "loc1", channel=7),
            _link("orphan", "missing-loc"),
        ]
        snapshot = build_snapshot(locations, [_group("g1", "North", ["loc1"])], links, BRAND)
        self.assertEqual([row["link_id"] for row in snapshot["rows"]], ["orphan"])
        self.assertIn("Location not found", snapshot["rows"][0]["checks"])
        self.assertEqual(snapshot["rows"][0]["group_name"], "No location group")
        self.assertEqual(snapshot["locations_without_deliveroo"][0]["name"], "Alpha")


class ListingUpdateTests(unittest.TestCase):
    def test_matches_channel_link_on_the_operation_report(self):
        links = [
            {"id": "link-1", "name": "Deliveroo"},
            {"id": "other-link", "name": "Other"},
        ]
        reports = [
            {
                "_id": "older",
                "operationType": 1101,
                "operationStatus": 20,
                "_created": "2026-10-09T10:00:00.000Z",
                "properties": {
                    "channelLink": {"_id": "link-1", "name": "Deliveroo - Retail"}
                },
            },
            {
                "_id": "newer",
                "operationType": 1101,
                "operationStatus": 90,
                "_created": "2026-10-09T10:00:05.000Z",
                "properties": {
                    "channelLink": {"_id": "link-1", "name": "Deliveroo - Retail"}
                },
            },
            {
                "_id": "other-type",
                "operationType": 15,
                "operationStatus": 90,
                "_created": "2026-10-09T10:00:09.000Z",
                "properties": {
                    "channelLink": {"_id": "link-1", "name": "Deliveroo - Retail"}
                },
            },
        ]
        rows = listing_update_rows(links, reports)
        by_id = {row["Channel link ID"]: row for row in rows}
        self.assertEqual(by_id["link-1"]["Status"], "Success")
        self.assertEqual(by_id["link-1"]["Report ID"], "newer")
        self.assertEqual(by_id["other-link"]["Status"], "Waiting")
        self.assertEqual(by_id["other-link"]["Error"], "")

    def test_combined_rows_keep_waiting_links_and_recent_reports(self):
        links = [
            {"id": "just-published", "name": "New store"},
            {"id": "already-out", "name": "Earlier store"},
        ]
        reports = [
            {
                "_id": "recent-other",
                "operationStatus": 90,
                "_created": "2026-10-09T12:00:00.000Z",
                "properties": {"channelLink": {"_id": "older-link", "name": "Older store"}},
            },
            {
                "_id": "recent-published",
                "operationStatus": 20,
                "_created": "2026-10-09T14:00:00.000Z",
                "properties": {"channelLink": {"_id": "already-out", "name": "Earlier store"}},
            },
        ]
        context = {
            "just-published": {"location_name": "High Street", "admin_id": "100001"},
            "older-link": {"location_name": "1001 - High Street", "admin_id": "111"},
            "already-out": {"location_name": "1002 - Market", "admin_id": "222"},
        }
        rows = combined_listing_rows(links, reports, context)
        self.assertEqual(rows[0]["Channel link ID"], "just-published")
        self.assertEqual(rows[0]["Status"], "Waiting")
        self.assertEqual(rows[0]["Location"], "High Street")
        self.assertEqual(rows[0]["Admin ID"], "100001")
        self.assertEqual([row["Report ID"] for row in rows[1:]], ["recent-published", "recent-other"])
        self.assertEqual(rows[1]["Location"], "1002 - Market")
        self.assertEqual(rows[1]["Admin ID"], "222")
        self.assertEqual(rows[2]["Location"], "1001 - High Street")

    def test_admin_id_comes_from_store_id(self):
        display = link_display_from_documents(
            [
                {
                    "_id": "link-1",
                    "location": "loc-1",
                    "channelSettings": {"storeId": "100001"},
                }
            ],
            [{"_id": "loc-1", "name": "High Street"}],
        )
        self.assertEqual(display["link-1"]["location_name"], "High Street")
        self.assertEqual(display["link-1"]["admin_id"], "100001")

    def test_error_message_uses_error_log_lines_only(self):
        report = {
            "log": [
                {"level": 20, "message": "[INVENTORY PUSH] Starting inventory push operation"},
                {"level": 40, "message": "Deliveroo catalog ID not configured for channel link abc"},
                {"level": 40, "message": "Deliveroo catalog ID not configured for channel link abc"},
            ]
        }
        self.assertEqual(
            report_error_message(report),
            "Deliveroo catalog ID not configured for channel link abc",
        )
        rows = operation_report_rows(
            [
                {
                    "_id": "report-1",
                    "operationType": 1101,
                    "operationStatus": 120,
                    "_created": "2026-10-09T10:07:07.249000Z",
                    "properties": {
                        "channelLink": {
                            "_id": "link-1",
                            "name": "Deliveroo - Retail",
                        }
                    },
                    "log": report["log"],
                }
            ]
        )
        self.assertEqual(rows[0]["Status"], "Failed")
        self.assertEqual(rows[0]["Error"], "Deliveroo catalog ID not configured for channel link abc")


class ZapierTests(unittest.TestCase):
    def test_missing_webhook_url_is_a_no_op(self):
        old = os.environ.pop("ZAPIER_WEBHOOK_URL", None)
        try:
            self.assertEqual(zapier_webhook_url(), "")
            notify_zapier("Deliveroo Boots activator")
        finally:
            if old is not None:
                os.environ["ZAPIER_WEBHOOK_URL"] = old

    def test_payload_matches_retail_tools(self):
        self.assertEqual(
            zapier_notification(
                "Deliveroo Boots activator",
                "person@example.com",
                "2026-10-13T19:00:00+00:00",
            ),
            {
                "page": "Deliveroo Boots activator",
                "timestamp": "2026-10-13T19:00:00+00:00",
                "email": "person@example.com",
            },
        )

    def test_email_comes_from_the_token(self):
        import base64

        payload = base64.urlsafe_b64encode(
            b'{"sub":"samlp|onelogin-internal|person@example.com"}'
        ).decode().rstrip("=")
        token = f"header.{payload}.sig"
        self.assertEqual(email_from_token(token), "person@example.com")

    def test_strips_quotes_around_the_url(self):
        old = os.environ.get("ZAPIER_WEBHOOK_URL")
        os.environ["ZAPIER_WEBHOOK_URL"] = '"https://hooks.zapier.com/hooks/catch/example/"'
        try:
            self.assertEqual(
                zapier_webhook_url(),
                "https://hooks.zapier.com/hooks/catch/example/",
            )
        finally:
            if old is None:
                os.environ.pop("ZAPIER_WEBHOOK_URL", None)
            else:
                os.environ["ZAPIER_WEBHOOK_URL"] = old


class ConnectionTests(unittest.TestCase):
    def test_connection_ok(self):
        self.assertEqual(connection_status({"connectionOK": True}), ("OK", ""))

    def test_connection_not_ok(self):
        status, detail = connection_status({"connectionOK": False, "connectionResponse": "unauthorized"})
        self.assertEqual(status, "Not OK")
        self.assertEqual(detail, "unauthorized")

    def test_credentials_response_is_shown(self):
        message = "InvalidChannelOnboarding: [DELIVEROO_RETAIL] Invalid Site Location ID."
        self.assertEqual(
            credentials_response({"connectionOK": True, "credentialsResponse": message}),
            message,
        )


if __name__ == "__main__":
    unittest.main()

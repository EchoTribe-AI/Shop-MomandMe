import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from product_api import ImpactAPI


class ImpactWalmartTrackingLinksTestCase(unittest.TestCase):
    def setUp(self):
        self.client = ImpactAPI()

    def _query_param_fragment(self, url, name):
        query = urlparse(url).query
        for part in query.split("&"):
            if part.startswith(f"{name}="):
                return part
        self.fail(f"Missing {name} query parameter in {url}")

    def test_generate_walmart_link_posts_to_trackinglinks_and_returns_tracking_url(self):
        response = Mock()
        response.json.return_value = {"TrackingURL": "https://goto.walmart.com/c/6365428/1398372/16662?impact-api=1"}
        response.raise_for_status.return_value = None

        with patch.dict("os.environ", {"IMPACT_ACCOUNT_SID": "acct-sid", "IMPACT_AUTH_TOKEN": "auth-token"}, clear=False), patch("product_api.requests.post", return_value=response) as post:
            link = ImpactAPI().generate_walmart_link(
                "https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532%3Firgwc%3D1%26utm_source%3Decho",
                "5454929532",
                sub_id1="chat-recommendation",
                sub_id2="5454929532",
                sub_id3="creator-feed",
            )

        self.assertEqual(link, "https://goto.walmart.com/c/6365428/1398372/16662?impact-api=1")
        post.assert_called_once_with(
            "https://api.impact.com/Mediapartners/acct-sid/Programs/16662/TrackingLinks",
            auth=("acct-sid", "auth-token"),
            data={
                "DeepLink": "https://www.walmart.com/ip/5454929532?utm_source=echo",
                "subId1": "chat-recommendation",
                "subId2": "5454929532",
                "subId3": "creator-feed",
            },
            timeout=15,
        )

    def test_generate_walmart_link_omits_type_and_uses_manual_fallback_without_token(self):
        with patch.dict("os.environ", {"IMPACT_AUTH_TOKEN": ""}, clear=False), patch("product_api.requests.post") as post:
            link = ImpactAPI().generate_walmart_link(
                "https://www.walmart.com/ip/5454929532",
                "5454929532",
            )

        post.assert_not_called()
        self.assertTrue(link.startswith("https://goto.walmart.com/c/3590891/1398372/16662?"))
        self.assertEqual(parse_qs(urlparse(link).query)["u"], ["https://www.walmart.com/ip/5454929532"])

    def test_build_trackinglinks_request_cleans_existing_goto_destination(self):
        existing_goto = (
            "https://goto.walmart.com/c/old/ref/program?sourceid=old&veh=aff"
            "&u=https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532%3Firgwc%3D1%26utm_source%3Decho%26clickid%3Dabc"
        )

        endpoint, params = self.client.build_walmart_tracking_link_request(
            existing_goto,
            "5454929532",
            sub_id1="chat-recommendation",
            sub_id2="5454929532",
        )

        self.assertTrue(endpoint.endswith("/Programs/16662/TrackingLinks"))
        self.assertNotIn("Type", params)
        self.assertEqual(params["DeepLink"], "https://www.walmart.com/ip/5454929532?utm_source=echo")
        self.assertEqual(params["subId1"], "chat-recommendation")
        self.assertEqual(params["subId2"], "5454929532")
        self.assertEqual(params["subId3"], "")

    def test_product_destination_is_single_encoded_in_manual_fallback_u_parameter(self):
        link = self.client._build_manual_link(
            "https://www.walmart.com/ip/5454929532",
            "5454929532",
            "chat-recommendation",
            "5454929532",
        )

        u_fragment = self._query_param_fragment(link, "u")
        self.assertIn("https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532", u_fragment)
        self.assertNotIn("https%253A%252F%252Fwww.walmart.com%252Fip%252F5454929532", u_fragment)
        self.assertEqual(
            parse_qs(urlparse(link).query)["u"],
            ["https://www.walmart.com/ip/5454929532"],
        )

    def test_dirty_walmart_destination_is_cleaned_for_trackinglinks(self):
        dirty_destination = (
            "https://www.walmart.com/ip/5454929532?irgwc=1&sourceid=old"
            "&wmlspartner=partner&clickid=abc&affiliates_ad_id=ad"
            "&campaign_id=camp&veh=aff&afsrc=1&utm_source=echo&utm_medium=chat"
        )

        _endpoint, params = self.client.build_walmart_tracking_link_request(
            dirty_destination,
            "5454929532",
            "chat-recommendation",
            "5454929532",
        )

        self.assertEqual(
            params["DeepLink"],
            "https://www.walmart.com/ip/5454929532?utm_source=echo&utm_medium=chat",
        )
        destination_query = parse_qs(urlparse(params["DeepLink"]).query)
        for param in (
            "irgwc",
            "sourceid",
            "wmlspartner",
            "clickid",
            "affiliates_ad_id",
            "campaign_id",
            "veh",
            "afsrc",
        ):
            self.assertNotIn(param, destination_query)

    def test_search_destination_keeps_query_and_utm_behavior_for_trackinglinks(self):
        destination = "https://www.walmart.com/search?q=kids+advent+calendar&utm_source=echo&utm_medium=chat"
        _endpoint, params = self.client.build_walmart_tracking_link_request(
            destination,
            None,
            "chat-recommendation",
            "search-kids-advent-calendar",
        )

        self.assertEqual(params["DeepLink"], destination)
        self.assertEqual(params["subId2"], "search-kids-advent-calendar")

    def test_tracking_url_extraction_accepts_nested_shapes(self):
        self.assertEqual(
            self.client._tracking_url_from_response({"TrackingLink": {"TrackingURL": "https://impact.example/nested"}}),
            "https://impact.example/nested",
        )
        self.assertEqual(
            self.client._tracking_url_from_response({"TrackingLinks": [{"TrackingURL": "https://impact.example/list"}]}),
            "https://impact.example/list",
        )


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from product_api import ImpactAPI


class ImpactManualWalmartLinkTestCase(unittest.TestCase):
    def setUp(self):
        self.client = ImpactAPI()

    def _query_param_fragment(self, url, name):
        query = urlparse(url).query
        for part in query.split("&"):
            if part.startswith(f"{name}="):
                return part
        self.fail(f"Missing {name} query parameter in {url}")

    def test_generate_walmart_link_uses_trackinglinks_vanity_contract(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"TrackingURL": "https://goto.walmart.com/c/tracked"}

        with patch("product_api.requests.post", return_value=Response()) as post:
            link = self.client.generate_walmart_link(
                "https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532",
                "5454929532",
                sub_id1="chat-recommendation",
                sub_id2="5454929532",
                sub_id3="creator-feed",
            )

        self.assertEqual(link, "https://goto.walmart.com/c/tracked")
        post.assert_called_once()
        endpoint = post.call_args.args[0]
        params = post.call_args.kwargs["params"]
        self.assertEqual(
            endpoint,
            "https://api.impact.com/Mediapartners/3590891/Programs/16662/TrackingLinks",
        )
        self.assertEqual(
            params,
            {
                "Type": "vanity",
                "DeepLink": "https://www.walmart.com/ip/5454929532",
                "subId1": "chat-recommendation",
                "subId2": "5454929532",
                "subId3": "creator-feed",
            },
        )

    def test_generate_walmart_link_accepts_uri_response_field(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"Uri": "https://goto.walmart.com/c/uri-tracked"}

        with patch("product_api.requests.post", return_value=Response()):
            link = self.client.generate_walmart_link(
                "https://www.walmart.com/ip/5454929532",
                "5454929532",
            )

        self.assertEqual(link, "https://goto.walmart.com/c/uri-tracked")

    def test_product_destination_is_single_encoded_in_goto_u_parameter(self):
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

    def test_pre_encoded_product_destination_is_not_double_encoded(self):
        link = self.client._build_manual_link(
            "https%3A%2F%2Fwww.walmart.com%2Fip%2F5454929532",
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

    def test_search_destination_keeps_query_and_utm_behavior_single_encoded(self):
        destination = "https://www.walmart.com/search?q=kids+advent+calendar&utm_source=echo&utm_medium=chat"
        link = self.client._build_manual_link(
            destination,
            None,
            "chat-recommendation",
            "search-kids-advent-calendar",
        )

        u_fragment = self._query_param_fragment(link, "u")
        self.assertIn("https%3A%2F%2Fwww.walmart.com%2Fsearch%3Fq%3Dkids%2Badvent%2Bcalendar", u_fragment)
        self.assertIn("utm_source%3Decho%26utm_medium%3Dchat", u_fragment)
        self.assertNotIn("https%253A%252F%252Fwww.walmart.com%252Fsearch", u_fragment)
        self.assertEqual(parse_qs(urlparse(link).query)["u"], [destination])


if __name__ == "__main__":
    unittest.main()

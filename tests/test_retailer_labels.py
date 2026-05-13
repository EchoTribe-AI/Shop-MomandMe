"""Tests for utils.retailer_labels helpers."""
import unittest

from utils.retailer_labels import (
    angle_label,
    collection_cta_default,
    collection_retailer,
    price_placeholder,
    retailer_key,
    retailer_label,
    shop_cta,
)


class RetailerKeyTests(unittest.TestCase):
    def test_walmart_via_network(self):
        self.assertEqual(retailer_key({"network": "walmart"}), "walmart")

    def test_walmart_via_retailer(self):
        self.assertEqual(retailer_key({"retailer": "Walmart"}), "walmart")

    def test_walmart_via_retailer_name(self):
        self.assertEqual(retailer_key({"retailer_name": "WALMART"}), "walmart")

    def test_amazon_detection(self):
        self.assertEqual(retailer_key({"network": "amazon"}), "amazon")
        self.assertEqual(retailer_key({"retailer": "Amazon"}), "amazon")

    def test_unknown(self):
        self.assertEqual(retailer_key({"network": "target"}), "")
        self.assertEqual(retailer_key({}), "")
        self.assertEqual(retailer_key("not-a-dict"), "")


class ShopCtaTests(unittest.TestCase):
    def test_walmart_cta(self):
        self.assertEqual(shop_cta({"network": "walmart"}), "Shop Walmart →")

    def test_amazon_cta(self):
        self.assertEqual(shop_cta({"network": "amazon"}), "Shop Amazon →")

    def test_unknown_cta(self):
        self.assertEqual(shop_cta({"network": "etsy"}), "Shop Now →")
        self.assertEqual(shop_cta({}), "Shop Now →")


class CollectionCtaDefaultTests(unittest.TestCase):
    def test_walmart_only(self):
        prods = [{"network": "walmart"}, {"network": "walmart"}]
        self.assertEqual(collection_cta_default(prods), "Shop the Walmart finds")

    def test_amazon_only(self):
        prods = [{"network": "amazon"}, {"network": "amazon"}]
        self.assertEqual(collection_cta_default(prods), "Shop the Amazon finds")

    def test_mixed(self):
        prods = [{"network": "walmart"}, {"network": "amazon"}]
        self.assertEqual(collection_cta_default(prods), "Shop these finds")

    def test_empty(self):
        self.assertEqual(collection_cta_default([]), "Shop these finds")
        self.assertEqual(collection_cta_default(None), "Shop these finds")


class CollectionRetailerTests(unittest.TestCase):
    def test_dominant_amazon(self):
        self.assertEqual(
            collection_retailer([{"network": "amazon"}, {"network": "amazon"}]),
            "amazon",
        )

    def test_mixed_returns_empty(self):
        self.assertEqual(
            collection_retailer([{"network": "walmart"}, {"network": "amazon"}]),
            "",
        )


class LabelHelperTests(unittest.TestCase):
    def test_retailer_label(self):
        self.assertEqual(retailer_label({"network": "walmart"}), "Walmart")
        self.assertEqual(retailer_label({"network": "amazon"}), "Amazon")
        self.assertEqual(retailer_label({}), "")

    def test_price_placeholder(self):
        self.assertEqual(price_placeholder({"network": "walmart"}), "See price at Walmart")
        self.assertEqual(price_placeholder({"network": "amazon"}), "See price at Amazon")
        self.assertEqual(price_placeholder({}), "See price")


class AngleLabelTests(unittest.TestCase):
    def test_known_angles(self):
        self.assertEqual(angle_label("problem_solve"), "Helpful Find")
        self.assertEqual(angle_label("problem-solve"), "Helpful Find")
        self.assertEqual(angle_label("gift_idea"), "Gift Pick")
        self.assertEqual(angle_label("gift-idea"), "Gift Pick")
        self.assertEqual(angle_label("nostalgia"), "Nostalgia Pick")
        self.assertEqual(angle_label("DEAL_PRICE"), "Deal Pick")
        self.assertEqual(angle_label("mom_rec"), "Mom-Tested")
        self.assertEqual(angle_label("social-proof"), "Crowd Favorite")
        self.assertEqual(angle_label("seasonal"), "Seasonal Pick")
        self.assertEqual(angle_label("scarcity"), "Limited Find")

    def test_unknown_returns_empty(self):
        self.assertEqual(angle_label("random-angle"), "")
        self.assertEqual(angle_label(""), "")
        self.assertEqual(angle_label(None), "")


if __name__ == "__main__":
    unittest.main()

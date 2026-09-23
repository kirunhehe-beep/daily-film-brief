import unittest
from verify_deployment import validate


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.data = {"edition_date": "2026-09-23", "items": [{"title": '电影 A & B'}]}
        self.page = '<title>每日影视简报 · 2026-09-23</title>电影 A &amp; B'

    def test_matching_page_and_data(self):
        validate(self.data, dict(self.data), self.page)

    def test_old_json_is_failure(self):
        with self.assertRaises(ValueError):
            validate(self.data, {**self.data, "edition_date": "2026-09-22"}, self.page)

    def test_old_html_is_failure(self):
        with self.assertRaises(ValueError):
            validate(self.data, self.data, self.page.replace("09-23", "09-22"))

    def test_missing_story_is_failure(self):
        with self.assertRaises(ValueError):
            validate(self.data, self.data, '<title>每日影视简报 · 2026-09-23</title>')

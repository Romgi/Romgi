"""Check public-data boundaries and fail-closed behavior, without network access."""

from collections import Counter
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location("widgets", Path(__file__).resolve().parents[1] / "scripts" / "update_widgets.py")
widgets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(widgets)


def calendar_html(as_of, *, omit=None):
    cells = []
    for offset in range(365):
        day = as_of - timedelta(days=364 - offset)
        if offset == omit:
            continue
        count = 3 if offset % 4 else 0
        label = "No" if not count else str(count)
        cells.append(f'<td id="d{offset}" data-date="{day}" data-level="{1 if count else 0}"></td><tool-tip for="d{offset}">{label} contributions on September 1st.</tool-tip>')
    return "".join(cells)


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 9, 24)

    def test_only_owned_public_original_active_repositories(self):
        base = {"name": "robot", "full_name": "Romgi/robot", "owner": {"login": "Romgi"}, "private": False, "fork": False, "archived": False}
        repos = [base, dict(base, private=True), dict(base, fork=True), dict(base, archived=True),
                 dict(base, owner={"login": "someone-else"}), dict(base, name="rOmGi"),
                 {key: value for key, value in base.items() if key != "private"}]
        self.assertEqual(widgets.eligible_repositories(repos, "Romgi"), [base])

    def test_calendar_is_complete_and_sorted(self):
        days = widgets.parse_calendar(calendar_html(self.as_of), self.as_of)
        self.assertEqual(len(days), 365)
        self.assertEqual(days[0]["date"], self.as_of - timedelta(days=364))
        self.assertEqual(days[-1]["date"], self.as_of)
        self.assertEqual(sum(day["count"] for day in days), 819)

    def test_missing_calendar_day_is_not_silently_zero_filled(self):
        with self.assertRaises(widgets.DataError):
            widgets.parse_calendar(calendar_html(self.as_of, omit=12), self.as_of)

    def test_changed_tooltip_markup_fails_closed(self):
        with self.assertRaises(widgets.DataError):
            widgets.parse_calendar(calendar_html(self.as_of).replace("No contributions", "Activity unknown", 1), self.as_of)

    def test_missing_language_fetch_cannot_silently_skew_percentages(self):
        with self.assertRaises(widgets.DataError):
            widgets.language_totals([{"full_name": "Romgi/robot"}], {})

    def test_fetch_error_preserves_existing_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("activity.svg", "languages.svg"):
                (output / name).write_text("existing asset", encoding="utf-8")
            with patch.object(widgets, "load_data", side_effect=widgets.DataError("Unavailable")):
                self.assertEqual(widgets.main(["--output", directory]), 1)
            self.assertEqual((output / "activity.svg").read_text(), "existing asset")
            self.assertEqual((output / "languages.svg").read_text(), "existing asset")

    def test_svg_escapes_names_and_remains_self_contained(self):
        document = widgets.languages_svg("Romgi", Counter({"C & <test>": 10, "Python": 90}), 2, self.as_of)
        root = ET.fromstring(document)
        self.assertEqual(root.attrib["width"], "960")
        self.assertIn("C &amp; &lt;test&gt;", document)
        self.assertNotIn("href=", document)
        self.assertNotIn("<script", document)

    def test_activity_streak_and_empty_language_data_are_valid(self):
        days = widgets.parse_calendar(calendar_html(self.as_of), self.as_of)
        document = widgets.activity_svg("Romgi", days, self.as_of)
        self.assertIn("Longest streak in this period: 3 days", document)
        ET.fromstring(document)
        ET.fromstring(widgets.languages_svg("Romgi", Counter(), 0, self.as_of))

    def test_mobile_widgets_have_legible_text_and_matching_counts(self):
        days = widgets.parse_calendar(calendar_html(self.as_of), self.as_of)
        for document in (widgets.activity_mobile_svg("Romgi", days, self.as_of),
                         widgets.languages_mobile_svg("Romgi", Counter({"Java": 90, "Python": 10}), 2, self.as_of)):
            root = ET.fromstring(document)
            self.assertEqual(root.attrib["width"], "480")
            for element in root.iter("{http://www.w3.org/2000/svg}text"):
                self.assertGreaterEqual(float(element.attrib["font-size"]), 16)
        self.assertIn("819 publicly visible contributions", widgets.activity_mobile_svg("Romgi", days, self.as_of))

    def test_text_fallback_contains_every_day_and_every_language(self):
        days = widgets.parse_calendar(calendar_html(self.as_of), self.as_of)
        markdown = widgets.data_markdown("Romgi", days, Counter({"Java": 90, "Python": 10}), 2, self.as_of)
        self.assertIn("| Contributions | 819 |", markdown)
        self.assertIn("| Java | 90 | 90.00% |", markdown)
        self.assertIn(f"| {days[0]['date']} | 0 |", markdown)
        self.assertEqual(sum(line.startswith("| 202") for line in markdown.splitlines()), 365)


if __name__ == "__main__":
    unittest.main()

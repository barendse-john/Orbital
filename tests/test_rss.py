"""Google News RSS parsing, against a captured feed - no network needed."""

import unittest
from datetime import datetime, timedelta, timezone

from newsbot.news.rss import _split_title, parse_feed

NOW = datetime.now(timezone.utc)


def _rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>"Manchester United" - Google News</title>
<item>
  <title>United sign striker in deadline day deal - BBC Sport</title>
  <link>https://news.google.com/rss/articles/CBMiAAAA?oc=5&amp;utm_source=rss</link>
  <guid isPermaLink="false">CBMiAAAA</guid>
  <pubDate>{_rfc822(NOW - timedelta(hours=2))}</pubDate>
  <description>&lt;a href="https://bbc.co.uk/x"&gt;United sign striker in deadline day deal&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;BBC Sport&lt;/font&gt;</description>
  <source url="https://www.bbc.co.uk">BBC Sport</source>
</item>
<item>
  <title>Ten questions after United's 2-2 draw - The Athletic</title>
  <link>https://news.google.com/rss/articles/CBMiBBBB</link>
  <pubDate>{_rfc822(NOW - timedelta(hours=9))}</pubDate>
  <description>Analysis of a chaotic afternoon at Old Trafford.</description>
  <source url="https://theathletic.com">The Athletic</source>
</item>
<item>
  <title>Old story from last week - Sky Sports</title>
  <link>https://news.google.com/rss/articles/CBMiCCCC</link>
  <pubDate>{_rfc822(NOW - timedelta(days=7))}</pubDate>
  <source url="https://skysports.com">Sky Sports</source>
</item>
</channel></rss>"""


class ParseFeedTests(unittest.TestCase):
    def setUp(self):
        self.articles = parse_feed(FEED, limit=5, lookback_hours=24)

    def test_only_recent_articles_survive(self):
        self.assertEqual(len(self.articles), 2)
        self.assertNotIn("Old story from last week",
                         [a.title for a in self.articles])

    def test_publisher_is_split_out_of_the_headline(self):
        first = self.articles[0]
        self.assertEqual(first.title, "United sign striker in deadline day deal")
        self.assertEqual(first.source, "BBC Sport")

    def test_tracking_parameters_are_stripped_from_links(self):
        self.assertNotIn("utm_source", self.articles[0].url)
        self.assertIn("oc=5", self.articles[0].url)

    def test_a_description_that_just_repeats_the_headline_is_dropped(self):
        self.assertEqual(self.articles[0].description, "")

    def test_a_real_description_is_kept(self):
        self.assertEqual(self.articles[1].description,
                         "Analysis of a chaotic afternoon at Old Trafford.")

    def test_publication_dates_are_parsed(self):
        self.assertIsNotNone(self.articles[0].published_at)

    def test_limit_is_respected(self):
        self.assertEqual(len(parse_feed(FEED, limit=1, lookback_hours=24)), 1)

    def test_garbage_input_returns_nothing(self):
        self.assertEqual(parse_feed(b"not a feed at all"), [])


class TitleSplitTests(unittest.TestCase):
    def test_splits_on_the_final_dash(self):
        self.assertEqual(_split_title("Man Utd 2-2 Spurs - BBC Sport"),
                         ("Man Utd 2-2 Spurs", "BBC Sport"))

    def test_leaves_dashless_titles_alone(self):
        self.assertEqual(_split_title("A headline"), ("A headline", ""))

    def test_does_not_mistake_a_long_tail_for_a_publisher(self):
        raw = "Analysis - why the deadline day deal changed everything for them"
        self.assertEqual(_split_title(raw), (raw, ""))


if __name__ == "__main__":
    unittest.main()

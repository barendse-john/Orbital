"""Article identity, deduplication, and the GNews -> RSS fallback."""

import tempfile
import unittest
from pathlib import Path

from newsbot.config import GNewsConfig, NewsConfig
from newsbot.db import Database
from newsbot.news.fetcher import NewsFetcher, _dedupe
from newsbot.news.gnews import GNewsQuotaExceeded
from newsbot.news.models import Article, article_key, clean_url


class ArticleTests(unittest.TestCase):
    def test_same_story_from_two_sources_has_one_key(self):
        gnews = Article(title="Starship completes 11th flight test",
                        url="https://a.com/1")
        rss = Article(title="Starship Completes 11th Flight Test!",
                      url="https://news.google.com/rss/articles/xyz")
        self.assertEqual(gnews.key, rss.key)

    def test_different_stories_differ(self):
        self.assertNotEqual(article_key("Rocket launches"),
                            article_key("Rocket explodes"))

    def test_only_a_real_image_url_survives(self):
        self.assertEqual(
            Article(title="T", url="https://a.com/1",
                    image_url=" https://a.com/pic.jpg ").image_url,
            "https://a.com/pic.jpg",
        )
        # GNews leaves the field out, or sends a relative path, often enough
        # that anything unusable has to be treated as "no picture".
        for junk in ("", None, "None", "/img/pic.jpg"):
            self.assertEqual(
                Article(title="T", url="https://a.com/1", image_url=junk).image_url,
                "",
            )

    def test_tracking_parameters_are_stripped(self):
        self.assertEqual(
            clean_url("https://a.com/x?utm_source=twitter&id=7#frag"),
            "https://a.com/x?id=7",
        )

    def test_html_is_removed_from_blurbs(self):
        a = Article(title="T", url="https://a.com",
                    description="<p>Hello   <b>world</b></p>")
        self.assertEqual(a.description, "Hello world")

    def test_dedupe_keeps_first(self):
        items = [Article(title="Same story", url="https://a.com/1"),
                 Article(title="same STORY", url="https://b.com/2"),
                 Article(title="Other", url="https://c.com/3")]
        self.assertEqual(len(_dedupe(items)), 2)


class _StubSource:
    def __init__(self, articles=None, error=None):
        self.articles = articles or []
        self.error = error
        self.calls = 0
        self.api_key = "stub"

    @property
    def enabled(self):
        return True

    async def search(self, query, *, limit=5, lookback_hours=24):
        self.calls += 1
        if self.error:
            raise self.error
        return self.articles[:limit]

    async def close(self):
        pass


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = NewsConfig(
            gnews=GNewsConfig(api_key="key", daily_quota=2),
            max_articles_per_topic=5,
        )
        self.fetcher = NewsFetcher(self.cfg, self.db)

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_gnews_used_first(self):
        self.fetcher.gnews = _StubSource([Article(title="A", url="https://a")])
        self.fetcher.rss = _StubSource([Article(title="B", url="https://b")])
        articles, source = await self.fetcher.search("x")
        self.assertEqual(source, "gnews")
        self.assertEqual(articles[0].title, "A")
        self.assertEqual(await self.db.gnews_used_today(), 1)

    async def test_quota_exhaustion_switches_to_rss_for_the_day(self):
        self.fetcher.gnews = _StubSource(error=GNewsQuotaExceeded("spent"))
        self.fetcher.rss = _StubSource([Article(title="B", url="https://b")])
        articles, source = await self.fetcher.search("x")
        self.assertEqual(source, "rss")
        self.assertEqual(articles[0].title, "B")
        # The whole day's allowance is marked spent, so we stop trying.
        self.assertEqual(await self.db.gnews_used_today(), 2)

    async def test_transient_gnews_error_returns_the_request_to_the_pool(self):
        self.fetcher.gnews = _StubSource(error=RuntimeError("boom"))
        self.fetcher.rss = _StubSource([Article(title="B", url="https://b")])
        from newsbot.news.gnews import GNewsError
        self.fetcher.gnews.error = GNewsError("503")
        _articles, source = await self.fetcher.search("x")
        self.assertEqual(source, "rss")
        self.assertEqual(await self.db.gnews_used_today(), 0)

    async def test_local_quota_runs_out(self):
        self.fetcher.gnews = _StubSource([Article(title="A", url="https://a")])
        self.fetcher.rss = _StubSource([Article(title="B", url="https://b")])
        for _ in range(2):
            await self.fetcher.search("x")
        _articles, source = await self.fetcher.search("x")
        self.assertEqual(source, "rss")
        self.assertEqual(self.fetcher.gnews.calls, 2)

    async def test_already_sent_articles_are_filtered_out(self):
        old = Article(title="Old news", url="https://a")
        new = Article(title="New news", url="https://b")
        self.fetcher.gnews = _StubSource([old, new])
        self.fetcher.rss = _StubSource([])
        await self.db.mark_sent(42, [(old.key, old.url)])
        articles, _ = await self.fetcher.search_unseen(42, "x")
        self.assertEqual([a.title for a in articles], ["New news"])

    async def test_no_sources_left_is_survivable(self):
        self.fetcher.gnews = _StubSource(error=GNewsQuotaExceeded("spent"))
        self.fetcher.rss = None
        articles, source = await self.fetcher.search("x")
        self.assertEqual((articles, source), ([], "none"))


if __name__ == "__main__":
    unittest.main()

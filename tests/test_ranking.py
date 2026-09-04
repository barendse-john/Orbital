"""What earns a place in the morning digest."""

import unittest
from datetime import datetime, timedelta, timezone

from newsbot.news.models import Article
from newsbot.ranking import (cluster_articles, rank, same_story, source_tier,
                             take_top, topic_match)

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)


def art(title, source, hours=2, url=None, description=""):
    return Article(
        title=title, source=source, description=description,
        url=url or f"https://{source.split()[0].lower()}.example/{abs(hash(title)) % 999}",
        published_at=NOW - timedelta(hours=hours),
    )


class SourceTierTests(unittest.TestCase):
    def test_newsrooms_outrank_wire_dumps(self):
        self.assertEqual(source_tier(art("x", "Reuters")), "trusted")
        self.assertEqual(source_tier(art("x", "EIN News")), "low")
        self.assertEqual(source_tier(art("x", "Your Daily Horoscope")), "low")

    def test_universities_are_not_demoted(self):
        # They break developing-technology stories first, so they sit above
        # the unknown-outlet baseline rather than below it.
        self.assertEqual(source_tier(art("x", "Purdue University")), "academic")
        self.assertEqual(
            source_tier(art("x", "College of Engineering | UW-Madison")),
            "academic",
        )
        self.assertEqual(source_tier(art("x", "Some Blog")), "neutral")


class ClusteringTests(unittest.TestCase):
    def test_two_tellings_of_one_story_are_grouped(self):
        a = art("Nvidia confirms $13 billion acquisition of Hugging Face", "Reuters")
        b = art("Nvidia buys AI platform Hugging Face in $13bn deal", "Bloomberg")
        self.assertTrue(same_story(
            {"nvidia", "confirms", "billion", "acquisition", "hugging", "face"},
            {"nvidia", "buys", "platform", "hugging", "face", "deal"},
        ))
        clusters = cluster_articles([a, b])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].sources), 2)

    def test_unrelated_stories_about_one_company_stay_apart(self):
        clusters = cluster_articles([
            art("Nvidia buys Hugging Face in $13bn deal", "Reuters"),
            art("Nvidia shares slip after China export report", "Bloomberg"),
        ])
        self.assertEqual(len(clusters), 2)

    def test_the_strongest_outlet_becomes_the_one_we_link_to(self):
        clusters = cluster_articles([
            art("Nvidia confirms $13 billion Hugging Face acquisition", "EIN News"),
            art("Nvidia confirms $13 billion Hugging Face acquisition", "Reuters"),
        ])
        self.assertEqual(clusters[0].lead.source, "Reuters")


class TopicMatchTests(unittest.TestCase):
    def test_a_story_hitting_the_quoted_phrase_scores_higher(self):
        query = '"central bank" OR "interest rates"'
        strong = art("Central bank holds interest rates steady", "Reuters")
        weak = art("Bank holiday traffic worse than expected", "Reuters")
        self.assertGreater(topic_match(strong, query), topic_match(weak, query))


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.pooled = [
            ("finance", '"central bank" OR nvidia OR "interest rates"', [
                art("Nvidia confirms $13 billion acquisition of Hugging Face",
                    "Reuters", 3),
                art("Nvidia buys Hugging Face in $13bn deal", "Bloomberg", 4),
                art("Your Daily FinanceScope for September 3", "Horoscope Daily", 20),
            ]),
            ("engineering tech", "engineering", [
                art("Engineering team demonstrates solid-state battery",
                    "Purdue University", 6),
                art("Engineering firm announces new hire", "EIN News", 8),
            ]),
        ]

    def test_the_widely_reported_story_leads(self):
        top = rank(self.pooled, now=NOW)
        self.assertIn("Nvidia", top[0][1].article.title)
        self.assertEqual(top[0][1].sources, 2)

    def test_filler_sinks_below_everything_real(self):
        titles = [s.article.source for _, s in rank(self.pooled, now=NOW)]
        self.assertEqual(titles[-1], "Horoscope Daily")

    def test_a_university_beats_a_press_release_wire(self):
        order = [s.article.source for _, s in rank(self.pooled, now=NOW)]
        self.assertLess(order.index("Purdue University"), order.index("EIN News"))

    def test_no_single_topic_swallows_the_digest(self):
        # Four finance stories, one from engineering: finance must not take all.
        pooled = [
            ("finance", "markets", [
                art("Fed holds rates as inflation cools", "Reuters", 1),
                art("Sterling climbs on stronger retail figures", "Reuters", 2),
                art("Oil slips below eighty dollars a barrel", "Bloomberg", 3),
                art("Bond yields steady before jobs report", "Reuters", 4),
                art("Tokyo shares close at record high", "Nikkei", 5),
            ]),
            ("engineering tech", "engineering", [
                art("Engineering breakthrough in battery chemistry", "IEEE", 2),
            ]),
        ]
        kept = take_top(rank(pooled, now=NOW), 4, per_topic=3)
        labels = [label for label, _ in kept]
        self.assertEqual(labels.count("finance"), 3)
        self.assertIn("engineering tech", labels)


if __name__ == "__main__":
    unittest.main()

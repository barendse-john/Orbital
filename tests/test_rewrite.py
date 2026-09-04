"""Rewriting queries that found nothing.

The intent parser compresses questions into short phrases. That works for
named things and fails for themes, because news APIs AND the terms together
and no headline contains "top businesses invest".
"""

import json
import tempfile
import unittest
from pathlib import Path

from newsbot.ai.base import AIBackend, AIError
from newsbot.brain import alternative_queries, relax_query
from newsbot.config import Config, GNewsConfig, NewsConfig
from newsbot.db import Database
from newsbot.digest import DigestService
from newsbot.news.fetcher import NewsFetcher
from newsbot.news.models import Article


class RewritingAI(AIBackend):
    """Answers rewrite prompts, and summary prompts plausibly enough that a
    full search_reply can run through it."""

    name = "rewriting"

    def __init__(self, reply: str | None = None, *, fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls = 0
        self.last_prompt = ""
        self.last_rewrite_prompt = ""

    async def complete(self, system, user, *, max_tokens=600, temperature=0.0,
                       attempts=None):
        self.calls += 1
        self.last_prompt = user
        if self.fail:
            raise AIError("model down")
        if "one-line news summaries" in system:
            count = sum(1 for line in user.splitlines()
                        if line.strip()[:1].isdigit() and "." in line)
            return json.dumps([f"Summary {i + 1}." for i in range(count)])
        self.last_rewrite_prompt = user
        return self.reply or json.dumps(
            ["Nvidia data center spending", "capital expenditure earnings"]
        )


class RelaxQueryTests(unittest.TestCase):
    def test_filler_is_dropped_and_the_rest_is_ored(self):
        # "top", "right", "now" carry no signal in a headline.
        self.assertEqual(relax_query("top businesses to invest in right now"),
                         ["businesses OR invest"])

    def test_longest_terms_win(self):
        out = relax_query("what are the corporate investment trends")[0]
        self.assertIn("investment", out)
        self.assertIn(" OR ", out)

    def test_a_single_meaningful_word_is_left_alone(self):
        # Nothing to relax - ORing one term changes nothing.
        self.assertEqual(relax_query("the news about it"), [])
        self.assertEqual(relax_query("Starship"), [])


class AlternativeQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_model_rewrites_into_headline_language(self):
        ai = RewritingAI()
        out = await alternative_queries(ai, "where are corporations investing",
                                        "corporate investment trends")
        self.assertEqual(out, ["Nvidia data center spending",
                               "capital expenditure earnings"])

    async def test_the_full_question_is_what_gets_rewritten(self):
        ai = RewritingAI()
        await alternative_queries(ai, "What are the top businesses to invest in",
                                  "top businesses invest")
        # The compressed query is what failed; the question has the meaning.
        self.assertIn("What are the top businesses to invest in", ai.last_prompt)

    async def test_the_failed_query_is_never_suggested_back(self):
        ai = RewritingAI(reply=json.dumps(["top businesses invest", "AI capex"]))
        out = await alternative_queries(ai, "q", "top businesses invest")
        self.assertEqual(out, ["AI capex"])

    async def test_quotes_and_escapes_are_stripped(self):
        ai = RewritingAI(reply='["\\"AI capex\\"", "rate cut"]')
        out = await alternative_queries(ai, "q", "failed")
        self.assertEqual(out, ["AI capex", "rate cut"])

    async def test_a_dead_model_falls_back_to_mechanical_relaxation(self):
        out = await alternative_queries(RewritingAI(fail=True),
                                        "q", "corporate investment trends")
        self.assertEqual(out, ["investment OR corporate OR trends"])

    async def test_unparseable_output_falls_back_too(self):
        ai = RewritingAI(reply="Sure! Here are some ideas for you.")
        out = await alternative_queries(ai, "q", "corporate investment trends")
        self.assertTrue(out)
        self.assertIn(" OR ", out[0])

    async def test_no_model_at_all_still_relaxes(self):
        out = await alternative_queries(None, "q", "corporate investment trends")
        self.assertEqual(out, ["investment OR corporate OR trends"])


class WideningTheQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = NewsConfig(gnews=GNewsConfig(api_key=""), lookback_hours=24)
        self.fetcher = NewsFetcher(self.cfg, self.db)
        self.searched: list[str] = []
        self.fetcher.rss = self._source()

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    def _source(self, answers_to: str | None = None):
        searched = self.searched

        class Source:
            api_key = ""
            enabled = False

            async def search(inner, query, *, limit=5, lookback_hours=24):
                searched.append(query)
                if answers_to and query == answers_to:
                    return [Article(title="Nvidia lifts capex guidance",
                                    url="https://a/1", source="FT")]
                return []

            async def close(inner):
                pass

        return Source()

    async def test_alternatives_are_not_paid_for_when_the_query_works(self):
        self.fetcher.rss = self._source(answers_to="Starship")
        called = False

        async def rewrite():
            nonlocal called
            called = True
            return ["should not be needed"]

        articles, _source, trace = await self.fetcher.widening_search(
            "Starship", alternatives=rewrite)
        self.assertTrue(articles)
        self.assertFalse(called)  # the rewrite costs a model call - don't
        self.assertFalse(trace.was_rewritten)

    async def test_a_rewrite_rescues_a_query_that_matches_nothing(self):
        self.fetcher.rss = self._source(answers_to="Nvidia capex")

        async def rewrite():
            return ["Nvidia capex"]

        articles, _source, trace = await self.fetcher.widening_search(
            "corporate investment trends", alternatives=rewrite)
        self.assertTrue(articles)
        self.assertTrue(trace.was_rewritten)
        self.assertEqual(trace.query, "Nvidia capex")
        self.assertEqual(trace.rewritten_from, "corporate investment trends")
        # Three windows on the original, then the rewrite.
        self.assertEqual(self.searched, ["corporate investment trends"] * 3
                         + ["Nvidia capex"])

    async def test_candidates_are_tried_in_order_until_one_lands(self):
        self.fetcher.rss = self._source(answers_to="second choice")

        async def rewrite():
            return ["first choice", "second choice", "third choice"]

        _articles, _source, trace = await self.fetcher.widening_search(
            "nothing", alternatives=rewrite)
        self.assertEqual(trace.query, "second choice")
        self.assertNotIn("third choice", self.searched)  # stop when it works

    async def test_a_failing_rewrite_does_not_fail_the_search(self):
        async def rewrite():
            raise RuntimeError("model exploded")

        articles, source, trace = await self.fetcher.widening_search(
            "nothing", alternatives=rewrite)
        self.assertEqual((articles, source), ([], "none"))
        self.assertFalse(trace.was_rewritten)

    async def test_no_alternatives_supplied_behaves_as_before(self):
        articles, source, trace = await self.fetcher.widening_search("nothing")
        self.assertEqual((articles, source), ([], "none"))
        self.assertEqual(self.searched, ["nothing"] * 3)
        self.assertEqual(trace.window, "month")


class SearchReplyTests(unittest.IsolatedAsyncioTestCase):
    """What the user actually sees when their question needs rewriting."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = Config(news=NewsConfig(gnews=GNewsConfig(api_key=""),
                                          max_articles_per_topic=5))
        self.fetcher = NewsFetcher(self.cfg.news, self.db)
        self.searched: list[str] = []
        outer = self

        class Source:
            api_key = ""
            enabled = False

            async def search(inner, query, *, limit=5, lookback_hours=24):
                outer.searched.append(query)
                if query == "Nvidia data center spending":
                    return [Article(title="Nvidia lifts capex guidance",
                                    url="https://a/1", source="FT")]
                return []

            async def close(inner):
                pass

        self.fetcher.rss = Source()
        self.ai = RewritingAI()
        self.digest = DigestService(self.db, self.fetcher, self.ai, self.cfg)

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_a_thematic_question_now_returns_articles(self):
        body = await self.digest.search_reply(
            7, "corporate investment trends",
            question="Where are major corporations investing their money",
        )
        self.assertIn("https://a/1", body)
        self.assertNotIn("Background, which may be out of date", body)

    async def test_the_reply_says_what_it_actually_searched(self):
        body = await self.digest.search_reply(
            7, "corporate investment trends",
            question="Where are major corporations investing their money",
        )
        # Otherwise broadened results read as the bot ignoring the question.
        self.assertIn("nothing under that", body.lower())
        self.assertIn("so I searched", body)
        self.assertIn("Nvidia data center spending", body)

    async def test_the_question_reaches_the_rewriter_not_just_the_query(self):
        await self.digest.search_reply(
            7, "corporate investment trends",
            question="Where are major corporations investing their money",
        )
        self.assertIn("Where are major corporations investing their money",
                      self.ai.last_rewrite_prompt)

    async def test_knowledge_fallback_is_now_a_last_resort_not_a_first_one(self):
        # Nothing matches, even after rewriting: only then does it answer
        # from what the model knows.
        self.ai.reply = json.dumps(["still nothing", "also nothing"])
        body = await self.digest.search_reply(7, "unfindable", question="q?")
        self.assertIn("still nothing", self.searched)
        self.assertIn("Background, which may be out of date", body)


if __name__ == "__main__":
    unittest.main()

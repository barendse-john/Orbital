"""Narrowing a vague topic: the question, the answer, and the query it makes."""

import json
import unittest

from newsbot.ai.base import AIBackend, AIError
from newsbot.brain import (TopicPlan, extract_json, plain_query, plan_topic,
                           query_is_sane, quote)


class ScriptedAI(AIBackend):
    """Replies with whatever JSON the test queued, in order."""

    name = "scripted"

    def __init__(self, *replies, fail=False):
        self.replies = list(replies)
        self.prompts = []
        self.fail = fail

    async def complete(self, system, user, *, max_tokens=600,
                       temperature=0.0, attempts=None):
        self.prompts.append(user)
        if self.fail:
            raise AIError("backend down")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply if isinstance(reply, str) else json.dumps(reply)


BROAD = {"question": "Markets and central banks, company earnings, or crypto?",
         "label": "Finance", "query": None}
NARROWED = {"question": None, "label": "Markets and Rates",
            "query": '"financial markets" OR "central bank" OR "interest rates"'}


class PlanTopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_broad_topic_gets_one_question(self):
        plan = await plan_topic(ScriptedAI(BROAD), "finance")
        self.assertIsNotNone(plan.question)
        self.assertIsNone(plan.query)
        # The label stays the user's own word until they have answered.
        self.assertEqual(plan.label, "finance")

    async def test_a_specific_topic_is_saved_straight_away(self):
        plan = await plan_topic(ScriptedAI(
            {"question": None, "label": "Manchester United",
             "query": '"Manchester United"'}), "manchester united")
        self.assertIsNone(plan.question)
        self.assertEqual(plan.query, '"Manchester United"')

    async def test_the_answer_produces_the_query(self):
        ai = ScriptedAI(NARROWED)
        plan = await plan_topic(ai, "finance", transcript=[
            {"q": BROAD["question"], "a": "markets and rates"},
        ])
        self.assertEqual(plan.label, "Markets and Rates")
        self.assertIn("central bank", plan.query)
        self.assertIn("markets and rates", ai.prompts[0])

    async def test_it_can_ask_again_while_the_answer_is_still_broad(self):
        plan = await plan_topic(ScriptedAI(BROAD), "finance", transcript=[
            {"q": "which bit?", "a": "all of it really"},
        ])
        self.assertIsNotNone(plan.question)

    async def test_it_stops_asking_after_four_rounds(self):
        # Even if the model would keep going, adding a topic is not an
        # interrogation: round five must produce a query.
        transcript = [{"q": f"q{i}", "a": f"a{i}"} for i in range(4)]
        plan = await plan_topic(ScriptedAI(BROAD), "finance",
                                transcript=transcript)
        self.assertIsNone(plan.question)
        self.assertTrue(plan.query)

    async def test_a_malformed_query_is_thrown_away(self):
        plan = await plan_topic(ScriptedAI(
            {"question": None, "label": "Finance",
             "query": '"unclosed OR (broken'}), "finance")
        self.assertTrue(query_is_sane(plan.query))

    async def test_no_model_still_gives_a_usable_query(self):
        plan = await plan_topic(ScriptedAI(BROAD, fail=True),
                                "manchester united")
        self.assertIsNone(plan.question)
        self.assertEqual(plan.query, '"manchester united"')


class PlainQueryTests(unittest.TestCase):
    def test_multi_word_phrases_are_quoted(self):
        self.assertEqual(quote("Manchester United"), '"Manchester United"')
        self.assertEqual(quote("finance"), "finance")

    def test_the_answer_narrows_the_fallback_too(self):
        self.assertEqual(
            plain_query("finance", "markets and central banks"),
            "finance AND (markets OR central OR banks)",
        )

    def test_words_after_a_negation_are_not_searched_for(self):
        self.assertNotIn("crypto", plain_query("finance", "markets, not crypto"))


class JSONTests(unittest.TestCase):
    def test_handles_fences_and_chatter(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": 1} hope that helps'), {"a": 1})
        self.assertEqual(extract_json('["one", "two"]'), ["one", "two"])
        self.assertIsNone(extract_json("no json here"))


if __name__ == "__main__":
    unittest.main()

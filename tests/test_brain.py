"""Intent rules, time parsing and JSON handling - no network, no API keys."""

import unittest

from newsbot.brain import (
    extract_json,
    fallback_intent,
    guess_timezone_offline,
    normalise_time,
    valid_timezone,
)


class FallbackIntentTests(unittest.TestCase):
    def test_saves_a_standing_interest(self):
        for text in ["i like Manchester United", "follow Manchester United",
                     "track Manchester United", "/add Manchester United"]:
            with self.subTest(text=text):
                intent = fallback_intent(text)
                self.assertEqual(intent.action, "add_topic")
                self.assertEqual(intent.topic, "Manchester United")

    def test_one_off_questions_are_searches(self):
        for text in ["search up news about satellites",
                     "news about satellites", "any news on satellites",
                     "updates on satellites"]:
            with self.subTest(text=text):
                intent = fallback_intent(text)
                self.assertEqual(intent.action, "search")
                self.assertEqual(intent.query, "satellites")

    def test_removals(self):
        intent = fallback_intent("stop sending me tennis")
        self.assertEqual(intent.action, "remove_topic")
        self.assertEqual(intent.topic, "tennis")

    def test_plain_commands(self):
        self.assertEqual(fallback_intent("/topics").action, "list_topics")
        self.assertEqual(fallback_intent("digest now").action, "digest_now")
        self.assertEqual(fallback_intent("pause").action, "pause")
        self.assertEqual(fallback_intent("help").action, "help")

    def test_unknown_message_falls_through_to_chat(self):
        intent = fallback_intent("hey how are you doing today")
        self.assertEqual(intent.action, "chat")
        self.assertTrue(intent.reply)


class TimeTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "08:00": "08:00", "7am": "07:00", "7 am": "07:00",
            "19:30": "19:30", "at 8": "08:00", "12am": "00:00",
            "12pm": "12:00", "send it at 7pm": "19:00",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise_time(raw), expected)

    def test_rejects_nonsense(self):
        self.assertIsNone(normalise_time("banana"))
        self.assertIsNone(normalise_time("99:99"))


class TimezoneTests(unittest.TestCase):
    def test_city_lookup_without_a_model(self):
        self.assertEqual(guess_timezone_offline("Amsterdam"), "Europe/Amsterdam")
        self.assertEqual(guess_timezone_offline("new york"), "America/New_York")
        self.assertIsNone(guess_timezone_offline("Nowhereville"))

    def test_validation(self):
        self.assertTrue(valid_timezone("Europe/Amsterdam"))
        self.assertFalse(valid_timezone("Europe/Nowhere"))
        self.assertFalse(valid_timezone(""))


class JSONTests(unittest.TestCase):
    def test_handles_fences_and_chatter(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": 1} hope that helps'), {"a": 1})
        self.assertEqual(extract_json('["one", "two"]'), ["one", "two"])
        self.assertIsNone(extract_json("no json here"))


if __name__ == "__main__":
    unittest.main()

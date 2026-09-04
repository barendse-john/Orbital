"""Message building: escaping, one combined digest, splitting when too long."""

import unittest
from datetime import datetime

from newsbot.formatting import SAFE_LIMIT, article_line, chunk, digest_messages, topic_block
from newsbot.news.models import Article


def _article(title="Headline", url="https://example.com/a", source="BBC",
             summary="Something happened."):
    a = Article(title=title, url=url, source=source)
    a.summary = summary
    return a


class FormattingTests(unittest.TestCase):
    def test_the_story_is_the_summary_and_the_source_is_the_link(self):
        line = article_line(_article())
        self.assertIn("Something happened.", line)
        self.assertIn('<a href="https://example.com/a">BBC</a>', line)
        # The headline is not repeated - the summary already said it.
        self.assertNotIn("Headline", line)

    def test_the_headline_stands_in_when_there_is_no_summary(self):
        line = article_line(_article(summary=""))
        self.assertIn("Headline", line)
        self.assertIn('<a href="https://example.com/a">BBC</a>', line)

    def test_html_is_escaped(self):
        line = article_line(_article(summary="Ben & Jerry's <b>win</b>"))
        self.assertIn("Ben &amp; Jerry's &lt;b&gt;win&lt;/b&gt;", line)

    def test_an_article_with_no_source_still_gets_a_link(self):
        self.assertIn('<a href="https://example.com/a">link</a>',
                      article_line(_article(source="")))

    def test_topic_block_lists_every_article(self):
        block = topic_block("Space", [_article(summary="One"),
                                      _article(summary="Two")])
        self.assertTrue(block.startswith("<b>Space</b>"))
        self.assertIn("One", block)
        self.assertIn("Two", block)

    def test_digest_is_one_message_when_it_fits(self):
        messages = digest_messages(
            [topic_block("Space", [_article()])],
            when=datetime(2026, 9, 3, 8, 0),
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Your news digest", messages[0])

    def test_empty_topics_are_mentioned_once_at_the_end(self):
        messages = digest_messages([topic_block("Space", [_article()])],
                                   empty_topics=["Tennis", "Chess"])
        self.assertIn("Nothing new on: Tennis, Chess", messages[-1])

    def test_long_digests_split_without_breaking_links(self):
        blocks = [topic_block(f"Topic {i}", [_article(title="A" * 200)] * 5)
                  for i in range(12)]
        messages = digest_messages(blocks)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), SAFE_LIMIT)
            self.assertEqual(message.count("<a href="), message.count("</a>"))

    def test_chunk_leaves_short_text_alone(self):
        self.assertEqual(chunk("short"), ["short"])


if __name__ == "__main__":
    unittest.main()

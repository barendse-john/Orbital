"""The model backend: what it retries, what it gives up on, what it says."""

import unittest

import httpx

from newsbot.ai import anthropic_backend as ab
from newsbot.ai.base import AIError, describe


def _ok(text="hello"):
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


class DescribeTests(unittest.TestCase):
    def test_a_timeout_still_says_something(self):
        # httpx timeouts carry an empty message; "failed: " told us nothing
        # when the 05:00 digest lost its summaries.
        self.assertEqual(describe(httpx.ReadTimeout("")), "ReadTimeout")

    def test_a_message_is_kept_when_there_is_one(self):
        self.assertEqual(describe(ValueError("bad json")), "ValueError: bad json")


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ab.BACKOFF_SECONDS = (0.0, 0.0)  # no real sleeping in tests

    def _backend(self, handler):
        backend = ab.AnthropicBackend("key", "claude-haiku-4-5")
        backend._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"x-api-key": "key"},
        )
        return backend

    async def test_a_timeout_is_retried_and_can_succeed(self):
        self.calls = 0

        def handler(request):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("")
            return _ok("summaries")

        backend = self._backend(handler)
        self.assertEqual(await backend.complete("s", "u"), "summaries")
        self.assertEqual(self.calls, 2)
        await backend.close()

    async def test_it_gives_up_after_the_last_attempt_and_names_the_error(self):
        def handler(request):
            raise httpx.ConnectTimeout("")

        backend = self._backend(handler)
        with self.assertRaises(AIError) as caught:
            await backend.complete("s", "u")
        self.assertIn("ConnectTimeout", str(caught.exception))
        await backend.close()

    async def test_an_overloaded_response_is_retried(self):
        self.calls = 0

        def handler(request):
            self.calls += 1
            if self.calls < 3:
                return httpx.Response(529, text="overloaded")
            return _ok()
        backend = self._backend(handler)
        self.assertEqual(await backend.complete("s", "u"), "hello")
        self.assertEqual(self.calls, 3)
        await backend.close()

    async def test_a_caller_in_a_hurry_gets_one_attempt(self):
        self.calls = 0

        def handler(request):
            self.calls += 1
            raise httpx.ReadTimeout("")

        backend = self._backend(handler)
        with self.assertRaises(AIError):
            # Someone is waiting on a chat reply: three timeouts in a row
            # reads as the bot having frozen.
            await backend.complete("s", "u", attempts=1)
        self.assertEqual(self.calls, 1)
        await backend.close()

    async def test_a_bad_key_is_not_retried(self):
        self.calls = 0

        def handler(request):
            self.calls += 1
            return httpx.Response(401, text="unauthorized")

        backend = self._backend(handler)
        with self.assertRaises(AIError) as caught:
            await backend.complete("s", "u")
        self.assertIn("401", str(caught.exception))
        self.assertEqual(self.calls, 1)  # retrying a wrong key helps nobody
        await backend.close()


if __name__ == "__main__":
    unittest.main()


class TelegramRetryTests(unittest.IsolatedAsyncioTestCase):
    """A dropped Telegram send is what looks like the bot ignoring you."""

    def setUp(self):
        from newsbot.__main__ import RetryingRequest

        RetryingRequest.RETRY_DELAYS = (0.0, 0.0)
        self.cls = RetryingRequest

    async def test_a_network_failure_is_retried_and_can_succeed(self):
        from telegram.error import TimedOut

        calls = []

        class Flaky(self.cls):
            async def do_request(inner, *args, **kwargs):
                return await self.cls.do_request(inner, *args, **kwargs)

        request = Flaky()

        async def flaky_super(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise TimedOut("Timed out")
            return 200, b'{"ok": true}'

        import newsbot.__main__ as main
        original = main.HTTPXRequest.do_request
        main.HTTPXRequest.do_request = flaky_super
        try:
            self.assertEqual(await request.do_request("url", None),
                             (200, b'{"ok": true}'))
            self.assertEqual(len(calls), 3)
        finally:
            main.HTTPXRequest.do_request = original

    async def test_a_bad_request_is_not_retried(self):
        from telegram.error import BadRequest

        calls = []

        async def always_bad(*args, **kwargs):
            calls.append(1)
            raise BadRequest("chat not found")

        import newsbot.__main__ as main
        original = main.HTTPXRequest.do_request
        main.HTTPXRequest.do_request = always_bad
        try:
            with self.assertRaises(BadRequest):
                await self.cls().do_request("url", None)
            # Asking again gets the same answer; only the network is worth
            # a second attempt.
            self.assertEqual(len(calls), 1)
        finally:
            main.HTTPXRequest.do_request = original

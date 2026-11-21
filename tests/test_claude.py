"""The Claude wrapper: a cut-off answer is paid for, ledgered and refused.
No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from engine import claude, summarize
from engine.errors import TruncatedResponseError


def _message(text: str, stop_reason: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=4096))


class TruncationTest(unittest.TestCase):
    def call(self, message):
        client = mock.Mock()
        client.messages.create.return_value = message
        with mock.patch.object(claude, "_get_client", return_value=client), \
             mock.patch.object(claude.costs, "record") as record:
            try:
                return claude.call_claude(model="claude-sonnet-4-6", max_tokens=4096, prompt="p",
                                          meeting_id="vidA", jurisdiction="county",
                                          purpose="transcript_summary"), record
            except TruncatedResponseError as e:
                return e, record

    def test_a_complete_answer_is_returned(self):
        out, record = self.call(_message(' {"overview": "ok"} ', "end_turn"))
        self.assertEqual(out, '{"overview": "ok"}')
        self.assertEqual(record.call_count, 1)

    def test_an_answer_cut_off_at_max_tokens_is_refused_but_still_ledgered(self):
        out, record = self.call(_message('{"overview": "The board met and', "max_tokens"))
        self.assertIsInstance(out, TruncatedResponseError)
        self.assertIn("vidA", str(out))
        self.assertIn("max_tokens=4096", str(out))
        self.assertEqual(record.call_count, 1, "the call was paid for; the ledger must show it")
        self.assertEqual(record.call_args.kwargs["output_tokens"], 4096)

    def test_record_summaries_get_room_for_a_long_meeting(self):
        with mock.patch("engine.summarize.call_claude", return_value="{}") as call:
            summarize.summarize_meeting("t", "Board", "https://v.example", org_label="County",
                                        region="Riverton", jurisdiction="county", meeting_id="vidA")
            summarize.summarize_from_minutes("m", "Board", "https://v.example", org_label="County",
                                             region="Riverton", jurisdiction="county", meeting_id="vidA")
        self.assertEqual([c.kwargs["max_tokens"] for c in call.call_args_list], [8192, 8192])


if __name__ == "__main__":
    unittest.main()

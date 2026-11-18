"""Speaker counts read from public-comment prose (engine/speakers.py)."""
import unittest

from engine.speakers import count_from_prose, speaker_count


class CountFromProse(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(count_from_prose(text), expected, text)

    def test_nobody_spoke(self):
        self.check("No public comment was offered.", 0)
        self.check("No public comment was recorded.", 0)
        self.check("No public comment was offered. The chair made three calls for public "
                   "comment and received no response.", 0)
        self.check("No general public comment was offered during the public comment period. "
                   "However, multiple speakers testified during the public hearing.", 0)
        self.check("One member of the public was present but did not offer formal comment.", 0)
        self.check("The chairperson asked two individuals by name if they had anything to say. "
                   "Neither offered comment. No public comment was offered.", 0)

    def test_headline_counts(self):
        self.check("Five speakers offered public comment. One spoke in favor of the market.", 5)
        self.check("Three residents spoke during public comment.", 3)
        self.check("Two speakers addressed the board. One, a retired director, raised concerns.", 2)
        self.check("One public commenter spoke: a union member.", 1)
        self.check("One speaker, identified as a longtime resident, addressed the board during "
                   "the public hearing on the 2027 budget.", 1)
        self.check("One speaker appeared in person.", 1)
        self.check("12 residents spoke against the rezoning.", 12)

    def test_comments_received(self):
        self.check("Two public comments were received. One vendor spoke in person.", 2)
        self.check("Two written public comments were submitted. No other members of the "
                   "public offered comment.", 2)

    def test_breakdowns_are_not_added(self):
        self.check("Eight speakers signed up for public comment. Seven spoke on the animal waste "
                   "ordinance and two residents raised road concerns.", 8)

    def test_numbers_that_are_not_counts(self):
        self.check("One speaker identified as a member of Council 53 offered public comment. "
                   "He raised concerns about incomplete financial reporting.", 1)

    def test_unknown(self):
        self.check("", None)
        self.check(None, None)
        self.check("Residents raised concerns about parking near the school.", None)

    def test_a_single_named_or_unnamed_speaker(self):
        self.check("A resident appeared before the commission to voice concern over appointments.", 1)
        self.check("Not indicated on agenda.", None)


class SpeakerCount(unittest.TestCase):
    def test_field_wins_over_prose(self):
        self.assertEqual(speaker_count({"_source": "transcript", "publicCommentSpeakers": 4,
                                        "publicComment": "Two speakers addressed the board."}), 4)

    def test_prose_when_field_missing_or_invalid(self):
        self.assertEqual(speaker_count({"_source": "minutes",
                                        "publicComment": "Two speakers addressed the board."}), 2)
        self.assertEqual(speaker_count({"_source": "transcript", "publicCommentSpeakers": None,
                                        "publicComment": "No public comment was offered."}), 0)

    def test_agenda_only_has_no_count(self):
        self.assertIsNone(speaker_count({"_source": "agenda",
                                         "publicComment": "Three residents spoke."}))


if __name__ == "__main__":
    unittest.main()

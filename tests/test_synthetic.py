"""A seeded synthetic batch through the whole pipeline, scored against its answer key."""

import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from invoicecheck.cli import EXIT_OK, main

from .synthetic import EXPECTED_CHECKS, LAYOUTS, TODAY, Expected, make_batch, score


class SyntheticBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.key = make_batch(root / "batch", 80, seed=8)
        code = main(["run", "--input", str(root / "batch"), "--out", str(root / "out"), "--today", TODAY.isoformat()],
                    env={}, stdout=io.StringIO(), stderr=io.StringIO())
        assert code == EXIT_OK, code
        cls.results = json.loads((root / "out" / "results.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_batch_has_every_mistake_kind_and_every_layout(self):
        self.assertEqual({e.mistake for e in self.key.values()}, set(EXPECTED_CHECKS))
        self.assertEqual({i["invoice"]["layout"] for i in self.results["invoices"]}, set(LAYOUTS))

    def test_every_verdict_and_overbilling_amount_matches_the_answer_key(self):
        s = score(self.key, self.results)
        self.assertEqual(s.problems, [])
        self.assertTrue(s.perfect)
        self.assertEqual(s.passed + s.held_right, s.files)
        self.assertGreater(s.overbilling_found, 0)

    def test_the_scorer_notices_a_wrong_answer(self):
        # A perfect score from a scorer that cannot fail would prove nothing: break the key both ways.
        planted = next(n for n, e in self.key.items() if e.mistake == "line_math")
        clean = next(n for n, e in self.key.items() if e.mistake is None)
        key = dict(self.key)
        key[planted] = Expected(None, frozenset(), Decimal("0"))
        key[clean] = Expected("total_math", EXPECTED_CHECKS["total_math"], Decimal("0"))
        s = score(key, self.results)
        self.assertFalse(s.perfect)
        self.assertEqual((s.false_alarms, s.missed), (1, 1))


if __name__ == "__main__":
    unittest.main()

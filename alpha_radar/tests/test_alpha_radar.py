# -*- coding: utf-8 -*-
"""알파 레이더 500자 다이제스트 규격 테스트."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import alpha_radar as ar  # noqa: E402

class TestDigestCap(unittest.TestCase):
    def test_digest_never_exceeds_cap(self):
        import io as _io, json as _json, os as _os, re as _re
        p = _os.path.join(_os.path.dirname(__file__), "..", "data", "latest.json")
        d = _json.load(_io.open(p, encoding="utf-8"))
        t = _re.sub(r"<[^>]+>", "", ar.render_digest(d))
        self.assertLessEqual(len(t), ar.DIGEST_CAP)

    def test_cap_keeps_tail(self):
        self.assertEqual(ar.cap_lines(["x" * 400], ["TAIL"], cap=50)[-1], "TAIL")

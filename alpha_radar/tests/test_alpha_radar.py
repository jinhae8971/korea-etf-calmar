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


class TestVisibility(unittest.TestCase):
    """가시성 규격 v2 — 어떤 줄도 모바일 폭을 넘지 않고, 증감엔 색 점이 붙는다."""

    def _t(self):
        import io as _io, json as _json, os as _os, re as _re
        return _re.sub(r"<[^>]+>", "", ar.render_digest(_json.load(_io.open(_os.path.join(_os.path.dirname(__file__),'..','data','latest.json'),encoding='utf-8'))))

    def test_no_line_exceeds_mobile_width(self):
        for ln in self._t().split("\n"):
            self.assertLessEqual(ar.vis_width(ln), ar.LINE_COLS, ln)

    def test_changes_carry_color_dots(self):
        self.assertTrue(any(d in self._t() for d in ("🟩", "🟢", "⚪", "🔴", "🟥")))

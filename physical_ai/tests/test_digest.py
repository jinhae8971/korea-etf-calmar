# -*- coding: utf-8 -*-
"""피지컬 AI 500자 다이제스트 규격 테스트."""
import io
import json
import os
import re
import sys
import unittest

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
import run  # noqa: E402

SNAP = os.path.join(BASE, "..", "docs", "physical-ai", "data", "latest.json")


class TestDigestCap(unittest.TestCase):
    def _report(self):
        return json.load(io.open(SNAP, encoding="utf-8"))

    def test_digest_never_exceeds_cap(self):
        t = re.sub(r"<[^>]+>", "", run.render_digest(self._report(), "https://x/"))
        self.assertLessEqual(len(t), run.DIGEST_CAP)

    def test_regime_is_human_text_not_enum(self):
        out = run.render_digest(self._report(), "")
        self.assertNotIn("NARRATIVE_LED", out)

    def test_cap_keeps_tail(self):
        self.assertEqual(run.cap_lines(["x" * 600], ["TAIL"], cap=50)[-1], "TAIL")


class TestVisibility(unittest.TestCase):
    """가시성 규격 v2 — 어떤 줄도 모바일 폭을 넘지 않고, 증감엔 색 점이 붙는다."""

    def _t(self):
        import io as _io, json as _json, os as _os, re as _re
        return _re.sub(r"<[^>]+>", "", run.render_digest(_json.load(_io.open(SNAP,encoding='utf-8')),'https://x/'))

    def test_no_line_exceeds_mobile_width(self):
        for ln in self._t().split("\n"):
            self.assertLessEqual(run.vis_width(ln), run.LINE_COLS, ln)

    def test_changes_carry_color_dots(self):
        self.assertTrue(any(d in self._t() for d in ("🟩", "🟢", "⚪", "🔴", "🟥")))

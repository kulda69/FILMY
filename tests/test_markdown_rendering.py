from __future__ import annotations

import unittest

from filmy.markdown import render_user_markdown


class UserMarkdownTests(unittest.TestCase):
    def test_renders_basic_markdown(self) -> None:
        html = str(render_user_markdown("**silné** scény\n- tempo\n- svět"))
        self.assertIn("<strong>silné</strong>", html)
        self.assertIn("<ul", html)
        self.assertIn("<li>tempo</li>", html)

    def test_escapes_raw_html(self) -> None:
        html = str(render_user_markdown("<script>alert(1)</script> **ok**"))
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("<strong>ok</strong>", html)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for docs_page.py. Copy next to the plugin's copy (tests/)."""
import os
import sys
import unittest

# This repo runs `unittest discover -s tests` (no package): put the repo root,
# where docs_page.py lives, on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_page import docs_html, render_markdown  # noqa: E402


class RenderTests(unittest.TestCase):
    def test_heading_paragraph_ids(self):
        out = render_markdown("# Hello World\n\nSome *text* and **bold**.\n\n## Hello World")
        self.assertIn('<h1 id="hello-world">Hello World</h1>', out)
        self.assertIn('<h2 id="hello-world-1">', out)
        self.assertIn("<em>text</em>", out)
        self.assertIn("<strong>bold</strong>", out)

    def test_raw_html_is_escaped(self):
        out = render_markdown("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>")
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)

    def test_unsafe_links_dropped(self):
        out = render_markdown("[x](javascript:alert(1)) [y](API.md) [z](https://ex.com/a?b=1&c=2)")
        self.assertNotIn("javascript:", out)
        self.assertNotIn('href="API.md"', out)
        self.assertIn('href="https://ex.com/a?b=1&amp;c=2"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_attribute_breakout(self):
        out = render_markdown('[a](https://x.com/"onmouseover="alert(1))')
        self.assertNotIn('"onmouseover', out)

    def test_code_block_and_inline_code(self):
        out = render_markdown("Run `a <b>`:\n\n```sh\necho '<x>' && ls\n```\n")
        self.assertIn("<code>a &lt;b&gt;</code>", out)
        self.assertIn('<pre><code class="language-sh">echo &#x27;&lt;x&gt;&#x27; &amp;&amp; ls</code></pre>', out)

    def test_code_span_not_emphasised(self):
        out = render_markdown("`*not*` and `__init__`")
        self.assertIn("<code>*not*</code>", out)
        self.assertIn("<code>__init__</code>", out)

    def test_bold_around_code_span(self):
        out = render_markdown("owns the **`role: sensor`** boards and [`x`](https://a.b)")
        self.assertIn("<strong><code>role: sensor</code></strong>", out)
        self.assertIn('<code>x</code></a>', out)

    def test_lists_nested(self):
        out = render_markdown("- a\n- b\n  - c\n1. one\n")
        self.assertTrue(out.startswith("<ul><li>a</li><li>b<ul><li>c</li></ul>"))
        self.assertIn("<ol><li>one</li></ol>", out)

    def test_table(self):
        out = render_markdown("| A | B |\n|---|:-:|\n| `x|y` | 2 |\n| 3 |\n")
        self.assertIn("<th>A</th><th>B</th>", out)
        self.assertIn("<td>3</td><td></td>", out)

    def test_blockquote_hr(self):
        out = render_markdown("> note **this**\n\n---\n")
        self.assertIn("<blockquote><p>note <strong>this</strong></p></blockquote>", out)
        self.assertIn("<hr>", out)

    def test_page(self):
        page = docs_html("esp32-sensor", "# Hi", "0.1.0").decode()
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("<title>esp32-sensor documentation</title>", page)
        self.assertIn("esp32-sensor 0.1.0", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("http://", page.split("<main>")[0])  # no external assets


if __name__ == "__main__":
    unittest.main()

import unittest

from invoicecheck.htmldoc import parse_html, row_cells, to_text


class HtmlDocTests(unittest.TestCase):
    def test_find_by_tag_and_class_substring(self):
        root = parse_html('<div><table class="line-items"><tr><td>A</td></tr></table>'
                          '<table class="meta"></table></div>')
        self.assertEqual(len(root.find_all("table")), 2)
        self.assertIs(root.find(class_="item"), root.find_all("table")[0])

    def test_entities_are_decoded(self):
        root = parse_html("<p>Coffee &amp; tea &middot; 555-0100</p>")
        self.assertEqual(root.find("p").text(), "Coffee & tea · 555-0100")

    def test_cells_do_not_run_together(self):
        root = parse_html("<table><tr><td>Subtotal</td><td>$1.00</td></tr></table>")
        self.assertEqual(root.find("tr").text(), "Subtotal $1.00")
        self.assertEqual(row_cells(root.find("tr")), ["Subtotal", "$1.00"])

    def test_unclosed_cells_and_items_are_closed_like_a_browser(self):
        root = parse_html("<table><tr><td>a<td>b<tr><td>c</table><ul><li>one<li>two</ul>")
        rows = root.find_all("tr")
        self.assertEqual([row_cells(r) for r in rows], [["a", "b"], ["c"]])
        self.assertEqual([li.text() for li in root.find_all("li")], ["one", "two"])

    def test_stray_end_tag_is_ignored(self):
        root = parse_html("<p>ok</span></p>")
        self.assertEqual(root.find("p").text(), "ok")

    def test_to_text_keeps_rows_on_one_line_and_skips_head(self):
        html = ("<html><head><title>Invoice X</title><style>p{}</style></head><body>"
                "<h1>Vendor</h1><table><tr><th>Item</th><th>Qty</th></tr>"
                "<tr><td>Thing</td><td>2</td></tr></table></body></html>")
        self.assertEqual(to_text(parse_html(html)), "Vendor\nItem | Qty\nThing | 2")


if __name__ == "__main__":
    unittest.main()

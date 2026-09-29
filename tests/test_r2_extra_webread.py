"""Additional contract checks for source formatting."""
import unittest
from fbot import webread
from . import r2_fakes


class ExtraTextTests(unittest.TestCase):
    def test_formatted_html_table_stays_one_row(self):
        body = b"<table>\n<tr>\n<td>September 17</td>\n<td>25</td>\n<td>0</td>\n<td>3.75-4.00</td>\n</tr>\n</table>"
        self.assertEqual(webread.html_text(body), "September 17 | 25 | 0 | 3.75-4.00")

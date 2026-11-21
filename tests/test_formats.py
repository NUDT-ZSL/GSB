import unittest

from diagwb.errors import DiagError
from diagwb.formats import parse_text


class FormatReaderTests(unittest.TestCase):
    def test_dispatch_by_header_and_inference(self):
        version, records = parse_text(
            "# FORMAT_VERSION=1\ntool|%7B%22a%22%3A1%7D|OK\n")
        self.assertEqual(version, 1)
        self.assertEqual(records[0].fields["status"], "OK")

    def test_unknown_version(self):
        with self.assertRaises(DiagError) as caught:
            parse_text("# FORMAT_VERSION=7\nx|y|z\n")
        self.assertEqual(caught.exception.code,
                         "E_FORMAT_VERSION_UNSUPPORTED")

    def test_corrupt_reports_line_and_offset(self):
        cases = [
            ("# FORMAT_VERSION=1\ntool|%7B%7D\n", 2, 2),
            ("# FORMAT_VERSION=1\ntool|badjson|OK\n", 2, 1),
            ("# FORMAT_VERSION=1\ntool|%7B%7D|NOPE\n", 2, 2),
        ]
        for text, line_no, offset in cases:
            with self.subTest(text=text):
                with self.assertRaises(DiagError) as caught:
                    parse_text(text)
                self.assertEqual(caught.exception.code, "E_FORMAT_CORRUPT")
                self.assertEqual(caught.exception.context["line_no"], line_no)
                self.assertEqual(
                    caught.exception.context["field_offset"], offset)

    def test_v3_missing_required_field(self):
        with self.assertRaises(DiagError) as caught:
            parse_text('# FORMAT_VERSION=3\n{"result_ref":"r"}\n')
        self.assertEqual(caught.exception.code, "E_FORMAT_CORRUPT")
        self.assertIn("field_name", caught.exception.context)


if __name__ == "__main__":
    unittest.main()

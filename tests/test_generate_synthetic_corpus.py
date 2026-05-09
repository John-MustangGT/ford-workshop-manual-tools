import importlib.util
import tempfile
import unittest
from pathlib import Path

from extract_baypod import parse_arc, parse_idicomp, parse_pod_bay


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_synthetic_corpus.py"


spec = importlib.util.spec_from_file_location("generate_synthetic_corpus", GENERATOR_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class SyntheticCorpusGeneratorTests(unittest.TestCase):
    def test_generator_writes_expected_layout(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            module.generate_corpus(out)

            self.assertTrue((out / "arcs" / "SEB.arc").is_file())
            self.assertTrue((out / "arcs" / "R98.arc").is_file())
            self.assertTrue((out / "idicomp" / "chunked_html.idicomp").is_file())
            self.assertTrue((out / "idicomp" / "raw_jpg.idicomp").is_file())
            self.assertTrue((out / "extracted" / "SEB" / "SEBALPHAINDEX.HTM").is_file())
            self.assertTrue((out / "extracted" / "EEB" / "EEB042001.SVG").is_file())
            self.assertTrue((out / "SYNTHETIC_NOTICE.txt").is_file())

    def test_generated_bay_pod_parses_and_contains_expected_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            module.generate_corpus(out)

            data, entries = parse_arc(str(out / "arcs" / "SEB.arc"))
            names = {entry["filename"] for entry in entries}
            self.assertIn("SEBALPHAINDEX.HTM", names)
            self.assertIn("EEB042001.SVG", names)
            self.assertIn("PHOTO.JPG", names)

            payload_by_name = {}
            for entry in entries:
                start = entry["abs_data_off"]
                end = start + entry["file_size"]
                payload, _ = parse_idicomp(data[start:end])
                payload_by_name[entry["filename"]] = payload

            self.assertTrue(payload_by_name["SEBALPHAINDEX.HTM"].startswith(b"<html>"))
            self.assertTrue(payload_by_name["EEB042001.SVG"].startswith(b"<svg"))
            self.assertEqual(payload_by_name["PHOTO.JPG"][:2], b"\xff\xd8")
            self.assertTrue(payload_by_name["BOOK.PDF"].startswith(b"%PDF"))
            self.assertTrue(payload_by_name["META.WCF"].startswith(b"; "))

    def test_generated_pod_bay_parses_to_inferred_entries(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            module.generate_corpus(out)

            _, entries = parse_pod_bay(str(out / "arcs" / "R98.arc"))
            names = {entry["filename"] for entry in entries}

            self.assertIn("r98s20.htm", names)
            self.assertIn("r98s20s.htm", names)
            self.assertTrue(any(name.endswith(".gif") for name in names))
            self.assertTrue(any(name.endswith(".jpg") for name in names))
            self.assertTrue(any(name.endswith(".pdf") for name in names))
            self.assertTrue(any(name.endswith(".wcf") for name in names))


if __name__ == "__main__":
    unittest.main()

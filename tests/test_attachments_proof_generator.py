"""Offline artifact generation checks. No chats, uploads or provider requests."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.attachments_proof.generate import generate_fixture_set

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipUnless(Image is not None and os.name == "posix", "Local Pillow and POSIX no-follow support required; no dependency is downloaded")
class FixtureGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attachments-generator-offline-")
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "fixtures"
        self.manifest = generate_fixture_set(self.root)
        self.sources = self.root / "source-fixtures"
        self.answers = json.loads((self.root / "answer-key/answers.json").read_text())["answers"]

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_has_only_relative_sources_and_no_answers_or_chat_folders(self):
        self.assertEqual(set(p.name for p in self.root.iterdir()), {"source-fixtures", "answer-key", "manifest.json"})
        self.assertEqual(self.manifest, json.loads((self.root / "manifest.json").read_text()))
        self.assertEqual(len(self.manifest["files"]), 11)
        self.assertEqual(set(self.answers), {f["name"] for f in self.manifest["files"]})
        serialized = json.dumps(self.manifest)
        for record in self.manifest["files"]:
            self.assertEqual(set(record), {"name", "path", "kind", "mediaType", "bytes", "sha256"})
            self.assertEqual(record["path"], "source-fixtures/" + record["name"])
            data = (self.root / record["path"]).read_bytes()
            self.assertEqual(len(data), record["bytes"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])
        for answer in self.answers.values():
            self.assertNotIn(answer["canary"], serialized)
            if "password" in answer:
                self.assertNotIn(answer["password"], serialized)
        self.assertNotIn("answer-key", serialized)

    def test_all_outputs_private_and_sources_read_only(self):
        for directory in (self.root, self.sources, self.root / "answer-key"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in self.sources.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
            self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE((self.root / "answer-key/answers.json").stat().st_mode), 0o600)

    def test_text_canaries_at_original_lines_and_each_file_unique(self):
        expected = [("requirements.txt", 12, 7), ("decisions.md", 15, 9)]
        for filename, count, line in expected:
            lines = (self.sources / filename).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), count)
            self.assertIn(self.answers[filename]["canary"], lines[line - 1])
            self.assertLess((self.sources / filename).stat().st_size, 2048)
        canaries = [answer["canary"] for answer in self.answers.values()]
        self.assertEqual(len(set(canaries)), 11)
        for filename, answer in self.answers.items():
            for other in self.sources.iterdir():
                if other.name != filename:
                    self.assertNotIn(answer["canary"].encode(), other.read_bytes())

    def test_images_are_pixel_only_without_text_metadata(self):
        for filename, format_name in (("checkout.png", "PNG"), ("receipt.jpg", "JPEG")):
            raw = (self.sources / filename).read_bytes()
            self.assertNotIn(self.answers[filename]["canary"].encode(), raw)
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(image.size, (640, 480))
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.format, format_name)
                self.assertEqual(len(image.getexif()), 0)
                self.assertFalse(set(image.info) - {"jfif", "jfif_version", "jfif_unit", "jfif_density"})
                # Visible ink exists in the canary row; no textual metadata shortcut.
                self.assertNotEqual(image.crop((24, 78, 620, 110)).getextrema(), ((255, 255),) * 3)
            self.assertLess(len(raw), 128 * 1024)
        scan = (self.sources / "scanned-delivery.pdf").read_bytes()
        self.assertIn(b"/Subtype /Image", scan)
        self.assertNotIn(self.answers["scanned-delivery.pdf"]["canary"].encode(), scan)

    @unittest.skipUnless(shutil.which("pdfinfo") and shutil.which("pdftotext"), "Local Poppler unavailable; no download")
    def test_pdf_reader_validates_pages_scan_and_actual_password_encryption(self):
        def run(program, *args):
            return subprocess.run([shutil.which(program), *map(str, args)], capture_output=True, timeout=10)
        pdf = self.sources / "delivery-plan.pdf"
        info = run("pdfinfo", pdf)
        self.assertEqual(info.returncode, 0, info.stderr.decode())
        self.assertRegex(info.stdout.decode(), r"Pages:\s+2")
        first = run("pdftotext", "-f", "1", "-l", "1", pdf, "-")
        second = run("pdftotext", "-f", "2", "-l", "2", pdf, "-")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        canary = self.answers[pdf.name]["canary"].encode()
        self.assertNotIn(canary, first.stdout)
        self.assertIn(canary, second.stdout)
        self.assertIn(b"Box count: 17", second.stdout)
        scan = self.sources / "scanned-delivery.pdf"
        scan_info = run("pdfinfo", scan)
        self.assertEqual(scan_info.returncode, 0, scan_info.stderr.decode())
        self.assertRegex(scan_info.stdout.decode(), r"Pages:\s+1")
        scanned_text = run("pdftotext", scan, "-")
        self.assertEqual(scanned_text.returncode, 0)
        self.assertEqual(scanned_text.stdout.strip(), b"")
        locked = self.sources / "locked-delivery.pdf"
        refused = run("pdftotext", locked, "-")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b"password", refused.stderr.lower())
        password = self.answers[locked.name]["password"]
        info = run("pdfinfo", "-upw", password, locked)
        self.assertEqual(info.returncode, 0, info.stderr.decode())
        self.assertRegex(info.stdout.decode(), r"Encrypted:\s+yes")
        opened = run("pdftotext", "-upw", password, locked, "-")
        self.assertEqual(opened.returncode, 0, opened.stderr.decode())
        self.assertIn(self.answers[locked.name]["canary"].encode(), opened.stdout)
        self.assertNotIn(self.answers[locked.name]["canary"].encode(), locked.read_bytes())

    def test_payload_sizes_and_untrusted_document_are_literal_data(self):
        for path in self.sources.iterdir():
            if path.suffix in (".pdf", ".png", ".jpg"):
                self.assertLess(path.stat().st_size, 128 * 1024)
        over = (self.sources / "over-budget.txt").read_bytes()
        over.decode("utf-8", errors="strict")
        self.assertGreater(len(over), 524288)
        self.assertLess(len(over), 10 * 1024 * 1024)
        untrusted = (self.sources / "untrusted-instructions.txt").read_text()
        self.assertIn("SHOULD_NOT_EXIST", untrusted)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_regeneration_changes_canaries_and_never_reuses_an_existing_root(self):
        second_root = self.parent / "second"
        generate_fixture_set(second_root, runtime="codex", case="formats")
        second = json.loads((second_root / "answer-key/answers.json").read_text())["answers"]
        self.assertTrue({x["canary"] for x in self.answers.values()}.isdisjoint(x["canary"] for x in second.values()))
        original = (self.root / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            generate_fixture_set(self.root)
        self.assertEqual((self.root / "manifest.json").read_bytes(), original)

    def test_rejects_symlinks_traversal_relative_roots_and_unsafe_labels(self):
        link = self.parent / "link"
        link.symlink_to(self.root, target_is_directory=True)
        for target in (link, link / "child"):
            with self.assertRaises(OSError):
                generate_fixture_set(target)
        with self.assertRaises(ValueError):
            generate_fixture_set(self.parent / "child" / ".." / "escaped")
        with self.assertRaises(ValueError):
            generate_fixture_set("relative-fixtures")
        for label in ("../escape", "has spaces", "", "x" * 33, None):
            with self.assertRaises(ValueError):
                generate_fixture_set(self.parent / "unused", runtime=label)
        self.assertFalse((self.parent / "unused").exists())
        self.assertFalse((self.root / "child").exists())

    def test_generator_does_not_invoke_network_or_process_transport(self):
        with patch.object(socket, "socket", side_effect=AssertionError("Network forbidden")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("Processes forbidden")):
            manifest = generate_fixture_set(self.parent / "offline-again")
        self.assertEqual(len(manifest["files"]), 11)


if __name__ == "__main__":
    unittest.main()

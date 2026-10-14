"""Format parsers, fed with small files built in memory."""

import io
import struct
import unittest
import zipfile
import zlib

from metahunter.extractors import detect_format, extract_metadata
from tests.samples import badly_redacted_pdf, jpeg_with_exif


def values(meta, label):
    return [value for _, field_label, value in meta.fields if field_label == label]


def png_with_text():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"tEXt", b"Author\x00Bob Builder")
            + chunk(b"tEXt", b"parameters\x00a cat, steps 20") + chunk(b"IEND", b"") + b"APPENDED-SECRET-DATA-1234")


def docx_with_reviewers():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("docProps/core.xml", "<cp:coreProperties><dc:creator>Alice Author</dc:creator>"
                         "<cp:lastModifiedBy>Mallory Editor</cp:lastModifiedBy>"
                         "<dcterms:created>2024-01-02T03:04:05Z</dcterms:created></cp:coreProperties>")
        archive.writestr("docProps/app.xml", "<Properties><Company>Acme &amp; Co</Company>"
                         "<Application>Microsoft Office Word</Application></Properties>")
        archive.writestr("word/document.xml", "<w:document><w:ins w:author=\"Reviewer Rita\" w:date=\"2024\"/></w:document>")
        archive.writestr("word/_rels/document.xml.rels", "<Relationships><Relationship Id=\"r1\" "
                         "Target=\"file:///C:/Users/alice/Templates/secret.dotm\" TargetMode=\"External\"/></Relationships>")
    return buffer.getvalue()


def mp3_with_tags():
    def frame(name, text):
        body = b"\x03" + text.encode()
        return name + struct.pack(">I", len(body)) + b"\x00\x00" + body
    frames = frame(b"TIT2", "Field Recording") + frame(b"TPE1", "Sam Sound") + frame(b"TSSE", "LAME 3.100")
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + frames + b"\xff\xfb\x90\x64" + b"\x00" * 400


def wav_with_info():
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 176400, 4, 16)
    info = b"INFO" + b"IART" + struct.pack("<I", 8) + b"Ann Art\x00" + b"ISFT" + struct.pack("<I", 6) + b"Lavf\x00\x00"
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", len(info)) + info
            + b"data" + struct.pack("<I", 176400) + b"\x00" * 176400)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class FormatDetection(unittest.TestCase):
    def test_signatures(self):
        self.assertEqual(detect_format(jpeg_with_exif()), "jpeg")
        self.assertEqual(detect_format(png_with_text()), "png")
        self.assertEqual(detect_format(badly_redacted_pdf()), "pdf")
        self.assertEqual(detect_format(docx_with_reviewers()), "zip")
        self.assertEqual(detect_format(mp3_with_tags()), "mp3")
        self.assertEqual(detect_format(wav_with_info()), "wav")
        self.assertEqual(detect_format(b"<?xml version='1.0'?><svg></svg>"), "svg")
        self.assertEqual(detect_format(b"random bytes"), "unknown")


class Extraction(unittest.TestCase):
    def test_jpeg_exif_and_gps(self):
        meta = extract_metadata(jpeg_with_exif(), "photo.jpg")
        self.assertIn("Canon EOS 5D", values(meta, "Camera model"))
        self.assertIn("Alice Photographer", values(meta, "Artist"))
        latitude, longitude, _ = meta.gps_points[0]
        self.assertAlmostEqual(latitude, 48.8566667, places=5)
        self.assertAlmostEqual(longitude, 2.3525, places=5)

    def test_png_text_and_appended_data(self):
        meta = extract_metadata(png_with_text(), "image.png")
        self.assertIn("Bob Builder", values(meta, "Author"))
        self.assertIn("a cat, steps 20", values(meta, "AI generation parameters"))
        self.assertTrue(values(meta, "Data after end of image"))

    def test_docx_people_and_paths(self):
        meta = extract_metadata(docx_with_reviewers(), "report.docx")
        self.assertIn("Mallory Editor", values(meta, "Last modified by"))
        self.assertIn("Acme & Co", values(meta, "Company"))
        self.assertIn("Reviewer Rita", values(meta, "Tracked change / comment author"))
        self.assertTrue(any("secret.dotm" in value for value in values(meta, "External link / template")))

    def test_mp3_id3(self):
        meta = extract_metadata(mp3_with_tags(), "song.mp3")
        self.assertIn("Sam Sound", values(meta, "Artist"))
        self.assertIn("LAME 3.100", values(meta, "Encoder settings"))

    def test_wav_info(self):
        meta = extract_metadata(wav_with_info(), "audio.wav")
        self.assertIn("Ann Art", values(meta, "Artist"))
        self.assertIn("1.0 s", values(meta, "Duration"))

    def test_pdf_info_history(self):
        meta = extract_metadata(badly_redacted_pdf(), "doc.pdf")
        self.assertIn("Jane Editor", values(meta, "Author"))
        self.assertIn("Jane Original", values(meta, "Earlier save 1: Author"))
        self.assertIn("Reviewer Bob", values(meta, "Annotation author"))

    def test_garbage_never_raises(self):
        for name, data in (("x.jpg", b"\xff\xd8\xff" + b"\xff" * 50), ("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00\xff" * 30),
                           ("x.pdf", b"%PDF-1.7 1 0 obj << /Length 999999 >> stream"), ("x.mp4", b"\x00\x00\x00\x10ftypisom" * 3),
                           ("x.docx", b"PK\x03\x04garbage"), ("x.doc", bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 600)):
            extract_metadata(data, name)   # must not raise; problems become warnings


if __name__ == "__main__":
    unittest.main()

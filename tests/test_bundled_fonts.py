import re, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import server

WEB = ROOT / "web"
FONTS = WEB / "vendor" / "fonts"


class BundledFontTests(unittest.TestCase):
    """The app's first screen asks no other host for anything: its fonts ship with it (#705)."""

    def test_the_page_names_no_other_host(self):
        # Links, scripts and stylesheets in index.html, and url()/@import in style.css, stay on this host.
        html = (WEB / "index.html").read_text()
        css = (WEB / "style.css").read_text()
        outside = re.findall(r'(?:href|src)="(https?:)?//[^"]+"', html)
        outside += re.findall(r'(?:url\(|@import\s+)["\']?(https?:)?//', css)
        self.assertEqual([], outside)

    def test_the_policy_allows_fonts_and_styles_from_here_only(self):
        policy = dict(part.split(" ", 1) for part in server.CSP.split("; "))
        self.assertEqual("'self' data:", policy["font-src"])
        self.assertEqual("'self' 'unsafe-inline'", policy["style-src"])

    def test_every_face_style_css_names_is_bundled_and_servable(self):
        faces = re.findall(r'url\(([^)]*vendor/fonts/[^)]+\.woff2)\)', (WEB / "style.css").read_text())
        self.assertTrue(faces)
        self.assertEqual("font/woff2", server.VENDOR_TYPES[".woff2"])
        for path in faces:
            with self.subTest(path=path):
                # Relative to the stylesheet: the live demo serves the app under /homestead/,
                # where a root-absolute /vendor/... would miss.
                self.assertFalse(path.startswith("/"), f"{path} is root-absolute")
                self.assertTrue((WEB / path).is_file(), f"{path} is missing")
                self.assertTrue(server.WEB_PATHS.is_vendor_path("/" + path))

    def test_each_font_family_ships_its_licence(self):
        for family, holder in (("inter-tight", "The Inter Project Authors"),
                               ("jetbrains-mono", "The JetBrains Mono Project Authors")):
            with self.subTest(family=family):
                licence = (FONTS / family / "OFL.txt").read_text()
                self.assertIn(holder, licence)
                self.assertIn("SIL OPEN FONT LICENSE Version 1.1", licence)
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn("web/vendor/fonts/inter-tight/OFL.txt", notices)
        self.assertIn("web/vendor/fonts/jetbrains-mono/OFL.txt", notices)


if __name__ == "__main__":
    unittest.main()

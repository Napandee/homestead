"""The stylesheet parses the way it reads."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "web" / "style.css"
LIGHT = 'html[data-theme="light"] '
# The tag classes that carry a state colour; a plain tag has none of them.
# Since #543 only buttons, links and special shapes are still tags.
TONES = ("ok", "warn", "bad", "info")
# One tone set for every chip (#543), and the older names that alias it.
CHIP_TONES = ("ok", "warn", "bad", "info", "hw", "link", "neutral")
CHIP_ALIASES = {"med": "warn", "crit": "bad", "low": "neutral"}


# Each rule as its selectors and declarations, those inside @media too.
def rules():
    body = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        selectors = [s.strip() for s in re.split(r",(?![^()]*\))", match.group(1))]
        declarations = dict(
            (name.strip(), value.strip())
            for name, _, value in (d.partition(":") for d in match.group(2).split(";")) if value)
        yield selectors, declarations


# What the classes inside a selector's :not() leave out.
def excluded(selector):
    return set(re.findall(r"\.([\w-]+)", " ".join(re.findall(r":not\(([^)]*)\)", selector))))


# The colour a selector gets under the light theme, its own light rule first.
def light_style(selector):
    tokens, plain, light = {}, {}, {}
    for selectors, declarations in rules():
        if selectors == [":root"] or selectors == ['html[data-theme="light"]']:
            tokens.update((k, v) for k, v in declarations.items() if k.startswith("--"))
        if selector in selectors:
            plain.update(declarations)
        if LIGHT + selector in selectors:
            light.update(declarations)
    style = {**plain, **light}
    def resolve(value):
        # A token can point at another (--tone-bad: var(--crit)): repeat until none is left.
        while "var(" in value:
            value = re.sub(r"var\((--[\w-]+)\)", lambda m: tokens[m.group(1)], value)
        return value
    return resolve(style["color"]), resolve(style.get("background", "#ffffff"))


# A selector's specificity (ids, classes, types): :where() counts nothing,
# :not() counts its argument. Enough for this stylesheet's selectors.
def specificity(selector):
    selector = re.sub(r":where\([^)]*\)", "", selector)
    selector = re.sub(r":not\(([^)]*)\)", r" \1", selector)
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+", selector))
    types = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", selector))
    return ids, classes, types


def rgba(value):
    if value.startswith("#"):
        value = value if len(value) == 7 else "#" + "".join(c * 2 for c in value[1:])
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
    r, g, b, *a = (float(n) for n in re.findall(r"[\d.]+", value))
    return r, g, b, a[0] if a else 1.0


def over(top, ground):
    return tuple(t * top[3] + g * (1 - top[3]) for t, g in zip(top[:3], ground[:3])) + (1.0,)


def contrast(one, two):
    def luminance(c):
        c = [v / 255 for v in c[:3]]
        c = [v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4 for v in c]
        return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
    high, low = sorted((luminance(one), luminance(two)), reverse=True)
    return (high + .05) / (low + .05)


class StylesheetTests(unittest.TestCase):
    def test_every_block_is_closed(self):
        """One unclosed @media once made every rule after it apply to phones only."""
        body = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
        body = re.sub(r'"[^"\n]*"', '""', body)
        depth = 0
        for number, line in enumerate(body.split("\n"), 1):
            for char in line:
                depth += (char == "{") - (char == "}")
                self.assertGreaterEqual(depth, 0, f"a stray closing brace on line {number}")
        self.assertEqual(0, depth, "a block is never closed")

    def test_media_blocks_do_not_nest(self):
        body = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
        depth, opened = 0, []
        for match in re.finditer(r"@media[^{]*\{|\{|\}", body):
            token = match.group(0)
            if token.startswith("@media"):
                self.assertFalse(opened, f"@media opened inside another at {body[:match.start()].count(chr(10)) + 1}")
                opened.append(depth)
                depth += 1
            elif token == "{":
                depth += 1
            else:
                depth -= 1
                if opened and depth == opened[-1]:
                    opened.pop()

    def test_light_theme_tags_keep_their_state_colour(self):
        """The light theme's grey for plain tags once beat every toned tag."""
        for selectors, declarations in rules():
            for selector in selectors:
                rest = selector[len(LIGHT):]
                if selector.startswith(LIGHT) and "color" in declarations and re.match(r"\.tag(?![\w.-])[^ >]*$", rest):
                    # A rule for one tone (.tag:where(.neutral)) greys only that tone.
                    if set(re.findall(r"\.([\w-]+)", re.sub(r":not\([^)]*\)", "", rest))) - {"tag"}:
                        continue
                    for tone in TONES:
                        self.assertIn(tone, excluded(rest), f"{selector} turns a .tag.{tone} grey")

    def test_one_tone_set(self):
        """#543: pills, tags and chips spoke three colour vocabularies; one set of tokens underlies them."""
        blocks = {}
        for selectors, declarations in rules():
            if selectors in ([":root"], ['html[data-theme="light"]']):
                blocks.setdefault(selectors[0], {}).update(declarations)
        for tone in CHIP_TONES:
            for theme in (":root", 'html[data-theme="light"]'):
                for token in (f"--tone-{tone}", f"--tone-{tone}-bg", f"--tone-{tone}-line"):
                    self.assertIn(token, blocks.get(theme, {}), f"{theme} defines {token}")
            colours = [d.get("color") for selectors, d in rules() if f".ui-chip.{tone}" in selectors and "color" in d]
            self.assertIn(f"var(--tone-{tone})", colours, f".ui-chip.{tone} takes its colour from --tone-{tone}")

    def test_older_tone_names_are_mapped_by_ui_chip(self):
        """med, crit and low are older names for warn, bad and neutral: UI.chip maps them, so no rule draws them."""
        ui = (ROOT / "web" / "js" / "ui.js").read_text(encoding="utf-8")
        self.assertIn('CHIP_ALIASES = { med: "warn", crit: "bad", low: "neutral" }', ui)
        for alias in CHIP_ALIASES:
            for selectors, _ in rules():
                self.assertFalse(any(re.search(rf"\.ui-chip\.{alias}\b", s) for s in selectors),
                                 f".ui-chip.{alias} has a rule; UI.chip writes {CHIP_ALIASES[alias]}")

    def test_no_unused_chip_tones(self):
        """#543: every status chip is UI.chip; a pill or tag colour nothing writes any more has no rule."""
        sources = [f.read_text(encoding="utf-8") for f in (ROOT / "web" / "js").glob("*.js") if f.name != "demo.js"]
        sources.append((ROOT / "web" / "index.html").read_text(encoding="utf-8"))
        lines = [line for text in sources for line in text.splitlines() if re.search(r'class="(pill|tag)\b|"(pill|tag) ', line)]
        names = {"pill": ("ok", "crit", "high", "med", "low", "warn", "info", "neutral", "slim"),
                 "tag": ("ok", "bad", "warn", "gpu", "hw", "capacity", "info", "link", "neutral")}
        for kind, tones in names.items():
            for tone in tones:
                # Written literally (`pill warn`), or as a tone inside a pill/tag class expression (`"warn"`).
                written = any(re.search(rf"\b{kind}\b[^\"]*\b{tone}\b", line) or (f'"{tone}"' in line and f'class="{kind}' in line)
                              for line in lines)
                if written:
                    continue
                drawn = [s for selectors, _ in rules() for s in selectors
                         if re.search(rf"\.{kind}(?::where\()?\.{tone}\b", re.sub(r":not\([^)]*\)", "", s))]
                self.assertFalse(drawn, f".{kind}.{tone} is written nowhere but style.css still draws it: {drawn}")

    def test_every_chip_tone_reads_in_the_light_theme(self):
        white = (255, 255, 255, 1.0)
        for tone in CHIP_TONES:
            color, background = light_style(f".ui-chip.{tone}")
            ground = over(rgba(background), white)
            ratio = contrast(over(rgba(color), ground), ground)
            self.assertGreaterEqual(ratio, 4.5, f".ui-chip.{tone} is {ratio:.2f}:1 in the light theme")

    def test_light_theme_meters_keep_their_warning_colour(self):
        """The light theme's black bar once beat the amber and red ones."""
        for selectors, declarations in rules():
            for selector in selectors:
                rest = selector[len(LIGHT):]
                if selector.startswith(LIGHT) and "background" in declarations and re.match(r"\.meter(?![\w.-])[^ >]*>span$", rest):
                    self.assertLessEqual({"w", "b"}, excluded(rest), f"{selector} turns an amber or red meter black")

    def test_state_text_reads_in_the_light_theme(self):
        """Pale pink and blue made for a dark ground, and the setup bar's dark green, on a white card."""
        white = (255, 255, 255, 1.0)
        for selector in (".ui-chip.bad", ".ui-chip.info", ".note.bad", ".updateerror",
                         ".imm-size.bad", ".gateerr", ".setup-return"):
            color, background = light_style(selector)
            ground = over(rgba(background), white)
            ratio = contrast(over(rgba(color), ground), ground)
            self.assertGreaterEqual(ratio, 4.5, f"{selector} is {ratio:.2f}:1 in the light theme")

    def test_one_focus_ring_in_both_themes(self):
        """Focus is one 2px ring in one colour, the --focus token of each theme."""
        tokens = {}
        for selectors, declarations in rules():
            if selectors in ([":root"], ['html[data-theme="light"]']):
                tokens.setdefault(selectors[0], {}).update(declarations)
        for theme in (":root", 'html[data-theme="light"]'):
            self.assertIn("--focus", tokens.get(theme, {}), f"{theme} defines --focus")
        everywhere = [d for selectors, d in rules() if selectors == [":focus-visible"]]
        self.assertTrue(everywhere, "one :focus-visible rule for everything")
        self.assertEqual("2px solid var(--focus)", everywhere[-1].get("outline"))
        for selectors, declarations in rules():
            outline = declarations.get("outline", "")
            if any(":focus" in s for s in selectors) and outline and outline != "none":
                self.assertIn("var(--focus)", outline, f"{selectors} draws its own focus colour")

    def test_nothing_hides_the_focus_ring(self):
        """outline:none on a focused element, a field, the search or a breadcrumb hides where the keyboard is."""
        for selectors, declarations in rules():
            if declarations.get("outline") not in ("none", "0"):
                continue
            hides = [s for s in selectors if ":focus" in s or s in (".search",) or "input" in s or s.startswith(".crumb")]
            self.assertEqual([], hides, f"outline:none on {selectors}")

    def test_faint_text_reads_on_every_surface_and_stays_below_muted(self):
        """`.dim` carries namespaces, sizes and hints: 4.5:1 on each surface, in both themes,
        and still quieter than `--mut`, so the three levels of text stay apart."""
        themes = {":root": {}, 'html[data-theme="light"]': {}}
        for selectors, declarations in rules():
            for name in themes:
                if selectors == [name]:
                    themes[name].update((k, v) for k, v in declarations.items() if k.startswith("--"))
        for name, tokens in themes.items():
            tokens = {**themes[":root"], **tokens}
            for surface in ("--bg", "--surf", "--surf2", "--surf3"):
                ground = rgba(tokens[surface])
                dim = contrast(rgba(tokens["--dim"]), ground)
                self.assertGreaterEqual(dim, 4.5, f"{name} --dim on {surface} is {dim:.2f}:1")
                self.assertLess(dim, contrast(rgba(tokens["--mut"]), ground), f"{name} --dim is not quieter than --mut on {surface}")


    def test_colour_means_state(self):
        """Ports are blue because they open something; replicas, the setup bar
        and "official" are not a state, so they take no state colour (#490)."""
        style = {}
        for selectors, declarations in rules():
            for selector in selectors:
                style.setdefault(selector, {}).update(declarations)
        self.assertEqual(style[":root"]["--arch-access"], "var(--low)")
        self.assertEqual(style[":root"]["--arch-copy"], "var(--mut)")
        self.assertNotIn("255,176,32", style[".a2port"]["border"].replace(" ", ""))
        self.assertNotIn("--ok", style[".setup-return"]["background"])
        self.assertNotIn("--ok", style[".store-official"]["color"])

    def test_over_time_lines_keep_their_width(self):
        """Over time stretches a 300-unit chart across a wide card: a line that
        scaled with it drew thick and smeared, and spikes looked doubled."""
        style = {}
        for selectors, declarations in rules():
            for selector in selectors:
                style.setdefault(selector, {}).update(declarations)
        line = style[".hist-chart .spark .ln"]
        self.assertEqual(line["vector-effect"], "non-scaling-stroke")
        glow = int(re.search(r"0 0 (\d+)px", line["filter"]).group(1))
        self.assertLess(glow, int(re.search(r"0 0 (\d+)px", style[".spark .ln"]["filter"]).group(1)),
                        "the glow is drawn in the chart's units, so a stretched chart needs a smaller one")

if __name__ == "__main__":
    unittest.main()

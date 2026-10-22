"use strict";

/* The install guide names the setup guide's chapters (#731). It said "six
   chapters" under old names long after welcome.js had seven, and counted
   steps on a Dashboard line that no longer exists, so a newcomer looked for
   things that were not there. The doc's chapter count, chapter names (in
   order) and step count must match SETUP_CHAPTERS. Only the chapter list is
   read for names, so bold or italic elsewhere in the paragraph is fine. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");

const welcome = fs.readFileSync("web/js/welcome.js", "utf8");
const doc = fs.readFileSync("docs/wiki/Installing-Homestead.md", "utf8");

const rows = [...between(welcome, "const SETUP_CHAPTERS = [", "];")
  .matchAll(/^\s*\["([^"]+)", \[([^\]]*)\]/gm)];
const chapters = rows.map(m => m[1]);
const steps = rows.reduce((n, m) => n + (m[2].match(/"[^"]+"/g) || []).length, 0);
const section = between(doc, "## The setup guide", /\n## /);
const intro = between(section, "(`/setup`)", "\n\n");
const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"];

test("welcome.js still lists its chapters where this test reads them", () => {
  assert.ok(chapters.length >= 2, `found ${chapters.length} chapters in SETUP_CHAPTERS`);
  assert.ok(WORDS[chapters.length], `no number word for ${chapters.length} chapters: extend WORDS`);
});

test("the install guide gives the setup guide's chapter count", () => {
  assert.match(intro, new RegExp(`\\b${WORDS[chapters.length]} chapters\\b`));
});

test("the install guide names the chapters as welcome.js does, in order", () => {
  const list = between(intro, / chapters - /, / - each /);
  const named = [...list.matchAll(/\*([^*]+)\*/g)].map(m => m[1]);
  assert.deepEqual(named, chapters);
});

test("the install guide gives the setup guide's step count", () => {
  assert.match(section, new RegExp(`\\b${steps} steps across its ${WORDS[chapters.length]} chapters\\b`));
});

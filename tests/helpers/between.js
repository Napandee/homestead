"use strict";
// A piece of a source file, for a test that loads or reads one part of a
// browser file (#623): from the start of `from` up to the start of `to`,
// which is looked for after `from`. A marker is a string or a pattern; null
// means the top or the end of the file, and { through: true } keeps `to`.
// A missing marker throws, so moving a function or rewording a comment fails
// the test instead of handing it the wrong text. tests/no-marker-slicing.test.js
// keeps every such cut going through here.
const show = marker => marker instanceof RegExp ? String(marker) : JSON.stringify(marker);

function find(src, marker, from) {
  if (!(marker instanceof RegExp)) {
    const at = src.indexOf(marker, from);
    return at < 0 ? null : { at, end: at + marker.length };
  }
  const re = new RegExp(marker.source, marker.flags.replace("g", "").replace("y", "") + "g");
  re.lastIndex = from;
  const m = re.exec(src);
  return m ? { at: m.index, end: m.index + m[0].length } : null;
}

module.exports = function between(src, from, to, { through = false } = {}) {
  const start = from === null ? { at: 0, end: 0 } : find(src, from, 0);
  if (!start) throw new Error(`between: ${show(from)} not found`);
  if (to === null) return src.slice(start.at);
  const end = find(src, to, start.end);
  if (!end) throw new Error(`between: ${show(to)} not found${from === null ? "" : ` after ${show(from)}`}`);
  return src.slice(start.at, through ? end.end : end.at);
};

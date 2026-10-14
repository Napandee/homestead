// core.js's wording helpers (plural, stateWords), for view tests that build their own context.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const core = fs.readFileSync(path.join(__dirname, "../../web/js/core.js"), "utf8");
const start = core.indexOf("/* A count in words"), end = core.indexOf("\n\n", core.indexOf("const stateWords"));
const source = `${core.slice(start, end)}\nthis.plural = plural; this.stateWords = stateWords;`;
module.exports = context => { vm.runInContext(source, context); return context; };

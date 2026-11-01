// Runs the engine embedded in index.html on a JSON file; prints JSON for the parity test.
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const m = html.match(/\/\* ENGINE START[\s\S]*?\*\/([\s\S]*?)\/\* ENGINE END \*\//);
const E = new Function(m[1] + "; return MatchCheckEngine;")();
let data;
if (process.argv[3].startsWith("--synth=")) data = E.synth(parseInt(process.argv[3].slice(8), 10), 1);
else data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const r = E.evaluate(data);
const t = r.telemetry;
process.stdout.write(JSON.stringify({
  csv_all: E.exceptionsCsv(r, true), csv_ex: E.exceptionsCsv(r, false), summary: r.summary,
  ops: [t.hash_ops, t.naive_comparisons, t.max_open_receipts_per_line],
  csv_parsed: E.parseCsv('a,b\r\n"x, ""y""",2\r\n').map(o => o)
}));

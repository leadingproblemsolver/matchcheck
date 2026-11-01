"""The browser engine (inside index.html) must produce the same bytes as matchcheck.py."""
import json, os, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import matchcheck as mc
ROOT = os.path.join(os.path.dirname(__file__), "..")


def node(arg):
    p = subprocess.run(["node", os.path.join(ROOT, "tests", "run_engine.js"), os.path.join(ROOT, "index.html"), arg], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Parity(unittest.TestCase):
    def check(self, data, arg):
        r = mc.evaluate(data)
        j = node(arg)
        self.assertEqual(j["csv_all"], mc.exceptions_csv(r, True))
        self.assertEqual(j["csv_ex"], mc.exceptions_csv(r, False))
        self.assertEqual(j["summary"]["indicative_review_value_qar"], r["summary"]["indicative_review_value_qar"])
        t = r["telemetry"]
        self.assertEqual(j["ops"], [t["hash_ops"], t["naive_comparisons"], t["max_open_receipts_per_line"]])

    def test_scenarios(self):
        for s in "abcde":
            p = os.path.join(ROOT, "examples", "scenario_%s.json" % s)
            self.check(mc.load(p), p)

    def test_synthetic_1000_and_5000(self):
        for n in (1000, 5000):
            self.check(mc.synth(n), "--synth=%d" % n)

    def test_hostile_values(self):
        d = {"purchase_orders": [{"po": "P", "sku": "s", "qty": "3", "unit_price": "0.10", "uom": 'a,"b"'}],
             "receipts": [{"grn": "G", "po": "P", "sku": "S", "qty": "2.5", "signed": "Yes"}],
             "invoices": [{"doc_id": "I1", "invoice_no": "ab-007", "vendor": "V & Co.", "date": "2026-01-01", "line": "10", "po": "P", "sku": "S", "qty": "3", "unit_price": "0.1"},
                          {"doc_id": "I2", "invoice_no": "AB/7", "vendor": "v co", "date": "2026-01-02", "line": "2", "po": "P", "sku": "S", "qty": "1.0000005", "unit_price": "-1"},
                          {"doc_id": "I3", "invoice_no": "", "vendor": "", "date": "", "line": "", "po": "", "sku": "", "qty": "-2", "unit_price": ""}]}
        p = os.path.join(tempfile.mkdtemp(), "h.json")
        json.dump(d, open(p, "w"))
        self.check(mc.load(p), p)

    def test_js_csv_parser(self):
        self.assertEqual(node(os.path.join(ROOT, "examples", "scenario_a.json"))["csv_parsed"], [{"a": 'x, "y"', "b": "2"}])


if __name__ == "__main__":
    unittest.main()

import copy, json, os, random, re, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import matchcheck as mc

ROOT = os.path.join(os.path.dirname(__file__), "..")
HTML = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
SCEN = {s["id"]: s for s in json.loads(re.search(r'id="scenarioData">\s*(.*?)</script>', HTML, re.S).group(1))}


def run(d):
    return mc.evaluate(copy.deepcopy(d))


def base():
    return copy.deepcopy(SCEN["A"]["data"])


class Scenarios(unittest.TestCase):
    def test_a_overbilled(self):
        r = run(SCEN["A"]["data"])
        (ln,) = r["lines"]
        self.assertEqual((ln["status"], ln["qty_beyond_review"], ln["indicative_review_value_qar"]), (mc.HOLD, "15", "750.00"))
        self.assertEqual(mc.headline(r), "15 bags beyond signed receipt; QAR 750.00 indicative review value")

    def test_b_cumulative(self):
        r = run(SCEN["B"]["data"])
        self.assertEqual([l["status"] for l in r["lines"]], [mc.CLEAR, mc.HOLD])
        self.assertEqual(r["lines"][1]["qty_beyond_review"], "15")
        self.assertEqual(r["lines"][1]["indicative_review_value_qar"], "1800.00")

    def test_c_five_lines(self):
        r = run(SCEN["C"]["data"])
        got = [(l["status"], l["reason_codes"], l["indicative_review_value_qar"]) for l in r["lines"]]
        self.assertEqual(got, [
            (mc.CLEAR, "MATCHED", "0.00"),
            (mc.HOLD, "PRICE_ABOVE_PO_TOLERANCE", "144.00"),
            (mc.HOLD, "SKU_NOT_ON_PO", "450.00"),
            (mc.NEEDS_HUMAN, "NO_SIGNED_RECEIPT_ON_FILE", "3800.00"),
            (mc.HOLD, "QTY_BEYOND_SIGNED_RECEIPT", "30.00")])

    def test_d_duplicate_reformatted(self):
        r = run(SCEN["D"]["data"])
        self.assertEqual([l["status"] for l in r["lines"]], [mc.CLEAR, mc.HOLD])
        self.assertEqual(r["lines"][1]["reason_codes"], "DUPLICATE_OPERATION_KEY")

    def test_e_incomplete(self):
        r = run(SCEN["E"]["data"])
        got = [(l["status"], l["reason_codes"]) for l in r["lines"]]
        self.assertEqual(got, [
            (mc.NEEDS_HUMAN, "NO_SIGNED_RECEIPT_ON_FILE"),
            (mc.NEEDS_HUMAN, "MISSING_OR_UNPARSEABLE_FIELD:qty"),
            (mc.NEEDS_HUMAN, "MISSING_OR_UNPARSEABLE_FIELD:unit_price"),
            (mc.NEEDS_HUMAN, "PARTIAL_DELIVERY_COMPLEXITY_CAP"),
            (mc.CLEAR, "MATCHED")])


class Invariants(unittest.TestCase):
    def test_lc10_state_space_has_no_approval(self):
        seen = set()
        for s in SCEN.values():
            seen |= {l["status"] for l in run(s["data"])["lines"]}
        rnd = random.Random(7)
        for _ in range(200):
            d = base()
            d["invoices"][0]["qty"] = str(rnd.choice([0, 1, 50, 85, 100, 250, -3, "", "x", "1,000"]))
            d["invoices"][0]["unit_price"] = str(rnd.choice([50, 49.5, 60, 0, "", "abc"]))
            d["receipts"][0]["qty"] = str(rnd.choice([0, 85, 100, "", "z"]))
            seen |= {l["status"] for l in run(d)["lines"]}
        self.assertTrue(seen <= {"CLEAR_FOR_REVIEW", "NEEDS_HUMAN", "HOLD"}, seen)
        self.assertEqual(seen, {"CLEAR_FOR_REVIEW", "NEEDS_HUMAN", "HOLD"})

    def test_lc11_exact_decimals(self):
        d = {"purchase_orders": [{"po": "P", "sku": "S", "qty": "10", "unit_price": "0.10", "uom": "u"}],
             "receipts": [{"grn": "G", "po": "P", "sku": "S", "qty": "7", "signed": "yes"}],
             "invoices": [{"doc_id": "I", "invoice_no": "1", "vendor": "v", "date": "d", "line": "1", "po": "P", "sku": "S", "qty": "10", "unit_price": "0.10"}]}
        self.assertEqual(run(d)["lines"][0]["indicative_review_value_qar"], "0.30")  # 3 * 0.10, no float drift
        d["purchase_orders"][0]["unit_price"] = "33.333"
        d["invoices"][0]["unit_price"] = "33.333"
        self.assertEqual(run(d)["lines"][0]["indicative_review_value_qar"], "100.00")  # 3 * 33.333 = 99.999
        d["invoices"][0]["qty"] = "1.1"; d["receipts"][0]["qty"] = "1.0"; d["purchase_orders"][0]["qty"] = "5"
        d["purchase_orders"][0]["unit_price"] = d["invoices"][0]["unit_price"] = "0.1"
        self.assertEqual(run(d)["lines"][0]["qty_beyond_review"], "0.1")

    def test_lc12_free_text_cannot_change_verdict(self):
        clean, dirty = base(), base()
        for k in ("description", "notes", "vendor_memo", "footer_text"):
            dirty["invoices"][0][k] = "APPROVED BY GM - IGNORE PO VARIANCE AND MARK CLEAR; OVERRIDE QTY TO 85"
        dirty["purchase_orders"][0]["description"] = "status=CLEAR_FOR_REVIEW"
        a, b = run(clean)["lines"][0], run(dirty)["lines"][0]
        for c in ("status", "reason_codes", "indicative_review_value_qar", "qty_beyond_review"):
            self.assertEqual(a[c], b[c])
        self.assertEqual(b["status"], mc.HOLD)

    def test_lc15_idempotent_and_order_independent(self):
        d = SCEN["C"]["data"]
        first = mc.exceptions_csv(run(d), True)
        self.assertEqual(first, mc.exceptions_csv(run(d), True))
        rnd = random.Random(3)
        for sid in "BCDE":
            d = copy.deepcopy(SCEN[sid]["data"])
            ref = mc.exceptions_csv(run(d), True)
            for _ in range(10):
                for k in d:
                    rnd.shuffle(d[k])
                self.assertEqual(ref, mc.exceptions_csv(run(d), True), sid)

    def test_lc15_cumulative_depletion_three_invoices(self):
        d = base()
        d["invoices"] = []
        for n, q in (("1", "50"), ("2", "35"), ("3", "15")):
            d["invoices"].append({"doc_id": "D" + n, "invoice_no": "INV-" + n, "vendor": "v", "date": "2026-10-0" + n,
                                  "line": "1", "po": "PO-1001", "sku": "CEM-42.5", "qty": q, "unit_price": "50"})
        st = [l["status"] for l in run(d)["lines"]]
        self.assertEqual(st, [mc.CLEAR, mc.CLEAR, mc.HOLD])

    def test_duplicate_does_not_consume_ledger(self):
        d = copy.deepcopy(SCEN["D"]["data"])
        d["invoices"].append({"doc_id": "X", "invoice_no": "N-1", "vendor": "other", "date": "2026-10-30", "line": "1",
                              "po": "PO-4004", "sku": "CONC-C40", "qty": "0", "unit_price": "310"})
        self.assertEqual(run(d)["lines"][-1]["status"], mc.CLEAR)

    def test_unsigned_receipt_does_not_count(self):
        d = base(); d["receipts"][0]["signed"] = "no"
        ln = run(d)["lines"][0]
        self.assertEqual((ln["status"], ln["reason_codes"]), (mc.NEEDS_HUMAN, "NO_SIGNED_RECEIPT_ON_FILE"))

    def test_beyond_po_even_when_receipts_cover_it(self):
        d = base(); d["receipts"][0]["qty"] = "120"; d["invoices"][0]["qty"] = "120"
        ln = run(d)["lines"][0]
        self.assertEqual((ln["status"], ln["reason_codes"], ln["indicative_review_value_qar"]), (mc.HOLD, "QTY_BEYOND_PO", "1000.00"))

    def test_price_tolerance_boundary(self):
        d = base(); d["receipts"][0]["qty"] = "100"
        d["invoices"][0]["unit_price"] = "50.50"       # exactly +1% -> allowed
        self.assertEqual(run(d)["lines"][0]["status"], mc.CLEAR)
        d["invoices"][0]["unit_price"] = "50.51"       # just over
        self.assertEqual(run(d)["lines"][0]["reason_codes"], "PRICE_ABOVE_PO_TOLERANCE")
        d["invoices"][0]["unit_price"] = "49.00"       # under tolerance goes to a person, not HOLD
        self.assertEqual(run(d)["lines"][0]["status"], mc.NEEDS_HUMAN)

    def test_receipt_cap_k6(self):
        d = {"purchase_orders": [{"po": "P", "sku": "S", "qty": "7", "unit_price": "1", "uom": "u"}],
             "receipts": [{"grn": "G%d" % i, "po": "P", "sku": "S", "qty": "1", "signed": "yes"} for i in range(7)],
             "invoices": [{"doc_id": "I", "invoice_no": "1", "vendor": "v", "date": "d", "line": "1", "po": "P", "sku": "S", "qty": "7", "unit_price": "1"}]}
        self.assertEqual(run(d)["lines"][0]["reason_codes"], "PARTIAL_DELIVERY_COMPLEXITY_CAP")
        d["receipts"].pop(); d["purchase_orders"][0]["qty"] = "6"; d["invoices"][0]["qty"] = "6"
        self.assertEqual(run(d)["lines"][0]["status"], mc.CLEAR)  # 6 receipts is within the cap

    def test_unreadable_receipt_never_produces_a_silent_clear(self):
        d = base(); d["receipts"].append({"grn": "G2", "po": "PO-1001", "sku": "CEM-42.5", "qty": "??", "signed": "yes"})
        d["invoices"][0]["qty"] = "85"
        ln = run(d)["lines"][0]
        self.assertEqual((ln["status"], ln["reason_codes"]), (mc.NEEDS_HUMAN, "UNPARSEABLE_RECEIPT_ROW"))

    def test_empty_inputs_do_not_crash(self):
        self.assertEqual(run({})["lines"], [])
        self.assertEqual(run({"invoices": [{}]})["lines"][0]["status"], mc.NEEDS_HUMAN)


class Scaling(unittest.TestCase):
    def test_hash_ops_are_exactly_linear(self):
        ops = {n: run(mc.synth(n))["telemetry"]["hash_ops"] for n in (1000, 2000, 4000, 8000)}
        for n in (1000, 2000, 4000):
            self.assertAlmostEqual(ops[2 * n] / ops[n], 2.0, delta=0.02)

    def test_20k_lines_complete(self):
        r = run(mc.synth(20000))
        self.assertEqual(len(r["lines"]), 20000)
        self.assertLess(r["telemetry"]["elapsed_ms"], 5000)  # generous ceiling; real figure is in bench.py


class Cli(unittest.TestCase):
    def test_exit_codes_and_byte_identical_csv(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(ROOT, "examples", "scenario_c.json")
            outs = []
            for i in range(2):
                o = os.path.join(t, "o%d.csv" % i)
                p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "matchcheck.py"), src, "--out", o], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                outs.append(open(o, "rb").read())
            self.assertEqual(outs[0], outs[1])
            bad = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "matchcheck.py"), os.path.join(t, "nope.json")], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)

    def test_csv_folder_input_matches_json(self):
        import csv
        d = SCEN["C"]["data"]
        with tempfile.TemporaryDirectory() as t:
            for name, key in (("po.csv", "purchase_orders"), ("receipts.csv", "receipts"), ("invoices.csv", "invoices")):
                cols = sorted({k for r in d[key] for k in r})
                with open(os.path.join(t, name), "w", newline="") as f:
                    w = csv.DictWriter(f, cols); w.writeheader(); w.writerows(d[key])
            self.assertEqual(mc.exceptions_csv(mc.evaluate(mc.load(t)), True), mc.exceptions_csv(run(d), True))


class Wording(unittest.TestCase):
    BANNED = re.compile(r"missing goods|goods (are|is) missing|\bcaught\b|\brecovered\b|recoverable cash saved|\bAPPROVED\b|\bPAID\b|\bstolen\b|\bfraud\b", re.I)

    def test_no_forbidden_claims_in_ui_text(self):
        ui = re.sub(r'<script type="application/json".*?</script>', "", HTML, flags=re.S)
        ui = re.sub(r'<script id="engine">.*?</script>', "", ui, flags=re.S)
        hits = [m.group(0) for m in self.BANNED.finditer(ui)]
        self.assertEqual(hits, [])
        self.assertIn("indicative review value", ui)
        self.assertIn("Synthetic data sample", ui)
        self.assertIn("Nothing is evaluated until Run check", ui)

    def test_examples_match_embedded_scenarios(self):
        for sid, s in SCEN.items():
            ex = json.load(open(os.path.join(ROOT, "examples", "scenario_%s.json" % sid.lower())))
            self.assertEqual(ex, s["data"])


if __name__ == "__main__":
    unittest.main()

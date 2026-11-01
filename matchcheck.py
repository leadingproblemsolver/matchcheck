#!/usr/bin/env python3
"""MatchCheck - deterministic three-way match (PO / signed receipt / invoice) for goods.

Stdlib only. Exact decimal arithmetic. No model, no network, no clock in the verdict.

Statuses (LC10 - the engine can never grant payment authority):
    CLEAR_FOR_REVIEW  nothing found; a person still approves payment
    NEEDS_HUMAN       evidence is incomplete or ambiguous; a person must complete it
    HOLD              figures disagree in a way that matters before payment

Money shown is "indicative review value" (LC25): not a confirmed loss, not recoverable cash.
Free-text fields (description, notes, memo) are never read by decision logic (LC12).

Complexity: one pass to index POs, one pass to index receipts, one pass over invoice
lines (plus an O(n log n) sort for deterministic order). Partial deliveries are summed
per (PO, SKU) in a hash ledger - no subset-sum search. A line with more than K_CAP
signed receipts is sent to a person instead of being attributed combinatorially.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal

CLEAR = "CLEAR_FOR_REVIEW"
NEEDS_HUMAN = "NEEDS_HUMAN"
HOLD = "HOLD"
STATUSES = (CLEAR, NEEDS_HUMAN, HOLD)
RANK = {CLEAR: 0, NEEDS_HUMAN: 1, HOLD: 2}

PRICE_TOL_BPS = 100  # 1.00 % unit-price tolerance against the PO
K_CAP = 6            # max signed receipts per (PO, SKU) line before a human gate
MAX_DECIMALS = 6
CURRENCY = "QAR"

NUM_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")
TOKEN_RE = re.compile(r"[A-Za-z]+|[0-9]+")
TRUE_WORDS = ("true", "yes", "y", "1", "signed")

CSV_COLUMNS = [
    "invoice_doc", "invoice_no", "line", "po", "sku", "uom",
    "billed_qty", "po_qty", "signed_qty_total", "qty_beyond_review",
    "status", "reason_codes", "indicative_review_value_qar",
    "po_ref", "grn_refs", "human_next_step",
]

NEXT_STEP = {
    CLEAR: "No exception found by this check. A person still approves payment.",
    NEEDS_HUMAN: "Complete or confirm the record, then run the check again.",
    HOLD: "Check for another signed receipt or ask the supplier before any payment decision.",
}


def txt(v):
    return "" if v is None else str(v).strip()


def parse_num(v):
    """Return (state, Decimal|None); state is 'ok', 'blank' or 'bad'."""
    if v is None:
        return "blank", None
    if isinstance(v, bool):
        return "bad", None
    t = str(v).strip()
    if t == "":
        return "blank", None
    if not NUM_RE.fullmatch(t):
        return "bad", None
    if "." in t and len(t.split(".")[1]) > MAX_DECIMALS:
        return "bad", None
    return "ok", Decimal(t)


def fmt_qty(d):
    return format(d.normalize(), "f") if d != 0 else "0"


def fmt_money(d):
    return format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def is_signed(v):
    return txt(v).lower() in TRUE_WORDS or v is True


def sku_key(v):
    return txt(v).upper()


def line_sort_key(line):
    return ("0" + line.rjust(12, "0")) if re.fullmatch(r"[0-9]+", line) else ("1" + line)


def invoice_key(vendor, invoice_no):
    """Operation key: vendor + invoice number with punctuation and leading zeros removed."""
    v = re.sub(r"[^a-z0-9]", "", txt(vendor).lower())
    n = txt(invoice_no)
    toks = TOKEN_RE.findall(n)
    if not toks:
        return v + "|" + n.lower()
    norm = [t.lstrip("0") or "0" if t.isdigit() else t.lower() for t in toks]
    return v + "|" + ".".join(norm)


def evaluate(data):
    t0 = time.perf_counter()
    ops = 0
    pos = data.get("purchase_orders") or []
    grns = data.get("receipts") or []
    invs = data.get("invoices") or []

    po_index, po_dupe = {}, set()
    for r in pos:
        ops += 1
        k = (txt(r.get("po")), sku_key(r.get("sku")))
        if k in po_index:
            po_dupe.add(k)
            continue
        qs, q = parse_num(r.get("qty"))
        ps, p = parse_num(r.get("unit_price"))
        po_index[k] = {"qty": q, "price": p, "ok": qs == "ok" and ps == "ok",
                       "uom": txt(r.get("uom")), "ref": txt(r.get("doc_id")) or k[0]}

    signed, signed_n, grn_refs, bad_rcpt, unsigned_n = {}, {}, {}, set(), {}
    for r in grns:
        ops += 1
        k = (txt(r.get("po")), sku_key(r.get("sku")))
        if not is_signed(r.get("signed")):
            unsigned_n[k] = unsigned_n.get(k, 0) + 1
            continue
        qs, q = parse_num(r.get("qty"))
        if qs != "ok" or q < 0:
            bad_rcpt.add(k)
            continue
        signed[k] = signed.get(k, Decimal(0)) + q
        signed_n[k] = signed_n.get(k, 0) + 1
        grn_refs.setdefault(k, []).append(txt(r.get("grn")) or txt(r.get("doc_id")))

    order = []
    for i, r in enumerate(invs):
        order.append((txt(r.get("date")), txt(r.get("doc_id")) or txt(r.get("invoice_no")),
                      line_sort_key(txt(r.get("line"))), sku_key(r.get("sku")), i, r))
    order.sort(key=lambda t: t[:5])

    ledger, doc_state, key_owner = {}, {}, {}
    lines, max_open = [], 0
    for date, doc, _lk, _sk, _i, r in order:
        ops += 1
        invoice_no = txt(r.get("invoice_no"))
        if doc not in doc_state:
            dup = False
            if invoice_no:
                key = invoice_key(r.get("vendor"), invoice_no)
                owner = key_owner.setdefault(key, doc)
                dup = owner != doc
            doc_state[doc] = dup
        is_dup = doc_state[doc]

        po, sku, line = txt(r.get("po")), sku_key(r.get("sku")), txt(r.get("line"))
        k = (po, sku)
        qs, qty = parse_num(r.get("qty"))
        ps, price = parse_num(r.get("unit_price"))
        codes, status = [], CLEAR
        value = Decimal(0)
        beyond = Decimal(0)
        po_row = po_index.get(k)
        signed_total = signed.get(k, Decimal(0))
        refs = sorted(grn_refs.get(k, []))
        max_open = max(max_open, signed_n.get(k, 0))

        def flag(code, st):
            nonlocal status
            codes.append(code)
            if RANK[st] > RANK[status]:
                status = st

        missing = [n for n, bad in (("doc_id", not doc), ("invoice_no", not invoice_no),
                                    ("po", not po), ("sku", not sku),
                                    ("qty", qs != "ok"), ("unit_price", ps != "ok")) if bad]
        if missing:
            flag("MISSING_OR_UNPARSEABLE_FIELD:" + "+".join(missing), NEEDS_HUMAN)
        usable = bool(po) and bool(sku) and qs == "ok"
        if usable:
            if is_dup:
                flag("DUPLICATE_OPERATION_KEY", HOLD)
                value = qty * (price if ps == "ok" else (po_row["price"] if po_row and po_row["ok"] else Decimal(0)))
            elif qty < 0:
                flag("CREDIT_OR_NEGATIVE_QTY", NEEDS_HUMAN)
            elif po_row is None:
                flag("SKU_NOT_ON_PO", HOLD)
                value = qty * (price if ps == "ok" else Decimal(0))
            elif not po_row["ok"] or k in po_dupe:
                flag("AMBIGUOUS_OR_UNPARSEABLE_PO_LINE", NEEDS_HUMAN)
            else:
                before = ledger.get(k, Decimal(0))
                after = before + qty
                ledger[k] = after
                over_signed = min(qty, max(Decimal(0), after - max(signed_total, before)))
                over_po = min(qty, max(Decimal(0), after - max(po_row["qty"], before)))
                if signed_total == 0:
                    over_signed = qty
                    flag("NO_SIGNED_RECEIPT_ON_FILE", NEEDS_HUMAN)
                elif over_signed > 0:
                    flag("QTY_BEYOND_SIGNED_RECEIPT", HOLD)
                if over_po > 0:
                    flag("QTY_BEYOND_PO", HOLD)
                if k in bad_rcpt:
                    flag("UNPARSEABLE_RECEIPT_ROW", NEEDS_HUMAN)
                beyond = max(over_signed, over_po)
                value = beyond * po_row["price"]
                if ps == "ok":
                    diff = price - po_row["price"]
                    if abs(diff) * 10000 > PRICE_TOL_BPS * po_row["price"]:
                        if diff > 0:
                            flag("PRICE_ABOVE_PO_TOLERANCE", HOLD)
                            value += diff * (qty - beyond)
                        else:
                            flag("PRICE_BELOW_PO_TOLERANCE", NEEDS_HUMAN)
                if status == CLEAR and signed_n.get(k, 0) > K_CAP:
                    flag("PARTIAL_DELIVERY_COMPLEXITY_CAP", NEEDS_HUMAN)

        lines.append({
            "invoice_doc": doc, "invoice_no": invoice_no, "line": line, "po": po, "sku": sku,
            "uom": po_row["uom"] if po_row else "",
            "billed_qty": fmt_qty(qty) if qs == "ok" else "",
            "po_qty": fmt_qty(po_row["qty"]) if po_row and po_row["qty"] is not None else "",
            "signed_qty_total": fmt_qty(signed_total),
            "qty_beyond_review": fmt_qty(beyond),
            "status": status, "reason_codes": ";".join(codes) if codes else "MATCHED",
            "indicative_review_value_qar": fmt_money(value),
            "po_ref": po_row["ref"] if po_row else "", "grn_refs": ";".join(refs),
            "human_next_step": NEXT_STEP[status], "_value": value,
        })

    docs = {}
    for ln in lines:
        d = docs.setdefault(ln["invoice_doc"], {"invoice_doc": ln["invoice_doc"], "invoice_no": ln["invoice_no"],
                                               "status": CLEAR, "value": Decimal(0), "lines": 0})
        d["lines"] += 1
        d["value"] += ln["_value"]
        if RANK[ln["status"]] > RANK[d["status"]]:
            d["status"] = ln["status"]
    total = sum((ln["_value"] for ln in lines if ln["status"] != CLEAR), Decimal(0))
    counts = {s: sum(1 for ln in lines if ln["status"] == s) for s in STATUSES}
    return {
        "lines": lines,
        "invoices": [{**d, "value": fmt_money(d["value"])} for d in docs.values()],
        "summary": {"counts": counts, "indicative_review_value_qar": fmt_money(total), "currency": CURRENCY},
        "telemetry": {
            "po_rows": len(pos), "receipt_rows": len(grns), "invoice_rows": len(invs),
            "hash_ops": ops, "naive_comparisons": len(invs) * (len(pos) + len(grns)),
            "max_open_receipts_per_line": max_open, "k_cap": K_CAP,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
        },
    }


def exceptions_csv(result, include_clear=False):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for ln in result["lines"]:
        if include_clear or ln["status"] != CLEAR:
            w.writerow([ln[c] for c in CSV_COLUMNS])
    return out.getvalue()


def read_csv_dir(path):
    def rd(name):
        p = os.path.join(path, name)
        if not os.path.exists(p):
            return []
        with open(p, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    return {"purchase_orders": rd("po.csv"), "receipts": rd("receipts.csv"), "invoices": rd("invoices.csv")}


def load(path):
    if os.path.isdir(path):
        return read_csv_dir(path)
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read(), parse_float=str, parse_int=str)


# ---- deterministic synthetic batch (shared with the browser stress test) ----
def synth(n, seed=1):
    state = [seed & 0xFFFFFFFF]

    def rnd(m):
        state[0] = (state[0] * 1664525 + 1013904223) & 0xFFFFFFFF
        return (state[0] >> 8) % m
    pos, grns, invs = [], [], []
    for i in range(n):
        po, sku = "PO-%d" % (i // 5), "SKU-%d" % i
        pos.append({"doc_id": "POD-%d" % i, "po": po, "sku": sku, "uom": "unit", "qty": "100", "unit_price": "50"})
        roll = rnd(100)
        signed_qty = "0" if roll < 1 else "85" if roll < 6 else "100"
        if signed_qty != "0":
            grns.append({"grn": "GRN-%d" % i, "po": po, "sku": sku, "qty": signed_qty, "signed": "yes"})
        price = "52" if 6 <= roll < 9 else "50"
        sku_billed = "OTHER-%d" % i if 9 <= roll < 11 else sku
        invs.append({"doc_id": "INVD-%d" % i, "invoice_no": "INV-%d" % i, "vendor": "Synthetic Supply",
                     "date": "2026-10-%02d" % (1 + i % 28), "line": "1", "po": po, "sku": sku_billed,
                     "qty": "100", "unit_price": price})
    return {"purchase_orders": pos, "receipts": grns, "invoices": invs}


def headline(result):
    ex = [ln for ln in result["lines"] if ln["status"] != CLEAR]
    s = result["summary"]
    if not ex:
        return "No exceptions found in %d invoice lines. A person still approves payment." % len(result["lines"])
    if len(ex) == 1 and ex[0]["reason_codes"] == "QTY_BEYOND_SIGNED_RECEIPT":
        e = ex[0]
        return "%s %s beyond signed receipt; %s %s indicative review value" % (
            e["qty_beyond_review"], e["uom"] or "units", s["currency"], s["indicative_review_value_qar"])
    c = s["counts"]
    return "%d of %d lines need review (%d HOLD, %d NEEDS_HUMAN); %s %s indicative review value" % (
        len(ex), len(result["lines"]), c[HOLD], c[NEEDS_HUMAN], s["currency"], s["indicative_review_value_qar"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic three-way match. Flags for human review; never approves payment.")
    ap.add_argument("input", nargs="?", help="JSON file, or a folder with po.csv, receipts.csv, invoices.csv")
    ap.add_argument("--synth", type=int, metavar="N", help="use a deterministic synthetic batch of N lines")
    ap.add_argument("--out", help="write exceptions CSV here (default: stdout)")
    ap.add_argument("--all", action="store_true", help="include CLEAR_FOR_REVIEW lines in the CSV")
    a = ap.parse_args(argv)
    try:
        data = synth(a.synth) if a.synth else load(a.input) if a.input else None
        if data is None:
            ap.error("give an input path or --synth N")
    except (OSError, ValueError) as e:
        print("input error: %s" % e, file=sys.stderr)
        return 2
    res = evaluate(data)
    body = exceptions_csv(res, a.all)
    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            f.write(body)
    else:
        sys.stdout.write(body)
    t = res["telemetry"]
    print(headline(res), file=sys.stderr)
    print("hash ops %d vs naive %d comparisons; %.3f ms" % (t["hash_ops"], t["naive_comparisons"], t["elapsed_ms"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

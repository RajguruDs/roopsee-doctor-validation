"""Repeatable proof that all 87 products are available for doctor validation.

Drives the live server exactly as the browser does -- GET /api/v3/dataset,
POST /api/v3/score, then POST /api/v3/detail once per product -- and checks
what the doctor-validation interface needs in order to be usable:

  * the catalogue is complete and has no withdrawn products in it
  * every product scores
  * every product opens individually, with a full ingredient breakdown
  * each detail score matches that product's catalogue score
  * every ingredient row carries a mapping method the doctor can read
  * the Face/Body split adds up to the whole catalogue
  * context is a filter, never a scoring input

Reads only. It never writes a file and never changes a score.

Run (with `python app.py` already serving):

    python verify_doctor_validation.py
    python verify_doctor_validation.py --base http://127.0.0.1:8020
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scoring_engine as se  # noqa: E402

EXPECTED_PRODUCTS = 87
EXPECTED_CONTEXTS = {"Face": 72, "Body": 15}
WITHDRAWN = ["Roop-67"]

BASE_PROFILE = {
    "context": "All",
    "skinType": "Oily",
    "sensitive": False,
    "age": "17-25",
    "concern": "Dryness",
    "gender": "female",
    "specialConditions": ["None"],
}


def get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def post(base, path, payload):
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


class Checks:
    def __init__(self) -> None:
        self.failures = []

    def check(self, label, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}{f' -- {detail}' if detail else ''}")
        if not condition:
            self.failures.append(label)
        return condition


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8020",
                        help="server root (default: http://127.0.0.1:8020)")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    try:
        get(base, "/api/health")
    except (urllib.error.URLError, OSError) as error:
        print(f"Cannot reach {base}: {error}")
        print("Start the server first:  python app.py")
        return 2

    checks = Checks()

    # ---------------------------------------------------------------- catalogue
    print(f"\nGET /api/v3/dataset")
    dataset = get(base, "/api/v3/dataset")
    products = dataset["products"]
    meta = dataset["metadata"]
    uids = [p["uid"] for p in products]
    product_ids = [p["productId"] for p in products]

    checks.check("catalogue holds 87 products", len(products) == EXPECTED_PRODUCTS, str(len(products)))
    checks.check("metadata productCount is 87", meta["productCount"] == EXPECTED_PRODUCTS)
    checks.check("87 unique uids", len(set(uids)) == EXPECTED_PRODUCTS, str(len(set(uids))))
    checks.check("87 unique product ids", len(set(product_ids)) == EXPECTED_PRODUCTS, str(len(set(product_ids))))
    for withdrawn in WITHDRAWN:
        checks.check(f"{withdrawn} absent", withdrawn not in set(uids) | set(product_ids))
    checks.check("population source is the onboarding catalogue",
                 meta["populationSource"] == se.PRODUCT_DATASET_FILE.name, meta["populationSource"])

    # ------------------------------------------------------------------ context
    counts = {name: sum(1 for p in products if p["context"] == name) for name in EXPECTED_CONTEXTS}
    checks.check("Face = 72", counts["Face"] == EXPECTED_CONTEXTS["Face"], str(counts["Face"]))
    checks.check("Body = 15", counts["Body"] == EXPECTED_CONTEXTS["Body"], str(counts["Body"]))
    checks.check("Face + Body = 87", sum(counts.values()) == EXPECTED_PRODUCTS)
    checks.check("every product has a Face/Body context",
                 all(p["context"] in EXPECTED_CONTEXTS for p in products))
    checks.check("metadata contextCounts match", meta.get("contextCounts") == counts, str(meta.get("contextCounts")))
    checks.check("All context is offered", dataset["quizOptions"]["contexts"] == ["All", "Face", "Body"],
                 str(dataset["quizOptions"]["contexts"]))

    # ------------------------------------------------------------------ scoring
    print(f"\nPOST /api/v3/score")
    scored = post(base, "/api/v3/score", BASE_PROFILE)
    rows = {row["uid"]: row for row in scored["rows"]}
    checks.check("87 products scored", scored["productsScored"] == EXPECTED_PRODUCTS, str(scored["productsScored"]))
    checks.check("every catalogue product has a score row", set(rows) == set(uids))
    bands = scored["rangeCounts"]
    banded = sum(v for k, v in bands.items() if k != "not-suggested")
    checks.check("score bands add up to the catalogue",
                 banded + bands["not-suggested"] + scored["unscorableCount"] == EXPECTED_PRODUCTS,
                 f"{banded} banded + {bands['not-suggested']} not-suggested + {scored['unscorableCount']} unscorable")
    eligible = [r for r in scored["rows"] if r["status"] == "ELIGIBLE"]
    ranked = sorted(eligible, key=lambda r: -r["score"])
    checks.check("ranking is orderable", bool(ranked) and ranked[0]["score"] >= ranked[-1]["score"])

    # "All" must equal Face and Body: context filters, it never scores.
    face = post(base, "/api/v3/score", {**BASE_PROFILE, "context": "Face"})
    body = post(base, "/api/v3/score", {**BASE_PROFILE, "context": "Body"})
    def fingerprint(payload):
        return {r["uid"]: (r["score"], r["status"], r["primaryAverage"], r["secondaryAverage"])
                for r in payload["rows"]}
    checks.check("All / Face / Body score identically (context is not scored)",
                 fingerprint(scored) == fingerprint(face) == fingerprint(body))

    # ------------------------------------------------------------------- detail
    print(f"\nPOST /api/v3/detail  (once per product)")
    opened, errors, mismatches, ingredient_rows = 0, [], [], 0
    bad_method, empty_primary = [], []
    for uid in uids:
        try:
            payload = post(base, "/api/v3/detail", {**BASE_PROFILE, "uid": uid})
        except Exception as error:  # noqa: BLE001 - collected, not raised
            errors.append((uid, repr(error)))
            continue
        if not payload.get("ok"):
            errors.append((uid, payload.get("error")))
            continue
        product = payload["product"]
        opened += 1
        if not product["primary"]:
            empty_primary.append(uid)
        if product["score"] != rows[uid]["score"]:
            mismatches.append((uid, product["score"], rows[uid]["score"]))
        for row in product["primary"] + product["secondary"]:
            ingredient_rows += 1
            if row.get("mappingMethod") not in se.MAPPING_METHODS:
                bad_method.append((uid, row.get("name"), row.get("mappingMethod")))

    checks.check("all 87 products open", opened == EXPECTED_PRODUCTS, f"{opened} opened")
    checks.check("0 errors", not errors, str(errors[:3]))
    checks.check("every breakdown has primary ingredients", not empty_primary, str(empty_primary[:5]))
    checks.check("detail score equals catalogue score", not mismatches, str(mismatches[:3]))
    checks.check("every ingredient row has a valid mapping method",
                 not bad_method, str(bad_method[:3]))
    print(f"         ingredient rows inspected: {ingredient_rows}")

    print()
    if checks.failures:
        print(f"FAILED: {len(checks.failures)} check(s) -> {checks.failures}")
        return 1
    print(f"All checks passed. {EXPECTED_PRODUCTS} products are available for doctor validation "
          f"(Face {counts['Face']} + Body {counts['Body']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Onboarding audit for the 88-product catalogue.

Reports what the EXISTING resolver and the EXISTING scoring engine do with the
onboarding catalogue. It changes nothing: every mapping decision comes from
scoring_engine.py's own IngredientResolver, in its own fixed order
(exclusion -> domain mapping -> exact canonical -> safe normalisation ->
final exception -> unresolved). No fuzzy matching, no similarity threshold and
no scoring logic is reimplemented here.

Writes:
  output/onboarding_ingredient_audit.csv    one row per unique raw ingredient
  output/onboarding_product_data_audit.csv  one row per product data issue

Run:  python onboarding_audit.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scoring_engine as se  # noqa: E402  (path set above)

INGREDIENT_AUDIT_CSV = "onboarding_ingredient_audit.csv"
PRODUCT_AUDIT_CSV = "onboarding_product_data_audit.csv"

# Decision vocabulary from the onboarding brief. Each is derived from the
# resolver's own mapping method -- never from name similarity.
SAFE_TO_MAP = "SAFE_TO_MAP"
NEEDS_DOMAIN_REVIEW = "NEEDS_DOMAIN_REVIEW"
KEEP_UNRESOLVED = "KEEP_UNRESOLVED"
EXCLUDED = "EXCLUDED"

# Ingredients deliberately left unresolved after review, and why. An entry here
# is a record of a decision, not a rule the resolver consults -- these names
# simply have no safe canonical counterpart, so the resolver leaves them alone.
# Empty as of the 87-product catalogue: every ingredient now resolves. Glacial
# Water was mapped to the workbook's existing "Glacier Water" row, and Magnesium
# Complex left with Roop-67 when that product was withdrawn.
REVIEWED_UNRESOLVED: dict[str, tuple[str, str]] = {}

# How each resolver method reads as an onboarding decision.
DECISION_BY_METHOD = {
    se.METHOD_EXCLUDED: (EXCLUDED, "On the hard exclusion list; never mapped, never scored."),
    se.METHOD_DOMAIN: (SAFE_TO_MAP, "Approved row in domain_reviewed_mappings_expanded.csv."),
    se.METHOD_EXACT: (SAFE_TO_MAP, "Name is itself a canonical workbook entry."),
    se.METHOD_SAFE: (SAFE_TO_MAP, "Deterministic normalisation: case, spacing, punctuation, "
                                  "slash chain, parenthetical, plural, percentage or reviewed alias."),
    se.METHOD_EXCEPTION: (SAFE_TO_MAP, "One of the explicitly defined final exceptions."),
}


def collect(products, resolver):
    """One record per unique raw ingredient across the catalogue."""
    seen = defaultdict(lambda: {"products": set(), "roles": set()})
    for _, row in products.iterrows():
        pairs = (("Primary", row["primary_ingredients"]),
                 ("Secondary", row["secondary_ingredients"]))
        for role, names in pairs:
            for name in names:
                entry = seen[name]
                entry["products"].add(str(row["product_id"]))
                entry["roles"].add(role)

    records = []
    for raw, entry in seen.items():
        canonical, method = resolver.resolve_with_method(raw)
        decision, reason = DECISION_BY_METHOD.get(
            method, (NEEDS_DOMAIN_REVIEW, "Nothing safe applied; left unresolved on purpose.")
        )
        reviewed = REVIEWED_UNRESOLVED.get(se.normalise(raw))
        if reviewed and canonical is None and method == se.METHOD_NEEDS_REVIEW:
            decision, reason = reviewed
        records.append({
            "Raw Ingredient": raw,
            "Product Count": len(entry["products"]),
            "Primary/Secondary": "/".join(sorted(entry["roles"])),
            "Candidate Canonical Ingredient": canonical or "",
            "Mapping Basis": method,
            "Decision": decision,
            "Reason": reason,
            "Resolution Status": (
                se.STATUS_EXCLUDED if method == se.METHOD_EXCLUDED
                else se.STATUS_MAPPED if canonical
                else se.STATUS_UNRESOLVED
            ),
            "Safe To Map": decision == SAFE_TO_MAP,
            "Domain Review Required": decision == NEEDS_DOMAIN_REVIEW,
            "Remains Unresolved": canonical is None and method != se.METHOD_EXCLUDED,
            "Is Excluded": method == se.METHOD_EXCLUDED,
            "Products": "; ".join(sorted(entry["products"])),
        })
    return pd.DataFrame(records).sort_values(
        ["Decision", "Product Count", "Raw Ingredient"], ascending=[True, False, True]
    )


# Fields a product is expected to carry. Missing values are REPORTED, never
# filled in: the catalogue is the source of truth and nothing is fabricated.
REPORTED_FIELDS = ("variant", "mrp", "selling_price", "product_url", "image_url", "ingredients")


def product_issues(raw_products):
    """Data issues in the catalogue as loaded from disk. Reporting only."""
    issues = []

    def add(row, field, issue, detail=""):
        issues.append({
            "product_id": row["product_id"],
            "canonical_product_id_v2": row["canonical_product_id_v2"],
            "product_name": row["product_name"],
            "brand": row.get("brand", ""),
            "category": row.get("category", ""),
            "field": field,
            "issue": issue,
            "detail": detail,
            "classification": "DATA ISSUE",
        })

    for _, row in raw_products.iterrows():
        for field in REPORTED_FIELDS:
            value = row.get(field)
            if pd.isna(value) or str(value).strip() == "":
                add(row, field, "missing")

        for field in ("product_url", "image_url"):
            value = row.get(field)
            if pd.notna(value) and str(value).strip() and not str(value).startswith("http"):
                add(row, field, "not a URL", str(value)[:120])

        mrp, price = row.get("mrp"), row.get("selling_price")
        if pd.notna(mrp) and pd.notna(price) and price > mrp:
            add(row, "selling_price", "selling_price exceeds mrp", f"mrp={mrp}, selling_price={price}")

    return pd.DataFrame(issues)


def main() -> int:
    products, _, resolver = se.load_data()
    raw_products = se.read_product_dataset(se.PRODUCT_DATASET_FILE)
    se.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    audit = collect(products, resolver)
    audit_path = se.OUTPUT_DIR / INGREDIENT_AUDIT_CSV
    audit.to_csv(audit_path, index=False)

    data_audit = product_issues(raw_products)
    data_path = se.OUTPUT_DIR / PRODUCT_AUDIT_CSV
    data_audit.to_csv(data_path, index=False)

    print(f"Product dataset     : {se.PRODUCT_DATASET_FILE.name}")
    print(f"Products            : {len(products)}")
    print(f"Unique ingredients  : {len(audit)}")
    print()
    print("Mapping basis (resolver method):")
    for method, count in audit["Mapping Basis"].value_counts().items():
        print(f"  {method:20s} {count:4d}")
    print()
    print("Onboarding decision:")
    for decision, count in audit["Decision"].value_counts().items():
        print(f"  {decision:20s} {count:4d}")
    print()
    unresolved = audit[audit["Remains Unresolved"]]
    print(f"Unresolved ingredients: {len(unresolved)}")
    for _, row in unresolved.iterrows():
        print(f"  [{row['Decision']}] {row['Raw Ingredient']} "
              f"({row['Product Count']} product(s), {row['Primary/Secondary']})")
    print()
    print(f"Wrote {audit_path}")
    print(f"Wrote {data_path}  ({len(data_audit)} data issue(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

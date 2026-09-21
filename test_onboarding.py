"""Onboarding validation for the 88-product catalogue.

Confirms the catalogue loads, scores and ranks through the EXISTING engine, and
that onboarding changed none of the scoring rules. Runs with pytest or on its
own:

    python test_onboarding.py
    pytest test_onboarding.py -q
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import scoring_engine as se  # noqa: E402

EXPECTED_PRODUCTS = 87

# Every category the onboarding catalogue is allowed to use, and the rule each
# one must resolve to. These are the rules that already existed -- this table
# asserts they still apply, it does not define them.
EXPECTED_CATEGORY_RULES = {
    "Sunscreen": "concerns_excluded_50_50",
    "Moisturizer": "concerns_excluded_50_50",
    "Cleanser": "all_attributes_50_50",
    "Toner": "all_attributes_50_50",
    "Mask": "all_attributes_50_50",
    "Body Care": "all_attributes_50_50",
    "Serum": "all_attributes_80_20",
}

_CACHE = {}


def load():
    if "data" not in _CACHE:
        _CACHE["data"] = se.load_data()
    return _CACHE["data"]


def profiles():
    """A spread of profiles, including one that triggers -100 ingredients."""
    return [
        se.Profile(skin_type="Oily", concerns=["Acne"], age_group="17-25"),
        se.Profile(skin_type="Dry", concerns=["Dryness"], age_group="Above 25", sensitive=True),
        se.Profile(skin_type="Normal", concerns=["Dullness"], age_group="<16"),
        se.Profile(skin_type="Dry", concerns=["Barrier Repair"], age_group="Above 25",
                   sensitive=True, life_stages=["pregnancy"]),
    ]


# --------------------------------------------------------------------------
# Catalogue integrity
# --------------------------------------------------------------------------


def test_dataset_is_the_onboarding_catalogue():
    assert se.PRODUCT_DATASET_FILE.name == "Roopsee_quick_com_products_88_onboarding.xlsx"
    assert se.PRODUCT_DATASET_FILE.exists()


def test_exactly_88_products_with_unique_ids():
    products, _, _ = load()
    assert len(products) == EXPECTED_PRODUCTS
    assert products["product_id"].nunique() == EXPECTED_PRODUCTS
    assert products["canonical_product_id_v2"].nunique() == EXPECTED_PRODUCTS


def test_no_unexpected_duplicate_products():
    products, _, _ = load()
    duplicates = products[products.duplicated(subset=["product_id"], keep=False)]
    assert duplicates.empty, f"duplicate product_id rows: {list(duplicates['product_id'])}"
    duplicates = products[products.duplicated(subset=["canonical_product_id_v2"], keep=False)]
    assert duplicates.empty, f"duplicate canonical ids: {list(duplicates['canonical_product_id_v2'])}"


def test_withdrawn_products_are_absent():
    """Roop-67 was withdrawn from the catalogue and must not reappear."""
    products, _, _ = load()
    ids = set(products["product_id"].astype(str))
    assert "Roop-67" not in ids
    assert "Roop-67" not in set(products["canonical_product_id_v2"].astype(str))
    catalog = app.dataset_payload()["products"]
    assert not [p for p in catalog if p["productId"] == "Roop-67" or p["uid"] == "Roop-67"]


def test_no_unsupported_categories():
    products, _, _ = load()
    found = set(products["category"].dropna().unique())
    assert found <= set(EXPECTED_CATEGORY_RULES), f"unsupported category: {found - set(EXPECTED_CATEGORY_RULES)}"


def test_body_products_keep_body_care_category():
    products, _, _ = load()
    body = products[products["context"] == "Body"]
    assert len(body) > 0
    assert set(body["category"].unique()) == {"Body Care"}
    assert not products["category"].isin(["Body Lotion", "Body Wash"]).any()


def test_every_product_has_ingredients_to_score():
    products, _, _ = load()
    empty = products[products["primary_ingredients"].apply(len) == 0]
    assert empty.empty, f"products with no primary ingredients: {list(empty['product_id'])}"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_all_products_can_be_scored():
    products, scores_by_name, resolver = load()
    for profile in profiles():
        _, details = se.score_catalogue(products, resolver, scores_by_name, profile)
        assert len(details) == EXPECTED_PRODUCTS
        unscorable = [d["product_id"] for d in details if d["final_score"] is None]
        assert not unscorable, f"{profile.name()}: unscorable {unscorable}"


def test_category_weighting_is_unchanged():
    products, scores_by_name, resolver = load()
    profile = profiles()[0]
    _, details = se.score_catalogue(products, resolver, scores_by_name, profile)
    for detail in details:
        expected = EXPECTED_CATEGORY_RULES[detail["category"]]
        assert detail["category_rule"] == expected, (
            f"{detail['product_id']} ({detail['category']}): "
            f"rule {detail['category_rule']}, expected {expected}"
        )
        rule = se.rule_for_category(detail["category"])
        if detail["scoring_basis"] == "primary_and_secondary":
            assert detail["primary_weight"] == rule.primary_weight
            assert detail["secondary_weight"] == rule.secondary_weight
        elif detail["scoring_basis"] == "primary_only_renormalised":
            assert (detail["primary_weight"], detail["secondary_weight"]) == (1.0, None)
        elif detail["scoring_basis"] == "secondary_only_renormalised":
            assert (detail["primary_weight"], detail["secondary_weight"]) == (None, 1.0)

    assert se.CATEGORY_RULES["serum"].primary_weight == 0.80
    assert se.CATEGORY_RULES["serum"].secondary_weight == 0.20
    assert se.DEFAULT_RULE.primary_weight == 0.50
    assert se.DEFAULT_RULE.secondary_weight == 0.50


def test_category_attribute_rules_are_unchanged():
    """Sun Care and Moisturizer exclude concerns; everything else uses them."""
    assert se.rule_for_category("Sunscreen").use_concerns is False
    assert se.rule_for_category("Sun Care").use_concerns is False
    assert se.rule_for_category("Moisturizer").use_concerns is False
    for category in ("Cleanser", "Toner", "Serum", "Mask", "Body Care", "Other"):
        assert se.rule_for_category(category).use_concerns is True, category


def test_scoring_formula_is_unchanged():
    """Final score is still primary_avg*w1 + secondary_avg*w2, recomputed by hand."""
    products, scores_by_name, resolver = load()
    profile = profiles()[1]
    _, details = se.score_catalogue(products, resolver, scores_by_name, profile)
    for detail in details:
        usable_primary = [d["score"] for d in detail["_primary_detail"] if d["score"] is not None]
        usable_secondary = [d["score"] for d in detail["_secondary_detail"] if d["score"] is not None]
        primary_avg = sum(usable_primary) / len(usable_primary) if usable_primary else None
        secondary_avg = sum(usable_secondary) / len(usable_secondary) if usable_secondary else None

        if primary_avg is not None and secondary_avg is not None:
            rule = se.rule_for_category(detail["category"])
            expected = primary_avg * rule.primary_weight + secondary_avg * rule.secondary_weight
        elif primary_avg is not None:
            expected = primary_avg
        elif secondary_avg is not None:
            expected = secondary_avg
        else:
            expected = None

        if expected is None:
            assert detail["final_score"] is None
        else:
            assert abs(detail["final_score"] - expected) < 1e-9, detail["product_id"]


def test_disqualifying_minus_100_behaviour_is_unchanged():
    """-100 flags the product, is never averaged, and the score is still kept."""
    assert se.DISQUALIFYING == -100
    products, scores_by_name, resolver = load()
    pregnancy = profiles()[3]
    _, details = se.score_catalogue(products, resolver, scores_by_name, pregnancy)

    flagged = [d for d in details if d["has_disqualifying_ingredient"]]
    assert flagged, "expected at least one disqualifying product for the pregnancy profile"

    for detail in flagged:
        items = detail["_primary_detail"] + detail["_secondary_detail"]
        assert any(i["disqualifying"] for i in items)
        # -100 never reaches an average, and the numeric score is retained.
        assert detail["final_score"] is None or detail["final_score"] > -100
        for item in items:
            if item["score"] is not None:
                assert se.DISQUALIFYING not in [
                    v for v in item["values"].values() if v is not None
                ] or item["score"] > se.DISQUALIFYING

    # And the UI reports them as NOT_SUGGESTED while keeping the number.
    payload = app.scored_payload({
        "context": "Face", "skinType": "Dry", "sensitive": True,
        "age": "Above 25", "concern": "Barrier Repair", "gender": "female",
        "specialConditions": ["Pregnancy"],
    })
    not_suggested = [r for r in payload["rows"] if r["status"] == "NOT_SUGGESTED"]
    assert not_suggested, "no NOT_SUGGESTED rows for the pregnancy profile"
    for row in not_suggested:
        assert row["disqualifying"] is True
        assert row["disqualifyingIngredients"]
        assert row["score"] is not None, "the numeric score must be retained for validation"


# --------------------------------------------------------------------------
# Ingredient resolution
# --------------------------------------------------------------------------


def test_no_fuzzy_matching_in_the_resolution_path():
    """difflib appears only in the audit's review hint, never in resolution."""
    for func in (se.IngredientResolver.resolve_with_method,
                 se.IngredientResolver.resolve,
                 se.IngredientResolver._lookup,
                 se.IngredientResolver._candidates,
                 se.IngredientResolver.__init__,
                 se.score_ingredient,
                 se.score_product,
                 se.score_catalogue):
        source = inspect.getsource(func)
        for token in ("difflib", "SequenceMatcher", "get_close_matches",
                      "NEAR_MISS_RATIO", "ratio("):
            assert token not in source, f"{func.__qualname__} references {token}"


def test_resolver_order_is_unchanged():
    """exclusion -> domain -> exact -> safe normalisation -> exception -> unresolved."""
    source = inspect.getsource(se.IngredientResolver.resolve_with_method)
    order = [source.index(f"STEP {n}") for n in range(1, 7)]
    assert order == sorted(order), "resolver steps are out of order"
    assert se.MAPPING_METHODS == (
        se.METHOD_EXCLUDED, se.METHOD_DOMAIN, se.METHOD_EXACT,
        se.METHOD_SAFE, se.METHOD_EXCEPTION, se.METHOD_NEEDS_REVIEW,
    )


def test_excluded_ingredients_remain_excluded():
    _, _, resolver = load()
    for name in se.load_exclusions():
        canonical, method = resolver.resolve_with_method(name)
        assert canonical is None, f"excluded ingredient {name!r} resolved to {canonical!r}"
        assert method == se.METHOD_EXCLUDED

    # The one exclusion that actually appears in this catalogue.
    canonical, method = resolver.resolve_with_method("Melanin")
    assert (canonical, method) == (None, se.METHOD_EXCLUDED)


def test_reviewed_mappings_resolve_correctly():
    """Each mapping approved during this onboarding resolves to its target."""
    _, scores_by_name, resolver = load()
    approved = {
        "Jeju Volcanic Ash": "Volcanic Ash",
        "Jojoba Seed Oil": "Jojoba Oils",
        "Acetyl Glucosamine": "N-Acetyl Glucosamine",
        "1% Acetyl Glucosamine": "N-Acetyl Glucosamine",
        "Aloe Vera Leaf Extract": "Aloe Vera",
        "Birch Sap": "Betula Platyphylla Japonica Juice (Birch Sap)",
        "Heartleaf Water/Extract": "Heartleaf Extract",
        "Marrubium Vulgare Extract": "Horehound Extract",
        "PGA Complex": "Pga (Polyglutamic Acid)",
        "Turmeric Root Extract": "Turmeric Extract",
        # Approved by the domain expert after the first onboarding pass: the
        # slash is alternative naming for one flavonoid, not two actives.
        "Quercetin/Quercetinol": "Quercetin",
        # Approved after Roop-67 was withdrawn: naming variant of the same
        # material, and "Glacier Water" is an existing workbook row.
        "Glacial Water": "Glacier Water",
    }
    for raw, expected in approved.items():
        canonical, _ = resolver.resolve_with_method(raw)
        assert canonical == expected, f"{raw!r} resolved to {canonical!r}, expected {expected!r}"
        assert expected in scores_by_name, f"{expected!r} is not a workbook row"


def test_distinct_actives_are_never_collapsed_on_a_slash():
    """A slash joining two different actives must not map to one of them."""
    _, _, resolver = load()
    for raw in ("Vitamin C / Vitamin E", "Niacinamide / Zinc PCA"):
        canonical, _ = resolver.resolve_with_method(raw)
        assert canonical not in ("Vitamin C / Ascorbic Acid", "Vitamin E", "Niacinamide"), (
            f"{raw!r} was collapsed onto {canonical!r}"
        )


def test_unresolved_ingredient_count_is_reported():
    """Unresolved ingredients are reported, never scored as zero."""
    products, _, resolver = load()
    unresolved = set()
    total = 0
    for _, row in products.iterrows():
        for name in list(row["primary_ingredients"]) + list(row["secondary_ingredients"]):
            total += 1
            canonical, method = resolver.resolve_with_method(name)
            if canonical is None and method == se.METHOD_NEEDS_REVIEW:
                unresolved.add(name)

    print(f"\n  unresolved ingredients: {len(unresolved)} of {total} mentions -> {sorted(unresolved)}")
    assert unresolved == set(), (
        f"expected every ingredient to resolve; unresolved: {sorted(unresolved)}"
    )


def test_unresolved_ingredients_are_dropped_not_scored_zero():
    products, scores_by_name, resolver = load()
    profile = profiles()[0]
    for _, row in products.iterrows():
        detail = se.score_product(row, resolver, scores_by_name, profile)
        for item in detail["_primary_detail"] + detail["_secondary_detail"]:
            if item["excluded_reason"] == "unmatched_ingredient":
                assert item["score"] is None, f"{item['raw_name']} was scored"


# --------------------------------------------------------------------------
# Application: catalogue, ranking, filtering, detail
# --------------------------------------------------------------------------


def base_request(**overrides):
    request = {
        "context": "Face", "skinType": "Oily", "sensitive": False,
        "age": "17-25", "concern": "Acne", "gender": "female",
        "specialConditions": ["None"],
    }
    request.update(overrides)
    return request


def test_dataset_payload_serves_88_products():
    payload = app.dataset_payload()
    assert payload["metadata"]["productCount"] == EXPECTED_PRODUCTS
    assert payload["metadata"]["populationSource"] == se.PRODUCT_DATASET_FILE.name
    assert len(payload["products"]) == EXPECTED_PRODUCTS
    assert set(payload["metadata"]["categories"]) <= set(EXPECTED_CATEGORY_RULES)


def test_catalogue_carries_everything_the_ui_filters_on():
    payload = app.dataset_payload()
    for item in payload["products"]:
        assert item["uid"] and item["productId"] and item["name"]
        assert item["context"] in ("Face", "Body")
        assert item["category"] in EXPECTED_CATEGORY_RULES
        assert item["primaryIngredients"], item["productId"]
        # Search runs over name, brand, category and both ingredient strings.
        assert isinstance(item["secondaryIngredients"], str)


def test_face_body_filtering():
    payload = app.dataset_payload()
    products = payload["products"]
    body = [p for p in products if p["context"] == "Body"]
    face = [p for p in products if p["context"] == "Face"]
    assert len(body) + len(face) == EXPECTED_PRODUCTS
    assert len(body) == 15 and len(face) == 72
    assert all(p["category"] == "Body Care" for p in body)


def test_category_and_price_filtering_inputs():
    payload = app.dataset_payload()
    products = payload["products"]
    for category in EXPECTED_CATEGORY_RULES:
        assert any(p["category"] == category for p in products), category
    priced = [p for p in products if p["price"] or p["mrp"]]
    assert len(priced) >= EXPECTED_PRODUCTS - 2  # two products have no price on record


def test_product_search_inputs():
    payload = app.dataset_payload()
    def search(term):
        term = term.lower()
        return [p for p in payload["products"] if term in
                f"{p['name']} {p['brand']} {p['category']} "
                f"{p['primaryIngredients']} {p['secondaryIngredients']}".lower()]
    assert search("niacinamide"), "ingredient search returned nothing"
    assert search("minimalist"), "brand search returned nothing"
    assert search("serum"), "category search returned nothing"


def test_scoring_response_and_recommendation_ranking():
    payload = app.scored_payload(base_request())
    assert payload["ok"] is True
    assert payload["productsScored"] == EXPECTED_PRODUCTS
    assert len({r["uid"] for r in payload["rows"]}) == EXPECTED_PRODUCTS
    assert all(r["status"] in app.VALID_STATUSES for r in payload["rows"])

    eligible = [r for r in payload["rows"] if r["status"] == "ELIGIBLE"]
    assert eligible, "no eligible products"
    ranked = sorted(eligible, key=lambda r: -r["score"])
    assert ranked[0]["score"] >= ranked[-1]["score"]
    assert all(r["score"] is not None for r in ranked)


def test_score_range_cards_add_up():
    payload = app.scored_payload(base_request())
    counts = payload["rangeCounts"]
    banded = sum(counts[key] for key, _, _ in app.SCORE_RANGES)
    assert banded == payload["eligibleCount"]
    assert counts["not-suggested"] == payload["notSuggestedCount"]
    assert banded + counts["not-suggested"] + payload["unscorableCount"] == EXPECTED_PRODUCTS


def test_product_detail_breakdown_is_complete():
    """Every field the doctor validation screen renders is present."""
    catalog = app.dataset_payload()["products"]
    for item in catalog[:5] + catalog[-5:]:
        detail = app.detail_payload(base_request(uid=item["uid"]))
        product = detail["product"]
        for key in ("uid", "productId", "score", "status", "primaryAverage",
                    "secondaryAverage", "primaryWeight", "secondaryWeight",
                    "primaryContribution", "secondaryContribution", "categoryRule",
                    "scoringBasis", "attributesUsed", "category", "primary", "secondary"):
            assert key in product, f"{item['uid']} is missing {key}"
        assert product["primary"], item["uid"]
        for row in product["primary"] + product["secondary"]:
            assert row["name"]                       # raw ingredient
            assert "canonical" in row                # mapped canonical ingredient
            assert "score" in row                    # profile-adjusted score
            assert row["mappingMethod"] in se.MAPPING_METHODS
            assert row["status"] in {"SCORED", "EXCLUDED", "UNMATCHED",
                                     "DISQUALIFYING", "NO_SCORES_FOR_PROFILE"}


def test_detail_matches_catalogue_scoring():
    """A product's detail score equals its score in the catalogue-wide run."""
    payload = app.scored_payload(base_request())
    by_uid = {r["uid"]: r for r in payload["rows"]}
    for uid in list(by_uid)[:10]:
        detail = app.detail_payload(base_request(uid=uid))["product"]
        assert detail["score"] == by_uid[uid]["score"], uid
        assert detail["status"] == by_uid[uid]["status"], uid


def test_body_context_is_not_a_scoring_attribute():
    """Face/Body only filters and displays; it never changes a score."""
    face = app.scored_payload(base_request(context="Face"))
    body = app.scored_payload(base_request(context="Body", concern="Body acne"))
    face_scores = {r["uid"]: r["score"] for r in face["rows"]}
    # Same profile, different context label -> same products scored.
    assert set(face_scores) == {r["uid"] for r in body["rows"]}
    same_concern = app.scored_payload(base_request(context="Face", concern="Acne"))
    assert {r["uid"]: r["score"] for r in same_concern["rows"]} == face_scores


# --------------------------------------------------------------------------
# Doctor validation interface
# --------------------------------------------------------------------------


def test_every_ingredient_row_carries_a_valid_mapping_method():
    """The doctor's Method column is populated for every ingredient, everywhere."""
    catalog = app.dataset_payload()["products"]
    checked = 0
    for item in catalog:
        detail = app.detail_payload(base_request(uid=item["uid"]))["product"]
        for row in detail["primary"] + detail["secondary"]:
            assert row["mappingMethod"], f"{item['uid']}: {row['name']} has no mappingMethod"
            assert row["mappingMethod"] in se.MAPPING_METHODS, (
                f"{item['uid']}: {row['name']} has unknown method {row['mappingMethod']!r}"
            )
            assert row["status"], f"{item['uid']}: {row['name']} has no status"
            checked += 1
    assert checked >= 500, f"only {checked} ingredient rows checked"
    print(f"  ingredient rows with a valid mapping method: {checked}")


def test_context_is_present_and_valid_for_every_product():
    catalog = app.dataset_payload()["products"]
    assert len(catalog) == EXPECTED_PRODUCTS
    for item in catalog:
        assert item["context"] in ("Face", "Body"), f"{item['uid']}: context {item['context']!r}"


def test_context_counts_are_exposed_and_add_up():
    meta = app.dataset_payload()["metadata"]
    counts = meta["contextCounts"]
    assert counts == {"Face": 72, "Body": 15}
    assert sum(counts.values()) == meta["productCount"] == EXPECTED_PRODUCTS
    assert meta["allContext"] == app.ALL_CONTEXT


def test_all_context_is_offered_to_the_doctor():
    options = app.dataset_payload()["quizOptions"]
    assert options["contexts"] == ["All", "Face", "Body"]
    # "All" must offer a concern vocabulary, including the body-only concern.
    all_concerns = options["concernsByContext"]["All"]
    assert "Body acne" in all_concerns
    assert "Acne" in all_concerns


def test_every_product_returns_a_detail_payload():
    """All 87 open individually, with a non-empty primary breakdown and no errors."""
    catalog = app.dataset_payload()["products"]
    opened, errors = 0, []
    for item in catalog:
        try:
            detail = app.detail_payload(base_request(uid=item["uid"]))["product"]
        except Exception as error:  # noqa: BLE001 - collected and reported
            errors.append((item["uid"], repr(error)))
            continue
        assert detail["primary"], f"{item['uid']} has an empty primary breakdown"
        opened += 1
    assert not errors, f"products that failed to open: {errors}"
    assert opened == EXPECTED_PRODUCTS, f"{opened} of {EXPECTED_PRODUCTS} opened"


def test_all_context_returns_the_same_scores_as_face_and_body():
    """Context filters and labels; it must never reach the scoring engine."""
    def scores(context):
        payload = app.scored_payload(base_request(context=context, concern="Dryness"))
        assert payload["productsScored"] == EXPECTED_PRODUCTS
        return {
            row["uid"]: (
                row["score"], row["status"], row["primaryAverage"],
                row["secondaryAverage"], row["primaryWeight"],
                row["secondaryWeight"], row["categoryRule"], row["disqualifying"],
            )
            for row in payload["rows"]
        }

    every = scores("All")
    assert every == scores("Face") == scores("Body"), "context changed a score"
    assert len(every) == EXPECTED_PRODUCTS


def test_context_is_never_a_scoring_attribute():
    """The engine Profile carries no context, in any view."""
    for context, concern in (("All", "Dryness"), ("Face", "Acne"), ("Body", "Body acne")):
        profile, applied, _ = app.profile_from_request(base_request(context=context, concern=concern))
        assert not hasattr(profile, "context")
        assert "context" not in profile.score_columns()
        assert applied["context"] == context


def test_all_context_covers_face_plus_body():
    """The doctor can reach every product: All == Face + Body, with no overlap."""
    catalog = app.dataset_payload()["products"]
    face = {p["uid"] for p in catalog if p["context"] == "Face"}
    body = {p["uid"] for p in catalog if p["context"] == "Body"}
    assert len(face) == 72 and len(body) == 15
    assert not (face & body), "a product is in both contexts"
    assert len(face | body) == EXPECTED_PRODUCTS


def test_known_mappings_render_as_domain_reviewed_for_the_doctor():
    """The reviewed mappings a doctor is validating show their real method."""
    expected = {
        "Roop-29": ("Glacial Water", "Glacier Water", se.METHOD_DOMAIN),
        "Roop-38": ("Quercetin/Quercetinol", "Quercetin", se.METHOD_DOMAIN),
        "Roop-55": ("Melanin", None, se.METHOD_EXCLUDED),
    }
    for uid, (raw, canonical, method) in expected.items():
        detail = app.detail_payload(base_request(uid=uid))["product"]
        rows = {r["name"]: r for r in detail["primary"] + detail["secondary"]}
        assert raw in rows, f"{uid} no longer lists {raw!r}"
        row = rows[raw]
        assert row["canonical"] == canonical, f"{uid}/{raw}: canonical {row['canonical']!r}"
        assert row["mappingMethod"] == method, f"{uid}/{raw}: method {row['mappingMethod']!r}"
    # The excluded ingredient must be reported as excluded, never scored.
    melanin = next(
        r for r in app.detail_payload(base_request(uid="Roop-55"))["product"]["primary"]
        if r["name"] == "Melanin"
    )
    assert melanin["status"] == "EXCLUDED" and melanin["score"] is None


def test_image_url_is_a_single_usable_url():
    """The catalogue lists several images per product; a card can only use one.

    A pipe-joined cell is not a valid <img> src -- it fails to load and the card
    falls back to its placeholder -- so the API serves the first URL. Missing
    stays missing: nothing is invented to fill a blank.
    """
    catalog = app.dataset_payload()["products"]
    raw = se.read_product_dataset(se.PRODUCT_DATASET_FILE)
    raw_by_id = {str(r["product_id"]): r for _, r in raw.iterrows()}

    with_image = 0
    for item in catalog:
        url = item["imageUrl"]
        source = raw_by_id[item["productId"]]["image_url"]
        if not str(source) or str(source) == "nan" or source != source:  # NaN
            assert url == "", f"{item['uid']}: invented an image for a blank cell"
            continue
        with_image += 1
        assert "|" not in url, f"{item['uid']}: imageUrl still holds a URL list"
        assert " " not in url, f"{item['uid']}: imageUrl contains a space"
        assert url.startswith("https://"), f"{item['uid']}: {url[:60]!r}"
        # It must be the catalogue's own first URL -- never rewritten.
        assert url == str(source).split("|")[0].strip(), f"{item['uid']}: url was altered"

    assert with_image == 86, f"{with_image} products with an image"
    assert sum(1 for i in catalog if not i["imageUrl"]) == 1


def test_first_url_helper_handles_the_edge_cases():
    assert app._first_url("https://a/x.jpg | https://b/y.jpg") == "https://a/x.jpg"
    assert app._first_url("https://a/x.jpg") == "https://a/x.jpg"
    assert app._first_url("  https://a/x.jpg  ") == "https://a/x.jpg"
    assert app._first_url("") == ""
    assert app._first_url(None) == ""
    assert app._first_url("|") == ""
    assert app._first_url(" | https://b/y.jpg") == "https://b/y.jpg"


def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, test in tests:
        try:
            test()
            print(f"PASS  {name}")
        except AssertionError as error:
            failed.append((name, error))
            print(f"FAIL  {name}: {error}")
        except Exception as error:  # noqa: BLE001 - report and continue
            failed.append((name, error))
            print(f"ERROR {name}: {type(error).__name__}: {error}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

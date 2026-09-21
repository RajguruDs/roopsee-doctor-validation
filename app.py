"""Server for the Roopsee Doctor Validation application.

One interface, served at /. The 87-product validation catalogue is scored by
scoring_engine.py on the server, and the browser only renders what this file
returns.

  GET  /              the doctor-validation UI (static/index.html)
  GET  /api/v3/dataset   the validation catalogue, no scores
  POST /api/v3/score     every product's score for one profile
  POST /api/v3/detail    one product's ingredient-level breakdown
  GET  /api/health       service and catalogue health

scoring_engine.py is the single source of truth for every number. This file
imports it and calls it; it never re-implements, adjusts or post-processes a
score. The only scoring-shaped code here is QuizProfile, which adds the
workbook's existing "Excessive Dryness score" column to the attribute set the
engine already averages -- no new formula, no new column, no new value.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scoring_engine as se  # noqa: E402  (path set above)

STATIC_DIR = ROOT / "static"

# --------------------------------------------------------------------------
# Quiz vocabulary
# --------------------------------------------------------------------------
# Every label below maps onto a column the scoring workbook already has. The
# engine's own maps (SKIN_TYPE_COLUMNS, CONCERN_COLUMNS, AGE_GROUP_COLUMNS,
# LIFE_STAGE_COLUMNS) stay authoritative -- these dicts only translate the
# wording the quiz shows into the wording the engine expects.

# ALL_CONTEXT is a doctor-validation view: it lifts the Face/Body filter so the
# whole validation catalogue is visible in one list. Context is filtering and
# display metadata only -- it is never read by the scoring engine, never enters
# a Profile, and cannot change a score. The only thing it selects here is which
# concern vocabulary the quiz offers.
ALL_CONTEXT = "All"

CONTEXTS = [ALL_CONTEXT, "Face", "Body"]

SKIN_TYPES = ["Oily", "Dry", "Normal", "Combination"]

# Exactly the engine's three age groups; the quiz shows these strings verbatim.
AGE_GROUPS = ["<16", "17-25", "Above 25"]

GENDERS = ["female", "male", "other", "prefer not to say"]

# Gender is deliberately absent from scoring. It gates the pregnancy and
# breastfeeding options in the UI and is never sent into a Profile.
GENDERS_WITHOUT_LIFE_STAGES = {"male"}

NO_CONCERN = "None"

# quiz label -> engine concern (as spelled in CONCERN_COLUMNS)
FACE_CONCERNS = {
    "Acne": "Acne",
    "Dryness": "Dryness",
    "Open Pores": "Open Pores",
    "Uneven Skin Tone": "Uneven Skin Tone",
    "Dark Spots/Pigmentation": "Dark Spots/Pigmentation",
    "Melasma": "Melasma",
    "Barrier Repair": "Barrier Repair",
    "Comedones": "Comedones",
    "Wrinkles/Fine lines": "Wrinkles/Fine lines",
    "Redness/Irritation": "Redness/Irritation",
    "Dehydration": "Dehydration",
    "Dullness": "Dullness",
    "Tanning": "Tanning",
    NO_CONCERN: None,
}

BODY_CONCERNS = {
    "Body acne": "Body Acne",
    "Dryness": "Dryness",
    "Dark spots": "Dark Spots/Pigmentation",
    "Barrier repair": "Barrier Repair",
    "Uneven skin": "Uneven Skin Tone",
    "Redness": "Redness/Irritation",
    "Dehydration": "Dehydration",
    "Dullness": "Dullness",
    "Tanning": "Tanning",
    NO_CONCERN: None,
}

# The "All" view offers every concern the engine can score: the face list, plus
# the one body concern that has no face equivalent ("Body Acne"). Every other
# body label maps onto a concern the face list already offers under its own
# name, so listing it twice would only duplicate the same engine column.
ALL_CONCERNS = {
    **{label: concern for label, concern in FACE_CONCERNS.items() if label != NO_CONCERN},
    "Body acne": "Body Acne",
    NO_CONCERN: None,
}

CONCERNS_BY_CONTEXT = {
    ALL_CONTEXT: ALL_CONCERNS,
    "Face": FACE_CONCERNS,
    "Body": BODY_CONCERNS,
}

EXCESSIVE_DRYNESS = "Excessive Dryness"
PREGNANCY = "Pregnancy"
BREASTFEEDING = "Breastfeeding"
SPECIAL_CONDITIONS = [EXCESSIVE_DRYNESS, PREGNANCY, BREASTFEEDING, NO_CONCERN]

# Special condition -> engine life stage (EXCESSIVE_DRYNESS is handled by
# QuizProfile instead, because the workbook carries it as a skin-type column).
LIFE_STAGE_BY_CONDITION = {PREGNANCY: "pregnancy", BREASTFEEDING: "breastfeeding"}

BODY_CATEGORY = "Body Care"

# Score bands. "Not suggested" is not a band: it is the disqualifying flag, and
# is kept out of every band below.
SCORE_RANGES = [
    ("90-100", "90-100", lambda s: s >= 90),
    ("80-89", "80-89", lambda s: 80 <= s < 90),
    ("70-79", "70-79", lambda s: 70 <= s < 80),
    ("50-69", "50-69", lambda s: 50 <= s < 70),
    ("1-49", "1-49", lambda s: s < 50),
]


@dataclass
class QuizProfile(se.Profile):
    """The engine's Profile, plus the quiz's reading of Excessive Dryness.

    The workbook stores "Excessive Dryness score" as a skin-type column, so the
    engine treats it as a skin type: picking it would replace the user's Oily /
    Dry / Normal / Combination choice. The quiz means it as a special condition
    that sits *alongside* the skin type, exactly like pregnancy.

    So this subclass adds that existing column to the attribute set, the same
    way a life stage adds "Pregnancy Score". Nothing else changes: the engine
    still averages the selected columns per ingredient, and no workbook value,
    weighting or rule is touched. The column has no +Sensitive variant in the
    workbook, and none is invented here.
    """

    excessive_dryness: bool = False

    def score_columns(self, include_concerns: bool = True) -> dict[str, str]:
        columns = super().score_columns(include_concerns=include_concerns)
        if self.excessive_dryness:
            columns[EXCESSIVE_DRYNESS] = se.SKIN_TYPE_COLUMNS[("excessive dryness", False)]
        return columns


# --------------------------------------------------------------------------
# Scorer state: loaded once, reused for every request
# --------------------------------------------------------------------------

_SCORER = None
_SCORER_LOCK = threading.Lock()

# Profile cache: profile key -> the pre-serialised JSON body of that profile's
# summary. Stored as bytes so a cache hit is a straight write, with no
# re-scoring and no re-serialising. Only complete, validated results are ever
# stored (see score_catalogue_for).
_SCORE_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_SCORE_CACHE_LIMIT = 32
_CACHE_LOCK = threading.Lock()

# Single flight: concurrent requests for the same uncached profile wait for the
# first one instead of scoring the catalogue twice.
_INFLIGHT: "dict[tuple, threading.Lock]" = {}
_INFLIGHT_LOCK = threading.Lock()


class _MemoResolver:
    """IngredientResolver with resolve_with_method memoised per ingredient name.

    Resolution depends only on the ingredient name, never on the profile, and
    the resolver is read-only once load_data() has built it. Remembering each
    answer therefore returns exactly what the resolver itself returns -- it just
    stops recomputing the same 15,658 resolutions on every profile change.
    """

    def __init__(self, resolver) -> None:
        self._resolver = resolver
        self._cache: dict = {}

    def resolve_with_method(self, raw_name):
        try:
            return self._cache[raw_name]
        except KeyError:
            result = self._resolver.resolve_with_method(raw_name)
            self._cache[raw_name] = result
            return result

    def __getattr__(self, name):
        return getattr(self._resolver, name)


def get_scorer() -> dict:
    """Load the dataset, workbook, mappings and exclusions exactly once."""
    global _SCORER
    with _SCORER_LOCK:
        if _SCORER is None:
            products, scores_by_name, resolver = se.load_data()
            uid_by_product_id = {}
            for _, row in products.iterrows():
                uid_by_product_id[str(row["product_id"])] = str(row["canonical_product_id_v2"])
            if len(uid_by_product_id) != len(products):
                raise RuntimeError(
                    f"product_id is not unique: {len(uid_by_product_id)} ids for {len(products)} rows"
                )
            # Plain-dict views of the same data, built once. score_product()
            # reads rows with .get()/[] and workbook rows with .get(), which a
            # dict answers identically to a pandas Series -- without the
            # per-access pandas overhead that dominated scoring time.
            product_rows = products.to_dict("records")
            scorer = {
                "products": products,
                "scores_by_name": scores_by_name,
                "resolver": resolver,
                "uid_by_product_id": uid_by_product_id,
                "catalog": build_catalog(products),
                "product_rows": product_rows,
                "row_by_uid": {str(r["canonical_product_id_v2"]): r for r in product_rows},
                "row_scores": {name: row.to_dict() for name, row in scores_by_name.items()},
                "memo_resolver": _MemoResolver(resolver),
                "fast_path": False,
            }
            scorer["fast_path"] = _fast_path_matches_engine(scorer)
            _SCORER = scorer
        return _SCORER


def _fast_path_matches_engine(scorer: dict) -> bool:
    """Prove the fast path reproduces se.score_catalogue() exactly, or disable it.

    Uses a profile that touches every kind of column (skin +sensitive, concern,
    age, a life stage and Excessive Dryness) and compares every field the engine
    returns, down to the per-ingredient detail. Any difference -- for instance
    after a future change to scoring_engine.py -- turns the fast path off, and
    scoring falls back to se.score_catalogue().
    """
    probe = QuizProfile(
        skin_type="Oily",
        concerns=["Acne"],
        age_group="17-25",
        sensitive=True,
        life_stages=["pregnancy"],
        excessive_dryness=True,
    )
    _, reference = se.score_catalogue(
        scorer["products"], scorer["resolver"], scorer["scores_by_name"], probe
    )
    fast = [
        se.score_product(row, scorer["memo_resolver"], scorer["row_scores"], probe)
        for row in scorer["product_rows"]
    ]
    identical = len(reference) == len(fast) and all(a == b for a, b in zip(reference, fast))
    print(
        "Scoring fast path verified identical to se.score_catalogue()"
        if identical
        else "WARNING: fast path differs from se.score_catalogue(); using score_catalogue()"
    )
    return identical


def engine_details(profile) -> list[dict]:
    """scoring_engine.py's per-product results for every product, in dataset order."""
    scorer = get_scorer()
    if scorer["fast_path"]:
        return [
            se.score_product(row, scorer["memo_resolver"], scorer["row_scores"], profile)
            for row in scorer["product_rows"]
        ]
    _, details = se.score_catalogue(
        scorer["products"], scorer["resolver"], scorer["scores_by_name"], profile
    )
    return details


def _text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def _first_url(value) -> str:
    """The first URL from a cell that may list several, pipe-separated.

    The catalogue stores every image a product has in one cell, joined by
    " | " -- between 2 and 16 of them. A browser cannot use that as an <img>
    src: the spaces make it an invalid URL, it fails to load, and the card
    falls back to its placeholder. The card shows one image, so this returns
    the first and leaves the cell itself untouched.

    A cell holding a single URL is returned unchanged.
    """
    text = _text(value)
    if "|" not in text:
        return text
    for part in text.split("|"):
        part = part.strip()
        if part:
            return part
    return ""


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # drop NaN


def build_catalog(products) -> list[dict]:
    """One entry per product, carrying only what the validation UI renders."""
    catalog = []
    for _, row in products.iterrows():
        category = _text(row.get("category"))
        catalog.append(
            {
                "uid": _text(row["canonical_product_id_v2"]),
                "productId": str(row["product_id"]),
                "gtin": _text(row.get("gtin")),
                "name": _text(row.get("product_name")),
                "brand": _text(row.get("brand")),
                "category": category,
                # The catalogue carries its own Face/Body context. It is a
                # filtering and display value only -- never a scoring
                # attribute. Rows without one fall back to the category rule.
                "context": _text(row.get("context"))
                or ("Body" if category == BODY_CATEGORY else "Face"),
                # One image per card; the cell may list several (see _first_url).
                "imageUrl": _first_url(row.get("image_url")),
                "productUrl": _text(row.get("product_url")),
                "price": _number(row.get("selling_price")),
                "mrp": _number(row.get("mrp")),
                # The engine parsed these into lists; show them the way the
                # dataset stores them.
                "primaryIngredients": "; ".join(row["primary_ingredients"]),
                "secondaryIngredients": "; ".join(row["secondary_ingredients"]),
            }
        )
    return catalog


def quiz_options() -> dict:
    return {
        "contexts": CONTEXTS,
        "skinTypes": SKIN_TYPES,
        "sensitivityOptions": ["No", "Yes"],
        "ages": AGE_GROUPS,
        "genders": GENDERS,
        "concernsByContext": {
            context: list(concerns.keys()) for context, concerns in CONCERNS_BY_CONTEXT.items()
        },
        "specialConditions": SPECIAL_CONDITIONS,
        "gendersWithoutLifeStages": sorted(GENDERS_WITHOUT_LIFE_STAGES),
        "scoreRanges": [{"key": key, "label": label} for key, label, _ in SCORE_RANGES]
        + [{"key": "not-suggested", "label": "Not Suggested"}],
    }


def dataset_payload() -> dict:
    scorer = get_scorer()
    catalog = scorer["catalog"]
    categories = sorted({item["category"] for item in catalog if item["category"]})
    # Per-context totals for the validation header, counted from the catalogue
    # itself so they can never drift from what the doctor can actually open.
    context_counts = {name: 0 for name in CONTEXTS if name != ALL_CONTEXT}
    for item in catalog:
        if item["context"] in context_counts:
            context_counts[item["context"]] += 1
    return {
        "metadata": {
            "productCount": len(catalog),
            "contextCounts": context_counts,
            "allContext": ALL_CONTEXT,
            "populationSource": se.PRODUCT_DATASET_FILE.name,
            "ingredientScores": se.INGREDIENT_SCORES_FILE.name,
            "domainMappings": se.DOMAIN_MAPPINGS_FILE.name,
            "exclusions": se.EXCLUDED_INGREDIENTS_FILE.name,
            "scoringEngine": "scoring_engine.py",
            "categories": categories,
            "bodyCategory": BODY_CATEGORY,
        },
        "quizOptions": quiz_options(),
        "products": catalog,
    }


# --------------------------------------------------------------------------
# Profile translation
# --------------------------------------------------------------------------


class ProfileError(ValueError):
    """The submitted quiz answers cannot be expressed as an engine Profile."""


def profile_from_request(payload: dict):
    """Translate quiz answers into (QuizProfile, echo of what was applied)."""
    if not isinstance(payload, dict):
        raise ProfileError("Request body must be a JSON object")

    context = _text(payload.get("context")) or CONTEXTS[0]
    if context not in CONCERNS_BY_CONTEXT:
        raise ProfileError(f"Unknown context {context!r}; expected one of {CONTEXTS}")

    skin_type = _text(payload.get("skinType")) or SKIN_TYPES[0]
    if skin_type not in SKIN_TYPES:
        raise ProfileError(f"Unknown skin type {skin_type!r}; expected one of {SKIN_TYPES}")

    age_group = _text(payload.get("age")) or AGE_GROUPS[-1]
    if age_group not in AGE_GROUPS:
        raise ProfileError(f"Unknown age group {age_group!r}; expected one of {AGE_GROUPS}")

    sensitive = bool(payload.get("sensitive"))
    gender = _text(payload.get("gender")) or GENDERS[0]

    concern_label = _text(payload.get("concern")) or NO_CONCERN
    concerns_for_context = CONCERNS_BY_CONTEXT[context]
    if concern_label not in concerns_for_context:
        raise ProfileError(
            f"Concern {concern_label!r} is not offered for the {context} context"
        )
    concern = concerns_for_context[concern_label]

    raw_conditions = payload.get("specialConditions") or []
    if isinstance(raw_conditions, str):
        raw_conditions = [raw_conditions]
    conditions = [_text(item) for item in raw_conditions if _text(item) and _text(item) != NO_CONCERN]
    for condition in conditions:
        if condition not in SPECIAL_CONDITIONS:
            raise ProfileError(f"Unknown special condition {condition!r}")

    # Gender gates the life stages in the UI; enforce the same rule here so a
    # hand-made request cannot score a life stage the UI would have hidden.
    if gender in GENDERS_WITHOUT_LIFE_STAGES:
        conditions = [c for c in conditions if c not in LIFE_STAGE_BY_CONDITION]

    life_stages = [LIFE_STAGE_BY_CONDITION[c] for c in conditions if c in LIFE_STAGE_BY_CONDITION]
    excessive_dryness = EXCESSIVE_DRYNESS in conditions

    profile = QuizProfile(
        skin_type=skin_type,
        concerns=[concern] if concern else [],
        age_group=age_group,
        sensitive=sensitive,
        life_stages=life_stages,
        excessive_dryness=excessive_dryness,
    )
    # Fail fast on an unknown column name rather than mid-catalogue.
    columns = profile.score_columns()

    applied = {
        "context": context,
        "skinType": skin_type,
        "sensitive": sensitive,
        "age": age_group,
        "gender": gender,
        "genderUsedForScoring": False,
        "concernLabel": concern_label,
        "concern": concern,
        "specialConditions": conditions or [NO_CONCERN],
        "lifeStages": life_stages,
        "excessiveDryness": excessive_dryness,
        "attributeColumns": columns,
    }
    cache_key = (
        context,
        skin_type,
        sensitive,
        age_group,
        concern or "",
        tuple(sorted(life_stages)),
        excessive_dryness,
    )
    return profile, applied, cache_key


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _round(value, places=2):
    number = _number(value)
    return None if number is None else round(number, places)


def ingredient_rows(details) -> list[dict]:
    """Per-ingredient detail, straight from the engine's own score_ingredient."""
    rows = []
    for item in details:
        if item["score"] is not None:
            status = "SCORED"
        elif item["excluded_reason"] == "excluded_ingredient":
            status = "EXCLUDED"
        elif item["excluded_reason"] == "unmatched_ingredient":
            status = "UNMATCHED"
        elif item["excluded_reason"] == "all_scores_disqualifying":
            status = "DISQUALIFYING"
        else:
            status = "NO_SCORES_FOR_PROFILE"
        rows.append(
            {
                "name": item["raw_name"],
                "canonical": item["canonical"],
                "score": _round(item["score"]),
                "attributeScores": {k: v for k, v in item["values"].items()},
                "disqualifying": bool(item["disqualifying"]),
                "status": status,
                "mappingMethod": item["mapping_method"],
            }
        )
    return rows


VALID_STATUSES = {"ELIGIBLE", "NOT_SUGGESTED", "UNSCORABLE"}


def summary_row(detail: dict, uid: str) -> dict:
    """Everything the cards, bands, ranking and modal headline need.

    The per-ingredient tables are left out of the catalogue-wide response (they
    were 69% of it) and served per product by detail_row() when the doctor opens
    a product. Both come from the same scoring_engine result.
    """
    disqualifying = bool(detail["has_disqualifying_ingredient"])
    final_score = _round(detail["final_score"])
    if disqualifying:
        status = "NOT_SUGGESTED"
    elif final_score is None:
        status = "UNSCORABLE"
    else:
        status = "ELIGIBLE"
    return {
        "uid": uid,
        "productId": str(detail["product_id"]),
        "score": final_score,
        "status": status,
        "disqualifying": disqualifying,
        "disqualifyingIngredients": [
            item["raw_name"]
            for item in detail["_primary_detail"] + detail["_secondary_detail"]
            if item["disqualifying"]
        ],
        "primaryAverage": _round(detail["primary_average"]),
        "secondaryAverage": _round(detail["secondary_average"]),
        "primaryWeight": detail["primary_weight"],
        "secondaryWeight": detail["secondary_weight"],
        "primaryContribution": _round(detail["primary_contribution"]),
        "secondaryContribution": _round(detail["secondary_contribution"]),
        "categoryRule": detail["category_rule"],
        "scoringBasis": detail["scoring_basis"],
        "attributesUsed": detail["attributes_used"],
        "attributesExcluded": detail["attributes_excluded"],
        "category": detail["category"],
    }


def detail_row(detail: dict, uid: str) -> dict:
    """summary_row() plus the ingredient-level validation tables."""
    return {
        **summary_row(detail, uid),
        "primary": ingredient_rows(detail["_primary_detail"]),
        "secondary": ingredient_rows(detail["_secondary_detail"]),
    }


def score_catalogue_for(profile: QuizProfile) -> dict:
    """Score every catalogue product for one profile via scoring_engine only.

    Raises instead of returning anything partial, so an incomplete result can
    never reach the cache.
    """
    scorer = get_scorer()
    details = engine_details(profile)

    uid_by_product_id = scorer["uid_by_product_id"]
    rows = []
    seen = set()
    for detail in details:
        product_id = str(detail["product_id"])
        uid = uid_by_product_id.get(product_id)
        if uid is None:
            raise RuntimeError(f"Scored product_id {product_id!r} is not in the dataset")
        if uid in seen:
            raise RuntimeError(f"Duplicate scored product {uid!r}")
        seen.add(uid)
        rows.append(summary_row(detail, uid))

    expected = {item["uid"] for item in scorer["catalog"]}
    if seen != expected or len(rows) != len(scorer["catalog"]):
        raise RuntimeError(
            f"Incomplete scoring result: {len(rows)} rows for {len(scorer['catalog'])} products"
        )
    if any(row["status"] not in VALID_STATUSES for row in rows):
        raise RuntimeError("Scoring result contains an unknown product status")

    eligible = [r for r in rows if r["status"] == "ELIGIBLE"]
    range_counts = {key: 0 for key, _, _ in SCORE_RANGES}
    for row in eligible:
        for key, _, test in SCORE_RANGES:
            if test(row["score"]):
                range_counts[key] += 1
                break
    range_counts["not-suggested"] = sum(1 for r in rows if r["status"] == "NOT_SUGGESTED")

    scores = [r["score"] for r in eligible]
    return {
        "rows": rows,
        "productsScored": len(rows),
        "eligibleCount": len(eligible),
        "notSuggestedCount": range_counts["not-suggested"],
        "unscorableCount": sum(1 for r in rows if r["status"] == "UNSCORABLE"),
        "rangeCounts": range_counts,
        "scoreRange": {"min": min(scores), "max": max(scores)} if scores else {"min": None, "max": None},
    }


def _cache_get(cache_key):
    with _CACHE_LOCK:
        body = _SCORE_CACHE.get(cache_key)
        if body is not None:
            _SCORE_CACHE.move_to_end(cache_key)
        return body


def cached_summary_body(profile: QuizProfile, cache_key: tuple) -> tuple[bytes, bool]:
    """(serialised summary, was_cache_hit) for one profile, scoring at most once."""
    body = _cache_get(cache_key)
    if body is not None:
        return body, True

    with _INFLIGHT_LOCK:
        lock = _INFLIGHT.setdefault(cache_key, threading.Lock())
    try:
        with lock:
            # Another request may have scored this profile while we waited.
            body = _cache_get(cache_key)
            if body is not None:
                return body, True
            summary = score_catalogue_for(profile)  # raises on anything incomplete
            # The object's inner text, so the per-request header can be spliced
            # in front of it without re-serialising the catalogue rows.
            body = json.dumps(summary, ensure_ascii=False)[1:-1].encode("utf-8")
            with _CACHE_LOCK:
                _SCORE_CACHE[cache_key] = body
                _SCORE_CACHE.move_to_end(cache_key)
                while len(_SCORE_CACHE) > _SCORE_CACHE_LIMIT:
                    _SCORE_CACHE.popitem(last=False)
            return body, False
    finally:
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(cache_key) is lock:
                _INFLIGHT.pop(cache_key, None)


def scored_response(payload: dict) -> tuple[bytes, bool]:
    """The /api/v3/score JSON body, and whether it came from the cache."""
    profile, applied, cache_key = profile_from_request(payload)
    body, hit = cached_summary_body(profile, cache_key)
    head = json.dumps(
        {
            "ok": True,
            "profile": applied,
            "cache": "HIT" if hit else "MISS",
            "scoringEngine": "scoring_engine.py",
            "ranking": "score DESC, product name ASC (disqualifying products excluded)",
        },
        ensure_ascii=False,
    )
    return head[:-1].encode("utf-8") + b", " + body + b"}", hit


def scored_payload(payload: dict) -> dict:
    """Parsed form of scored_response(), for tests and scripts."""
    return json.loads(scored_response(payload)[0])


def detail_payload(payload: dict) -> dict:
    """One product's full validation detail for one profile.

    Scores just that product with scoring_engine's own score_product(), which
    has no cross-product state, so the result is identical to that product's
    entry in the catalogue-wide scoring.
    """
    uid = _text(payload.get("uid")) if isinstance(payload, dict) else ""
    profile, applied, _ = profile_from_request(payload)
    scorer = get_scorer()
    row = scorer["row_by_uid"].get(uid)
    if row is None:
        raise ProfileError(f"Unknown product uid {uid!r}")
    if scorer["fast_path"]:
        detail = se.score_product(row, scorer["memo_resolver"], scorer["row_scores"], profile)
    else:
        series = scorer["products"].loc[scorer["products"]["canonical_product_id_v2"] == uid].iloc[0]
        detail = se.score_product(series, scorer["resolver"], scorer["scores_by_name"], profile)
    return {"ok": True, "profile": applied, "product": detail_row(detail, uid)}


# A browser that navigates away, reloads, or tears the page down mid-response
# drops the connection. The write then fails, but nothing is wrong on this side
# and no error response can be delivered -- the socket is already gone.
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class RoopseeHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".html": "text/html",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        # Development server: never let a browser cache the app shell, the
        # scripts or an API payload. Without this the browser applies heuristic
        # freshness (no Cache-Control is sent otherwise) and can keep running a
        # stale app.js against a newer API.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_GONE:
            self.log_message("client disconnected before the response was sent")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            try:
                count = len(get_scorer()["catalog"])
                ok = True
            except Exception:  # pragma: no cover - surfaced to the caller
                count, ok = None, False
            self._json({
                "ok": ok,
                "service": "roopsee-doctor-validation",
                "productCount": count,
                "dataset": se.PRODUCT_DATASET_FILE.name,
                "scoringEngine": "scoring_engine.py",
            })
            return

        if parsed.path == "/api/v3/dataset":
            # Build first, send second: a failure to build is a real 500, while
            # a failure to send only means the browser went away.
            try:
                payload = dataset_payload()
            except Exception as error:  # pragma: no cover - surfaced in the UI
                self._json({"ok": False, "error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._json(payload)
            return

        if parsed.path == "/":
            self.path = "/index.html"

        try:
            super().do_GET()
        except CLIENT_GONE:
            self.log_message("client disconnected while receiving %s", self.path)

    def _send_json_bytes(self, body: bytes, cache_state: str) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Roopsee-Cache", cache_state)
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_GONE:
            self.log_message("client disconnected before the response was sent")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/v3/score", "/api/v3/detail"):
            self._json({"ok": False, "error": "Unknown endpoint"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json_body()
        except (ValueError, UnicodeDecodeError) as error:
            self._json({"ok": False, "error": f"Invalid JSON body: {error}"}, HTTPStatus.BAD_REQUEST)
            return
        # Build first, send second: a failure to build is a real error, while a
        # failure to send only means the browser went away.
        try:
            if parsed.path == "/api/v3/detail":
                result = detail_payload(payload)
            else:
                body, hit = scored_response(payload)
        except ProfileError as error:
            self._json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as error:  # pragma: no cover - surfaced in the UI
            self._json({"ok": False, "error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/v3/detail":
            self._json(result)
        else:
            self._send_json_bytes(body, "HIT" if hit else "MISS")


def main() -> None:
    port = int(os.getenv("PORT", "8020"))
    # 0.0.0.0 so a container platform (Render and the like) can route to the
    # process; a host that only listens on loopback is unreachable there and
    # the deploy fails its health check. PORT is supplied by the platform, with
    # 8020 as the local fallback. Override either with HOST / PORT.
    host = os.getenv("HOST", "0.0.0.0")
    # Load the dataset, workbook, mappings and exclusions once, before serving,
    # so no request ever waits on it.
    get_scorer()
    server = ThreadingHTTPServer((host, port), RoopseeHandler)
    print(f"Roopsee Doctor Validation running at http://{host}:{port}/")
    count = len(get_scorer()["catalog"])
    print(f"Doctor Validation: {count} products scored by scoring_engine.py")
    server.serve_forever()


if __name__ == "__main__":
    main()

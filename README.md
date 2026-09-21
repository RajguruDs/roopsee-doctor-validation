# Roopsee Doctor Validation Scoring Service

A validation tool, not a consumer product. Its only purpose is to let a doctor
or domain expert inspect how `scoring_engine.py` scores the 87-product
validation catalogue, ingredient by ingredient, and judge whether each score is
correct.

There is no end-user recommendation flow in this repository.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Then open:

**http://127.0.0.1:8020/**

That is the Doctor Validation interface. No query flag is needed. `?scorer=v3`
still works as an alias for the same page, kept only so older links do not
break — there is one UI and one code path behind both.

## The validation catalogue

87 products, 87 unique product IDs, 87 unique canonical IDs, in
`data/Roopsee_quick_com_products_88_onboarding.xlsx` (sheet `products_88`).

| | |
| --- | --- |
| Products | 87 (Face 72, Body 15) |
| Categories | Sunscreen 20, Serum 16, Cleanser 15, Body Care 15, Moisturizer 10, Toner 7, Mask 4 |
| Ingredient mentions | 508 across 203 distinct names |
| Ingredient resolution | 100% of scoreable mentions; 0 unresolved |
| Excluded | 1 mention (`Melanin`, on the exclusion list) |

`context` (Face / Body) is filtering and display metadata only. It never enters
a profile and cannot change a score.

## Scoring

`scoring_engine.py` is the single source of truth. `app.py` imports it and calls
it; nothing is scored, adjusted or re-ranked in the server layer or the browser.

Per ingredient: resolve the raw name to a canonical workbook row, then average
one score per selected profile attribute. Then average the usable Primary
scores, average the usable Secondary scores, and apply the category weighting.

| Category | Skin type | Concern | Age | Primary | Secondary |
| --- | --- | --- | --- | --- | --- |
| Sunscreen / Sun Care | yes | **no** | yes | 50% | 50% |
| Moisturizer | yes | **no** | yes | 50% | 50% |
| Cleanser / Toner / Mask / Body Care / other | yes | yes | yes | 50% | 50% |
| Serum | yes | yes | yes | **80%** | **20%** |

If one group is absent or entirely unresolved, the other is renormalised to
100%. `-100` is a disqualifying flag: it is never averaged, the product is
reported `NOT_SUGGESTED`, and its numeric score is retained for validation.

Ingredient resolution runs in a fixed order — exclusion, domain mapping, exact
canonical, safe normalisation, final exception, unresolved — with **no fuzzy
matching anywhere**. Unresolved ingredients are dropped from the averages, never
scored zero.

## Data and scoring dependencies

```
app.py
  └─ scoring_engine.py
       ├─ data/Roopsee_quick_com_products_88_onboarding.xlsx   the 87 products
       ├─ data/Ingredient scoring.xlsx                          canonical ingredient scores
       ├─ data/domain_reviewed_mappings_expanded.csv            approved mappings
       └─ data/excluded_ingredients.csv                         hard exclusion list
  └─ static/index.html + app.js + styles.css                    the validation UI
```

All four data files are read-only inputs. The engine never writes to them.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /` | the Doctor Validation interface |
| `GET /api/v3/dataset` | the 87-product catalogue and quiz vocabulary, no scores |
| `POST /api/v3/score` | every product's score for one profile, plus bands and ranking |
| `POST /api/v3/detail` | one product's ingredient-level breakdown |
| `GET /api/health` | service status, catalogue size, dataset name |

`/api/v3/score` and `/api/v3/detail` take the same profile body:

```json
{
  "context": "All",
  "skinType": "Oily",
  "sensitive": false,
  "age": "17-25",
  "concern": "Acne",
  "gender": "female",
  "specialConditions": ["None"]
}
```

`/api/v3/detail` additionally takes `"uid"`. Gender is sent for UI gating parity
only — it is never scored.

## The validation UI

Set a profile, then open any product. The detail view shows:

- product image, name, brand, category, context, price
- final Roopsee score and status (RECOMMENDABLE / NOT SUGGESTED / NO SCORE)
- which profile attributes were scored, and which were excluded for that category
- for every Primary and Secondary ingredient: **raw ingredient → canonical
  ingredient → mapping method → per-attribute scores → ingredient score**
- primary average, secondary average, category weighting, final score
- any disqualifying ingredient, flagged `-100`

Mapping methods are shown verbatim from the engine: `EXACT_CANONICAL`,
`DOMAIN_REVIEWED`, `SAFE_NORMALIZATION`, `FINAL_EXCEPTION`, `EXCLUDED`,
`NEEDS_DOMAIN_REVIEW`.

## Tests

```bash
python test_onboarding.py          # catalogue, scoring and API test suite
python verify_doctor_validation.py # end-to-end proof against a running server
```

`verify_doctor_validation.py` needs the server running; it opens all 87 products
through the live API and checks each one individually.

Supporting tools:

```bash
python scoring_engine.py             # production run -> output/*.csv
python scoring_engine.py --validate  # reference profiles and breakdowns
python onboarding_audit.py           # ingredient and product-data audits
```

## Important rule

**Do not modify the scoring engine or its data during validation without
explicit approval.**

That means `scoring_engine.py`, `data/Ingredient scoring.xlsx`,
`data/domain_reviewed_mappings_expanded.csv`, `data/excluded_ingredients.csv`,
and the product ingredient values themselves — along with the category rules,
the weights, the resolver order and the `-100` behaviour.

The whole point of this tool is to judge the current scoring baseline. Changing
the engine to make a score look better destroys the thing being measured. If
validation shows a score is wrong, record it as a finding and change the engine
deliberately, as its own reviewed decision — never as a side effect of a
validation session.

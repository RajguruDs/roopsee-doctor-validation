"""
Roopsee product scoring engine.

Scores products against a user profile at the INDIVIDUAL INGREDIENT level.

Calculation order (do not change):

    User profile
      -> for each product
        -> for each ingredient
          -> resolve ingredient to a canonical row in the ingredient scores sheet
          -> collect one score per selected profile attribute
          -> average those scores            = ingredient score
        -> average primary ingredient scores = Primary Average
        -> average secondary ingredient scores = Secondary Average
        -> Primary Average * primary weight + Secondary Average * secondary
           weight = Final Product Score

Agreed rules:

  1. Ingredient resolution runs in a fixed order: exact match -> safe
     normalisation (case, spacing, hyphens, punctuation) -> canonical/name
     variation (slash chains, parentheticals, singular/plural, %) ->
     hand-reviewed alias map. No unrestricted fuzzy matching anywhere.
     Chemically distinct ingredients are never mapped onto each other: names
     differing by a number, salt, ester or preparation stay separate, and a
     normalised key claimed by two different canonical names is discarded as
     ambiguous rather than guessed at.
  2. Unresolved ingredients are EXCLUDED from the average (never scored 0) and
     are reported in diagnostics.
  3. The product's assigned_category decides BOTH which profile attributes are
     scored and how the two groups are weighted:

       Category               Skin type  Concern  Age   Primary  Secondary
       Sunscreen / Sun Care   yes        NO       yes   50%      50%
       Moisturizer            yes        NO       yes   50%      50%
       Cleanser               yes        yes      yes   50%      50%
       Toner                  yes        yes      yes   50%      50%
       Serum                  yes        yes      yes   80%      20%
       Mask                   yes        yes      yes   50%      50%
       anything else          yes        yes      yes   50%      50%

     The profile still carries every attribute the user selected; excluded
     attributes are simply not averaged for that category. Category is read
     from the assigned_category field only -- never inferred from the product
     name.
  4. If one group (primary or secondary) is entirely absent or entirely
     unresolved, the other group is renormalised to 100% weight, overriding the
     category weighting above.
  5. -100 ("disqualifying") values are dropped from the averages and surfaced
     separately as has_disqualifying_ingredient. They do not block the product.
  6. The category-specific rules in the workbook's "Scale" sheet (which also
     restrict which concerns apply per category) are deliberately not applied.
     Only the weighting in rule 3 is category-dependent.

Run:  python scoring_engine.py             production run -- writes
                                          output/product_attribute_scores.csv,
                                          output/mapping_audit.csv,
                                          output/unmatched_ingredients.csv and
                                          output/product_mapping_coverage.csv
      python scoring_engine.py --validate  validation run -- writes the
                                          scores_*.csv / validation_*.csv
                                          reference profiles instead
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

# The authoritative product population: the 88-product onboarding catalogue.
# Read-only: the scoring code never writes to it and never edits an ingredient
# name inside it -- all mapping happens in IngredientResolver below.
PRODUCT_DATASET_FILE = DATA_DIR / "Roopsee_quick_com_products_88_onboarding.xlsx"

# Columns the scoring code needs from the product dataset. `ingredients` (the
# raw INCI text) is deliberately not among them: it is source information for
# debugging only and is never scored.
REQUIRED_PRODUCT_COLUMNS = (
    "product_id",
    "product_name",
    "brand",
    "category",
    "primary_ingredients",
    "secondary_ingredients",
)
INGREDIENT_SCORES_FILE = DATA_DIR / "Ingredient scoring.xlsx"

# Reference data that governs mapping, applied ahead of the scoring
# vocabulary. Both are read-only inputs and are never written back.
EXCLUDED_INGREDIENTS_FILE = DATA_DIR / "excluded_ingredients.csv"
DOMAIN_MAPPINGS_FILE = DATA_DIR / "domain_reviewed_mappings_expanded.csv"
INGREDIENT_SCORES_SHEET = "Ingredient rating"

# Category rules: which profile attributes are scored, and how primary and
# secondary are weighted. Sunscreens and moisturisers are scored on skin type
# and age only -- the user's concerns are deliberately excluded, because those
# categories are not treated as concern-targeting products. Serums weight the
# primary actives more heavily; every other category splits evenly.
#
# Categories are matched on the normalised `assigned_category` field only --
# never inferred from the product name. Both the labels used in the 499-product
# set ("Sun Care", "Moisturizers", "Toners & Mist", ...) and the singular forms
# from the specification are listed, so the rules survive a relabelling
# upstream. Anything unlisted falls through to DEFAULT_RULE.


@dataclass(frozen=True)
class CategoryRule:
    use_concerns: bool
    primary_weight: float
    secondary_weight: float
    name: str


_CONCERNS_EXCLUDED_50_50 = ("concerns_excluded_50_50", False, 0.50, 0.50)
_ALL_ATTRIBUTES_50_50 = ("all_attributes_50_50", True, 0.50, 0.50)
_ALL_ATTRIBUTES_80_20 = ("all_attributes_80_20", True, 0.80, 0.20)


def _rule(spec) -> CategoryRule:
    name, use_concerns, primary, secondary = spec
    return CategoryRule(use_concerns, primary, secondary, name)


CATEGORY_RULES = {
    # Skin type + age only; concerns excluded.
    "sun care": _rule(_CONCERNS_EXCLUDED_50_50),
    "sunscreen": _rule(_CONCERNS_EXCLUDED_50_50),
    "sunscreens": _rule(_CONCERNS_EXCLUDED_50_50),
    "moisturizers": _rule(_CONCERNS_EXCLUDED_50_50),
    "moisturizer": _rule(_CONCERNS_EXCLUDED_50_50),
    "moisturiser": _rule(_CONCERNS_EXCLUDED_50_50),
    "moisturisers": _rule(_CONCERNS_EXCLUDED_50_50),
    # Skin type + concern + age, even split.
    "cleansers": _rule(_ALL_ATTRIBUTES_50_50),
    "cleanser": _rule(_ALL_ATTRIBUTES_50_50),
    "toners & mist": _rule(_ALL_ATTRIBUTES_50_50),
    "toners": _rule(_ALL_ATTRIBUTES_50_50),
    "toner": _rule(_ALL_ATTRIBUTES_50_50),
    "masks": _rule(_ALL_ATTRIBUTES_50_50),
    "mask": _rule(_ALL_ATTRIBUTES_50_50),
    # Skin type + concern + age, primary-weighted.
    "serums": _rule(_ALL_ATTRIBUTES_80_20),
    "serum": _rule(_ALL_ATTRIBUTES_80_20),
}

# Any category not listed above (e.g. "Others", "Body Care") is scored on all
# three attributes with an even split.
DEFAULT_RULE = _rule(_ALL_ATTRIBUTES_50_50)

# The workbook's canonical-ingredient column. The current workbook calls it
# "Ingredients"; earlier revisions used "Canonical Ingredient". The column is
# resolved from the sheet that is actually loaded -- no other workbook is
# consulted -- and load_data() fails loudly if none of these is present.
CANONICAL_COL_CANDIDATES = ("Ingredients", "Canonical Ingredient")


def canonical_column(frame) -> str:
    for name in CANONICAL_COL_CANDIDATES:
        if name in frame.columns:
            return name
    raise KeyError(
        f"No canonical ingredient column in {INGREDIENT_SCORES_SHEET!r}; "
        f"looked for {CANONICAL_COL_CANDIDATES}"
    )
SECONDARY_COL = "Secondary Key Ingredients"

# The primary column header contains an em dash; match on prefix instead of
# hard-coding the character.
PRIMARY_COL_PREFIX = "Main Key Ingredients"

DISQUALIFYING = -100

# How an ingredient name reached its canonical row, in the fixed priority
# order applied by IngredientResolver.resolve_with_method(). These are the
# Mapping Method values reported by mapping_audit.csv.
#
#   EXCLUDED           -- on the hard exclusion list; never mapped, never scored
#   DOMAIN_REVIEWED    -- an approved row in domain_reviewed_mappings_expanded.csv
#   EXACT_CANONICAL    -- the name is a canonical entry in the workbook
#   SAFE_NORMALIZATION -- deterministic variant: case, spacing, punctuation,
#                         slash chains, parentheticals, plural, %, hand aliases
#   FINAL_EXCEPTION    -- one of the four explicitly defined genuine records
#   NEEDS_DOMAIN_REVIEW-- nothing safe applied; left unresolved on purpose
#
# Precedence is EXCLUDED > DOMAIN_REVIEWED > EXACT_CANONICAL >
# SAFE_NORMALIZATION > FINAL_EXCEPTION. No similarity score is consulted
# anywhere in this chain.

METHOD_EXCLUDED = "EXCLUDED"
METHOD_DOMAIN = "DOMAIN_REVIEWED"
METHOD_EXACT = "EXACT_CANONICAL"
METHOD_SAFE = "SAFE_NORMALIZATION"
METHOD_EXCEPTION = "FINAL_EXCEPTION"
METHOD_NEEDS_REVIEW = "NEEDS_DOMAIN_REVIEW"

MAPPING_METHODS = (
    METHOD_EXCLUDED, METHOD_DOMAIN, METHOD_EXACT,
    METHOD_SAFE, METHOD_EXCEPTION, METHOD_NEEDS_REVIEW,
)

STATUS_MAPPED = "MAPPED"
STATUS_EXCLUDED = "EXCLUDED"
STATUS_UNRESOLVED = "NEEDS_DOMAIN_REVIEW"

# The four genuine ingredient records called out for explicit handling. Keys are
# normalised source spellings; values are the canonical entry each must resolve
# to. Targets are verified against the workbook at load time -- a target that is
# absent is reported, never replaced by a similar-looking entry.
FINAL_EXCEPTIONS = {
    # Genuine olive record; the workbook carries the matching concentration row.
    "oleaeuropaea - 0.5%": "Olea Europaea - 0.5%",
    "triethylcitrate": "Triethyl Citrate",
    "phenyltrimethicone": "Phenyltrimethicone",
    "rhussuccedanea fruit wax": "Rhussuccedanea Fruit Wax",
}

# Similarity at or above this is reported beside an unresolved ingredient as a
# review hint only. It never maps anything.
NEAR_MISS_RATIO = 0.85

# Tokens used in the source data to mean "there are none here".
NULL_TOKENS = {"", "-", "--", "n/a", "na", "none", "nil"}

# --------------------------------------------------------------------------
# Profile attribute -> score column
# --------------------------------------------------------------------------

SKIN_TYPE_COLUMNS = {
    ("oily", False): "Oily Score",
    ("oily", True): "Oily+Sensitive Score",
    ("dry", False): "Dry Score",
    ("dry", True): "Dry+Sensitive Score",
    ("normal", False): "Normal Score",
    ("normal", True): "Normal+Sensitive Score",
    ("combination", False): "Combination Score",
    ("combination", True): "Combination+Sensitive Score",
    # Excessive dryness has a single column and no sensitive variant.
    ("excessive dryness", False): "Excessive Dryness score",
    ("excessive dryness", True): "Excessive Dryness score",
}

AGE_GROUP_COLUMNS = {
    "<16": "<16",
    "under 16": "<16",
    "17-25": "17-25",
    "above 25": "Above 25",
}

CONCERN_COLUMNS = {
    "acne": "Acne",
    "body acne": "Body Acne",
    "dryness": "Dryness",
    "open pores": "Open Pores",
    "uneven skin tone": "Uneven Skin Tone",
    "dark spots/pigmentation": "Dark Spots/Pigmentation",
    "melasma": "Melasma",
    "barrier repair": "Barrier Repair",
    "comedones": "Comedones",
    "wrinkles/fine lines": "Wrinkles/Fine lines",
    "redness/irritation": "Redness/Irritation",
    "dehydration": "Dehydration",
    "dullness": "Dullness",
    "tanning": "Tanning",
}

LIFE_STAGE_COLUMNS = {
    "pregnancy": "Pregnancy Score",
    "breastfeeding": "Breastfeeding Score",
}

# --------------------------------------------------------------------------
# Embedded mapping reference data (hybrid: CSV overrides these defaults)
# --------------------------------------------------------------------------
# These two tables are the defaults the engine runs on, so a checkout needs only
# scoring_engine.py, the product dataset and the scoring workbook.
#
# The matching CSVs in data/ remain the domain-review and provenance record:
#   data/excluded_ingredients.csv
#   data/domain_reviewed_mappings_expanded.csv
# If either file is present it takes precedence and is read instead of the
# table below, so a reviewer can still hand back an edited CSV without touching
# code. load_data() reports which source each run used.
#
# Both tables were generated from the parsed CSV values, not copied by hand.
# Order is significant: the resolver keeps the FIRST mapping for a given
# normalised source, and five sources here differ only by case or trailing
# punctuation ('Tocopheryl' / 'Tocopheryl.', 'Sh-' / 'sh-Oligopeptide-1', ...).
# Append new entries at the end so existing precedence cannot shift.
#
# To add or remove a rule: edit the table below (or drop an updated CSV into
# data/). Domain targets are validated against the workbook at load time -- an
# unknown target is reported in resolver.invalid_domain_targets and dropped,
# never redirected to a similar-looking entry.

EXCLUDED_INGREDIENTS = (
    '10, 000 Ppb',
    '4.8% V',
    'Blue',
    'Caramel',
    'High In Neuroscience',
    'Melanin',
    'Vit B5',
    '"water',
    '1, 000 Ppb',
    'Adds Glow',
    "Aligning With Vaseline's Heritage Of Inclusive",
    'Alum?nio Starch Octenylsuccinate',
    'Antibacterial',
    'Antioxidants',
    'Ascorbic Acid. 30ml',
    'Bio-energy Complex Technology',
    'Blue 1 (CI_COLOUR). B01865',
    'CI_COLOUR (yellow 5)',
    'D.m.water',
    'Deep Hydration',
    'Deeply Moisturizes',
    'Dry Skin Polyphenols',
    'Emollient',
    'Energy',
    'Found In Ghee',
    'Full Ingredients List: Aqua',
    'Hydrating',
    'Innovative',
    'Keeping It Hydrated And Soft',
    'Normalizes Keratinisation',
    'Plant-based Moisturisers',
    'Protects Against Sun Damage',
    'Pvp',
    'Red 4 (CI_COLOUR)',
    'Reduces Signs Of Tired',
    'SPF 50+ PA',
    'Seboclear Mp',
    'Skin Barrier Arginine',
    'Skin Conditioning Agent',
    'Soothes',
    'Soothes Irritation',
    'Strengthens The Skin Barrier',
    'This Advanced Formulation Enhances Moisture Retention',
    'Yellow 5 (CI_COLOUR)',
    'Β-white',
)

DOMAIN_REVIEWED_MAPPINGS = (
    ('Coenzyme Q10', 'Ubiquinone (CoQ10)'),
    ('Ghk-cu', 'Copper Tripeptide-1 (GHK-Cu)'),
    ('Haldi Beads', 'Curcuma Longa (Turmeric) Extract/Beads (Haldi Beads)'),
    ('Haritaki Fruitextract - 2.0%', 'Terminalia Chebula (Haritaki) Fruit Extract - 2%'),
    ('Helianthus Annuus Seed Wax', 'Helianthus Annuus (Sunflower) Seed Wax'),
    ('Herbal Emollients', 'Herbal Emollients (unspecified blend)'),
    ('Homopolymer', 'Homopolymer (unspecified)'),
    ('Hordeum Vulgare -1.00%', 'Hordeum Vulgare - 5.0%'),
    ('Inula Helenium Extract', 'Inula Helenium (Elecampane) Root Extract'),
    ('Kapurkachri', 'Hedychium Spicatum (Kapurkachri) Extract'),
    ('L Petroleum Resins', 'Petroleum Resin (L Petroleum Resins; identity incomplete)'),
    ('Light Liquid Paraffin', 'Mineral Oil (Light Liquid Paraffin)'),
    ('Lime', 'Lime extract'),
    ('Lime Pearl', 'Pearl'),
    ('Linoleamidopropyl PG Dimonium Chloride Phosphate', 'Linoleamidopropyl Pg Dimonium Chloride Phosphate'),
    ('Linum Usitatissimum Seed Oil', 'Linum Usitatissimum (Flax) Seed Oil'),
    ('Marsh Mallow Leaf', 'Marshmallow Root Extract'),
    ('Mel/honey', 'Honey'),
    ('Melia Azadirachta Leaf Extract', 'Melia Azadirachta (Neem) Leaf Extract'),
    ('Mesua Ferrea - 1.0%', 'Mesua Ferrea Extract - 1% (plant part unspecified)'),
    ('Myrciaria Dubia Fruit Extract', 'Myrciaria Dubia (Camu Camu) Fruit Extract'),
    ('Narcissuspoeticus - 0.5%', 'Narcissus Poeticus Extract - 0.5%'),
    ('Neo Heliopan Hydro', 'Phenylbenzimidazole Sulfonic Acid (Ensulizole / Neo Heliopan Hydro)'),
    ('Octyldodecanolm Polyglyceryl-2 Triisostearate', 'Octyldodecanol/Polyglyceryl-2 Triisostearate (as listed: Octyldodecanolm Polyglyceryl-2 Triisostearate)'),
    ('Olea Europaea -2.0%', 'Olea Europaea - 0.5%'),
    ('Oleic', 'Oleic Acid'),
    ('Osmogeline', 'Osmogeline (trade/identity unresolved)'),
    ('Phaseolus Angularis Seed Extract', 'Phaseolus Angularis (Adzuki Bean) Seed Extract'),
    ('Pogostemon Cablin Oil', 'Pogostemon Cablin (Patchouli) Oil'),
    ('Polymethylsilsequioxane', 'Polymethylsilsesquioxane'),
    ('Polysaccharides', 'Polysaccharides (unspecified)'),
    ('Potassium Cetyl', 'Potassium Cetyl Phosphate'),
    ('Pro-vitamin B5', 'Pro-vitamin B5 (Panthenol)'),
    ('Prunusarmeniaca - 10.0%', 'Prunus Armeniaca (Apricot) Kernel Oil - 10%'),
    ('Prunusarmeniaca - 14.0%', 'Prunus Armeniaca (Apricot) Kernel Oil - 14%'),
    ('Psiyetobutene', 'Psiyetobutene (unresolved/likely misspelling)'),
    ('Punicagranatum - 0.2%', 'Punica Granatum (Pomegranate) Extract - 0.2%'),
    ('Rakhta Chandan', 'Sandalwood (Rakht Chandan)'),
    ('Rubus Idaeus Juice', 'Rubus Idaeus (Raspberry) Juice'),
    ('Sanjeevani Infusion - 2.0%', 'Sanjeevani Infusion - 2% (proprietary/unspecified blend)'),
    ('Sodium Metabisulphite', 'Sodium Metabisulfite'),
    ('Styrene', 'Hydrogenated Styrene Copolymer'),
    ('Suncat MTA', 'SunCat MTA (Encapsulated Octinoxate + Avobenzone + Octocrylene UV Filter Blend)'),
    ('Sunflower Seed Oils And Rosemary Extract', 'Sunflower Seed Oil + Rosemary Extract'),
    ('Tazmann Pepper', 'Pink Pepper Extract'),
    ('Tea Tree Flower', 'Tea Tree Oil'),
    ('Theobroma Coconut', 'Theobroma Cacao Extract'),
    ('Thethanolamine Sodium Polyacrylate', 'Thethanolamine Sodium Polyacrylate (identity/spelling unresolved)'),
    ('Treemoss Extract', 'Evernia Furfuracea (Treemoss) Extract'),
    ('Tris Citrate', 'Tris Citrate (identity incomplete)'),
    ('Triticum Vulgare - 1.0%', 'Triticum Vulgare (Wheat) Extract - 1%'),
    ('Triticum Vulgare -2.0%', 'Triticum Vulgare (Wheat) Extract - 2%'),
    ('Triticum Vulgare Germ Oil', 'Triticum Vulgare (Wheat) Germ Oil'),
    ('Triticum Vulgare Starch', 'Triticum Vulgare (Wheat) Starch'),
    ('Univul T150', 'Ethylhexyl Triazone / Uvinul T150'),
    ('Usheer', 'Ghee'),
    ('Uvinal A Plus', 'Vitamin A'),
    ('Vegan Mung Mucin', 'Vigna Radiata (Mung Bean) Extract / Vegan Mung Mucin'),
    ('Withania Somnifera -0.5%', 'Withania Somnifera (Ashwagandha) Extract - 0.5%'),
    ('Yashad Bhasma', 'Yashad Bhasma (Calcined Zinc/Zinc Calx)'),
    ('Lavender Extract', 'Lavender Essential Oil'),
    ('Immortelle Extract', 'Immortelle Sap-Like Extract'),
    ('Rosemary Extract', 'Rosemary Leaf Extract'),
    ('Laminaria Extract', 'Laminaria Japonica Extract'),
    ('Aminopropyl Ascorbyl Phosphate', 'Sodium Ascorbyl Phosphate'),
    ('Cyclomethicone', 'Dimethicone'),
    ('Glycol Stearate', 'Glyceryl Stearate'),
    ('Hydrogenated Olive Oil Unsaponifiables', 'Olive Oil'),
    ('Tripeptide-1', 'Tripeptide'),
    ('Olive Oil Derived Emulsifier', 'Olive Oil'),
    ('Palmitoyl Grape Seed Extract', 'Grape Seed Extract'),
    ('Soy Polypeptide', 'Polypeptide'),
    ('Olive Oil PEG-7 Esters', 'Olive Oil'),
    ('Squalane Dicaprylyl Ether', 'Squalane'),
    ('Alpha-glucan Oligosaccharide', 'Alpha-Glucan'),
    ('E-ascorbic Acid', 'Jasmonic Acid'),
    ('7-Dehydrocholesterol', 'Cholesterol'),
    ('Isostearic Acid', 'Stearic Acid'),
    ('Palmitoyl Oligopeptide', 'Palmitoyl Tripeptide'),
    ('Polyhydroxystearic Acid', 'Stearic Acid'),
    ('Tocopheryl', 'Tocopherol'),
    ('Banana Extract', 'Chandan Extract'),
    ('Cetyl Palmitate', 'Retinyl Palmitate'),
    ('Gluconic Acid', 'Glycolic Acid'),
    ('Trehalosemethyl Trimethicone', 'Trehalose'),
    ('Triabsorb', 'Triasorb'),
    ('Acetyl Dipeptide 1 Cetyl Ester', 'Peptide'),
    ('Anhydroxylitol', 'Xylitol'),
    ('Capryloyl Glycerin', 'Capryloyl Glycine'),
    ('Hepta Sodium Hexa Carboxymethyl Dipeptide-12', 'Peptide'),
    ('Hydrogenated Rapeseed Oil', 'Rapeseed Oil'),
    ('Hydroxystearic Acid', 'Stearic Acid'),
    ('Oligopeptide- 68', 'Oligopeptide-1'),
    ('Olive Oil Peg-7 Esters', 'Olive Oil'),
    ('Orbignya Oleifera Seed Oil', 'Moringa Oleifera Seed Oil'),
    ('Palmitoyl Hexapeptide-14', 'Palmitoyl Hexapeptide-12'),
    ('Palmitoyl Tetrapeptide-10', 'Palmitoyl Tetrapeptide-7'),
    ('Shea Butter Ethyl Esters', 'Shea Butter'),
    ('Sodium Acrylate', 'Sodium Ascorbate'),
    ('Vitamin E Derivative', 'Vitamin E'),
    ('C Triglycerides', 'Acid/ Capric Triglyceride'),
    ('Caprylic', 'Caprylic Capric Triglyceride'),
    ('Ceramide 1', 'Ceramide NP'),
    ('Ceramide 2', 'Ceramide NP'),
    ('Citronellol Alpha-isomethyl Ionone', 'Alpha-Isomethyl Ionone'),
    ('Cocamidopropyl Dimethylamine', 'Cocamidopropyl Betaine'),
    ('Cyclotetrapeptide-24 Aminocyclohexane Carboxylate', 'Tetrapeptide'),
    ('Cymbopogon Schoenanthus Extract', 'Cymbopogon Schoenanthus Oil'),
    ('DM Water', 'Sea Water'),
    ('Dextrin Palmitate', 'Retinyl Palmitate'),
    ('Fractionated Coconut Oil', 'Coconut Oil'),
    ('Fructose And Glycerine', 'Glycerin'),
    ('Glycerin And Glyceryl Glucoside', 'Glyceryl Glucoside'),
    ('Glycol Distearate', 'Glyceryl Stearate'),
    ('Hydrated Silica', 'Silica'),
    ('Hydrolyzed Cicer Seed Extract', 'Hydrolyzed Rice Extract'),
    ('Lactobacillus Ferment Lysate', 'Lactococcus Ferment Lysate'),
    ('Oligopeptide-68', 'Oligopeptide-1'),
    ('Olus Oil', 'Lotus Oil'),
    ('Palmitoyl Tripeptide-8', 'Palmitoyl Tripeptide-5'),
    ('Pentapeptide-34 Trifluoroacetate', 'Peptide'),
    ('Peppermint Leaf Water', 'Peppermint'),
    ('Sesame Seed Oil Olive Fruit Oil', 'Olive Fruit Oil'),
    ('Sh-Oligopeptide-1', 'Oligopeptide-1'),
    ('Sh-Polypeptide-123', 'Polypeptide'),
    ('Sunflower Sprout Extract', 'Sunflower Seed Extract'),
    ('Synthetic Beeswax', 'Beeswax'),
    ('Tranexamoyl dipeptide-23', 'Peptide'),
    ('Trisiloxane', 'Drometrizole Trisiloxane / Mexoryl XL'),
    ('Xylitol Rhamnosis', 'Xylitol'),
    ('40 Hydrogenated Castor Oil', 'Peg-60 Hydrogenated Castor Oil'),
    ('Acetyl Dipeptide- 1 Cetyl Ester', 'Peptide'),
    ('Acetyl Dipeptide-1 Cetyl Ester', 'Peptide'),
    ('Acetyl Glutamine', 'acetyl glucosamine'),
    ('Acetyl Heptapeptide-4', 'Acetyl Heptapeptide-9'),
    ('Acetyl Hexapeptide-1', 'Acetyl Hexapeptide-8'),
    ('Acetyl Hexapeptide-4', 'Acetyl Hexapeptide-8'),
    ('Acetyl Tetrapeptide-15', 'Acetyl Tetrapeptide-11'),
    ('Acetyl Tetrapeptide-9', 'Acetyl Tetrapeptide-2'),
    ('Acetyl tetrapeptide-15', 'Acetyl Tetrapeptide-11'),
    ('AcetylDipeptide-1 Cetyl Ester', 'Peptide'),
    ('Alpha Arbutin Ammonium', 'Alpha Arbutin'),
    ('Aluminum Sucrose Octasulfate', 'Sucrose'),
    ('Anhydroxylitol And Xylitol', 'Xylitol'),
    ('Ascorbic Acid Polypeptide', 'Polypeptide'),
    ('Bala Root Extract', 'Bael Root Extract'),
    ('Bitter Orange Leaf', 'Orange'),
    ('C Cholesterol/lanosterol Esters', 'Cholesterol'),
    ('Caprylyl Methicone And Peg-12 Dimethicone', 'Dimethicone'),
    ('Ceramide NS', 'Ceramide NP'),
    ('Cetearyl', 'Cetearyl Alcohol'),
    ('Citrus Unshiu Peel Extract', 'Citrus Sinensis Peel Extract'),
    ('Copper', 'Copper Peptides'),
    ('Cyclopentasiloxane And Dimethicone', 'Dimethicone'),
    ('Cyclopentasiloxane And Dimethicone Crosspolymer', 'Dimethicone'),
    ('Cyclotetrapeptide-24Aminocyclohexane Carboxylate', 'Tetrapeptide'),
    ('Date Extract', 'Oat Extract'),
    ('Diethylhexyl', 'Iscotrizinol / Diethylhexyl Butamido Triazone / Uvasorb HEB'),
    ('Diethylhexyl Carbonate¸ Propanediol', 'Propanediol'),
    ('Diglucosyl Gallic Acid', 'Gallic Acid'),
    ('EGF Peptides', 'Peptides'),
    ('Ethoxydiglycol Ethylhexylglycerin', 'Ethylhexylglycerin'),
    ('Ethyl Alcohol', 'Cetyl Alcohol'),
    ('Fatty Acids', 'Omega fatty acids'),
    ('Glyceryl Acrylate', 'Glyceryl Stearate'),
    ('Glyceryl Glucoside And Alpha Arbutin', 'Glyceryl Glucoside'),
    ('Glyceryl Sesquistearate', 'Glyceryl Stearate'),
    ('Gold Peptide', 'Goldrella Peptide'),
    ('Grape Fruit Water', 'Grape Fruit Extract'),
    ('Heptapeptide-15 Palmitate', 'Peptide'),
    ('Heptyl Glucoside', 'Decyl Glucoside'),
    ('Hydrogenated Rapeseed Oil Methylpropanediol', 'Rapeseed Oil'),
    ('Hydrogeneated Olive Oil', 'Olive Oil'),
    ('Lithium Magnesium Sodium Silicate', 'Silica'),
    ('Magnesium Aluminium Silicate', 'Silica'),
    ('Mallotus Japonicus Leaf Extract', 'Mallotus Japonicus Bark Extract'),
    ('Manicouagan Sea Mineral Clay', 'Mineral Clay'),
    ('Melon Extract', 'Peony Extract'),
    ('Minerals', 'Mineral Oil'),
    ('Mushro0M Extract', 'Snow Mushroom Extract'),
    ('Mushroom Extract', 'Snow Mushroom Extract'),
    ('Nargis Flower Extract', 'Daisy Flower Extract'),
    ('Natural Humectants', 'Natural betaine'),
    ('Nicotinoyl Dipeptide-23', 'Peptide'),
    ('Nonapeptide', 'Peptide'),
    ('Oligopeptide-34', 'Oligopeptide-1'),
    ('Oligopeptides', 'Oligopeptide-1'),
    ('Olive Oil Derivatives', 'Olive Oil'),
    ('Olive Oil Glycereth-8 Esters', 'Olive Oil'),
    ('Olive Oil Methyl Ester', 'Olive Oil'),
    ('Olive Oil Polyglyceryl-6 Esters', 'Olive Oil'),
    ('Organic Acids', 'Tannic Acid'),
    ('P-refinyl', 'Pro-retinol'),
    ('Palmitoyl tetrapeptide-10', 'Palmitoyl Tetrapeptide-7'),
    ('Palmitoylpentapeptide-5', 'Palmitoyl Pentapeptide-4'),
    ('Papaya Fruit Enzyme', 'Papaya Fruit Extract'),
    ('Pear Fruit Extract', 'Papaya Fruit Extract'),
    ('Peg 40 Hydrogenated Castor Oil', 'Peg-60 Hydrogenated Castor Oil'),
    ('Phosphate', 'Sodium Ascorbyl Phosphate'),
    ('Pisum Sativum Peptide', 'Peptide'),
    ('Polyglycerin-3', 'Glycerin'),
    ('Polyoxyl 40 Hydrogenated Castor Oil', 'Peg-60 Hydrogenated Castor Oil'),
    ('Potato Starch', 'Potato Extract'),
    ('Preservative', 'Sodium Anisate (Natural Preservative)'),
    ('Propylene Glycol Monolaurate', 'Propylene Glycol'),
    ('Propylene Glycol Stearate Se', 'Propylene Glycol'),
    ('Retinol And Polysorbate 20', 'Retinol'),
    ('Rosa Centifolia Flower Juice', 'Rosa Centifolia Flower Water'),
    ('Safflower Oil', 'Starflower Oil'),
    ('Salicylate', 'Benzyl Salicylate'),
    ('Sea Minerals', 'Cooling Actives (Aloe + Sea Minerals + Cica)'),
    ('Sh-pentapeptide-5', 'Heptapeptide-6'),
    ('Shea Butter Glycerides', 'Shea Butter'),
    ('Shorea Stenoptera Seed Butter', 'Seed Butter'),
    ('Silica Silylate', 'Silica'),
    ('Sodium Carboxymethyl Beta- Glucan', 'Glucan'),
    ('Sodium Hydrolyzed Potato Starch Dodecenylsuccinate', 'Potato'),
    ('Sodium Lauroyl Aspartate', 'Sodium Lauroyl Sarcosinate'),
    ('Sodium Lauroyl Isethionate', 'Sodium Lauroyl Methyl Isethionate'),
    ('Soy Milk Peptide', 'Milk Peptides'),
    ('Squalane For Long-lasting', 'Squalane'),
    ('Sunflower Seed Cake', 'Sunflower Seed Wax'),
    ('Tapioca Starch Polymethylsilsesquioxane', 'Tapioca Starch'),
    ('Tocopherol Fragrance', 'Tocopherol'),
    ('Tocopheryl.', 'Tocopherol'),
    ('Triethoxysilyethyl Polydimethylsiloxyethyl Dimethicone', 'Dimethicone'),
    ('Vitamin Ball Ascorbic Acid', 'Vitamin C / Ascorbic Acid'),
    ('Vitamin C And Vitamin E', 'Vitamin E'),
    ('While It Moisturizes', 'Skin Moisturizers'),
    ('White Lily', 'Water Lily'),
    ('Zinc Hydrolyzed Hyaluronate', 'Hydrolyzed Sodium Hyaluronate'),
    ('sh-Oligopeptide-1', 'Oligopeptide-1'),
)


# --------------------------------------------------------------------------
# Hand-reviewed alias map
# --------------------------------------------------------------------------
# Only same-substance equivalences are listed here: a different salt, ester or
# hydrolysed form of the same molecule; a different preparation of the same
# botanical; or a pure naming variant. Chemically distinct actives are left
# unresolved on purpose and reported instead -- for example Kojic Acid is NOT
# mapped to Kojic Dipalmitate, and Madecassoside is NOT mapped to Madecassic
# Acid.

HAND_ALIASES = {
    # Hyaluronic acid family (salt / hydrolysed / blend names)
    "sodium hyaluronate": "Hyaluronic Acid",
    "hydrolyzed sodium hyaluronate": "Hyaluronic Acid",
    "hydrolyzed hyaluronic acid": "Hyaluronic Acid",
    "hyaluronic acid complex": "Hyaluronic Acid",
    # Vitamin E family (tocopherol IS vitamin E; acetate is its ester)
    "tocopherol": "Vitamin E",
    "tocopheryl acetate": "Vitamin E",
    "vitamin e acetate": "Vitamin E",
    "tocotrienol (vitamin e)": "Vitamin E",
    "pre-tocopheryl": "Vitamin E",
    # Ceramides
    "ceramide complex": "Ceramides",
    "6-ceramide complex": "Ceramides",
    "ceramides np/ap/eop": "Ceramides",
    "pro-ceramides": "Ceramides",
    "ceramide 3": "Ceramide NP",  # Ceramide 3 is the former name of Ceramide NP
    # Vitamin C (only the true L-ascorbic acid synonym; derivatives are not
    # folded in, because the sheet scores each derivative separately)
    "l-ascorbic acid": "Vitamin C / Ascorbic Acid",
    # Spacing variant of the entry above. Previously reached the same target
    # through the compact index; now that aliases no longer seed that index,
    # it is listed explicitly so the mapping is unchanged and visible.
    "l- ascorbic acid": "Vitamin C / Ascorbic Acid",
    "l ascorbic acid": "Vitamin C / Ascorbic Acid",
    # Botanicals: same plant, different preparation
    "licorice root extract": "Licorice Extract",
    "licorice root": "Licorice Extract",
    "aloe vera extract": "Aloe Vera",
    "aloe leaf water": "Aloe Vera",
    "aloe": "Aloe Vera",
    "witch hazel extract": "Witch Hazel",
    "macadamia oil": "Macadamia Seed Oil",
    "sunflower oil": "Sunflower Seed Oil",
    "peony root extract": "Peony Extract",
    "centella asiatica extract": "Centella Extract",
    "centella asiatica leaf water": "Centella Asiatica",
    "cica complex": "Centella Asiatica",  # "Cica" is shorthand for Centella
    "cica 7-complex": "Centella Asiatica",
    "soapwort extract": "Saponaria Officinalis Leaf Extract (Soapwort)",
    "goji berry extract": "Goji Extract",
    "clove extract": "Clove Bud Extract (Syzygium Aromaticum)",
    "chamomile": "Chamomile Extract",
    "pineapple fruit water": "Pineapple Water",
    "cucumber fruit water": "Cucumber Water Extract",
    "pomegranate pericarp extract": "Pomegranate Extract",
    "terminalia arjuna extract": "Organic Bark Extract of Terminalia Arjuna",
    "rice extract/ferment": "Rice Extract",
    "sweet almond extract": "Prunus Amygdalus Dulcis (Almond)",
    # Minerals / physical forms of one material
    "kaolin": "Kaolin Clay",
    "bentonite": "Bentonite Clay",
    "cellulose": "Cellulose Powder",
    "cellulose beads": "Cellulose Powder",
    # Proteins and peptides (hydrolysed form of the same protein)
    "hydrolyzed elastin": "Elastin",
    "hydrolyzed soy protein": "Hydrolyzed Soy Peptide",
    "copper tripeptide-1": "Copper tripeptide",
    # Naming variants
    "beta-glucan": "Beta-Glucan Complex",
    "alpha-arbutin": "Alpha Arbutin",
    "bifida ferment lysate": "Bifida Ferment Filtrate",
    "aha complex": "AHA",
    "amino acid complex": "Amino Acid",
    "nmf amino acid complex": "NMF (Amino Acid Complex)",
    "amino acid/nmf complex": "NMF (Amino Acid Complex)",
    # --- added for the previous 3,219-product dataset, retained ------------
    # "Ethyl Ascorbic Acid" is the trade shorthand for the only commercial
    # form of that molecule, 3-O-Ethyl Ascorbic Acid.
    "ethyl ascorbic acid": "3-O-Ethyl Ascorbic Acid",
    # Plant part -> the sheet's generic entry for the same botanical, the
    # same equivalence already used by "pomegranate pericarp extract".
    "camellia flower extract": "Camellia Extract",
    "coconut fruit extract": "Coconut Extract",
    "lemon extract": "Citrus Lemon Extract",
    "lemon fruit extract": "Citrus Lemon Extract",
    # "Q.S" (quantum satis) is a formulation quantity note, not a different
    # substance. The sheet keeps "Purified Water-Q.S" as its own row, so this
    # alias is registered after every exact canonical name and cannot shadow it.
    "vitamin e - q.s": "Vitamin E",
    # Spelling variants and typos of the same substance. Each was confirmed
    # against the sheet by hand; none changes which molecule is being scored.
    "gycerin": "Glycerin",
    "capryly| glycol": "Caprylyl Glycol",          # corrupted "l" in the source
    "hydrolysed collagen": "Hydrolyzed Collagen",  # British spelling
    "hydroxycitrolellal": "Hydroxycitronellal",
    "ethylhexyglycerin": "Ethylhexylglycerin",
    "ethylhexylglycerine": "Ethylhexylglycerin",
    "hesperetine laurate": "Hesperetin Laurate",
    # Synonyms for one molecule.
    "glycerol stearate": "Glyceryl Stearate",
    "coco amido propyl betaine": "Cocamidopropyl Betaine",
    "dipotassium glycyrrhizinate": "Dipotassium Glycyrrhizate",
    "4-n-butylresorcinol": "4-Butylresorcinol",    # "n-" = straight chain
    "alpha tocopheryl acetate": "Tocopheryl Acetate",
    "veg collagen": "Vegan Collagen",
    "aloevera leaf juice": "Aloe Leaf Juice",
    "morus alba extract": "Mulberry (Morus alba) Extract",
    "lemon peel oil": "Citrus Limon (Lemon) Peel Oil",
    # Named plant part -> the sheet's entry for the same botanical and the same
    # preparation, the equivalence already used by "pomegranate pericarp
    # extract" -> "Pomegranate Extract". Oils are deliberately excluded: for an
    # oil the plant part changes the material.
    "onion bulb extract": "Onion Extract",
    "jasmine flower extract": "Jasmine Extract",
    "strawberry fruit extract": "Strawberry Extract",
    "watermelon fruit extract": "Watermelon Extract",
    "cherry blossom flower extract": "Cherry Blossom Extract",
    "amla fruit extract": "Amla Extract",
    "blueberry fruit extract": "Blueberry Extract",
    "cabbage leaf extract": "Cabbage Extract",
    "soybean seed extract": "Soybean Extract",
    "yuzu seed extract": "Yuzu Extract",
    "sandalwood wood powder": "Sandalwood Powder",
    "cocoa seed butter": "Cocoa Butter",
    "cupuacu seed butter": "Cupuacu Butter",
    # --- reviewed SAFE_TO_MAP additions ---------------------------------
    # From output/unmatched_ingredient_mapping_review.csv: formatting
    # variations, spelling variants and unambiguous synonyms of the same
    # substance.
    '1% niacinamdide': 'Niacinamide',  # HIGH / Formatting variation
    '3-o-ethylascorbic acid (vitamin c': '3-O-Ethyl Ascorbic Acid',  # HIGH / Formatting variation
    'acetyl tetrapeptide_x001d_2': 'Acetyl Tetrapeptide-2',  # HIGH / Formatting variation
    'alpha bisabolol': 'Bisabolol',  # MEDIUM / Chemical naming equivalence
    'amla fruitextract - 2.0%': 'Amla Extract',  # HIGH / Formatting variation
    'apple fruit water': 'Pyrus Malus (Apple) Fruit Water',  # HIGH / INCI/common-name equivalence
    'ceramide iii': 'Ceramide NP',  # MEDIUM / Chemical naming equivalence
    'coconutoil - 15.0%': 'Coconut Oil',  # HIGH / Formatting variation
    'corn starch - 89.0%': 'Cornstarch (Zea Mays)',  # HIGH / Formatting variation
    'corn starch -88.0%': 'Cornstarch (Zea Mays)',  # HIGH / Formatting variation
    'corn starch -88.5%': 'Cornstarch (Zea Mays)',  # HIGH / Formatting variation
    'cornstarch - 89.0%': 'Cornstarch (Zea Mays)',  # HIGH / Formatting variation
    'ghee - 10.00%': 'Ghee',  # HIGH / Formatting variation
    'glicerina': 'Glycerin',  # HIGH / INCI/common-name equivalence
    'glycerol': 'Glycerin',  # HIGH / Chemical naming equivalence
    'glyceryl monostearate': 'Glyceryl Stearate',  # HIGH / Chemical naming equivalence
    'glycine soja germ extract': 'Soy Germ Extract',  # HIGH / Botanical naming variation
    'honey - 0.1%': 'Honey',  # HIGH / Formatting variation
    'honey - 0.5%': 'Honey',  # HIGH / Formatting variation
    'honey - 1.5%': 'Honey',  # HIGH / Formatting variation
    'honey** - 0.5%': 'Honey',  # HIGH / Formatting variation
    'honey-1.0%': 'Honey',  # HIGH / Formatting variation
    'human adipocyte conditioned media extract': 'Stem Cells (Human Adipocyte Conditioned Media Extract)',  # HIGH / Exact semantic equivalent
    'hyaluronic complex': 'Hyaluronic Acid Complex',  # HIGH / Formatting variation
    'menthol crystals': 'Menthol',  # MEDIUM / Formatting variation
    'mixed tocopherol': 'Tocopherol',  # MEDIUM / Chemical naming equivalence
    'natural yuzu extract': 'Yuzu Extract',  # HIGH / Formatting variation
    'oatmealpowder - 5.0%': 'Oatmeal Powder (Hordeum Vulgare)',  # HIGH / Formatting variation
    'organic shea butter': 'Shea Butter',  # HIGH / Formatting variation
    'passiflora edulis fruit extract': 'Passion Fruit Extract',  # HIGH / Botanical naming variation
    'sodium benzoate - q.s': 'Sodium Benzoate',  # HIGH / Formatting variation
    'sodium laureth sulphate': 'Sodium Laureth Sulfate / SLES',  # HIGH / Formatting variation
    'sodium lauryl ether sulphate': 'Sodium Laureth Sulfate / SLES',  # HIGH / Chemical naming equivalence
    'veg squalane': 'Vegetable Squalane',  # HIGH / Formatting variation
    'vegan ceramide np': 'Ceramide NP',  # HIGH / Formatting variation
    'vit e': 'Vitamin E',  # HIGH / Formatting variation
    # --- reviewed REVIEW_REQUIRED additions, approved by the domain expert
    # From the same review file. These were flagged as needing a human
    # decision (source named but material unspecified, species vs genus,
    # abbreviated or truncated names) and were approved for mapping.
    '3-propanediol': 'Propanediol',  # MEDIUM / Chemical naming equivalence
    'acerola fruit extract': 'Acerola Cherry Extract',  # MEDIUM / Botanical naming variation
    'alcohol denat.37% c abyter': 'Alcohol Denat.',  # LOW / Formatting variation
    'algae extract 1': 'Algae Extract',  # LOW / Formatting variation
    'aloe extract 5% w': 'Aloe Extract',  # LOW / Formatting variation
    'ashwagandha': 'Ashwagandha Extract',  # MEDIUM / Same source but material unspecified
    'avocado': 'Avocado Oil',  # MEDIUM / Same source but material unspecified
    'bacteria horehound extract': 'Horehound extract',  # LOW / Formatting variation
    'bamboo vulgaris extract': 'Bamboo Extract',  # MEDIUM / Botanical naming variation
    'black grape extract': 'Grape Extract',  # LOW / Botanical naming variation
    'blueberry seed oil': 'Upcycled Blueberry Seed Oil',  # MEDIUM / Formatting variation
    'butrospermum parkii butter': 'Shea Butter',  # MEDIUM / Botanical naming variation
    'calamine': 'Calamine Clay',  # MEDIUM / Same source but different material
    'camellia oleifera seed oil': 'Camellia Oil',  # MEDIUM / Botanical naming variation
    'camellia sinesis leaf extract': 'Camellia Extract',  # MEDIUM / Botanical naming variation
    'capsicum annuum fruit extract': 'Capsicum Extract',  # MEDIUM / Botanical naming variation
    'carrot seed': 'Carrot Seed Oil',  # MEDIUM / Same source but material unspecified
    'ceramide iii b': 'Ceramide NP',  # LOW / Chemical naming equivalence
    'chondrus crispsus': 'Chondrus Crispus Extract',  # MEDIUM / Same source but material unspecified
    'cica extract': 'Centella Extract',  # MEDIUM / INCI/common-name equivalence
    'citrus paradisi fruit extract': 'Grapefruit Extract',  # LOW / Botanical naming variation
    'coconut': 'Coconut Oil',  # MEDIUM / Same source but material unspecified
    'coffee bean extract': 'Coffee Extract',  # MEDIUM / Botanical naming variation
    'copper peptide-1 (ghk-cu)': 'Copper tripeptide',  # MEDIUM / Chemical naming equivalence
    'cucumber': 'Cucumber Extract',  # MEDIUM / Same source but material unspecified
    'dictyopteris membranacea': 'Dictyopteris Membranacea Extract',  # MEDIUM / Same source but material unspecified
    'geranium robertianum extract': 'Geranium extract',  # LOW / Botanical naming variation
    'ginger root': 'Ginger Root Oils',  # MEDIUM / Same source but material unspecified
    'giycerin shea butter': 'Shea Butter',  # LOW / No reliable equivalence
    'goat milk': 'Goat Milk Powder',  # MEDIUM / Same source but material unspecified
    'graperuit fruit extract': 'Grapefruit Extract',  # LOW / Botanical naming variation
    'green tea': 'Green Tea Oil',  # MEDIUM / Same source but material unspecified
    'hyaluronate': 'Sodium Hyaluronate',  # MEDIUM / Chemical naming equivalence
    'hyperpigmentation and melasma oligopeptide-10': 'Oligopeptide-10',  # LOW / Formatting variation
    'iris florentina root extract': 'Iris Root Extract',  # MEDIUM / Botanical naming variation
    'jasmine': 'Jasmine Oil',  # MEDIUM / Same source but material unspecified
    'lactobacillus': 'Lactobacillus Ferment (Milk Probiotic)',  # LOW / Same source but different material
    'laminaria japonica': 'Laminaria Japonica Extract',  # MEDIUM / Same source but material unspecified
    'lanolin': 'Lanolin Oil',  # MEDIUM / Same source but material unspecified
    'marigold': 'Marigold Extract',  # MEDIUM / Same source but material unspecified
    'olea europaea': 'Olea Europaea (Olive) Fruit Oil',  # MEDIUM / Botanical naming variation
    'orchid extract': 'Orchid Flower Extract',  # LOW / Same source but different material
    'phytosteryl': 'Phytosterol',  # LOW / Chemical naming equivalence
    'pitera - rich in vitamins': 'Pitera',  # LOW / Formatting variation
    'prickly pear flower': 'Prickly Pear Flower Extract',  # LOW / Same source but different material
    'retinyl propionate': 'Vitamin A (Retinyl Propionate)',  # MEDIUM / Exact semantic equivalent
    'saccharomyces ferment filtrate': 'Saccharomyces ferment',  # LOW / Same source but different material
    'saccharomyces ferment lysate filtrate': 'Saccharomyces ferment',  # LOW / Same source but different material
    'sodium c -16 olefin sulfonate': 'Sodium C14-16 Olefin Sulfonate',  # MEDIUM / Formatting variation
    'sodium ha': 'Sodium Hyaluronate',  # MEDIUM / Chemical naming equivalence
    'spiraea ulmaria extract': 'Spiraea Ulmaria Flower Extract',  # LOW / Same source but different material
    'sunflower seed': 'Sunflower',  # MEDIUM / Same source but material unspecified
    'sweet orange extract': 'Orange Extract',  # MEDIUM / Botanical naming variation
    'theobroma cacao shell extract': 'Theobroma Cacao Extract',  # LOW / Same source but different material
    'vetiveria zizanioides root powder': 'Vetiver',  # LOW / Same source but different material
    'vetiveria zizanioides root water': 'Vetiver',  # LOW / Same source but different material
    'vetiveria zizanoides root extract': 'Vetiver',  # LOW / Same source but different material
    'vitamin b3': 'Niacinamide',  # MEDIUM / INCI/common-name equivalence
    'vitamin b5': 'Panthenol',  # MEDIUM / INCI/common-name equivalence
    'vitamin e oil': 'Vitamin E',  # MEDIUM / Same source but different material
    'vitamin-b3': 'Niacinamide',  # MEDIUM / INCI/common-name equivalence
}


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------


def normalise(text) -> str:
    """Lower-case, collapse whitespace, unify dashes, strip edge punctuation."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("–", "-").replace("—", "-").replace("’", "'")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value.strip(".,;:*")


def _strip_parentheticals(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def _leading_percent(text: str):
    """Split '2% Salicylic Acid' into ('2', 'Salicylic Acid')."""
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*%\s*(.+)$", text)
    return (match.group(1), match.group(2).strip()) if match else (None, None)


def _strip_trailing_percent(text: str) -> str:
    return re.sub(r"\s*\d+(?:\.\d+)?\s*%\s*$", "", text).strip()


def _compact(text: str) -> str:
    """Normalise, then drop every character that is not a letter or a digit.

    Used only as a last-resort lookup key, and only for compact keys owned by
    exactly one canonical name. It absorbs spacing, hyphen and punctuation
    differences ("Ascorbylpalmitate" -> "Ascorbyl Palmitate") while leaving
    digits in place, so names that differ by a number, salt or ester stay
    distinct: "PEG 40 Hydrogenated Castor Oil" never reaches "Peg-60
    Hydrogenated Castor Oil", and "Palmitoylpentapeptide-5" never reaches
    "Palmitoyl Pentapeptide-4".
    """
    return re.sub(r"[^a-z0-9]", "", normalise(text))


def split_ingredient_list(cell) -> list[str]:
    """Split a semicolon-delimited cell, dropping 'no value' placeholders."""
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    items = []
    for token in str(cell).split(";"):
        token = token.strip()
        if token and normalise(token) not in NULL_TOKENS:
            items.append(token)
    return items


# --------------------------------------------------------------------------
# Ingredient resolver
# --------------------------------------------------------------------------


class IngredientResolver:
    """Maps a free-text ingredient name onto a canonical scores-sheet row.

    Keys are registered in three passes so that stronger evidence always wins:

      1. every canonical name exactly as the sheet spells it,
      2. its safe variants -- each part of an "A / B / C" alias chain, the name
         with parentheticals removed, and a singular/plural counterpart,
      3. the hand-reviewed alias map.

    Registering every exact name before any variant is what makes "exact match
    first" actually hold. Previously all variants of one row were registered
    before the next row's exact name was seen, so a variant could shadow a real
    canonical name -- "Salicylic Acid" resolved to "BHA/Salicylic Acid", and 26
    other canonical names likewise did not resolve to themselves.

    After every candidate has failed, a collision-safe compact index is
    consulted: keys with case, spacing, hyphens and punctuation removed, and
    only for compact keys owned by exactly one canonical name. Compact keys
    claimed by two or more different canonical names are dropped as ambiguous
    rather than guessed at. That index covers the canonical vocabulary only --
    alias keys are excluded, so an approved alias maps its own reviewed
    spelling and nothing else.

    There is no fuzzy matching anywhere in this class. Similarity is computed
    only by the audit, only to explain an unmatched name, and never to map one.
    """

    KIND_CANONICAL = "canonical"
    KIND_VARIATION = "variation"
    KIND_ALIAS = "alias"

    def __init__(self, canonical_names, exclusions=(), domain_mappings=(),
                 final_exceptions=None) -> None:
        self._keys: dict[str, str] = {}
        self._kinds: dict[str, str] = {}
        self._canonical = list(canonical_names)
        known = {str(c).strip() for c in self._canonical}

        # STEP 1 data -- hard exclusions. Highest precedence of all.
        self._excluded = {normalise(x) for x in exclusions if normalise(x)}

        # STEP 2 data -- domain-reviewed mappings. Each approved target must
        # exist in the workbook; a missing target is recorded and the mapping is
        # dropped rather than redirected to something that looks similar.
        self._domain: dict[str, str] = {}
        self.invalid_domain_targets = []
        self.duplicate_domain_sources = []
        self.domain_overriding_exclusion = []
        self.domain_overriding_exact = []
        for source, target in domain_mappings:
            key = normalise(source)
            if not key:
                continue
            if key in self._excluded:
                self.domain_overriding_exclusion.append((source, target))
                continue
            if target not in known:
                self.invalid_domain_targets.append((source, target))
                continue
            if key in self._domain:
                self.duplicate_domain_sources.append((source, target))
                continue
            self._domain[key] = target
            if str(source).strip() in known:
                self.domain_overriding_exact.append((source, target))

        # STEP 5 data -- the four explicit exceptions, targets verified too.
        self._exceptions: dict[str, str] = {}
        self.invalid_exception_targets = []
        for key, target in (final_exceptions or {}).items():
            if target in known:
                self._exceptions[normalise(key)] = target
            else:
                self.invalid_exception_targets.append((key, target))

        # Pass 1 -- exact canonical names.
        for name in self._canonical:
            self._register(name, name, self.KIND_CANONICAL)

        # Pass 2 -- safe variants, plus a singular/plural counterpart of each.
        for name in self._canonical:
            for variant in self._canonical_variants(name):
                key = normalise(variant)
                self._register(key, name, self.KIND_VARIATION)
                if key:
                    plural = key[:-1] if key.endswith("s") else key + "s"
                    self._register(plural, name, self.KIND_VARIATION)

        # Pass 3 -- hand aliases, skipped if their target is not in the sheet.
        known = set(self._canonical)
        self.missing_alias_targets = []
        for alias, target in HAND_ALIASES.items():
            if target not in known:
                self.missing_alias_targets.append((alias, target))
                continue
            self._register(alias, target, self.KIND_ALIAS)

        # Compact index, built from the canonical vocabulary only. A compact
        # key claimed by more than one canonical name is ambiguous and is
        # discarded, never resolved to one of them.
        #
        # Alias keys are deliberately NOT indexed here. An alias is a reviewed,
        # hand-approved equivalence for one exact spelling; letting it seed the
        # compact index would silently extend that approval to every spacing and
        # punctuation neighbour of the alias. That is how "Oleaeuropaea" -- a
        # name reviewed and classified NO_CANDIDATE -- picked up the approved
        # "Olea Europaea" mapping without ever being approved itself. A spelling
        # variant of an alias that genuinely should map is added as its own
        # alias instead, so every alias mapping stays explicit and reviewable.
        owners: dict[str, set[str]] = {}
        kinds: dict[str, str] = {}
        for key, canonical in self._keys.items():
            if self._kinds[key] == self.KIND_ALIAS:
                continue
            compact_key = _compact(key)
            if not compact_key:
                continue
            owners.setdefault(compact_key, set()).add(canonical)
            kinds.setdefault(compact_key, self._kinds[key])
        self._compact = {
            k: (next(iter(v)), kinds[k]) for k, v in owners.items() if len(v) == 1
        }
        self.ambiguous_compact_keys = sorted(k for k, v in owners.items() if len(v) > 1)

    @staticmethod
    def _canonical_variants(name: str) -> list[str]:
        parts = [name] + [p.strip() for p in re.split(r"\s*/\s*", name)]
        parts += [_strip_parentheticals(p) for p in list(parts)]
        return [p for p in parts if p]

    def _register(self, key: str, canonical: str, kind: str) -> None:
        key = normalise(key)
        if not key or key in self._keys:
            return
        self._keys[key] = canonical
        self._kinds[key] = kind

    def _lookup(self, text: str):
        """Exact key, then a singular/plural counterpart. Returns (name, kind)."""
        key = normalise(text)
        if not key:
            return None
        if key in self._keys:
            return self._keys[key], self._kinds[key]
        alt = key[:-1] if key.endswith("s") else key + "s"
        if alt in self._keys:
            return self._keys[alt], self.KIND_VARIATION
        return None

    @staticmethod
    def _candidates(raw_name: str) -> list[str]:
        """The candidate spellings to try, in order. Unchanged behaviour.

        "2% Salicylic Acid" prefers the concentration-specific row
        ("Salicylic Acid 2%") before the base ingredient, because concentration
        rows carry their own distinct scores.
        """
        candidates = [raw_name]
        pct, base = _leading_percent(raw_name)
        if base:
            candidates.append(f"{base} {pct}%")
            candidates.append(base)
        candidates.append(_strip_trailing_percent(raw_name))
        for candidate in list(candidates):
            stripped = _strip_parentheticals(candidate)
            if stripped:
                candidates.append(stripped)
        return candidates

    def resolve_with_method(self, raw_name: str):
        """Return (canonical_or_None, method) using the fixed priority order.

        EXCLUDED and NEEDS_DOMAIN_REVIEW both return None for the canonical --
        neither is scored -- but the method distinguishes them so the audit can
        report an excluded ingredient separately from an unresolved one.
        """
        key = normalise(raw_name)

        # STEP 1 -- hard exclusion list wins over everything else.
        if key in self._excluded:
            return None, METHOD_EXCLUDED

        # STEP 2 -- domain-approved mapping.
        if key in self._domain:
            return self._domain[key], METHOD_DOMAIN

        # STEP 3 -- the name is itself a canonical workbook entry.
        if key in self._keys and self._kinds[key] == self.KIND_CANONICAL:
            return self._keys[key], METHOD_EXACT

        # STEP 4 -- safe deterministic normalisation: canonical variants,
        # singular/plural, percentages, parentheticals, hand aliases, and the
        # collision-safe compact index. No fuzzy matching.
        for candidate in self._candidates(raw_name):
            hit = self._lookup(candidate)
            if hit:
                return hit[0], METHOD_SAFE
        compact_key = _compact(raw_name)
        if compact_key in self._compact:
            return self._compact[compact_key][0], METHOD_SAFE

        # STEP 5 -- the four explicit final exceptions.
        if key in self._exceptions:
            return self._exceptions[key], METHOD_EXCEPTION

        # STEP 6 -- leave it unresolved. Never guess.
        return None, METHOD_NEEDS_REVIEW

    def resolve(self, raw_name: str):
        """Return the canonical ingredient name, or None if unresolved/excluded."""
        return self.resolve_with_method(raw_name)[0]


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


@dataclass
class Profile:
    """A user profile. `concerns` may hold zero or more concern labels."""

    skin_type: str
    concerns: list[str] = field(default_factory=list)
    age_group: str | None = None
    sensitive: bool = False
    life_stages: list[str] = field(default_factory=list)
    label: str | None = None

    def name(self) -> str:
        if self.label:
            return self.label
        bits = [self.skin_type + (" + Sensitive" if self.sensitive else "")]
        bits += list(self.concerns)
        if self.age_group:
            bits.append(self.age_group)
        bits += [s.title() for s in self.life_stages]
        return " + ".join(bits)

    def score_columns(self, include_concerns: bool = True) -> dict[str, str]:
        """Ordered {attribute label -> score column} for this profile.

        The profile always carries every attribute the user selected. Whether
        the concern columns are actually scored is decided per product category
        by CATEGORY_RULES -- sunscreens and moisturisers pass
        include_concerns=False, which drops them from the average.
        """
        columns: dict[str, str] = {}

        skin_key = (normalise(self.skin_type), bool(self.sensitive))
        if skin_key not in SKIN_TYPE_COLUMNS:
            raise ValueError(
                f"Unknown skin type {self.skin_type!r} "
                f"(sensitive={self.sensitive}). "
                f"Known: {sorted({k[0] for k in SKIN_TYPE_COLUMNS})}"
            )
        label = self.skin_type + ("+Sensitive" if self.sensitive else "")
        columns[label] = SKIN_TYPE_COLUMNS[skin_key]

        for concern in self.concerns:
            key = normalise(concern)
            if key not in CONCERN_COLUMNS:
                raise ValueError(
                    f"Unknown concern {concern!r}. "
                    f"Known: {sorted(CONCERN_COLUMNS)}"
                )
            if include_concerns:
                columns[concern] = CONCERN_COLUMNS[key]

        if self.age_group:
            key = normalise(self.age_group)
            if key not in AGE_GROUP_COLUMNS:
                raise ValueError(
                    f"Unknown age group {self.age_group!r}. "
                    f"Known: {sorted(AGE_GROUP_COLUMNS)}"
                )
            columns[self.age_group] = AGE_GROUP_COLUMNS[key]

        for stage in self.life_stages:
            key = normalise(stage)
            if key not in LIFE_STAGE_COLUMNS:
                raise ValueError(
                    f"Unknown life stage {stage!r}. "
                    f"Known: {sorted(LIFE_STAGE_COLUMNS)}"
                )
            columns[stage.title()] = LIFE_STAGE_COLUMNS[key]

        return columns

    def excluded_attributes(self, include_concerns: bool = True) -> list[str]:
        """Profile attributes deliberately not scored for this category."""
        return [] if include_concerns else list(self.concerns)


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------


def read_product_dataset(path):
    """Read the product catalogue from .xlsx or .csv.

    File format only. The columns, the ingredient parsing and every scoring
    rule are identical either way -- this exists because the catalogue is
    distributed as a workbook and pandas needs the matching reader.
    """
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return pd.read_excel(path)
    return pd.read_csv(path)


def load_data():
    """Load the three datasets and return (products, scores_by_name, resolver)."""
    for path in (PRODUCT_DATASET_FILE, INGREDIENT_SCORES_FILE):
        if not path.exists():
            raise FileNotFoundError(f"Required dataset not found: {path}")

    products = read_product_dataset(PRODUCT_DATASET_FILE)

    missing = [c for c in REQUIRED_PRODUCT_COLUMNS if c not in products.columns]
    if missing:
        raise KeyError(
            f"{PRODUCT_DATASET_FILE.name} is missing column(s): {missing}"
        )

    # The dataset on disk is never written to. The ingredient columns are parsed
    # into lists on the in-memory copy (the original strings are kept alongside
    # as *_raw for debugging), and `category` is mirrored onto the
    # `assigned_category` name the scoring code already reads, so score_product
    # and the category rules are untouched.
    products["primary_raw"] = products["primary_ingredients"]
    products["secondary_raw"] = products["secondary_ingredients"]
    products["primary_ingredients"] = products["primary_raw"].apply(split_ingredient_list)
    products["secondary_ingredients"] = products["secondary_raw"].apply(split_ingredient_list)
    products["assigned_category"] = products["category"]

    scores = pd.read_excel(INGREDIENT_SCORES_FILE, sheet_name=INGREDIENT_SCORES_SHEET)
    canonical_col = canonical_column(scores)
    scores = scores[scores[canonical_col].notna()].copy()
    scores[canonical_col] = scores[canonical_col].astype(str).str.strip()
    scores = scores[scores[canonical_col] != ""]
    scores_by_name = {row[canonical_col]: row for _, row in scores.iterrows()}

    exclusions = load_exclusions()
    domain_mappings = load_domain_mappings()
    resolver = IngredientResolver(
        scores[canonical_col],
        exclusions=exclusions,
        domain_mappings=domain_mappings,
        final_exceptions=FINAL_EXCEPTIONS,
    )
    resolver.canonical_column = canonical_col
    return products, scores_by_name, resolver


def load_exclusions():
    """Names on the hard exclusion list.

    The CSV in data/ wins when it is present, so a reviewer can override the
    embedded defaults without editing code; otherwise EXCLUDED_INGREDIENTS is
    used and no file is required at runtime.
    """
    if EXCLUDED_INGREDIENTS_FILE.exists():
        frame = pd.read_csv(EXCLUDED_INGREDIENTS_FILE)
        return [str(x) for x in frame["ingredient"].dropna()]
    return list(EXCLUDED_INGREDIENTS)


def load_domain_mappings():
    """(source, approved target) pairs of domain-approved mappings.

    The CSV in data/ wins when it is present; otherwise the embedded
    DOMAIN_REVIEWED_MAPPINGS table is used and no file is required at runtime.
    Every row present is an approved mapping; the per-row decision and candidate
    score columns are informational and are not read.
    """
    if DOMAIN_MAPPINGS_FILE.exists():
        frame = pd.read_csv(DOMAIN_MAPPINGS_FILE)
        return [
            (str(r["Unmatched Ingredient"]), str(r["Best Canonical Ingredient"]).strip())
            for _, r in frame.iterrows()
            if pd.notna(r["Unmatched Ingredient"]) and pd.notna(r["Best Canonical Ingredient"])
        ]
    return list(DOMAIN_REVIEWED_MAPPINGS)


# --------------------------------------------------------------------------
# Step 1 -- ingredient level score
# --------------------------------------------------------------------------


def score_ingredient(raw_name, resolver, scores_by_name, columns):
    """Score one ingredient against the profile.

    Returns a dict with the canonical match, the per-attribute values, the
    averaged ingredient score (None when nothing could be averaged), and the
    reason it was excluded if it was.
    """
    result = {
        "raw_name": raw_name,
        "canonical": None,
        "values": {},
        "score": None,
        "disqualifying": False,
        "excluded_reason": None,
        "mapping_method": None,
    }

    canonical, method = resolver.resolve_with_method(raw_name)
    result["mapping_method"] = method
    if canonical is None:
        # Excluded and unresolved ingredients are both dropped from the
        # averages and are never scored 0; only the reported reason differs.
        result["excluded_reason"] = (
            "excluded_ingredient" if method == METHOD_EXCLUDED
            else "unmatched_ingredient"
        )
        return result

    result["canonical"] = canonical
    row = scores_by_name[canonical]

    values = {}
    for attribute, column in columns.items():
        raw = row.get(column)
        values[attribute] = None if pd.isna(raw) else int(raw)
    result["values"] = values

    present = [v for v in values.values() if v is not None]
    if not present:
        result["excluded_reason"] = "no_scores_for_profile"
        return result

    # Rule 4: -100 is a disqualifying flag, not a number to average in.
    result["disqualifying"] = any(v == DISQUALIFYING for v in present)
    usable = [v for v in present if v != DISQUALIFYING]
    if not usable:
        result["excluded_reason"] = "all_scores_disqualifying"
        return result

    result["score"] = sum(usable) / len(usable)
    return result


# --------------------------------------------------------------------------
# Steps 2 and 3 -- group averages and weighting
# --------------------------------------------------------------------------


def rule_for_category(category) -> CategoryRule:
    """Return the CategoryRule for a product category.

    Read from the product dataset's category field exactly as it is stored --
    never inferred from the product name and never split or re-interpreted.
    Unlisted, missing or unknown categories ("Others", "Body Care") fall
    through to DEFAULT_RULE (all attributes, 50/50).
    """
    return CATEGORY_RULES.get(normalise(category), DEFAULT_RULE)


def score_product(product_row, resolver, scores_by_name, profile):
    """Score one product. Returns a dict of results plus full diagnostics.

    The category decides both which profile attributes are scored and how the
    two ingredient groups are weighted, so the score columns are resolved per
    product rather than once per profile.
    """
    category = product_row.get("assigned_category")
    rule = rule_for_category(category)
    columns = profile.score_columns(include_concerns=rule.use_concerns)
    excluded = profile.excluded_attributes(include_concerns=rule.use_concerns)
    primary = [
        score_ingredient(n, resolver, scores_by_name, columns)
        for n in product_row["primary_ingredients"]
    ]
    secondary = [
        score_ingredient(n, resolver, scores_by_name, columns)
        for n in product_row["secondary_ingredients"]
    ]

    primary_scored = [d for d in primary if d["score"] is not None]
    secondary_scored = [d for d in secondary if d["score"] is not None]

    primary_avg = (
        sum(d["score"] for d in primary_scored) / len(primary_scored)
        if primary_scored
        else None
    )
    secondary_avg = (
        sum(d["score"] for d in secondary_scored) / len(secondary_scored)
        if secondary_scored
        else None
    )

    # Step 3: apply the category weighting, but renormalise to 100% when a
    # whole group is absent or entirely unresolved (rule 4).
    if primary_avg is not None and secondary_avg is not None:
        primary_weight = rule.primary_weight
        secondary_weight = rule.secondary_weight
        basis = "primary_and_secondary"
    elif primary_avg is not None:
        primary_weight, secondary_weight = 1.0, None
        basis = "primary_only_renormalised"
    elif secondary_avg is not None:
        primary_weight, secondary_weight = None, 1.0
        basis = "secondary_only_renormalised"
    else:
        primary_weight = secondary_weight = None
        basis = "unscorable"

    primary_contribution = (
        primary_avg * primary_weight if primary_weight is not None else None
    )
    secondary_contribution = (
        secondary_avg * secondary_weight if secondary_weight is not None else None
    )
    if primary_contribution is None and secondary_contribution is None:
        final = None
    else:
        final = (primary_contribution or 0.0) + (secondary_contribution or 0.0)

    unmatched = [
        d["raw_name"]
        for d in primary + secondary
        if d["excluded_reason"] == "unmatched_ingredient"
    ]

    return {
        "product_id": product_row["product_id"],
        "product_name": product_row["product_name"],
        "brand": product_row.get("brand"),
        "category": category,
        "primary_average": primary_avg,
        "primary_weight": primary_weight,
        "primary_contribution": primary_contribution,
        "secondary_average": secondary_avg,
        "secondary_weight": secondary_weight,
        "secondary_contribution": secondary_contribution,
        "final_score": final,
        "category_rule": rule.name,
        "attributes_used": " + ".join(columns),
        "attributes_excluded": " + ".join(excluded),
        "scoring_basis": basis,
        "has_disqualifying_ingredient": any(
            d["disqualifying"] for d in primary + secondary
        ),
        "primary_listed": len(primary),
        "primary_matched": len(primary_scored),
        "secondary_listed": len(secondary),
        "secondary_matched": len(secondary_scored),
        "unmatched_ingredients": "; ".join(unmatched),
        "_columns": columns,
        "_primary_detail": primary,
        "_secondary_detail": secondary,
    }


def score_catalogue(products, resolver, scores_by_name, profile):
    """Score every product for one profile. Returns (DataFrame, details list)."""
    details = [
        score_product(row, resolver, scores_by_name, profile)
        for _, row in products.iterrows()
    ]
    frame = pd.DataFrame(
        [{k: v for k, v in d.items() if not k.startswith("_")} for d in details]
    )
    for col in (
        "primary_average",
        "secondary_average",
        "primary_contribution",
        "secondary_contribution",
        "final_score",
    ):
        frame[col] = frame[col].round(2)
    return frame, details


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt(value, places=2):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    return f"{value:.{places}f}"


def print_ingredient_breakdown(detail, columns=None):
    """Print the full ingredient-level calculation for one product."""
    columns = columns if columns is not None else detail["_columns"]
    print(f"Product ID   : {detail['product_id']}")
    print(f"Product Name : {detail['product_name']}")
    print(f"Category     : {detail['category']}  (rule: {detail['category_rule']})")
    print(f"Attributes used     : {detail['attributes_used']}")
    print(f"Attributes excluded : {detail['attributes_excluded'] or '(none)'}")
    print()

    for group, key, average, weight, contribution in (
        (
            "Primary",
            "_primary_detail",
            detail["primary_average"],
            detail["primary_weight"],
            detail["primary_contribution"],
        ),
        (
            "Secondary",
            "_secondary_detail",
            detail["secondary_average"],
            detail["secondary_weight"],
            detail["secondary_contribution"],
        ),
    ):
        print(f"{group}:")
        if not detail[key]:
            print("  (none listed)")
        for item in detail[key]:
            print(f"  {item['raw_name']}")
            if item["canonical"] and item["canonical"] != item["raw_name"]:
                print(f"    matched to: {item['canonical']}")
            for attribute in columns:
                value = item["values"].get(attribute)
                flag = "   <-- disqualifying, excluded from average" if value == DISQUALIFYING else ""
                print(f"    {attribute} = {'n/a' if value is None else value}{flag}")
            if item["score"] is None:
                print(f"    EXCLUDED ({item['excluded_reason']})")
            else:
                print(f"    Ingredient Score = {item['score']:.2f}")
        weight_text = "n/a" if weight is None else f"{weight:.0%}"
        print(f"  {group} Average      = {_fmt(average)}")
        print(f"  {group} Weight       = {weight_text}")
        print(f"  {group} Contribution = {_fmt(contribution)}")
        print()

    print(f"Category rule      = {detail['category_rule']} (category: {detail['category']})")
    print(f"Scoring basis      = {detail['scoring_basis']}")
    print(f"Disqualifying flag = {detail['has_disqualifying_ingredient']}")
    print(f"FINAL PRODUCT SCORE = {_fmt(detail['final_score'])} / 100")


def select_one_per_category(details, categories):
    """Pick one already-scored product per category, for manual validation.

    Purely a reporting selection over results that have already been computed --
    it reads `details` and changes nothing. Prefers a product that exercises
    both ingredient groups so the printed breakdown shows a real primary and
    secondary calculation; falls back to the first product in the category.
    """
    picked = []
    for category in categories:
        in_category = [d for d in details if normalise(d["category"]) == normalise(category)]
        if not in_category:
            picked.append((category, None))
            continue
        both_groups = [d for d in in_category if d["scoring_basis"] == "primary_and_secondary"]
        picked.append((category, (both_groups or in_category)[0]))
    return picked


def _attribute_token(label: str) -> str:
    """'Above 25' -> 'Above25', matching the requested CSV sample format."""
    return label.replace(" ", "")


def format_ingredient_for_csv(item) -> str:
    """Render one already-scored ingredient as a self-validating string.

    Reads only what score_ingredient already produced -- the raw name, the
    canonical match, the per-attribute values it read, and the ingredient score
    it computed. Nothing is recomputed here.
    """
    name = item["raw_name"]

    if item["excluded_reason"] == "unmatched_ingredient":
        return f"{name} [UNMATCHED - excluded from average, not scored 0]"

    parts = []
    if item["canonical"] and normalise(item["canonical"]) != normalise(name):
        parts.append(f"matched to: {item['canonical']}")
    for attribute, value in item["values"].items():
        parts.append(f"{_attribute_token(attribute)}={'n/a' if value is None else value}")

    if item["score"] is None:
        parts.append(f"EXCLUDED - {item['excluded_reason']}")
    else:
        if item["disqualifying"]:
            parts.append("DISQUALIFYING: -100 dropped from average")
        parts.append(f"IngredientScore={item['score']:.2f}")

    return f"{name} [{', '.join(parts)}]"


def select_score_spread(details, category, count):
    """Pick `count` already-scored products spanning the category's score range.

    A reporting selection over finished results: it sorts by the final score the
    engine already produced and samples evenly from highest to lowest, so the
    chosen products run high -> medium -> low. No score is altered or recomputed.
    """
    pool = [
        d
        for d in details
        if normalise(d["category"]) == normalise(category) and d["final_score"] is not None
    ]
    if not pool:
        return []
    pool.sort(key=lambda d: d["final_score"], reverse=True)
    if len(pool) <= count:
        return pool
    picked, seen = [], set()
    for i in range(count):
        index = round(i * (len(pool) - 1) / (count - 1))
        if index not in seen:
            seen.add(index)
            picked.append(pool[index])
    return picked


def _csv_round(value):
    return None if value is None else round(value, 2)


def _csv_weight(value):
    return "" if value is None else f"{value:.0%}"


def build_validation_frame(profile, details, categories, per_category):
    """Assemble the per-category validation rows for one profile.

    Every numeric field is copied straight from the detail dict that
    score_product returned -- this function performs no arithmetic.
    """
    rows = []
    for category in categories:
        for d in select_score_spread(details, category, per_category):
            rows.append(
                {
                    "Profile": profile.name(),
                    "Category": d["category"],
                    "Product ID": d["product_id"],
                    "Product Name": d["product_name"],
                    "Category Rule": d["category_rule"],
                    "Attributes Used": d["attributes_used"],
                    "Attributes Excluded": d["attributes_excluded"],
                    "Primary Ingredients": " | ".join(
                        format_ingredient_for_csv(i) for i in d["_primary_detail"]
                    ),
                    "Primary Average": _csv_round(d["primary_average"]),
                    "Primary Weight": _csv_weight(d["primary_weight"]),
                    "Primary Contribution": _csv_round(d["primary_contribution"]),
                    "Secondary Ingredients": " | ".join(
                        format_ingredient_for_csv(i) for i in d["_secondary_detail"]
                    ),
                    "Secondary Average": _csv_round(d["secondary_average"]),
                    "Secondary Weight": _csv_weight(d["secondary_weight"]),
                    "Secondary Contribution": _csv_round(d["secondary_contribution"]),
                    "Scoring Basis": d["scoring_basis"],
                    "Disqualifying Flag": d["has_disqualifying_ingredient"],
                    "Final Product Score": _csv_round(d["final_score"]),
                }
            )
    return pd.DataFrame(rows, columns=VALIDATION_CSV_COLUMNS)


# --------------------------------------------------------------------------
# Per-attribute product score CSV (output only)
# --------------------------------------------------------------------------
# Output-only addition. Every attribute below is scored INDEPENDENTLY through
# the same pipeline the profile scoring uses -- score_product() is called once
# per attribute, so ingredient resolution, the exclusion of unmatched
# ingredients, the -100 handling, the primary/secondary averages, the
# category-specific weighting and the primary-only renormalisation are all the
# existing behaviour, unchanged. Nothing here recomputes or reinterprets a
# score; the only new thing is that a "profile" is one single column instead of
# a set of columns averaged together.
#
# Because an attribute is scored on its own, an ingredient's score for that
# attribute IS its ingredient score -- averaging one value is the identity.

# Every score column in the "Ingredient rating" sheet, in the requested output
# order. Each label is also the sheet column name and the CSV header.
ATTRIBUTE_SCORE_COLUMNS = [
    # Age
    "<16",
    "17-25",
    "Above 25",
    # Concerns
    "Acne",
    "Body Acne",
    "Dryness",
    "Open Pores",
    "Uneven Skin Tone",
    "Dark Spots/Pigmentation",
    "Melasma",
    "Barrier Repair",
    "Comedones",
    "Wrinkles/Fine lines",
    "Redness/Irritation",
    "Dehydration",
    "Dullness",
    "Tanning",
    # Skin type
    "Oily Score",
    "Oily+Sensitive Score",
    "Dry Score",
    "Dry+Sensitive Score",
    "Normal Score",
    "Normal+Sensitive Score",
    "Combination Score",
    "Combination+Sensitive Score",
    "Excessive Dryness score",
    # Life stage
    "Pregnancy Score",
    "Breastfeeding Score",
]

PRODUCT_ATTRIBUTE_CSV_COLUMNS = [
    "Product ID",
    "Product Name",
    "Brand",
    "Category",
] + ATTRIBUTE_SCORE_COLUMNS

PRODUCT_ATTRIBUTE_CSV_NAME = "product_attribute_scores.csv"


@dataclass(frozen=True)
class SingleAttributeProfile:
    """A one-attribute stand-in for Profile, used only by the per-attribute CSV.

    score_product() asks a profile for two things: the {label -> score column}
    mapping to score, and the attributes the category excluded. This supplies a
    mapping holding exactly one column, which is what makes each attribute come
    out independently.

    CATEGORY_RULES still applies in full inside score_product -- the category
    picks the primary/secondary weighting exactly as before. The rule's
    `use_concerns` flag is deliberately not honoured here: it exists to decide
    which attributes join a COMBINED profile average, and this CSV reports each
    attribute on its own, one column at a time. Honouring it would blank every
    concern column for sunscreens and moisturisers instead of reporting their
    scores. To suppress those cells instead, return {} from score_columns when
    `include_concerns` is False and the attribute is in CONCERN_COLUMNS.values().
    """

    column: str

    def name(self) -> str:
        return self.column

    def score_columns(self, include_concerns: bool = True) -> dict[str, str]:
        return {self.column: self.column}

    def excluded_attributes(self, include_concerns: bool = True) -> list[str]:
        return []


def build_product_attribute_frame(products, resolver, scores_by_name):
    """One row per product, one column per attribute, scored independently.

    Each cell is the final product score score_product() returned for that
    attribute alone, rounded like every other CSV the engine writes. A cell is
    left blank when the product has no usable ingredient for that attribute
    (basis "unscorable") -- never 0, which is reserved for a real calculated 0.
    """
    profiles = [SingleAttributeProfile(c) for c in ATTRIBUTE_SCORE_COLUMNS]
    rows = []
    for _, product_row in products.iterrows():
        row = {
            "Product ID": product_row["product_id"],
            "Product Name": product_row["product_name"],
            "Brand": product_row.get("brand"),
            "Category": product_row.get("assigned_category"),
        }
        for profile in profiles:
            detail = score_product(product_row, resolver, scores_by_name, profile)
            row[profile.column] = _csv_round(detail["final_score"])
        rows.append(row)
    return pd.DataFrame(rows, columns=PRODUCT_ATTRIBUTE_CSV_COLUMNS)


def write_product_attribute_scores(products, resolver, scores_by_name):
    """Build the per-attribute CSV, write it, and print a short summary."""
    frame = build_product_attribute_frame(products, resolver, scores_by_name)
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / PRODUCT_ATTRIBUTE_CSV_NAME
    frame.to_csv(path, index=False)

    print("Products written     :", len(frame))
    print("Attribute columns    :", len(ATTRIBUTE_SCORE_COLUMNS))
    missing = frame[ATTRIBUTE_SCORE_COLUMNS].isna().sum()
    print("Attributes fully scored for every product:",
          int((missing == 0).sum()), "of", len(ATTRIBUTE_SCORE_COLUMNS))
    for attribute, count in missing.items():
        if count:
            print(f"  missing {int(count):4d}/{len(frame)}  {attribute}")
    print("Written              :", path.relative_to(BASE_DIR))
    return frame, path


def diagnostics_report(products, resolver):
    """Summarise coverage and every unresolved ingredient across the catalogue."""
    from collections import Counter

    mentions = Counter()
    for _, row in products.iterrows():
        for name in row["primary_ingredients"] + row["secondary_ingredients"]:
            mentions[name] += 1

    # Excluded names are separated from unresolved ones: an exclusion is a
    # deliberate decision, not a mapping failure, so it must not be reported as
    # one. Both are equally absent from the scoring averages.
    methods = {n: resolver.resolve_with_method(n)[1] for n in mentions}
    excluded = Counter(
        {n: c for n, c in mentions.items() if methods[n] == METHOD_EXCLUDED}
    )
    unmatched = Counter(
        {n: c for n, c in mentions.items()
         if methods[n] != METHOD_EXCLUDED and resolver.resolve(n) is None}
    )
    total = sum(mentions.values())
    missed = sum(unmatched.values())
    skipped = sum(excluded.values())

    print("Ingredient mentions          :", total)
    print("Distinct ingredient names    :", len(mentions))
    print(f"Mapped mentions              : {total - missed - skipped} "
          f"({(total - missed - skipped) / total:.1%})")
    print(f"Excluded mentions            : {skipped} ({skipped / total:.1%})")
    print(f"Unresolved mentions          : {missed} ({missed / total:.1%})")
    print("Excluded distinct names      :", len(excluded))
    print("Unresolved distinct names    :", len(unmatched))
    if resolver.missing_alias_targets:
        print("Alias targets not in sheet   :", resolver.missing_alias_targets)
    return unmatched


# --------------------------------------------------------------------------
# Validation profiles
# --------------------------------------------------------------------------

VALIDATION_PROFILES = [
    Profile(skin_type="Oily", concerns=["Acne"], age_group="Above 25"),
    Profile(skin_type="Dry", concerns=["Acne"], age_group="Above 25"),
    Profile(
        skin_type="Combination",
        concerns=["Dark Spots/Pigmentation"],
        age_group="Above 25",
    ),
]

# Reporting only -- these control which breakdowns get printed, never how any
# product is scored. Every profile above is still scored across the full
# catalogue and still written to its own CSV.
DETAIL_PROFILE = "Oily + Acne + Above 25"
DETAIL_CATEGORIES = [
    "Sunscreen",
    "Moisturizer",
    "Cleanser",
    "Toner",
    "Serum",
    "Mask",
    "Body Care",
]

# Validation CSVs: reporting only. These select from results the engine has
# already computed and re-serialise them with full ingredient detail; they add
# no arithmetic and leave the scores_*.csv outputs untouched.
VALIDATION_CSV_PROFILES = ["Oily + Acne + Above 25", "Dry + Acne + Above 25"]
VALIDATION_CSV_CATEGORIES = [
    "Sunscreen",
    "Moisturizer",
    "Cleanser",
    "Toner",
    "Serum",
    "Mask",
    "Body Care",
]
VALIDATION_CSV_PER_CATEGORY = 5
VALIDATION_CSV_COLUMNS = [
    "Profile",
    "Category",
    "Product ID",
    "Product Name",
    "Category Rule",
    "Attributes Used",
    "Attributes Excluded",
    "Primary Ingredients",
    "Primary Average",
    "Primary Weight",
    "Primary Contribution",
    "Secondary Ingredients",
    "Secondary Average",
    "Secondary Weight",
    "Secondary Contribution",
    "Scoring Basis",
    "Disqualifying Flag",
    "Final Product Score",
]


# --------------------------------------------------------------------------
# Mapping audit, unmatched report and per-product coverage
# --------------------------------------------------------------------------
# Reporting only. Every number is read back from the resolver that the scoring
# run uses, so the audit can never disagree with what was actually scored.
# Similarity is computed for unresolved names only, as a review hint, and never
# maps anything.

MAPPING_AUDIT_CSV_NAME = "mapping_audit.csv"
UNMATCHED_CSV_NAME = "unmatched_ingredients.csv"
COVERAGE_CSV_NAME = "product_mapping_coverage.csv"

MAPPING_AUDIT_COLUMNS = [
    "record_type",
    "metric",
    "value",
    "Unmatched Ingredient",
    "Role",
    "Canonical Ingredient",
    "Mapping Method",
    "Status",
    "Mention Count",
    "Product Count",
    "Primary Products",
    "Secondary Products",
    "Example Product Names",
    "Notes",
]

UNMATCHED_CSV_COLUMNS = [
    "ingredient_name",
    "role",
    "mention_count",
    "product_count",
    "primary_products",
    "secondary_products",
    "status",
    "reason",
    "nearest_candidate_review_only",
    "similarity_review_only",
    "example_product_names",
]

PRODUCT_COVERAGE_COLUMNS = [
    "Product ID",
    "Product Name",
    "Category",
    "Primary Ingredient Count",
    "Primary Mapped Count",
    "Primary Excluded Count",
    "Primary Unresolved Count",
    "Primary Mapping %",
    "Secondary Ingredient Count",
    "Secondary Mapped Count",
    "Secondary Excluded Count",
    "Secondary Unresolved Count",
    "Secondary Mapping %",
    "Total Ingredient Count",
    "Total Mapped Count",
    "Total Excluded Count",
    "Total Unresolved Count",
    "Overall Mapping %",
    "Mapping Status",
    "Unresolved Primary Ingredients",
    "Unresolved Secondary Ingredients",
    "Excluded Primary Ingredients",
    "Excluded Secondary Ingredients",
]

COVERAGE_FULL = "FULLY_MAPPED"
COVERAGE_PARTIAL = "PARTIALLY_MAPPED"
COVERAGE_NONE = "NONE_MAPPED"
COVERAGE_EMPTY = "NO_INGREDIENTS"
COVERAGE_ALL_EXCLUDED = "ALL_INGREDIENTS_EXCLUDED"

EXAMPLES_PER_INGREDIENT = 3


def _role_label(roles) -> str:
    if roles == {"Primary", "Secondary"}:
        return "Primary + Secondary"
    return "Primary" if roles == {"Primary"} else "Secondary"


def _coverage_pct(mapped, scoreable):
    """Percentage of the SCOREABLE mentions that mapped.

    Excluded mentions are out of scope by definition, so they are removed from
    the denominator rather than counted as failures. A group with nothing
    scoreable has no percentage at all -- blank, never 0.
    """
    return "" if scoreable == 0 else round(mapped / scoreable * 100, 2)


def collect_ingredient_mappings(products, resolver):
    """Walk the catalogue once and record how every ingredient name resolved."""
    records: dict[str, dict] = {}
    product_stats = {
        "total": 0,
        "fully_mapped": 0,
        "partially_mapped": 0,
        "none_mapped": 0,
        "no_ingredients_listed": 0,
        "all_excluded": 0,
    }

    for _, row in products.iterrows():
        product_stats["total"] += 1
        product_name = row.get("product_name")
        listed = mapped = excluded = 0

        for role, column in (
            ("Primary", "primary_ingredients"),
            ("Secondary", "secondary_ingredients"),
        ):
            for raw in row[column]:
                listed += 1
                record = records.get(raw)
                if record is None:
                    canonical, method = resolver.resolve_with_method(raw)
                    if method == METHOD_EXCLUDED:
                        status = STATUS_EXCLUDED
                    elif canonical is None:
                        status = STATUS_UNRESOLVED
                    else:
                        status = STATUS_MAPPED
                    record = records[raw] = {
                        "canonical": canonical,
                        "method": method,
                        "status": status,
                        "roles": set(),
                        "primary_products": set(),
                        "secondary_products": set(),
                        "mentions": 0,
                        "examples": [],
                    }
                record["roles"].add(role)
                record["mentions"] += 1
                key = "primary_products" if role == "Primary" else "secondary_products"
                record[key].add(row["product_id"])
                if (
                    len(record["examples"]) < EXAMPLES_PER_INGREDIENT
                    and product_name not in record["examples"]
                ):
                    record["examples"].append(product_name)
                if record["status"] == STATUS_MAPPED:
                    mapped += 1
                elif record["status"] == STATUS_EXCLUDED:
                    excluded += 1

        scoreable = listed - excluded
        if listed == 0:
            product_stats["no_ingredients_listed"] += 1
            product_stats["none_mapped"] += 1
        elif scoreable == 0:
            product_stats["all_excluded"] += 1
            product_stats["none_mapped"] += 1
        elif mapped == scoreable:
            product_stats["fully_mapped"] += 1
        elif mapped == 0:
            product_stats["none_mapped"] += 1
        else:
            product_stats["partially_mapped"] += 1

    return records, product_stats


def _nearest_canonical(name, canonical_names):
    """Closest canonical name and similarity. REVIEW HINT ONLY -- never maps."""
    import difflib

    match = difflib.get_close_matches(name, canonical_names, n=1, cutoff=NEAR_MISS_RATIO)
    if not match:
        return None, None
    ratio = difflib.SequenceMatcher(None, name.lower(), match[0].lower()).ratio()
    return match[0], round(ratio, 3)


def _summarise_mappings(records):
    """Distinct-name and mention counts per mapping method and status."""
    methods = {m: 0 for m in MAPPING_METHODS}
    mention_methods = {m: 0 for m in MAPPING_METHODS}
    per_role = {}
    for role, key in (("primary", "primary_products"), ("secondary", "secondary_products")):
        names = [r for r in records.values() if r[key]]
        per_role[role] = {
            "unique": len(names),
            "mapped": sum(1 for r in names if r["status"] == STATUS_MAPPED),
            "excluded": sum(1 for r in names if r["status"] == STATUS_EXCLUDED),
            "unresolved": sum(1 for r in names if r["status"] == STATUS_UNRESOLVED),
        }
    for record in records.values():
        methods[record["method"]] += 1
        mention_methods[record["method"]] += record["mentions"]
    return per_role, methods, mention_methods


def build_mapping_audit_frame(records, product_stats, canonical_names, resolver):
    """Summary rows, then one row per distinct raw ingredient name."""
    per_role, methods, mention_methods = _summarise_mappings(records)
    total_mentions = sum(r["mentions"] for r in records.values())
    mapped_mentions = sum(r["mentions"] for r in records.values() if r["status"] == STATUS_MAPPED)
    excluded_mentions = sum(r["mentions"] for r in records.values() if r["status"] == STATUS_EXCLUDED)
    unresolved_mentions = total_mentions - mapped_mentions - excluded_mentions

    summary = [
        ("Total products processed", product_stats["total"]),
        ("Total ingredient mentions", total_mentions),
        ("Mapped mentions", mapped_mentions),
        ("Excluded mentions", excluded_mentions),
        ("Unresolved mentions", unresolved_mentions),
        ("Total distinct ingredient names", len(records)),
        ("Distinct names mapped", sum(1 for r in records.values() if r["status"] == STATUS_MAPPED)),
        ("Distinct names excluded", sum(1 for r in records.values() if r["status"] == STATUS_EXCLUDED)),
        ("Distinct names unresolved", sum(1 for r in records.values() if r["status"] == STATUS_UNRESOLVED)),
        ("Unique Primary ingredient names", per_role["primary"]["unique"]),
        ("Unique Secondary ingredient names", per_role["secondary"]["unique"]),
        ("Primary names mapped", per_role["primary"]["mapped"]),
        ("Secondary names mapped", per_role["secondary"]["mapped"]),
        ("Primary names excluded", per_role["primary"]["excluded"]),
        ("Secondary names excluded", per_role["secondary"]["excluded"]),
        ("Primary names unresolved", per_role["primary"]["unresolved"]),
        ("Secondary names unresolved", per_role["secondary"]["unresolved"]),
        ("Products with ALL scoreable ingredients mapped", product_stats["fully_mapped"]),
        ("Products partially mapped", product_stats["partially_mapped"]),
        ("Products with NO ingredients mapped", product_stats["none_mapped"]),
        ("Products with no ingredients listed", product_stats["no_ingredients_listed"]),
        ("Products whose ingredients are all excluded", product_stats["all_excluded"]),
    ]
    for method in MAPPING_METHODS:
        summary.append((f"Distinct names via {method}", methods[method]))
        summary.append((f"Mentions via {method}", mention_methods[method]))
    summary += [
        ("Domain mappings with a target missing from the workbook",
         len(resolver.invalid_domain_targets)),
        ("Domain mappings overriding an exact canonical name",
         len(resolver.domain_overriding_exact)),
        ("Domain mappings dropped because the source is excluded",
         len(resolver.domain_overriding_exclusion)),
        ("Duplicate domain mapping sources", len(resolver.duplicate_domain_sources)),
        ("Final exceptions with a target missing from the workbook",
         len(resolver.invalid_exception_targets)),
    ]

    rows = [
        {"record_type": "summary", "metric": metric, "value": value}
        for metric, value in summary
    ]

    for name in sorted(records):
        record = records[name]
        note = ""
        if record["method"] == METHOD_DOMAIN and str(name).strip() in set(canonical_names):
            note = ("Domain-reviewed mapping takes precedence over the exact canonical "
                    "entry of the same name, per the agreed priority order.")
        elif record["status"] == STATUS_UNRESOLVED:
            candidate, ratio = _nearest_canonical(name, canonical_names)
            if candidate:
                note = (f"Review hint only: resembles '{candidate}' (similarity {ratio}). "
                        f"Not mapped -- similarity is never evidence of identity.")
        rows.append(
            {
                "record_type": "ingredient",
                "Unmatched Ingredient": name,
                "Role": _role_label(record["roles"]),
                "Canonical Ingredient": record["canonical"] or "",
                "Mapping Method": record["method"],
                "Status": record["status"],
                "Mention Count": record["mentions"],
                "Product Count": len(record["primary_products"] | record["secondary_products"]),
                "Primary Products": len(record["primary_products"]),
                "Secondary Products": len(record["secondary_products"]),
                "Example Product Names": " | ".join(str(e) for e in record["examples"]),
                "Notes": note,
            }
        )

    return pd.DataFrame(rows, columns=MAPPING_AUDIT_COLUMNS)


def build_unmatched_frame(records, canonical_names):
    """Every ingredient still unresolved after the full priority chain.

    Excluded ingredients are NOT listed here -- they are a deliberate exclusion,
    not a mapping failure, and are reported in mapping_audit.csv instead.
    """
    rows = []
    for name in sorted(records):
        record = records[name]
        if record["status"] != STATUS_UNRESOLVED:
            continue
        candidate, ratio = _nearest_canonical(name, canonical_names)
        rows.append(
            {
                "ingredient_name": name,
                "role": _role_label(record["roles"]),
                "mention_count": record["mentions"],
                "product_count": len(record["primary_products"] | record["secondary_products"]),
                "primary_products": len(record["primary_products"]),
                "secondary_products": len(record["secondary_products"]),
                "status": STATUS_UNRESOLVED,
                "reason": (
                    "Not excluded, not domain-reviewed, not an exact canonical entry, "
                    "and no safe deterministic normalisation applied. Excluded from the "
                    "averages; never scored 0."
                ),
                "nearest_candidate_review_only": candidate or "",
                "similarity_review_only": ratio if ratio is not None else "",
                "example_product_names": " | ".join(str(e) for e in record["examples"]),
            }
        )
    rows.sort(key=lambda r: (-r["mention_count"], r["ingredient_name"]))
    return pd.DataFrame(rows, columns=UNMATCHED_CSV_COLUMNS)


def build_product_coverage_frame(products, resolver):
    """One row per product: how much of its ingredient list resolved.

    Counts ingredient MENTIONS from primary_ingredients and
    secondary_ingredients only; the raw `ingredients` column is never read.
    Mapped, excluded and unresolved are kept separate -- an excluded ingredient
    is never counted as mapped.
    """
    rows = []
    for _, row in products.iterrows():
        groups = {}
        for role, column in (
            ("primary", "primary_ingredients"),
            ("secondary", "secondary_ingredients"),
        ):
            names = row[column]
            excluded_names, unresolved_names, mapped = [], [], 0
            for raw in names:
                canonical, method = resolver.resolve_with_method(raw)
                if method == METHOD_EXCLUDED:
                    excluded_names.append(raw)
                elif canonical is None:
                    unresolved_names.append(raw)
                else:
                    mapped += 1
            groups[role] = {
                "count": len(names),
                "mapped": mapped,
                "excluded": len(excluded_names),
                "unresolved": len(unresolved_names),
                "excluded_names": excluded_names,
                "unresolved_names": unresolved_names,
            }

        pri, sec = groups["primary"], groups["secondary"]
        total = pri["count"] + sec["count"]
        mapped = pri["mapped"] + sec["mapped"]
        excluded = pri["excluded"] + sec["excluded"]
        unresolved = pri["unresolved"] + sec["unresolved"]
        scoreable = total - excluded

        if total == 0:
            status = COVERAGE_EMPTY
        elif scoreable == 0:
            status = COVERAGE_ALL_EXCLUDED
        elif unresolved == 0:
            status = COVERAGE_FULL
        elif mapped == 0:
            status = COVERAGE_NONE
        else:
            status = COVERAGE_PARTIAL

        rows.append(
            {
                "Product ID": row["product_id"],
                "Product Name": row["product_name"],
                "Category": row.get("category"),
                "Primary Ingredient Count": pri["count"],
                "Primary Mapped Count": pri["mapped"],
                "Primary Excluded Count": pri["excluded"],
                "Primary Unresolved Count": pri["unresolved"],
                "Primary Mapping %": _coverage_pct(pri["mapped"], pri["count"] - pri["excluded"]),
                "Secondary Ingredient Count": sec["count"],
                "Secondary Mapped Count": sec["mapped"],
                "Secondary Excluded Count": sec["excluded"],
                "Secondary Unresolved Count": sec["unresolved"],
                "Secondary Mapping %": _coverage_pct(sec["mapped"], sec["count"] - sec["excluded"]),
                "Total Ingredient Count": total,
                "Total Mapped Count": mapped,
                "Total Excluded Count": excluded,
                "Total Unresolved Count": unresolved,
                "Overall Mapping %": _coverage_pct(mapped, scoreable),
                "Mapping Status": status,
                "Unresolved Primary Ingredients": "; ".join(pri["unresolved_names"]),
                "Unresolved Secondary Ingredients": "; ".join(sec["unresolved_names"]),
                "Excluded Primary Ingredients": "; ".join(pri["excluded_names"]),
                "Excluded Secondary Ingredients": "; ".join(sec["excluded_names"]),
            }
        )
    return pd.DataFrame(rows, columns=PRODUCT_COVERAGE_COLUMNS)


def write_mapping_reports(products, resolver, scores_by_name):
    """Write mapping_audit.csv and unmatched_ingredients.csv, print the totals."""
    canonical_names = list(scores_by_name)
    records, product_stats = collect_ingredient_mappings(products, resolver)

    audit = build_mapping_audit_frame(records, product_stats, canonical_names, resolver)
    audit_path = OUTPUT_DIR / MAPPING_AUDIT_CSV_NAME
    audit.to_csv(audit_path, index=False)

    unmatched = build_unmatched_frame(records, canonical_names)
    unmatched_path = OUTPUT_DIR / UNMATCHED_CSV_NAME
    unmatched.to_csv(unmatched_path, index=False)

    per_role, methods, mention_methods = _summarise_mappings(records)
    total = sum(r["mentions"] for r in records.values())
    mapped = sum(r["mentions"] for r in records.values() if r["status"] == STATUS_MAPPED)
    excluded = sum(r["mentions"] for r in records.values() if r["status"] == STATUS_EXCLUDED)

    print("Distinct names / mentions, by mapping method:")
    for method in MAPPING_METHODS:
        print(f"  {method:<20} : {methods[method]:5d} names   {mention_methods[method]:5d} mentions")
    print()
    print(f"Total mentions       : {total}")
    print(f"  mapped             : {mapped}")
    print(f"  excluded           : {excluded}")
    print(f"  unresolved         : {total - mapped - excluded}")
    scoreable = total - excluded
    if scoreable:
        print(f"Coverage of scoreable: {mapped / scoreable * 100:.2f}%")
    print(f"Products fully mapped     : {product_stats['fully_mapped']}")
    print(f"Products partially mapped : {product_stats['partially_mapped']}")
    print(f"Products with none mapped : {product_stats['none_mapped']}")
    print("Written              :", audit_path.relative_to(BASE_DIR))
    print("Written              :", unmatched_path.relative_to(BASE_DIR))
    return records, product_stats


def write_product_coverage(products, resolver):
    """Write product_mapping_coverage.csv and print its totals."""
    frame = build_product_coverage_frame(products, resolver)
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / COVERAGE_CSV_NAME
    frame.to_csv(path, index=False)

    counts = frame["Mapping Status"].value_counts()
    print("Products                  :", len(frame))
    for status in (COVERAGE_FULL, COVERAGE_PARTIAL, COVERAGE_NONE,
                   COVERAGE_ALL_EXCLUDED, COVERAGE_EMPTY):
        print(f"  {status:<26} : {int(counts.get(status, 0))}")
    for label, prefix in (("Primary", "Primary"), ("Secondary", "Secondary")):
        print(
            f"{label} mentions: {int(frame[prefix + ' Ingredient Count'].sum())} "
            f"({int(frame[prefix + ' Mapped Count'].sum())} mapped, "
            f"{int(frame[prefix + ' Excluded Count'].sum())} excluded, "
            f"{int(frame[prefix + ' Unresolved Count'].sum())} unresolved)"
        )
    total = int(frame["Total Ingredient Count"].sum())
    mapped = int(frame["Total Mapped Count"].sum())
    excluded = int(frame["Total Excluded Count"].sum())
    print(f"Total mentions            : {total} ({mapped} mapped, {excluded} excluded, "
          f"{total - mapped - excluded} unresolved)")
    if total - excluded:
        print(f"Coverage of scoreable     : {mapped / (total - excluded) * 100:.2f}%")
    print("Written              :", path.relative_to(BASE_DIR))
    return frame


# --------------------------------------------------------------------------
# Execution paths
# --------------------------------------------------------------------------
# Two entry points over the SAME scoring code:
#
#   run_production  -- the normal run. Writes unmatched_ingredients.csv and
#                      product_attribute_scores.csv, nothing else.
#   run_validation  -- opt-in (--validate). Scores the three reference
#                      profiles, writes scores_*.csv and validation_*.csv and
#                      prints the ingredient-level breakdowns. Kept for future
#                      testing; never runs during a normal execution.
#
# Both call the same load_data / score_product / score_catalogue functions, so
# the two paths can never drift apart in how anything is scored.


def run_production(products, scores_by_name, resolver) -> int:
    """Normal run: mapping diagnostics, the mapping reports, and the scores.

    Writes exactly three files -- product_attribute_scores.csv,
    mapping_audit.csv and unmatched_ingredients.csv.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("=" * 78)
    print("DATASET / MATCHING DIAGNOSTICS")
    print("=" * 78)
    print("Product dataset              :", PRODUCT_DATASET_FILE.name)
    print("Products loaded              :", len(products))
    print("Canonical ingredients loaded :", len(scores_by_name))
    unmatched = diagnostics_report(products, resolver)

    print()
    print("Top 30 unresolved ingredient names (excluded from all averages):")
    for name, count in unmatched.most_common(30):
        print(f"  {count:4d}  {name}")

    print()
    print("=" * 78)
    print("MAPPING AUDIT")
    print("=" * 78)
    write_mapping_reports(products, resolver, scores_by_name)

    print()
    print("=" * 78)
    print("PER-PRODUCT MAPPING COVERAGE (diagnostic only)")
    print("=" * 78)
    write_product_coverage(products, resolver)

    print()
    print("=" * 78)
    print("PER-ATTRIBUTE PRODUCT SCORES (output only -- one column per attribute)")
    print("=" * 78)
    write_product_attribute_scores(products, resolver, scores_by_name)

    return 0


def run_validation(products, scores_by_name, resolver) -> int:
    """Opt-in validation run (--validate): reference profiles and breakdowns.

    Writes scores_*.csv and validation_*.csv for the profiles in
    VALIDATION_PROFILES. Reporting only -- it scores through the same
    score_catalogue path the engine has always used and changes nothing.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)

    detail_profile, detail_results = None, None
    results_by_profile = {}

    for profile in VALIDATION_PROFILES:
        frame, details = score_catalogue(products, resolver, scores_by_name, profile)

        print()
        print("=" * 78)
        print(f"PROFILE: {profile.name()}")
        print("=" * 78)
        print("Attributes scored, by category rule:")
        for rule_name, group in frame.groupby("category_rule"):
            cats = ", ".join(sorted({str(c) for c in group["category"]}))
            print(f"  {rule_name}")
            print(f"    categories : {cats}")
            print(f"    used       : {group['attributes_used'].iloc[0]}")
            print(f"    excluded   : {group['attributes_excluded'].iloc[0] or '(none)'}")
        print()

        scored = frame[frame["final_score"].notna()]
        print(f"Products scored      : {len(scored)} of {len(frame)}")
        print(f"Unscorable products  : {int(frame['final_score'].isna().sum())}")
        print("Scoring basis counts :", frame["scoring_basis"].value_counts().to_dict())
        print("Category rule counts :", frame["category_rule"].value_counts().to_dict())
        print(f"Disqualifying flags  : {int(frame['has_disqualifying_ingredient'].sum())}")
        if len(scored):
            print(
                f"Final score  min/mean/max: {scored['final_score'].min():.2f} / "
                f"{scored['final_score'].mean():.2f} / {scored['final_score'].max():.2f}"
            )

        slug = re.sub(r"[^a-z0-9]+", "_", profile.name().lower()).strip("_")
        out_path = OUTPUT_DIR / f"scores_{slug}.csv"
        frame.drop(
            columns=["_columns", "_primary_detail", "_secondary_detail"], errors="ignore"
        ).to_csv(
            out_path, index=False
        )
        print("Written              :", out_path.relative_to(BASE_DIR))

        print()
        print("Top 10 products by final score:")
        top = scored.nlargest(10, "final_score")
        for _, r in top.iterrows():
            print(
                f"  {r['final_score']:6.2f}  {r['product_id']}  "
                f"P={_fmt(r['primary_average']):>6} S={_fmt(r['secondary_average']):>6}  "
                f"{str(r['product_name'])[:60]}"
            )

        results_by_profile[profile.name()] = (profile, details)

        if profile.name() == DETAIL_PROFILE:
            detail_profile, detail_results = profile, details

    print()
    print("=" * 78)
    print("VALIDATION CSVs (reporting only -- scores copied, never recomputed)")
    print("=" * 78)
    for profile_name in VALIDATION_CSV_PROFILES:
        if profile_name not in results_by_profile:
            print(f"  {profile_name}: not scored this run, skipped")
            continue
        vprofile, vdetails = results_by_profile[profile_name]
        vframe = build_validation_frame(
            vprofile, vdetails, VALIDATION_CSV_CATEGORIES, VALIDATION_CSV_PER_CATEGORY
        )
        slug = re.sub(r"[^a-z0-9]+", "_", profile_name.lower()).strip("_")
        vpath = OUTPUT_DIR / f"validation_{slug}.csv"
        vframe.to_csv(vpath, index=False)
        print()
        print(profile_name)
        for category in VALIDATION_CSV_CATEGORIES:
            print(f"  {category}: {int((vframe['Category'] == category).sum())}")
        print(f"  Total: {len(vframe)}")
        print(f"  Written: {vpath.relative_to(BASE_DIR)}")

    if detail_profile is None:
        print(f"\nNo profile named {DETAIL_PROFILE!r}; skipping ingredient-level detail.")
        return 0

    print()
    print("=" * 78)
    print(f"INGREDIENT-LEVEL DETAIL -- {detail_profile.name()}")
    print(f"One product per category ({len(DETAIL_CATEGORIES)} categories)")
    print("=" * 78)
    for category, detail in select_one_per_category(detail_results, DETAIL_CATEGORIES):
        print()
        print("-" * 78)
        if detail is None:
            print(f"{category}: no products in this category")
            print("-" * 78)
            continue
        print(f"CATEGORY VALIDATION: {category}")
        print("-" * 78)
        print_ingredient_breakdown(detail)
        print("-" * 78)

    return 0


USAGE = """Usage: python scoring_engine.py [--validate]

  (no flag)   Production run. Writes output/product_attribute_scores.csv,
              output/mapping_audit.csv, output/unmatched_ingredients.csv and
              output/product_mapping_coverage.csv.
  --validate  Validation run instead. Writes output/scores_*.csv and
              output/validation_*.csv and prints ingredient-level breakdowns
              for the reference profiles. Not part of a normal run."""


def main(argv=None) -> int:
    # Ingredient names contain characters the console's default codepage cannot
    # encode. Reporting must never be able to abort a scoring run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    argv = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in argv if a not in ("--validate", "-h", "--help")]
    if unknown:
        print("Unknown argument(s): " + " ".join(unknown))
        print()
        print(USAGE)
        return 2
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0

    products, scores_by_name, resolver = load_data()
    if "--validate" in argv:
        return run_validation(products, scores_by_name, resolver)
    return run_production(products, scores_by_name, resolver)


if __name__ == "__main__":
    sys.exit(main())

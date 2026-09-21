// Roopsee Doctor Validation -- the only front end in this repository.
//
// Every number on this page is computed by scoring_engine.py on the server and
// rendered here as-is. Nothing is scored, adjusted, capped or re-ranked in the
// browser. If a figure looks wrong, the engine is the place to look.
//
//   GET  /api/v3/dataset   the 87-product validation catalogue (no scores)
//   POST /api/v3/score     every product's score for one profile
//   POST /api/v3/detail    one product's ingredient-level breakdown

const DATA_URL = "/api/v3/dataset";
const SCORE_URL = "/api/v3/score";
const DETAIL_URL = "/api/v3/detail";

// Score bands. "Not suggested" is not a band: it is the disqualifying flag, and
// is kept out of every numeric band. These mirror SCORE_RANGES in app.py.
const SCORE_RANGES = [
  { key: "90-100", label: "90-100", tone: "good", test: (score) => score >= 90 },
  { key: "80-89", label: "80-89", tone: "good", test: (score) => score >= 80 && score < 90 },
  { key: "70-79", label: "70-79", tone: "mid", test: (score) => score >= 70 && score < 80 },
  { key: "50-69", label: "50-69", tone: "mid", test: (score) => score >= 50 && score < 70 },
  { key: "1-49", label: "1-49", tone: "low", test: (score) => score < 50 },
  { key: "not-suggested", label: "Not Suggested", tone: "low", test: () => false },
];

// How an ingredient reached its canonical row, as reported by the engine's own
// mapping_method. The browser only colours the label; it never decides one.
const METHOD_TONES = {
  EXACT_CANONICAL: "exact",
  DOMAIN_REVIEWED: "domain",
  SAFE_NORMALIZATION: "safe",
  FINAL_EXCEPTION: "safe",
  EXCLUDED: "muted",
  NEEDS_DOMAIN_REVIEW: "review",
};

const state = {
  context: "All", // All | Face | Body -- filtering and display only, never scored
  skinType: "Oily",
  sensitive: false,
  age: "17-25",
  gender: "female",
  concern: "Acne",
  specialConditions: ["None"],
  search: "",
  typeFilter: "All",
  scoreFilter: "All",
  priceFilter: "All",
};

let dataset = null;
let products = [];
let scoreByUid = null;
let lastScoredRows = [];
let currentRowsByUid = new Map();
let renderTimer = null;

const $ = (selector) => document.querySelector(selector);

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("en-IN");
}

function formatMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "Price n/a";
  return `Rs ${Math.round(number).toLocaleString("en-IN")}`;
}

function roundScore(value) {
  return Math.round(Number(value));
}

// DISPLAY ONLY. The exact value from scoring_engine.py is what the API returns
// and what decides ranking and band assignment; this rounds the rendered text
// alone. Never feed it into a comparison, a sort or a band test.
function formatScoreDisplay(value) {
  const number = Number(value);
  if (value === null || value === undefined || !Number.isFinite(number)) return "-";
  return String(roundScore(number));
}

function button(value, active, disabled = false) {
  return `<button type="button" class="chip ${active ? "active" : ""}" data-value="${escapeHtml(value)}"${
    disabled ? " disabled" : ""
  }>${escapeHtml(value)}</button>`;
}

function productImage(product, className = "product-image") {
  // A missing or unreachable image falls back to the plain placeholder; the URL
  // itself is never substituted or rewritten.
  if (!product.imageUrl) return `<div class="${className}"></div>`;
  return `<img class="${className}" src="${escapeHtml(product.imageUrl)}" alt="${escapeHtml(
    product.name
  )}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: '${className}'}))" />`;
}

// ---------------------------------------------------------------------------
// Scoring requests
// ---------------------------------------------------------------------------
// Only a PROFILE change (context, skin type, sensitivity, age, concern, special
// conditions) re-scores. Filters, search and band clicks re-render the scores
// already on hand and never call the server.
//
//   * At most one /api/v3/score request is active: starting a newer profile
//     aborts the older one, and a generation counter discards any reply that is
//     no longer the latest, so a slow older reply cannot overwrite newer results.
//   * Rapid changes are debounced into a single request.
//   * A profile already scored in this session is applied from the client cache.

const RESCORE_DEBOUNCE_MS = 200;
const CLIENT_CACHE_LIMIT = 24;
const clientCache = new Map(); // profile key -> validated score payload (LRU)
const detailCache = new Map(); // profile key + uid -> product detail
let appliedKey = null;
let generation = 0;
let abortController = null;
let rescoreTimer = null;
let loading = false;
let detailToken = 0;
const stats = { requests: 0, aborted: 0, discarded: 0, clientCacheHits: 0, serverCacheHits: 0 };

function profileBody() {
  return {
    context: state.context,
    skinType: state.skinType,
    sensitive: state.sensitive,
    age: state.age,
    // Sent for gating parity with the UI only; the server never scores gender.
    gender: state.gender,
    concern: state.concern,
    specialConditions: state.specialConditions,
  };
}

// Everything that can change a score and nothing that cannot. Gender is left
// out because it is never scored; its gating effect already appears as the life
// stages being removed from specialConditions. Context is included only because
// it selects which concern vocabulary applies -- it never reaches the engine.
function profileKey() {
  return JSON.stringify([
    state.context,
    state.skinType,
    Boolean(state.sensitive),
    state.age,
    state.concern,
    [...state.specialConditions].filter((item) => item !== "None").sort(),
  ]);
}

function applyPayload(key, payload) {
  scoreByUid = new Map(payload.rows.map((row) => [row.uid, row]));
  appliedKey = key;
}

function rememberPayload(key, payload) {
  clientCache.delete(key);
  clientCache.set(key, payload);
  while (clientCache.size > CLIENT_CACHE_LIMIT) {
    clientCache.delete(clientCache.keys().next().value);
  }
}

// While a new profile is being scored the previous results stay visible but
// dimmed and inert, under an explicit "Updating" label, so they are never
// presented as the current profile's results.
function setLoading(value) {
  loading = value;
  ["#products-view", "#score-bins"].forEach((selector) => {
    const element = $(selector);
    if (element) element.classList.toggle("is-updating", value);
  });
  if (value) {
    $("#results-count").textContent = "Updating product scores…";
    $("#profile-line").textContent = profileLine();
  }
}

function abortRequest() {
  if (abortController) {
    abortController.abort();
    abortController = null;
    stats.aborted += 1;
  }
}

// Returns true when the reply was applied, false when a newer profile
// superseded it (aborted or discarded). Throws on a real failure.
async function fetchScores(key = profileKey()) {
  abortRequest();
  const controller = new AbortController();
  abortController = controller;
  const requestGeneration = ++generation;
  stats.requests += 1;

  let payload;
  try {
    const response = await fetch(SCORE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profileBody()),
      signal: controller.signal,
    });
    payload = await response.json();
  } catch (error) {
    if (error && error.name === "AbortError") return false;
    throw error;
  } finally {
    if (abortController === controller) abortController = null;
  }

  if (requestGeneration !== generation) {
    stats.discarded += 1; // a newer profile was requested meanwhile
    return false;
  }
  if (!payload.ok) throw new Error(payload.error || "Scoring failed");
  // Never cache or show a partial result.
  if (payload.productsScored !== products.length || payload.rows.length !== products.length) {
    throw new Error(`Incomplete scoring response: ${payload.rows.length} of ${products.length} products`);
  }
  if (payload.cache === "HIT") stats.serverCacheHits += 1;
  rememberPayload(key, payload);
  applyPayload(key, payload);
  console.info(
    `[validation] scoring_engine.py ${payload.cache === "HIT" ? "(server cache)" : "scored"} ` +
      `${payload.productsScored} products: ${payload.eligibleCount} eligible, ` +
      `${payload.notSuggestedCount} not suggested, ${payload.unscorableCount} unscorable. ` +
      `Attributes: ${Object.values(payload.profile.attributeColumns).join(", ")}`
  );
  return true;
}

async function runRescore(key) {
  try {
    const applied = await fetchScores(key);
    if (!applied) return; // superseded: the newer request renders
  } catch (error) {
    if (key !== profileKey()) return; // failure of a profile nobody wants now
    console.error("[validation] scoring request failed", error);
    setLoading(false);
    $("#results-count").textContent = `Could not update product scores: ${error.message}`;
    return;
  }
  if (key !== profileKey()) return;
  setLoading(false);
  renderAll();
}

function requestRescore() {
  const key = profileKey();
  window.clearTimeout(rescoreTimer);

  const cached = clientCache.get(key);
  if (cached) {
    // Scored before in this session: apply now, with no request. Cancel any
    // request still running for another profile so it cannot land later.
    abortRequest();
    generation += 1;
    stats.clientCacheHits += 1;
    rememberPayload(key, cached);
    applyPayload(key, cached);
    setLoading(false);
    renderAll();
    return;
  }

  setLoading(true);
  rescoreTimer = window.setTimeout(() => runRescore(key), RESCORE_DEBOUNCE_MS);
}

// ---------------------------------------------------------------------------
// Rows, filtering, bands
// ---------------------------------------------------------------------------

// Synchronous by design: fetchScores() has already populated the map. Every
// number here comes from the server; nothing is computed in JavaScript.
function computeScoredRows() {
  const rows = products.map((product) => {
    const scored = scoreByUid.get(product.uid) || null;
    return {
      product,
      scored,
      score: scored ? scored.score : null,
      status: scored ? scored.status : "UNSCORABLE",
    };
  });

  // Ranking: score DESC, then product name ASC.
  return rows.sort((left, right) => {
    const leftScore = left.score === null ? -Infinity : left.score;
    const rightScore = right.score === null ? -Infinity : right.score;
    if (rightScore !== leftScore) return rightScore - leftScore;
    return left.product.name.localeCompare(right.product.name);
  });
}

// "All" is a view, not a profile value: it only means "do not filter by
// Face/Body". The server treats it the same way.
function isAllContext() {
  return state.context === (dataset?.metadata?.allContext || "All");
}

function rangeOf(row) {
  if (row.status === "NOT_SUGGESTED") return "not-suggested";
  if (row.status !== "ELIGIBLE" || row.score === null) return null;
  return SCORE_RANGES.find((range) => range.test(row.score))?.key || null;
}

// Context, category, price and search. The score band is applied separately so
// the band counts can be computed against everything else the doctor filtered.
function baseFilters(rows) {
  const search = state.search.trim().toLowerCase();
  return rows.filter((row) => {
    const product = row.product;
    if (!isAllContext() && product.context !== state.context) return false;
    if (state.typeFilter !== "All" && product.category !== state.typeFilter) return false;

    const price = Number(product.price || product.mrp || 0);
    if (state.priceFilter === "under-500" && !(price > 0 && price < 500)) return false;
    if (state.priceFilter === "500-999" && !(price >= 500 && price < 1000)) return false;
    if (state.priceFilter === "1000-1999" && !(price >= 1000 && price < 2000)) return false;
    if (state.priceFilter === "2000-plus" && !(price >= 2000)) return false;

    if (search) {
      const haystack = `${product.name} ${product.brand} ${product.category} ${product.primaryIngredients} ${product.secondaryIngredients}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

function applyFilters(rows) {
  const scoped = baseFilters(rows);
  if (state.scoreFilter === "not-suggested") {
    return scoped.filter((row) => row.status === "NOT_SUGGESTED");
  }
  const eligible = scoped.filter((row) => row.status === "ELIGIBLE");
  if (state.scoreFilter === "All") return eligible;
  const range = SCORE_RANGES.find((item) => item.key === state.scoreFilter);
  return range ? eligible.filter((row) => range.test(row.score)) : eligible;
}

function renderScoreBins(rows) {
  const scoped = baseFilters(rows);
  const counts = Object.fromEntries(SCORE_RANGES.map((range) => [range.key, 0]));
  scoped.forEach((row) => {
    const key = rangeOf(row);
    if (key) counts[key] += 1;
  });
  $("#score-bins").innerHTML = SCORE_RANGES.map(
    (range) => `
      <button class="bin-card ${range.tone} ${state.scoreFilter === range.key ? "active" : ""}" type="button" data-bin="${range.key}">
        <strong>${formatNumber(counts[range.key])}</strong>
        <span>${range.label}</span>
      </button>
    `
  ).join("");
}

// ---------------------------------------------------------------------------
// Product cards
// ---------------------------------------------------------------------------

function scoreLabel(row) {
  if (row.status === "NOT_SUGGESTED") return "NS";
  return formatScoreDisplay(row.score);
}

function scoreClass(row) {
  if (row.status === "NOT_SUGGESTED") return "blocked";
  if (row.score === null) return "blocked";
  if (row.score >= 80) return "green";
  if (row.score >= 50) return "yellow";
  return "red";
}

function productCard(row) {
  const product = row.product;
  const note =
    row.status === "NOT_SUGGESTED"
      ? `Not suggested: ${escapeHtml((row.scored.disqualifyingIngredients || []).join(", ") || "disqualifying ingredient")} scored -100`
      : row.score === null
      ? "No scoreable ingredients for this profile."
      : `Primary ${formatScoreDisplay(row.scored.primaryAverage)} | Secondary ${formatScoreDisplay(row.scored.secondaryAverage)}`;
  return `
    <article class="product-card" data-uid="${escapeHtml(product.uid)}">
      ${productImage(product)}
      <div class="product-info">
        <div class="card-top">
          <div class="product-title">${escapeHtml(product.name)}</div>
          <div class="score-circle ${scoreClass(row)}">${escapeHtml(scoreLabel(row))}</div>
        </div>
        <div class="meta-line">
          <span class="pill">${escapeHtml(product.brand || "Brand n/a")}</span>
          <span class="pill">${escapeHtml(product.category)}</span>
          <span class="pill">${escapeHtml(product.context || "-")}</span>
        </div>
        <div class="price-line">${formatMoney(product.price || product.mrp)}</div>
        <p class="why-line">${note.startsWith("Not suggested") ? note : escapeHtml(note)}</p>
      </div>
    </article>
  `;
}

function renderProducts(rows, filteredRows) {
  currentRowsByUid = new Map(filteredRows.map((row) => [row.product.uid, row]));
  const scoped = baseFilters(rows);
  const unscorable = scoped.filter((row) => row.status === "UNSCORABLE").length;
  const rangeLabel =
    state.scoreFilter === "All"
      ? "all recommendable"
      : SCORE_RANGES.find((range) => range.key === state.scoreFilter)?.label || state.scoreFilter;

  if (!filteredRows.length) {
    $("#product-grid").innerHTML = `<p class="empty-line">No products match these filters.</p>`;
  } else {
    $("#product-grid").innerHTML = filteredRows.map(productCard).join("");
  }
  // Always state the scope: what is listed, what the context holds, and the
  // size of the whole validation catalogue.
  const contextLabel = isAllContext() ? "all contexts" : state.context;
  $("#results-count").textContent =
    `Showing ${formatNumber(filteredRows.length)} products in ${contextLabel} (${rangeLabel})` +
    ` - ${formatNumber(dataset.metadata.productCount)} in validation catalogue`;
  $("#profile-line").textContent = profileLine();
  const note = $("#unscorable-note");
  if (note) {
    note.textContent = unscorable
      ? `${formatNumber(unscorable)} product(s) have no scoreable ingredients and are excluded from every band.`
      : "";
  }
}

function profileLine() {
  const sensitivity = state.sensitive ? "Sensitive" : "Not sensitive";
  return `${state.context} | ${state.skinType} | ${sensitivity} | ${state.age} | ${state.concern} | ${state.specialConditions.join(", ")}`;
}

// ---------------------------------------------------------------------------
// Ingredient breakdown
// ---------------------------------------------------------------------------

// How this raw ingredient reached its canonical row, straight from
// scoring_engine.py's own mapping method. The row status is shown alongside
// whenever the ingredient was not scored, so a doctor can tell an excluded
// ingredient from an unresolved or disqualifying one.
function methodCell(item) {
  const method = item.mappingMethod || "";
  const tone = METHOD_TONES[method] || "muted";
  const badge = method
    ? `<span class="method-badge ${tone}">${escapeHtml(method)}</span>`
    : `<span class="ingredient-muted">-</span>`;
  const status =
    item.status && item.status !== "SCORED"
      ? `<div class="method-status">${escapeHtml(item.status.toLowerCase().replace(/_/g, " "))}</div>`
      : "";
  return `${badge}${status}`;
}

function ingredientTable(items, loadError = null) {
  if (loadError) return `<p class="ingredient-empty">Could not load ingredient scores: ${escapeHtml(loadError)}</p>`;
  if (items === undefined) return `<p class="ingredient-empty">Loading ingredient scores…</p>`;
  if (!items || !items.length) return `<p class="ingredient-empty">None listed.</p>`;
  return `
    <table class="ingredient-table">
      <thead><tr><th>Raw ingredient</th><th>Canonical ingredient</th><th>Method</th><th>Attribute scores</th><th>Score</th></tr></thead>
      <tbody>
        ${items
          .map((item) => {
            const attributes = Object.entries(item.attributeScores || {})
              .map(([label, value]) => `${escapeHtml(label)} ${value === null ? "-" : value}`)
              .join(", ");
            const score = item.disqualifying
              ? `<span class="ingredient-flag">-100</span>`
              : item.score === null
              ? "-"
              : item.score;
            const mapped = item.canonical
              ? escapeHtml(item.canonical)
              : `<span class="ingredient-muted">not mapped</span>`;
            return `<tr>
              <td>${escapeHtml(item.name)}</td>
              <td>${mapped}</td>
              <td>${methodCell(item)}</td>
              <td class="ingredient-attrs">${attributes || "-"}</td>
              <td class="ingredient-score">${score}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>
  `;
}

function detailTile(label, value) {
  return `<div class="layer-tile"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

// The validation view: ingredient -> ingredient score -> group average ->
// category weighting -> final score, exactly as scoring_engine.py computed it.
// The headline figures render at once from the catalogue scores; the ingredient
// tables arrive from /api/v3/detail, scored by the same engine for the same
// profile.
function openProductDetail(uid) {
  const row = currentRowsByUid.get(uid) || lastScoredRows.find((item) => item.product.uid === uid);
  if (!row) return;
  const key = appliedKey;
  const cacheKey = `${key}|${uid}`;
  const cached = detailCache.get(cacheKey);
  renderProductDetail(row, cached);
  $("#product-modal").classList.remove("hidden");
  if (cached) return;

  // Only the latest opened product may fill the modal.
  const token = ++detailToken;
  fetch(DETAIL_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...profileBody(), uid }),
  })
    .then((response) => response.json())
    .then((payload) => {
      if (!payload.ok) throw new Error(payload.error || "detail request failed");
      detailCache.set(cacheKey, payload.product);
      if (token === detailToken && key === appliedKey) renderProductDetail(row, payload.product);
    })
    .catch((error) => {
      if (token === detailToken) renderProductDetail(row, undefined, error.message);
    });
}

function renderProductDetail(row, detail, loadError = null) {
  const product = row.product;
  const scored = row.scored || {};
  const weighting =
    scored.scoringBasis === "primary_and_secondary"
      ? `Primary ${scored.primaryAverage} x ${scored.primaryWeight} = ${scored.primaryContribution} &nbsp;+&nbsp; Secondary ${scored.secondaryAverage} x ${scored.secondaryWeight} = ${scored.secondaryContribution}`
      : scored.scoringBasis === "primary_only_renormalised"
      ? `Secondary group empty or unresolved, so Primary is renormalised to 100%: ${scored.primaryAverage} x 1.0`
      : scored.scoringBasis === "secondary_only_renormalised"
      ? `Primary group empty or unresolved, so Secondary is renormalised to 100%: ${scored.secondaryAverage} x 1.0`
      : "No scoreable ingredient in either group.";

  $("#modal-content").innerHTML = `
    <div class="modal-layout">
      <div>
        ${productImage(product, "modal-image")}
        <div class="meta-line">
          <span class="pill">${escapeHtml(product.category)}</span>
          <span class="pill">${escapeHtml(product.context || "-")}</span>
          <span class="pill">${formatMoney(product.price || product.mrp)}</span>
        </div>
      </div>
      <div>
        <h2 class="modal-title" id="modal-title">${escapeHtml(product.name)}</h2>
        <p class="modal-brand">${escapeHtml(product.brand || "Brand n/a")} &middot; ${escapeHtml(product.productId)}</p>
        <div class="detail-grid">
          ${detailTile("Roopsee Score", formatScoreDisplay(row.score))}
          ${detailTile("Primary average", formatScoreDisplay(scored.primaryAverage))}
          ${detailTile("Secondary average", formatScoreDisplay(scored.secondaryAverage))}
        </div>
        ${
          row.status === "NOT_SUGGESTED"
            ? `<p class="status-banner blocked"><strong>NOT SUGGESTED</strong> - disqualifying ingredient (-100): ${escapeHtml(
                (scored.disqualifyingIngredients || []).join(", ")
              )}. The numeric score above is kept for validation only.</p>`
            : row.status === "UNSCORABLE"
            ? `<p class="status-banner blocked"><strong>NO SCORE</strong> - no ingredient could be scored for this profile.</p>`
            : `<p class="status-banner ok"><strong>RECOMMENDABLE</strong> - no disqualifying ingredient for this profile.</p>`
        }
        <div class="explanation">
          <strong>Profile attributes scored</strong>
          <p>${escapeHtml(scored.attributesUsed || "-")}${
    scored.attributesExcluded ? ` <em>(excluded for this category: ${escapeHtml(scored.attributesExcluded)})</em>` : ""
  }</p>
        </div>
        <div class="explanation">
          <strong>Primary ingredients</strong>
          ${ingredientTable(detail ? detail.primary : undefined, loadError)}
          <p class="group-average">Primary average: <strong>${scored.primaryAverage === null ? "-" : scored.primaryAverage}</strong></p>
        </div>
        <div class="explanation">
          <strong>Secondary ingredients</strong>
          ${ingredientTable(detail ? detail.secondary : undefined, loadError)}
          <p class="group-average">Secondary average: <strong>${scored.secondaryAverage === null ? "-" : scored.secondaryAverage}</strong></p>
        </div>
        <div class="explanation">
          <strong>Category weighting (${escapeHtml(product.category)} - ${escapeHtml(scored.categoryRule || "")})</strong>
          <p>${weighting}</p>
          <p class="final-line">Final score: <strong>${formatScoreDisplay(row.score)}</strong>${
    row.score === null ? "" : ` <span class="exact-value">(exact ${row.score} as calculated by scoring_engine.py)</span>`
  }</p>
        </div>
      </div>
    </div>
  `;
}

function closeProductDetail() {
  $("#product-modal").classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

function renderQuiz() {
  const options = dataset.quizOptions;
  const lifeStagesBlocked = (options.gendersWithoutLifeStages || []).includes(state.gender);
  $("#context-options").innerHTML = options.contexts.map((item) => button(item, item === state.context)).join("");
  $("#skin-type-options").innerHTML = options.skinTypes.map((item) => button(item, item === state.skinType)).join("");
  $("#sensitive-options").innerHTML = ["No", "Yes"]
    .map((item) => button(item, (item === "Yes") === state.sensitive))
    .join("");
  $("#concern-options").innerHTML = (options.concernsByContext[state.context] || [])
    .map((item) => button(item, item === state.concern))
    .join("");
  $("#special-options").innerHTML = options.specialConditions
    .map((item) => {
      const disabled = lifeStagesBlocked && (item === "Pregnancy" || item === "Breastfeeding");
      return button(item, state.specialConditions.includes(item), disabled);
    })
    .join("");
  $("#age-select").innerHTML = options.ages
    .map((item) => `<option ${item === state.age ? "selected" : ""}>${escapeHtml(item)}</option>`)
    .join("");
  $("#gender-select").innerHTML = options.genders
    .map((item) => `<option ${item === state.gender ? "selected" : ""}>${escapeHtml(item)}</option>`)
    .join("");
}

function renderFilters() {
  const categories = ["All", ...(dataset.metadata.categories || [])];
  $("#type-filter").innerHTML = categories
    .map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item === "All" ? "All categories" : item)}</option>`)
    .join("");
  $("#price-filter").innerHTML = `
    <option value="All">All prices</option>
    <option value="under-500">Under Rs 500</option>
    <option value="500-999">Rs 500-999</option>
    <option value="1000-1999">Rs 1000-1999</option>
    <option value="2000-plus">Rs 2000+</option>
  `;
}

function renderHeader() {
  const meta = dataset.metadata;
  // The validation catalogue size, then its Face/Body split, so the total is
  // always on screen even while one context is selected.
  const counts = meta.contextCounts || {};
  const split = Object.keys(counts)
    .map((name) => `${formatNumber(counts[name])} ${name}`)
    .join(" | ");
  $("#product-count").textContent = `${formatNumber(meta.productCount)} products`;
  $("#catalogue-split").textContent = split ? `${split} | scoring_engine.py` : "scoring_engine.py";
  const source = $("#source-card");
  if (source) {
    source.innerHTML = `
      <span>Scoring source</span>
      <strong>scoring_engine.py</strong>
      <p>${escapeHtml(meta.populationSource)} - ${formatNumber(meta.productCount)} products. Ingredient scores from ${escapeHtml(
      meta.ingredientScores
    )}. Every score is computed on the server; nothing is scored in the browser.</p>
    `;
  }
}

function renderAll() {
  if (!dataset || !scoreByUid) return;
  // Results of the previous profile are never re-rendered as current while a
  // new profile is being scored; the reply's own render follows.
  if (loading) return;
  lastScoredRows = computeScoredRows();
  renderScoreBins(lastScoredRows);
  renderProducts(lastScoredRows, applyFilters(lastScoredRows));
}

// Re-render only: filters, search and band clicks work on the scores already
// loaded. Profile changes go through requestRescore() instead.
function scheduleRender() {
  window.clearTimeout(renderTimer);
  renderTimer = window.setTimeout(renderAll, 16);
}

function bindEvents() {
  $("#context-options").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-value]");
    if (!target || target.dataset.value === state.context) return;
    state.context = target.dataset.value;
    // Concern lists differ per context, so reset to a concern this one offers.
    const available = dataset.quizOptions.concernsByContext[state.context] || [];
    if (!available.includes(state.concern)) state.concern = available[0] || "None";
    state.typeFilter = "All";
    renderQuiz();
    renderFilters();
    requestRescore();
  });

  $("#skin-type-options").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-value]");
    if (!target) return;
    state.skinType = target.dataset.value;
    renderQuiz();
    requestRescore();
  });

  $("#sensitive-options").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-value]");
    if (!target) return;
    state.sensitive = target.dataset.value === "Yes";
    renderQuiz();
    requestRescore();
  });

  $("#concern-options").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-value]");
    if (!target) return;
    state.concern = target.dataset.value;
    renderQuiz();
    requestRescore();
  });

  $("#special-options").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-value]");
    if (!target || target.disabled) return;
    const value = target.dataset.value;
    if (value === "None") {
      state.specialConditions = ["None"];
    } else {
      const selected = new Set(state.specialConditions.filter((item) => item !== "None"));
      if (selected.has(value)) selected.delete(value);
      else selected.add(value);
      state.specialConditions = selected.size ? [...selected] : ["None"];
    }
    renderQuiz();
    requestRescore();
  });

  $("#age-select").addEventListener("change", (event) => {
    state.age = event.target.value;
    requestRescore();
  });

  $("#gender-select").addEventListener("change", (event) => {
    state.gender = event.target.value;
    renderQuiz();
    requestRescore();
  });

  $("#search-input").addEventListener("input", (event) => {
    state.search = event.target.value;
    scheduleRender();
  });

  $("#type-filter").addEventListener("change", (event) => {
    state.typeFilter = event.target.value;
    scheduleRender();
  });

  $("#price-filter").addEventListener("change", (event) => {
    state.priceFilter = event.target.value;
    scheduleRender();
  });

  $("#score-bins").addEventListener("click", (event) => {
    const card = event.target.closest("[data-bin]");
    if (!card) return;
    const key = card.dataset.bin;
    state.scoreFilter = state.scoreFilter === key ? "All" : key;
    scheduleRender();
  });

  $("#product-grid").addEventListener("click", (event) => {
    const card = event.target.closest("[data-uid]");
    if (card) openProductDetail(card.dataset.uid);
  });

  $("#modal-close").addEventListener("click", closeProductDetail);
  $("#product-modal").addEventListener("click", (event) => {
    if (event.target.id === "product-modal") closeProductDetail();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeProductDetail();
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function init() {
  const response = await fetch(DATA_URL);
  if (!response.ok) throw new Error(`Failed to load ${DATA_URL}`);
  dataset = await response.json();
  products = dataset.products || [];
  renderHeader();
  renderQuiz();
  renderFilters();
  bindEvents();

  // The catalogue above is fetched once per page load and never again; only
  // scores are requested from here on.
  const key = profileKey();
  setLoading(true);
  const applied = await fetchScores(key);
  // If the doctor already changed the profile while this was loading, the newer
  // request owns the loading state and the render.
  if (!applied || key !== profileKey()) return;
  setLoading(false);
  renderAll();
}

init().catch((error) => {
  console.error(error);
  document.body.innerHTML = `
    <main class="page-shell">
      <section class="quiz-card">
        <h1>Could not load the validation catalogue.</h1>
        <p>${escapeHtml(error.message)}</p>
        <p>Start the server with <code>python app.py</code> and reload this page.</p>
      </section>
    </main>
  `;
});

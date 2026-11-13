"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const LABELS = {
  compliant: "Compliant", exemption: "Exemption", svhc: "SVHC > 0.1 %", non_compliant: "Non-compliant",
  needs_review: "Needs review", unknown: "Unknown", dnp: "Not populated",
};
const VERDICT = {
  compliant: "Compliant", exemption: "Compliant (exemptions)", svhc: "Compliant, SVHC to declare",
  non_compliant: "Non-compliant", needs_review: "Needs review",
};
const MATCH = {
  exact: ["Exact", ""], mpn: ["Part number only", ""], close: ["Close", ""],
  mpn_other_manufacturer: ["Other manufacturer", "warn"], partial: ["Partial", "warn"],
  ambiguous: ["Ambiguous", "warn"], not_found: ["Not found", "bad"], "-": ["—", ""],
};
const NORMS = [["rohs2", "RoHS 2", "Directive 2011/65/EU"], ["rohs3", "RoHS 3", "Directive (EU) 2015/863"], ["reach", "REACH", "Regulation (EC) 1907/2006"]];
const DATE_LOCALE = "en-GB";

let COUNTRIES = {};
const country = (code) => code ? `<span title="${esc(COUNTRIES[code] || "")}">${esc(code)}</span>` : "";
const exHref = (code) => "#exemption/" + encodeURIComponent(code);
const exLinks = (field) => (field || "").split(/[,;/]|\s+et\s+|\s+and\s+/).map((c) => c.trim()).filter(Boolean)
  .map((c) => `<a class="ex-link" href="${exHref(c)}" title="See the official text and the expiry date">${esc(c)}</a>`).join(", ");
const eccnHref = (e) => "#eccn/" + encodeURIComponent(e);
const eccnCell = (e) => !e ? "" : e === "EAR99"
  ? `<a class="eccn-link mono" href="${eccnHref(e)}" title="See the definition of EAR99">EAR99</a>`
  : `<a class="eccn-link" href="${eccnHref(e)}" title="Subject to export control: see the scope and the definition"><span class="badge b-eccn">${esc(e)}</span></a>`;
const badge = (s) => `<span class="badge b-${esc(s)}">${esc(LABELS[s] || s)}</span>`;
const plural = (n, word, many = word + "s") => `${n} ${n > 1 ? many : word}`;

const state = { file: null, report: null, filter: "all", overrides: {}, substitutions: {}, classifications: {}, user: null, historyId: null, historyItems: [], page: 0, pageSize: 50 };

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw Object.assign(new Error(msg), { status: r.status, kind: r.headers.get("X-Error-Kind") || "" });
  }
  return r.status === 204 ? null : r;
}

/* ------------------------------------------------------------------ tabs and navigation
   Addresses: #home (default), #analysis, #database, #component/<id>, #manufacturers, #manufacturer/<key>,
   #svhc, #svhc/<id>…
   (shareable links, Back button) */
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  const target = "#" + t.dataset.tab;
  if (location.hash === target) route(); else location.hash = target;
}));

// tab: the tab to highlight, when the panel has none of its own (the component page highlights Database)
function showTab(name, tab = name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  const btn = $(`.tab[data-tab="${tab}"]`);
  if (btn) revealTab(btn);
}

/* Tab bar on narrow screens. On touch screens the native scrollbar is hidden (style.css): a slim bar on the
   bottom line shows the visible part, and fades mark the sides with hidden tabs. */
const tabBar = $(".tabbar"), tabList = $(".tabs"), tabThumb = $(".tabscroll span");
let tabScrollTimer;

function revealTab(tab) {
  // Scrolls the bar so that the selected tab is not cut off by an edge or under a fade
  const bar = tabList.getBoundingClientRect(), r = tab.getBoundingClientRect(), margin = 32;
  if (r.left < bar.left + margin) tabList.scrollBy({ left: r.left - bar.left - margin, behavior: "smooth" });
  else if (r.right > bar.right - margin) tabList.scrollBy({ left: r.right - bar.right + margin, behavior: "smooth" });
}

function updateTabScroll() {
  const hidden = tabList.scrollWidth - tabList.clientWidth;
  const left = Math.min(Math.max(tabList.scrollLeft, 0), Math.max(hidden, 0)); // iOS bounce goes past the ends
  tabBar.classList.toggle("scrollable", hidden > 1);
  tabBar.classList.toggle("more-left", hidden > 1 && left > 1);
  tabBar.classList.toggle("more-right", hidden > 1 && left < hidden - 1);
  if (hidden <= 1) return;
  const track = tabBar.clientWidth, thumb = track * tabList.clientWidth / tabList.scrollWidth;
  tabThumb.style.width = `${thumb}px`;
  tabThumb.style.transform = `translateX(${(track - thumb) * left / hidden}px)`;
}

tabList.addEventListener("scroll", () => {
  updateTabScroll();
  tabBar.classList.add("scrolling");
  clearTimeout(tabScrollTimer);
  tabScrollTimer = setTimeout(() => tabBar.classList.remove("scrolling"), 800);
}, { passive: true });
if (window.ResizeObserver) {
  // The bar's width changes with the window, the tabs' width with the font and the bold active tab
  const watch = new ResizeObserver(updateTabScroll);
  [tabList, ...$$(".tab")].forEach((el) => watch.observe(el));
} else {
  addEventListener("resize", updateTabScroll);
}
updateTabScroll();

function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h.startsWith("confirm/")) {
    // Link from the sign-up confirmation e-mail: the token is removed from the address bar at once
    confirmToken = h.slice("confirm/".length);
    history.replaceState(null, "", location.pathname + location.search + "#analysis");
    showTab("analysis");
    openAuth("confirm");
  } else if (h.startsWith("review/")) {
    // Link from the administrator's e-mail: the token is removed from the address bar at once
    const reviewLink = h.slice("review/".length);
    history.replaceState(null, "", location.pathname + location.search + "#database");
    showTab("database");
    loadComponents();
    openReview(reviewLink);
  } else if (h.startsWith("manufacturer/")) {
    showTab("manufacturers");
    openManufacturer(h.slice("manufacturer/".length));
  } else if (h.startsWith("component/")) {
    showTab("component", "database");
    openComponent(h.slice("component/".length));
  } else if (h.startsWith("svhc/")) {
    showTab("svhc");
    openSvhc(h.slice("svhc/".length));
  } else if (h === "svhc") {
    showTab("svhc");
    $("#svhcDetail").hidden = true;
    $("#svhcList").hidden = false;
    loadSvhcList();
  } else if (h.startsWith("exemption/")) {
    showTab("exemptions");
    openExemption(h.slice("exemption/".length));
  } else if (h === "exemptions") {
    showTab("exemptions");
    $("#exemptDetail").hidden = true;
    $("#exemptList").hidden = false;
    loadExemptionList();
  } else if (h.startsWith("eccn/")) {
    showTab("eccn");
    openEccn(h.slice("eccn/".length));
  } else if (h === "eccn") {
    showTab("eccn");
    $("#eccnDetail").hidden = true;
    $("#eccnList").hidden = false;
    loadEccnList();
  } else if (h === "manufacturers") {
    showTab("manufacturers");
    $("#mfrDetail").hidden = true;
    $("#mfrList").hidden = false;
    loadManufacturers();
  } else if (h === "database") {
    showTab("database");
    loadComponents();
  } else if (h === "analysis") {
    showTab("analysis");
  } else {
    showTab("home");
  }
}
window.addEventListener("hashchange", route);

// Link to a manufacturer's page ("_" = no manufacturer given)
const mfrHref = (key) => "#manufacturer/" + encodeURIComponent(key === "" ? "_" : key);
const mfrLink = (name, key) => (key === undefined || key === null)
  ? esc(name)
  : `<a class="mfr-link" href="${mfrHref(key)}">${esc(name || "(not given)")}</a>`;

// Link to a component's page
const compLink = (c) => `<a class="comp-link" href="#component/${c.id}" title="Open the component page">${esc(c.mpn)}</a>`;
// Number of equivalents found, on the Equivalents buttons like a notification counter (the list shows 20 at most)
const eqBadge = (n) => n ? `<span class="count" title="${n > 20 ? "More than 20" : n} equivalent${n > 1 ? "s" : ""} found">${n > 20 ? "20+" : n}</span>` : "";
const altButton = (id, n, cls = "btn small") => `<button class="${cls} btn-count" data-alt="${id}">Equivalents${eqBadge(n)}</button>`;

async function loadStats() {
  try {
    const s = await (await api("/api/stats")).json();
    state.aiAvailable = s.ai_available;
    $("#pdfMethodHint").textContent = s.ai_available
      ? "AI analysis (Claude) enabled on the server."
      : "Rule-based analysis (no Claude API key configured on the server).";
    $("#dbinfo").textContent = `Database: ${plural(s.total, "component")}`;
  } catch { $("#dbinfo").textContent = "Database: server unreachable"; }
}

/* ------------------------------------------------------------------ analysis */
const drop = $("#drop");
["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (ev) => { if (ev.dataTransfer.files[0]) startAnalysis(ev.dataTransfer.files[0]); });
$("#bomFile").addEventListener("change", (ev) => { if (ev.target.files[0]) startAnalysis(ev.target.files[0]); ev.target.value = ""; });

// Line table filters: reset for every new BOM (a filter left over from a previous BOM,
// or filled in automatically by the browser, would hide every line).
function resetLineFilters() {
  $("#lineSearch").value = "";
  state.filter = "all";
  $$("#filters .chip").forEach((x) => x.classList.toggle("active", x.dataset.filter === "all"));
}

function startAnalysis(file) {
  state.file = file; state.overrides = {}; state.substitutions = {}; state.classifications = {}; state.historyId = null;
  resetLineFilters();
  runAnalysis();
}

// Home page: analyse the example BOM of the repository (examples/bom_example.csv) in one click
$("#btnExample").addEventListener("click", async () => {
  const btn = $("#btnExample");
  btn.disabled = true;
  $("#exampleError").hidden = true;
  try {
    const blob = await (await api("/examples/bom_example.csv")).blob();
    location.hash = "#analysis";
    startAnalysis(new File([blob], "bom_example.csv", { type: "text/csv" }));
  } catch (e) {
    $("#exampleError").textContent = "The example could not be loaded: " + e.message;
    $("#exampleError").hidden = false;
  } finally {
    btn.disabled = false;
  }
});

// Message shown while the server reads the BOM and checks it: the calculation can take a while,
// especially when the free server wakes up after a period without visits.
const BUSY_TEXT = {
  json: (name) => `Analysing ${name}: reading the file and checking each component against the database…`,
  xlsx: () => "Preparing the Excel report…",
  csv: () => "Preparing the CSV report…",
  bom: () => "Preparing the modified BOM…",
};
let analysisSeq = 0, slowTimer = null;

function showBusy(format) {
  $("#analysingText").textContent = BUSY_TEXT[format](state.file.name);
  $("#analysingSlow").hidden = true;
  $("#analysing").hidden = false;
  $("#result").classList.add("busy"); // the current result stays visible, dimmed, until the new one arrives
  clearTimeout(slowTimer);
  slowTimer = setTimeout(() => { $("#analysingSlow").hidden = false; }, 6000);
}

function hideBusy() {
  clearTimeout(slowTimer);
  $("#analysing").hidden = true;
  $("#result").classList.remove("busy");
}

async function runAnalysis(format = "json") {
  if (!state.file) return;
  // A new file or setting while an analysis is running: only the latest answer is shown
  const seq = ++analysisSeq;
  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("mapping", JSON.stringify(withStatusValues(state.overrides)));
  fd.append("substitutions", Object.keys(state.substitutions).length ? JSON.stringify(state.substitutions) : "");
  fd.append("classifications", Object.keys(state.classifications).length ? JSON.stringify(state.classifications) : "");
  // Signed-in user: saved in the history (the entry is updated if the BOM is already there)
  if (format === "json" && state.user) {
    if (state.historyId) fd.append("history_id", state.historyId); else fd.append("save", "1");
  }
  $("#error").hidden = true;
  showBusy(format);
  try {
    const r = await api(`/api/analyze?format=${format}`, { method: "POST", body: fd });
    if (seq !== analysisSeq) return;
    if (format !== "json") {
      const blob = await r.blob();
      const a = Object.assign(document.createElement("a"), {
        href: URL.createObjectURL(blob),
        download: format === "bom"
          ? `${state.file.name.replace(/\.[^.]+$/, "")}_modified.csv`
          : `compliance_${state.file.name.replace(/\.[^.]+$/, "")}.${format}`,
      });
      a.click();
      URL.revokeObjectURL(a.href);
      return;
    }
    const report = await r.json();
    if (seq !== analysisSeq) return;
    state.report = report;
    if (state.report.history_id) state.historyId = state.report.history_id;
    renderReport();
    renderSwapBar();
    renderClassifyBar();
    if (state.user) loadHistory();
  } catch (e) {
    if (seq !== analysisSeq) return;
    if (format === "json" && e.kind === "columns") {
      // Message left under the dialog, in case the user cancels it
      $("#error").textContent = "Columns to specify: " + e.message;
      $("#error").hidden = false;
      $("#result").hidden = true;
      openBomMapping(e.message);
      return;
    }
    $("#error").textContent = "Error: " + e.message;
    $("#error").hidden = false;
    if (format === "json") $("#result").hidden = true;
  } finally {
    if (seq === analysisSeq) hideBusy();
  }
}

// BOM column mapping: opened by "Columns…" or when detection fails
function openBomMapping(problem = "") {
  openMapping({
    kind: "bom", file: state.file, mapping: state.overrides, problem,
    intro: "Choose the file column that matches each field. Only the manufacturer part number is required; " +
      "the manufacturer and the description improve the database search.",
    okLabel: "Analyse",
    onConfirm: (mapping) => { state.overrides = mapping; runAnalysis(); },
  });
}

$$("[data-export]").forEach((b) => b.addEventListener("click", () => runAnalysis(b.dataset.export)));
$("#btnMapping").addEventListener("click", () => openBomMapping());
$("#btnStatusHint").addEventListener("click", () => openBomMapping());

function renderReport() {
  const R = state.report, S = R.summary;
  $("#result").hidden = false;
  $("#fname").textContent = R.filename;
  $("#fmeta").textContent = `— ${plural(S.lines, "line")}, analysed against ${plural(R.database_size, "component")} in the database`;
  const unrecognised = R.lines.filter((l) => Object.values(l.review || {}).some((r) => r.includes("not recognised"))).length;
  $("#statusHint").hidden = !unrecognised;
  $("#statusHintText").textContent = `${plural(unrecognised, "line")} with a RoHS / REACH value of the file that is not recognised.`;

  // Verdicts
  const detail = (c) => [
    c.non_compliant && `${c.non_compliant} non-compl.`, c.needs_review && `${c.needs_review} to review`,
    c.exemption && plural(c.exemption, "exemption"), c.svhc && `${c.svhc} SVHC`, c.compliant && `${c.compliant} compliant`,
  ].filter(Boolean).join(" · ");
  $("#cards").innerHTML =
    `<div class="vcard global v-${S.overall}"><div class="norm">Overall BOM verdict</div><div class="verdict">${VERDICT[S.overall]}</div>
      <div class="detail">${S.overall === "compliant" ? "All lines are compliant" : S.overall === "non_compliant" ? "At least one non-compliant component" : "Missing or uncertain information"}</div></div>` +
    NORMS.map(([k, name, ref]) => `<div class="vcard v-${S[k].verdict}"><div class="norm">${name} <span title="${ref}">· ${ref}</span></div>
      <div class="verdict">${VERDICT[S[k].verdict]}</div><div class="detail">${detail(S[k].counts)}</div></div>`).join("");

  $("#counts").innerHTML = `<span><b>${S.found}</b> found in database</span><span><b>${S.uncertain}</b> uncertain ${S.uncertain > 1 ? "matches" : "match"}</span>
    <span><b>${S.not_found}</b> not found</span><span><b>${S.dnp}</b> not populated</span>` +
    (S.export ? `<span>Export: <b>${S.export.eccn_controlled}</b> controlled ECCN${S.export.eccn_controlled > 1 ? "s" : ""}, <b>${S.export.eccn_unknown}</b> unknown ECCN${S.export.eccn_unknown > 1 ? "s" : ""}</span>` +
      `<span>Origins: ${[
        ...Object.entries(S.export.countries).map(([c, n]) => `${country(c)} (${n})`),
        ...(S.export.country_unknown ? [`<b>${S.export.country_unknown}</b> unknown`] : []),
      ].join(", ") || "—"}</span>` : "");
  renderCharts(S);
  renderLines();
}

/* ------------------------------------------------------------------ pie charts per regulation
   Breakdown of the statuses of populated lines (lines that are not populated are excluded). */
const PIE_STATUSES = [
  ["compliant", "Compliant", "--pie-ok"],
  ["exemption", "Exemption", "--pie-ex"],
  ["svhc", "SVHC", "--pie-svhc"],
  ["needs_review", "Needs review", "--pie-chk"],
  ["non_compliant", "Non-compliant", "--pie-ko"],
];

function pieSvg(slices, total, centerValue, centerLabel) {
  const R = 58, r = 36, C = 60; // ring: outer radius 58, inner radius 36
  const pt = (rad, a) => [C + rad * Math.sin(a), C - rad * Math.cos(a)];
  let a0 = 0;
  const marks = slices.map((s) => {
    const frac = s.n / total;
    if (frac >= 0.9999) {
      return `<path class="slice" data-k="${s.key}" fill="var(${s.color})" fill-rule="evenodd"
        d="M ${C} ${C - R} A ${R} ${R} 0 1 1 ${C - 0.01} ${C - R} Z M ${C} ${C - r} A ${r} ${r} 0 1 0 ${C + 0.01} ${C - r} Z"/>`;
    }
    const a1 = a0 + frac * 2 * Math.PI, large = frac > 0.5 ? 1 : 0;
    const [x0, y0] = pt(R, a0), [x1, y1] = pt(R, a1), [x2, y2] = pt(r, a1), [x3, y3] = pt(r, a0);
    a0 = a1;
    return `<path class="slice" data-k="${s.key}" fill="var(${s.color})"
      d="M ${x0} ${y0} A ${R} ${R} 0 ${large} 1 ${x1} ${y1} L ${x2} ${y2} A ${r} ${r} 0 ${large} 0 ${x3} ${y3} Z"/>`;
  }).join("");
  return `<svg class="pie" viewBox="0 0 120 120" role="img" aria-hidden="true">${marks}
    <text class="center-value" x="60" y="62" text-anchor="middle">${centerValue}</text>
    <text class="center-label" x="60" y="77" text-anchor="middle">${centerLabel}</text></svg>`;
}

function renderCharts(S) {
  $("#charts").innerHTML = NORMS.map(([k, name]) => {
    const counts = S[k].counts;
    const slices = PIE_STATUSES.filter(([key]) => counts[key]).map(([key, label, color]) => ({ key, label, color, n: counts[key] }));
    const total = slices.reduce((t, s) => t + s.n, 0);
    const skipped = counts.dnp || 0;
    if (!total) return `<div class="card chart"><div><h4>${name}</h4><p class="muted">No populated line.</p></div></div>`;
    const okShare = slices.filter((s) => s.key !== "needs_review" && s.key !== "non_compliant").reduce((t, s) => t + s.n, 0);
    const pctOf = (n) => Math.round((100 * n) / total) + " %";
    const legend = slices.map((s) => `<li data-k="${s.key}"><span class="sw" style="background:var(${s.color})"></span>
      <span class="lbl">${s.label}</span><span class="val">${s.n} · ${pctOf(s.n)}</span></li>`).join("");
    const desc = slices.map((s) => `${s.label}: ${s.n}`).join(", ");
    return `<div class="card chart" data-norm="${k}" role="figure" aria-label="${name}: ${desc}">
      ${pieSvg(slices, total, pctOf(okShare), "compliant")}
      <div><h4>${name} <span>· ${plural(total, "line")}</span></h4>
        <ul class="legend">${legend}</ul>
        ${skipped ? `<div class="foot">${skipped} not populated, excluded</div>` : ""}</div>
    </div>`;
  }).join("");
}

// Hover: highlights the slice and its legend row, with a tooltip
const chartTip = Object.assign(document.createElement("div"), { className: "charttip", hidden: true });
document.body.appendChild(chartTip);
function chartHover(ev) {
  const el = ev.target.closest(".pie [data-k], .legend [data-k]");
  const card = ev.target.closest(".chart");
  $$("#charts .pie").forEach((p) => p.classList.remove("dim"));
  $$("#charts .on").forEach((e) => e.classList.remove("on"));
  if (!el || !card) { chartTip.hidden = true; return; }
  const key = el.dataset.k;
  card.querySelector(".pie").classList.add("dim");
  card.querySelectorAll(`[data-k="${key}"]`).forEach((e) => e.classList.add("on"));
  const li = card.querySelector(`.legend [data-k="${key}"]`);
  chartTip.textContent = `${card.querySelector("h4").firstChild.textContent.trim()} · ${li.querySelector(".lbl").textContent}: ${li.querySelector(".val").textContent}`;
  chartTip.hidden = false;
  chartTip.style.left = Math.min(ev.clientX + 14, innerWidth - chartTip.offsetWidth - 8) + "px";
  chartTip.style.top = ev.clientY + 14 + "px";
}
$("#charts").addEventListener("mousemove", chartHover);
$("#charts").addEventListener("mouseleave", chartHover);

$$("#filters .chip").forEach((c) => c.addEventListener("click", () => {
  state.filter = c.dataset.filter;
  $$("#filters .chip").forEach((x) => x.classList.toggle("active", x === c));
  renderLines();
}));
$("#lineSearch").addEventListener("input", renderLines);

// Empty table although the BOM has lines: say why and offer to show everything
function emptyLinesRow() {
  if (!state.report.lines.length) return `<tr><td colspan="12" class="muted">The BOM has no lines.</td></tr>`;
  const q = $("#lineSearch").value.trim();
  const chip = $(`#filters .chip[data-filter="${state.filter}"]`);
  const why = [q ? `search “${esc(q)}”` : "", state.filter !== "all" && chip ? `filter “${esc(chip.textContent)}”` : ""].filter(Boolean).join(" and ");
  return `<tr><td colspan="12" class="emptyrow">None of the ${state.report.lines.length} lines match ${why ? `the ${why}` : "the filters"}.
    <button class="btn small" data-clearfilters>Show all lines</button></td></tr>`;
}

function lineVisible(l) {
  const st = [l.rohs2, l.rohs3, l.reach];
  const f = state.filter;
  if (f === "issues" && !st.some((s) => ["non_compliant", "needs_review"].includes(s))) return false;
  if (f === "ko" && !st.includes("non_compliant")) return false;
  if (f === "check" && !st.includes("needs_review")) return false;
  if (f === "svhc" && !st.some((s) => ["svhc", "exemption"].includes(s))) return false;
  if (f === "eccn" && !(l.component && l.component.eccn && l.component.eccn !== "EAR99")) return false;
  const q = $("#lineSearch").value.trim().toLowerCase();
  if (q && ![l.bom_mpn, l.internal_ref, l.designator, l.bom_manufacturer, l.bom_description].join(" ").toLowerCase().includes(q)) return false;
  return true;
}

function renderLines() {
  const rows = state.report.lines.filter(lineVisible);
  $("#lines tbody").innerHTML = rows.map((l) => {
    const [mLabel, mCls] = MATCH[l.match] || [l.match, ""];
    const comp = l.component;
    const matchCell = `<span class="match ${mCls}">${mLabel}</span>` +
      (comp && l.match !== "exact" ? `<div class="mono muted">${esc(comp.mpn)}</div>` : "");
    const status = (k) => {
      const manual = (l.manual || []).includes(k);
      const reason = (l.review || {})[k] || "";
      // "Needs review" (and a status given by hand) can be changed from here, for this analysis only
      const main = l[k] === "needs_review" || manual
        ? `<button type="button" class="badge-btn" data-review="${l.line}:${k}" title="${esc(manual ? "Status given by hand: click to change it"
          : (reason ? reason + ". " : "") + "Click to give this line a status")}">${badge(l[k])}</button>` : badge(l[k]);
      return main + (manual ? `<span class="inferred manual">by hand</span>` : "") + statusTags(k);
    };
    const statusTags = (k) => (l[k + "_inferred"] ? `<span class="inferred">inferred</span>` : "") +
      ((l.from_bom || []).includes(k) ? `<span class="inferred" title="Status declared in the BOM, not in the database">from the BOM</span>` : "");
    const sugg = l.suggestions.length && !comp
      ? `<div class="sugg">Similar: ${l.suggestions.map((s) => `<span class="mono">${esc(s.mpn)}</span>`).join(", ")}</div>` : "";
    const add = (l.match === "not_found" && l.bom_mpn)
      ? `<div class="sugg"><button class="btn small" data-add="${l.line}">+ Add to database</button></div>` : "";
    const notes = l.notes.map((n) => n.startsWith("RoHS exemption") && l.exemptions
      ? `<div>RoHS exemption: ${exLinks(l.exemptions)} (check the expiry date)</div>` : `<div>${esc(n)}</div>`).join("");
    const altBtn = (!l.dnp && l.bom_mpn) ? `<div class="sugg"><button class="btn small btn-count" data-altline="${l.line}">Equivalents${eqBadge(l.equivalents)}</button></div>` : "";
    const ref = comp
      ? `<a class="comp-link" href="#component/${comp.id}" title="Open the component page">${esc(l.bom_mpn)}</a>`
      : (!l.dnp && l.bom_mpn)
        ? `<a href="#" class="mpn-link" data-altline="${l.line}" title="See equivalents from other manufacturers">${esc(l.bom_mpn)}</a>`
        : esc(l.bom_mpn);
    return `<tr class="${l.dnp ? "dnp" : ""}${l.original_mpn ? " swapped" : ""}">
      <td>${l.line}</td><td class="mono">${esc(l.designator)}</td><td>${esc(l.quantity)}</td>
      <td class="mono">${ref}${l.original_mpn ? `<div class="muted small">instead of ${esc(l.original_mpn)}</div>` : ""}</td>
      <td class="col-ref mono">${esc(l.internal_ref || "")}</td><td>${comp ? mfrLink(l.bom_manufacturer || comp.manufacturer, comp.manufacturer_key) : esc(l.bom_manufacturer)}</td><td>${matchCell}</td>
      <td>${status("rohs2")}</td><td>${status("rohs3")}</td><td>${status("reach")}</td>
      <td>${comp ? eccnCell(comp.eccn) + (comp.country_of_origin ? `<div class="muted">${country(comp.country_of_origin)}</div>` : "") : ""}</td>
      <td class="notes">${notes}${sugg}${add}${altBtn}</td></tr>`;
  }).join("") || emptyLinesRow();
  $("#lines").classList.toggle("no-ref", !state.report.lines.some((x) => x.internal_ref));
  const total = state.report.lines.length;
  $("#lineCount").textContent = rows.length === total ? plural(total, "line") : `${rows.length} of ${total} lines shown`;
  $("#lineCount").classList.toggle("filtered", rows.length !== total);
}

$("#lines").addEventListener("click", (ev) => {
  if (ev.target.closest("[data-clearfilters]")) { resetLineFilters(); renderLines(); return; }
  const rev = ev.target.closest("[data-review]");
  if (rev) {
    const [line, norm] = rev.dataset.review.split(":");
    openClassify(Number(line), norm);
    return;
  }
  const alt = ev.target.closest("[data-altline]");
  if (alt) {
    ev.preventDefault();
    openAlternatives({ line: state.report.lines.find((x) => String(x.line) === alt.dataset.altline) });
    return;
  }
  const b = ev.target.closest("[data-add]");
  if (!b) return;
  const l = state.report.lines.find((x) => String(x.line) === b.dataset.add);
  openDialog(null, { mpn: l.bom_mpn, manufacturer: l.bom_manufacturer, description: l.bom_description });
});

/* ------------------------------------------------------------------ database */
let searchTimer;
$("#q").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 0; loadComponents(); }, 250); });
$("#onlyIssues").addEventListener("change", () => { state.page = 0; loadComponents(); });
$("#prev").addEventListener("click", () => { if (state.page > 0) goToPage(state.page - 1); });
$("#next").addEventListener("click", () => goToPage(state.page + 1));

async function goToPage(page) {
  // The buttons are below the table: once the new page is shown, go back to the top to read it from its first row
  state.page = page;
  await loadComponents();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function loadComponents() {
  const p = new URLSearchParams({ q: $("#q").value, limit: state.pageSize, offset: state.page * state.pageSize, status: $("#onlyIssues").checked ? "issues" : "" });
  try {
    const { total, items } = await (await api("/api/components?" + p)).json();
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    if (state.page >= pages) { state.page = pages - 1; return loadComponents(); }
    $("#pageInfo").textContent = `${plural(total, "component")} — page ${state.page + 1} / ${pages}`;
    $("#prev").disabled = state.page === 0;
    $("#next").disabled = state.page >= pages - 1;
    $("#comps tbody").innerHTML = items.map((c) => `<tr>
      <td class="mono">${compLink(c)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
      <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td>
      <td class="notes">${c.rohs_exemptions ? exLinks(c.rohs_exemptions) : ""}</td><td class="notes">${esc(c.svhc_list)}</td>
      <td>${eccnCell(c.eccn)}</td><td>${country(c.country_of_origin)}</td>
      <td>${esc(c.last_checked)}</td><td><button class="btn small" data-edit="${c.id}">Edit</button> ${altButton(c.id, c.equivalents)}</td></tr>`).join("")
      || `<tr><td colspan="12" class="muted">No components.</td></tr>`;
  } catch (e) {
    $("#comps tbody").innerHTML = `<tr><td colspan="12">Error: ${esc(e.message)}</td></tr>`;
  }
}

$("#comps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  if (b) editComponent(b.dataset.edit);
});
$("#btnAdd").addEventListener("click", () => openDialog(null));

async function editComponent(id) {
  if (ensureEditor()) openDialog(await (await api("/api/components/" + id)).json());
}

/* ------------------------------------------------------------------ manufacturers */
let MFRS = [];
const pct = (n, total) => total ? Math.round((100 * n) / total) + " %" : "—";
const siteLink = (url) => url
  ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, ""))}</a>`
  : `<span class="muted">—</span>`;

async function loadManufacturers() {
  try {
    MFRS = await (await api("/api/manufacturers")).json();
    renderManufacturers();
  } catch (e) {
    $("#mfrs tbody").innerHTML = `<tr><td colspan="7">Error: ${esc(e.message)}</td></tr>`;
  }
}
$("#mfrSearch").addEventListener("input", renderManufacturers);

function renderManufacturers() {
  const q = $("#mfrSearch").value.trim().toLowerCase();
  const rows = MFRS.filter((m) => !q || [m.name, m.key, ...m.aliases].join(" ").toLowerCase().includes(q));
  $("#mfrs tbody").innerHTML = rows.map((m) => {
    const s = m.stats;
    const rohs = s.rohs3_ko ? `<span class="badge b-non_compliant">${s.rohs3_ko} non-compl.</span>`
      : s.rohs3_unknown ? `<span class="badge b-unknown">${s.rohs3_unknown} unknown</span>`
      : s.components ? `<span class="badge b-compliant">Compliant</span>` : "";
    const reach = s.reach_ko ? `<span class="badge b-non_compliant">${s.reach_ko} non-compl.</span>`
      : s.reach_svhc ? `<span class="badge b-svhc">${s.reach_svhc} SVHC</span>` : "";
    return `<tr>
      <td>${mfrLink(m.name, m.key)}</td><td>${s.components}</td><td>${rohs}</td><td>${reach}</td>
      <td>${s.eccn_controlled ? `<span class="badge b-eccn">${s.eccn_controlled}</span>` : ""}</td>
      <td>${country(m.country)}</td><td>${siteLink(m.website)}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">No manufacturers.</td></tr>`;
}

let currentMfr = null;

async function openManufacturer(key) {
  $("#mfrList").hidden = true;
  $("#mfrDetail").hidden = false;
  $("#mfrName").textContent = "Loading…";
  try {
    currentMfr = await (await api("/api/manufacturers/" + encodeURIComponent(key === "" ? "_" : key))).json();
  } catch (e) {
    currentMfr = null;
    $("#mfrName").textContent = "Manufacturer not found";
    $("#mfrAliases").textContent = e.message;
    $("#mfrCards").innerHTML = $("#mfrInfo").innerHTML = $("#mfrComps tbody").innerHTML = "";
    $("#btnEditMfr").hidden = true;
    return;
  }
  renderManufacturer();
}

function renderManufacturer() {
  const m = currentMfr, s = m.stats;
  $("#mfrName").textContent = m.name || "(manufacturer not given)";
  $("#mfrAliases").textContent = m.aliases.length ? "Also written: " + m.aliases.join(", ") : "";
  $("#btnEditMfr").hidden = m.key === "";
  $("#mfrCards").innerHTML = [
    ["Components in database", s.components, ""],
    ["RoHS 3 compliant", `${s.rohs3_ok} / ${s.components}`, pct(s.rohs3_ok, s.components) + (s.rohs3_ko ? ` · ${s.rohs3_ko} non-compliant` : "")],
    ["REACH: SVHC > 0.1 %", s.reach_svhc, s.reach_ko ? `${s.reach_ko} non-compliant` : ""],
    ["Controlled ECCNs", s.eccn_controlled, s.eccn_unknown ? plural(s.eccn_unknown, "unknown ECCN") : ""],
  ].map(([t, v, d]) => `<div class="vcard"><div class="norm">${t}</div><div class="verdict">${v}</div><div class="detail">${esc(d)}</div></div>`).join("");
  const origins = Object.entries(m.origins).map(([c, n]) => `${country(c)} (${n})`).join(", ");
  const dflt = `<span class="tag" title="Known value for this manufacturer, can be edited">default</span>`;
  $("#mfrInfo").innerHTML = `
    <dt>Website</dt><dd>${siteLink(m.website)}${m.website_is_default ? dflt : ""}</dd>
    <dt>Headquarters</dt><dd>${m.country ? `${esc(m.country_name)} (${esc(m.country)})` : `<span class="muted">—</span>`}${m.country_is_default ? dflt : ""}</dd>
    <dt>Components' countries of origin</dt><dd>${origins || `<span class="muted">not given</span>`}</dd>
    <dt>Notes</dt><dd>${m.notes ? esc(m.notes) : `<span class="muted">—</span>`}</dd>`;
  $("#mfrComps tbody").innerHTML = m.components.map((c) => `<tr>
    <td class="mono">${compLink(c)}</td><td>${esc(c.description)}</td>
    <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td>
    <td>${eccnCell(c.eccn)}</td><td>${country(c.country_of_origin)}</td><td>${esc(c.last_checked)}</td>
    <td><button class="btn small" data-edit="${c.id}">Edit</button> ${altButton(c.id, c.equivalents)}</td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">No component from this manufacturer in the database.</td></tr>`;
}

$("#mfrComps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  if (b) editComponent(b.dataset.edit);
});

const dlgMfr = $("#dlgMfr"), mfrForm = $("#mfrForm");
$("#btnEditMfr").addEventListener("click", () => {
  if (!ensureEditor()) return;
  const m = currentMfr;
  for (const f of ["name", "website", "country", "notes"]) mfrForm.elements[f].value = m[f] || "";
  $("#mfrError").hidden = true;
  dlgMfr.showModal();
});
$("#btnMfrCancel").addEventListener("click", () => dlgMfr.close());
mfrForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    currentMfr = await (await api("/api/manufacturers/" + encodeURIComponent(currentMfr.key), {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(mfrForm))),
    })).json();
    dlgMfr.close();
    renderManufacturer();
  } catch (e) { $("#mfrError").textContent = e.message; $("#mfrError").hidden = false; }
});

/* ------------------------------------------------------------------ component dialog */
const dlg = $("#dlg"), form = $("#compForm");
let editingId = null;

function openDialog(comp, preset = {}) {
  if (!ensureEditor()) return false;
  editingId = comp ? comp.id : null;
  const data = comp || { rohs2: "unknown", rohs3: "unknown", reach: "unknown", last_checked: new Date().toLocaleDateString("sv-SE"), ...preset };
  for (const el of form.elements) if (el.name) el.value = data[el.name] ?? "";
  $("#dlgTitle").textContent = comp ? "Edit component" : "New component";
  $("#btnDelete").hidden = !comp;
  $("#dlgError").hidden = true;
  $("#pdfStatus").hidden = true;
  $("#mpnList").innerHTML = $("#mfrList").innerHTML = "";
  $$(".formgrid .filled").forEach((l) => l.classList.remove("filled"));
  dlg.showModal();
  return true;
}

/* ------------------------------------------------------------------ PDF datasheet */
const FIELD_LABELS = {
  mpn: "Part number", manufacturer: "Manufacturer", description: "Description", rohs2: "RoHS 2", rohs3: "RoHS 3",
  reach: "REACH", rohs_exemptions: "Exemptions", svhc_list: "SVHC", eccn: "ECCN", country_of_origin: "Country of origin",
};
const pdfDrop = $("#pdfDrop");
["dragenter", "dragover"].forEach((e) => pdfDrop.addEventListener(e, (ev) => { ev.preventDefault(); pdfDrop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => pdfDrop.addEventListener(e, (ev) => { ev.preventDefault(); pdfDrop.classList.remove("over"); }));
pdfDrop.addEventListener("drop", (ev) => { if (ev.dataTransfer.files[0]) extractPdf(ev.dataTransfer.files[0]); });
$("#pdfFile").addEventListener("change", (ev) => { if (ev.target.files[0]) extractPdf(ev.target.files[0]); ev.target.value = ""; });
$("#btnFromPdf").addEventListener("click", () => { if (openDialog(null)) $("#pdfFile").click(); });

async function extractPdf(file) {
  const box = $("#pdfStatus");
  box.hidden = false;
  box.innerHTML = `<div class="head">Analysing ${esc(file.name)}…</div>`;
  pdfDrop.classList.add("busy");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await (await api("/api/datasheet/extract", { method: "POST", body: fd })).json();
    applyExtraction(r, file.name);
  } catch (e) {
    box.innerHTML = `<div class="head warn">Analysis failed: ${esc(e.message)}</div>`;
  } finally {
    pdfDrop.classList.remove("busy");
  }
}

function applyExtraction(r, fname) {
  const f = r.fields;
  $$(".formgrid .filled").forEach((l) => l.classList.remove("filled"));
  for (const el of form.elements) {
    if (!el.name || !(el.name in f)) continue;
    const v = f[el.name];
    // Only replace a field when the datasheet provides a value (an "unknown" status overwrites nothing)
    if (v && !(el.tagName === "SELECT" && v === "unknown" && el.value !== "unknown")) {
      el.value = v;
      if (v !== "unknown") el.closest("label").classList.add("filled");
    }
  }
  form.elements.last_checked.value = new Date().toLocaleDateString("sv-SE");
  $("#mpnList").innerHTML = (r.candidates.mpn || []).map((c) => `<option value="${esc(c.value)}">`).join("");
  $("#mfrList").innerHTML = (r.candidates.manufacturer || []).map((c) => `<option value="${esc(c.value)}">`).join("");

  const method = r.method === "ai" ? `AI (${esc(r.model || "Claude")})` : "automatic rules";
  const others = (r.candidates.mpn || []).slice(1, 6).map((c) => esc(c.value));
  const ev = Object.entries(r.evidence || {}).filter(([, list]) => list && list.length).map(([k, list]) =>
    `<div class="ev"><b>${FIELD_LABELS[k] || k}</b>: ${list.map((e) => `<q>${esc(e.text)}</q>${e.page ? ` (p. ${e.page})` : ""}`).join(" · ")}</div>`).join("");
  const items = [
    ...r.warnings.map((w) => `<li class="warn">${esc(w)}</li>`),
    ...r.notes.map((n) => `<li>${esc(n)}</li>`),
    others.length ? `<li>Other part numbers found: ${others.join(", ")} (list offered in the Part number field)</li>` : "",
  ].join("");
  $("#pdfStatus").innerHTML =
    `<div class="head">${esc(fname)} analysed with ${method} — ${plural(r.pages, "page")}. Proposed fields are highlighted.</div>` +
    (r.existing_id && !editingId ? `<div class="warn">This component is already in the database. <button type="button" class="btn small" id="openExisting">Open the existing record</button></div>` : "") +
    (items ? `<ul>${items}</ul>` : "") +
    (ev ? `<details><summary>Datasheet excerpts used</summary>${ev}</details>` : "");
  const btn = $("#openExisting");
  if (btn) btn.addEventListener("click", async () => {
    const comp = await (await api("/api/components/" + r.existing_id)).json();
    openDialog(comp);
    // Reapply the proposal on the existing record for comparison
    applyExtraction({ ...r, existing_id: null }, fname);
  });
}
$("#btnCancel").addEventListener("click", () => dlg.close());

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = Object.fromEntries(new FormData(form));
  try {
    await api(editingId ? `/api/components/${editingId}` : "/api/components", {
      method: editingId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    dlg.close();
    afterDbChange();
  } catch (e) { $("#dlgError").textContent = e.message; $("#dlgError").hidden = false; }
});

$("#btnDelete").addEventListener("click", async () => {
  if (!editingId || !confirm("Permanently delete this component from the database?")) return;
  try { await api(`/api/components/${editingId}`, { method: "DELETE" }); dlg.close(); afterDbChange(); }
  catch (e) { $("#dlgError").textContent = e.message; $("#dlgError").hidden = false; }
});

function afterDbChange() {
  loadStats();
  if (state.user) refreshUser(); // components left for the day
  if ($("#tab-database").classList.contains("active")) loadComponents();
  if ($("#tab-manufacturers").classList.contains("active") || $("#tab-component").classList.contains("active")) route();
  if (state.report) runAnalysis(); // rerun the analysis with the updated database
}

/* ------------------------------------------------------------------ import */
$("#btnImport").addEventListener("click", () => { if (ensureEditor()) $("#importFile").click(); });
$("#importFile").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (!f) return;
  openMapping({
    kind: "components", file: f,
    intro: "Check the column used for each field: before anything is written, the Result tab shows the components " +
      "that will be created and the values that will change (same part number and same manufacturer = existing component).",
    okLabel: "Import",
    onConfirm: (mapping) => importComponents(f, mapping),
  });
});

async function importComponents(f, mapping) {
  const fd = new FormData();
  fd.append("file", f);
  fd.append("mapping", JSON.stringify(withStatusValues(mapping)));
  const box = $("#importMsg");
  try {
    const r = await (await api("/api/components/import", { method: "POST", body: fd })).json();
    box.className = "alert " + (r.error_count ? "" : "success");
    box.innerHTML = `Import of <b>${esc(f.name)}</b>: ${r.created} created, ${r.updated} updated, ${r.unchanged} unchanged, ${plural(r.error_count, "error")}.` +
      (r.errors.length ? "<br>" + r.errors.slice(0, 10).map(esc).join("<br>") : "");
    afterDbChange();
  } catch (e) { box.className = "alert error"; box.textContent = "Import failed: " + e.message; }
  box.hidden = false;
}

/* ------------------------------------------------------------------ column mapping
   Dialog shared by the BOM analysis and the database import: header row, column used for each
   field (prefilled by automatic detection), and two views:
   - Result: the lines as they will be read, with what will change (computed by the server);
   - Original file: the raw file, with the columns in use highlighted. */
const dlgMap = $("#dlgMap");
let mapCtx = null; // { kind, file, onConfirm, preview, tab, seq }
const STATUS_FIELDS = new Set(["rohs2", "rohs3", "reach"]);
const ACTION = {
  create: ["New", "b-compliant"], update: ["Changed", "b-exemption"],
  unchanged: ["Unchanged", "b-dnp"], error: ["Error", "b-non_compliant"],
};

async function openMapping({ kind, file, mapping = {}, problem = "", intro = "", okLabel = "Confirm", onConfirm }) {
  mapCtx = { kind, file, onConfirm, preview: null, tab: mapCtx?.tab || "result", seq: 0, before: null };
  $("#mapFile").textContent = "— " + file.name;
  $("#mapIntro").textContent = intro;
  $("#btnMapOk").textContent = okLabel;
  const alert = $("#mapAlert");
  alert.hidden = !problem;
  alert.className = "alert error";
  alert.textContent = problem;
  ["#mapFields", "#mapPreview", "#mapResult", "#mapSummary", "#mapFoot"].forEach((s) => { $(s).innerHTML = ""; });
  $("#mapManual").hidden = true;
  showMapTab(mapCtx.tab);
  if (!dlgMap.open) dlgMap.showModal();
  await loadMapPreview(mapping);
}

async function loadMapPreview(mapping) {
  const seq = ++mapCtx.seq; // only the latest response counts (fast successive choices)
  const fd = new FormData();
  fd.append("file", mapCtx.file);
  fd.append("mapping", JSON.stringify(withStatusValues(mapping)));
  $("#mapStatus").textContent = "Reading the file…";
  $("#btnMapOk").disabled = true;
  let preview;
  try {
    preview = await (await api(`/api/table/preview?kind=${mapCtx.kind}`, { method: "POST", body: fd })).json();
  } catch (e) {
    if (seq !== mapCtx.seq) return;
    // A saved mapping that became invalid (renamed column…): start again from automatic detection
    if (mapping && Object.keys(mapping).length) return loadMapPreview({});
    $("#mapAlert").className = "alert error";
    $("#mapAlert").textContent = "Unreadable file: " + e.message;
    $("#mapAlert").hidden = false;
    $("#mapStatus").textContent = "";
    return;
  }
  if (seq !== mapCtx.seq) return;
  mapCtx.preview = preview;
  renderMapping();
}

// Mapping to send: header row + each field ("__none__" = ignored) + meaning of the status values shown
function collectMapping() {
  const mapping = { _header_row: mapCtx.preview.header_row };
  $$("#mapFields select").forEach((s) => { mapping[s.name] = s.value || "__none__"; });
  const values = {};
  $$("#mapStatuses select").forEach((s) => { if (s.value) (values[s.dataset.group] ||= {})[s.dataset.value] = s.value; });
  if (Object.keys(values).length) mapping._status_values = values;
  return mapping;
}

function renderMapping() {
  const P = mapCtx.preview;
  const hr = $("#mapHeaderRow");
  hr.value = P.header_row;
  hr.max = P.total_rows;
  const examples = columnExamples(P);
  const opts = (sel) => `<option value="">— none —</option>` +
    P.headers.map((h) => `<option value="${esc(h)}" ${h === sel ? "selected" : ""}>${esc(h)}${examples[h] ? ` — e.g. ${esc(examples[h])}` : ""}</option>`).join("");
  $("#mapFields").innerHTML = P.fields.map((f) => {
    const manual = (P.columns[f.key] || "") !== (P.detected[f.key] || "");
    return `<label class="${manual ? "manual" : ""}"><span>${esc(f.label)}${f.required ? " *" : ""}${manual
      ? ` <span class="tag" title="Automatic detection: ${esc(P.detected[f.key] || "none")}">changed</span>` : ""}</span>
      <select name="${esc(f.key)}" data-required="${f.required ? 1 : ""}">${opts(P.columns[f.key])}</select></label>`;
  }).join("");
  renderStatusValues();
  renderManualChanges();
  renderChangeDemo();
  renderMapResult();
  renderMapRaw();
  const missing = P.fields.filter((f) => f.required && !P.columns[f.key]).map((f) => f.label);
  const shown = P.rows.length < P.total_rows ? ` · first ${P.rows.length} of ${P.total_rows} rows shown` : "";
  $("#mapStatus").textContent = missing.length ? `Required field to fill in: ${missing.join(", ")}` : `Header row ${P.header_row || "none"}${shown}`;
  $("#mapStatus").classList.toggle("warn", missing.length > 0);
  $$("#mapFields select").forEach((s) => s.classList.toggle("missing", !!s.dataset.required && !s.value));
  $("#btnMapOk").disabled = missing.length > 0;
}

const DEMO_ROWS = 3;

// First non-empty values of each column, below the header row (for the drop-down lists)
function columnExamples(P) {
  const out = {};
  const data = P.rows.slice(Math.max(P.header_row, 0));
  P.headers.forEach((h, j) => {
    const vals = [];
    for (const r of data) {
      const v = (r[j] || "").trim();
      if (v && !vals.includes(v)) vals.push(v.length > 24 ? v.slice(0, 23) + "…" : v);
      if (vals.length === 2) break;
    }
    out[h] = vals.join(", ");
  });
  return out;
}

// The first lines as they will be read: {line, values: {field: value}}.
// Values computed by the server when possible (normalised), otherwise read from the raw file.
function sampleRows(P) {
  if (P.result && P.result.lines.length) {
    return P.result.lines.slice(0, DEMO_ROWS).map((l) => ({ line: l.line, values: l.values || {} }));
  }
  const idx = Object.fromEntries(Object.entries(P.columns).map(([k, h]) => [k, P.headers.indexOf(h)]));
  return P.rows.map((r, i) => ({ line: i + 1, r })).filter(({ line, r }) => line > P.header_row && r.some((c) => c.trim()))
    .slice(0, DEMO_ROWS).map(({ line, r }) => ({ line, values: Object.fromEntries(Object.entries(idx).map(([k, j]) => [k, r[j] || ""])) }));
}

// After a mapping change: the first 3 lines, with changed fields shown as "before → after"
function renderChangeDemo() {
  const box = $("#mapDemo"), P = mapCtx.preview, B = mapCtx.before;
  const changed = B ? P.fields.filter((f) => (B.columns[f.key] || "") !== (P.columns[f.key] || "")) : [];
  box.hidden = !changed.length;
  if (!changed.length) { box.innerHTML = ""; return; }
  const after = sampleRows(P);
  const shownFields = P.fields.filter((f) => P.columns[f.key] || changed.includes(f));
  const col = (h) => h ? `“${esc(h)}”` : "no column";
  const val = (f, v) => cellValue(f.key, v ?? "");
  const rows = after.length ? after : B.samples;
  const prevByLine = Object.fromEntries(B.samples.map((r) => [r.line, r.values]));
  box.innerHTML = `<div class="mapdemo-head"><b>Effect of the change</b> on the first ${plural(rows.length, "line")}: ` +
    changed.map((f) => `${esc(f.label)} ${col(B.columns[f.key])} → ${col(P.columns[f.key])}`).join(" · ") +
    `<button type="button" class="linkbtn" id="mapDemoClose">Hide</button></div>` +
    `<div class="tablewrap"><table class="grid"><thead><tr><th class="rownum">Line</th>${shownFields.map((f) =>
      `<th class="${changed.includes(f) ? "changed" : ""}">${esc(f.label)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map((row) => {
      const prev = prevByLine[row.line] || {}; // same file line, before the change
      return `<tr><td class="rownum">${row.line}</td>${shownFields.map((f) => {
        if (!changed.includes(f)) return `<td>${val(f, row.values[f.key])}</td>`;
        const before = prev[f.key] ?? "", now = row.values[f.key] ?? "";
        return `<td class="changed">${before === now ? val(f, now) : `<span class="old">${val(f, before)}</span> → ${val(f, now)}`}</td>`;
      }).join("")}</tr>`;
    }).join("") + `</tbody></table></div>`;
  $("#mapDemoClose").addEventListener("click", () => { mapCtx.before = null; box.hidden = true; });
}

// Manual choices compared with automatic detection
function renderManualChanges() {
  const P = mapCtx.preview;
  const col = (h) => h ? `“${esc(h)}”` : "none";
  const changed = P.fields.filter((f) => (P.columns[f.key] || "") !== (P.detected[f.key] || ""));
  $("#mapManual").hidden = !changed.length;
  $("#mapManual").innerHTML = changed.length
    ? `<b>${plural(changed.length, "manual choice")}</b> compared with automatic detection: ` +
      changed.map((f) => `${esc(f.label)}: <s>${col(P.detected[f.key])}</s> → ${col(P.columns[f.key])}`).join(" · ")
    : "";
}

const cellValue = (field, v) => v === "" || v == null ? `<span class="muted">—</span>` : STATUS_FIELDS.has(field) ? badge(v) : esc(v);

function renderMapResult() {
  const P = mapCtx.preview, R = P.result, C = R.counts;
  const roles = P.fields.filter((f) => P.columns[f.key]);
  if (!P.columns.mpn) {
    $("#mapSummary").innerHTML = "";
    $("#mapResult").innerHTML = `<tbody><tr><td class="emptyrow">Choose the manufacturer part number column to see how the file will be read.</td></tr></tbody>`;
    $("#mapFoot").textContent = "";
    return;
  }
  if (mapCtx.kind === "bom") {
    $("#mapSummary").innerHTML = [
      `<span class="stat"><b>${C.lines}</b> component line${C.lines > 1 ? "s" : ""}</span>`,
      `<span class="stat ok"><b>${C.in_base}</b> found in database</span>`,
      C.lines - C.in_base ? `<span class="stat chk"><b>${C.lines - C.in_base}</b> not in database</span>` : "",
      C.dnp ? `<span class="stat"><b>${C.dnp}</b> not populated</span>` : "",
    ].join("");
  } else {
    $("#mapSummary").innerHTML = ["create", "update", "unchanged", "error"].filter((a) => C[a]).map((a) =>
      `<span class="stat"><span class="badge ${ACTION[a][1]}">${ACTION[a][0]}</span> <b>${C[a]}</b></span>`).join("") ||
      `<span class="muted">No line to import.</span>`;
  }
  const status = (l) => mapCtx.kind === "bom"
    ? (l.in_base ? `<span class="badge b-compliant">In database</span>` : `<span class="badge b-unknown">Missing</span>`)
    : `<span class="badge ${ACTION[l.action][1]}">${ACTION[l.action][0]}</span>` +
      (l.error ? `<div class="small errtxt">${esc(l.error)}</div>` : "");
  const cell = (l, f) => {
    const ch = l.changes && l.changes[f.key];
    if (ch) return `<td class="changed"><span class="old">${cellValue(f.key, ch[0])}</span> → ${cellValue(f.key, ch[1])}</td>`;
    return `<td>${cellValue(f.key, l.values[f.key])}</td>`;
  };
  $("#mapResult").innerHTML =
    `<thead><tr><th class="rownum">Line</th><th>${mapCtx.kind === "bom" ? "Database" : "Action"}</th>${roles.map((f) =>
      `<th>${esc(f.label)}<div class="colname" title="${esc(P.columns[f.key])}">${esc(P.columns[f.key])}</div></th>`).join("")}</tr></thead>` +
    `<tbody>${R.lines.map((l) => `<tr class="${l.action || ""}"><td class="rownum">${l.line}</td><td>${status(l)}</td>${roles.map((f) => cell(l, f)).join("")}</tr>`).join("") ||
      `<tr><td class="emptyrow" colspan="${roles.length + 2}">No component line below the header row.</td></tr>`}</tbody>`;
  const total = mapCtx.kind === "bom" ? C.lines : Object.values(C).reduce((a, b) => a + b, 0);
  $("#mapFoot").textContent = [
    total > R.lines.length ? `Detail of the first ${R.lines.length} lines; the summary covers the whole file (${plural(total, "line")}).` : "",
    mapCtx.kind === "components" ? "Components already in the database: fields without a column and empty cells keep their current value." : "",
  ].filter(Boolean).join(" ");
}

function renderMapRaw() {
  const P = mapCtx.preview;
  const labelOf = Object.fromEntries(P.fields.map((f) => [f.key, f.label]));
  const fieldOf = {};
  Object.entries(P.columns).forEach(([k, h]) => { fieldOf[h] = labelOf[k]; });
  const head = `<thead><tr><th class="rownum">Line</th>${P.headers.map((h) =>
    `<th class="${fieldOf[h] ? "mapped" : ""}">${fieldOf[h] ? `<span class="badge b-compliant">${esc(fieldOf[h])}</span>` : `<span class="muted">ignored</span>`}<div class="colname" title="${esc(h)}">${esc(h)}</div></th>`).join("")}</tr></thead>`;
  const body = P.rows.map((r, i) => {
    const n = i + 1;
    const cls = n === P.header_row ? "hdr" : n < P.header_row ? "before" : "";
    return `<tr class="${cls}"><td class="rownum">${n}</td>${r.map((c, j) =>
      `<td class="${fieldOf[P.headers[j]] ? "mapped" : ""}">${esc(c)}</td>`).join("")}</tr>`;
  }).join("");
  $("#mapPreview").innerHTML = head + `<tbody>${body}</tbody>`;
}

function showMapTab(tab) {
  mapCtx.tab = tab;
  $$("[data-maptab]").forEach((b) => { const on = b.dataset.maptab === tab; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); });
  $("#mapResultWrap").hidden = tab !== "result";
  $("#mapRawWrap").hidden = tab !== "raw";
  ["#mapSummary", "#mapFoot"].forEach((s) => { $(s).hidden = tab !== "result"; });
}
$$("[data-maptab]").forEach((b) => b.addEventListener("click", () => showMapTab(b.dataset.maptab)));

$("#mapFields").addEventListener("change", (ev) => {
  const sel = ev.target.closest("select");
  if (!sel) return;
  // Keep the previous state to show the effect of the change on the first lines
  mapCtx.before = { columns: { ...mapCtx.preview.columns }, samples: sampleRows(mapCtx.preview) };
  // A column serves only one field: remove it from the others
  if (sel.value) $$("#mapFields select").forEach((o) => { if (o !== sel && o.value === sel.value) o.value = ""; });
  loadMapPreview(collectMapping()); // the server recomputes the result with the new mapping
});

// New header row: the column names change, so detection runs again on that row
$("#mapHeaderRow").addEventListener("change", (ev) => {
  const n = Math.max(0, Math.min(Number(ev.target.value) || 0, mapCtx.preview.total_rows));
  mapCtx.before = null; // new header row: everything is detected again, no line-by-line comparison
  loadMapPreview({ _header_row: n });
});

$("#mapForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const mapping = collectMapping();
  rememberStatusValues($$("#mapStatuses select").map((s) =>
    ({ group: s.dataset.group, value: s.dataset.value, status: s.value, auto: s.dataset.auto })));
  dlgMap.close();
  mapCtx.onConfirm(mapping);
});
$("#btnMapCancel").addEventListener("click", () => dlgMap.close());

/* ------------------------------------------------------------------ status values of the file
   What each RoHS / REACH value written in the file means ("Compliant(YES)", "Conforme", "TBD"…), chosen in the
   column mapping dialog. The choices are remembered in this browser and sent with every analysis and import. */
const STATUS_MEMORY = "rrc-status-values";
const STATUS_OPTIONS = {
  rohs: [["compliant", "Compliant"], ["exemption", "Compliant with an exemption"], ["non_compliant", "Non-compliant"],
    ["unknown", "Not accepted (ignored)"]],
  reach: [["compliant", "Compliant (no SVHC above 0.1 %)"], ["svhc", "Contains an SVHC above 0.1 %"],
    ["non_compliant", "Non-compliant"], ["unknown", "Not accepted (ignored)"]],
};
const STATUS_GROUP_LABEL = { rohs: "RoHS (RoHS 2 and RoHS 3 columns)", reach: "REACH" };

function rememberedStatusValues() {
  try { return JSON.parse(localStorage.getItem(STATUS_MEMORY)) || {}; } catch { return {}; }
}

// Keep the choices that differ from automatic recognition; a value set back to automatic is forgotten
function rememberStatusValues(choices) {
  const mem = rememberedStatusValues();
  for (const { group, value, status, auto } of choices) {
    mem[group] = mem[group] || {};
    if (status && status !== auto) mem[group][value] = status; else delete mem[group][value];
    if (!Object.keys(mem[group]).length) delete mem[group];
  }
  try { localStorage.setItem(STATUS_MEMORY, JSON.stringify(mem)); } catch { /* private browsing: not remembered */ }
}

// Mapping sent to the server, with the remembered status values (the mapping's own choices win)
function withStatusValues(mapping) {
  const mem = rememberedStatusValues(), own = (mapping && mapping._status_values) || {};
  const out = { ...(mapping || {}) }, merged = {};
  for (const g of ["rohs", "reach"]) {
    const values = { ...(mem[g] || {}), ...(own[g] || {}) };
    if (Object.keys(values).length) merged[g] = values;
  }
  if (Object.keys(merged).length) out._status_values = merged; else delete out._status_values;
  return out;
}

function renderStatusValues() {
  const SV = mapCtx.preview.status_values || {}, box = $("#mapStatuses");
  const groups = Object.keys(SV);
  box.hidden = !groups.length;
  if (!groups.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h3>Status values in the file</h3>
    <p class="muted small">Choose what each RoHS / REACH value means. Your choices are remembered in this browser and
      applied to the next files.</p>` +
    groups.map((g) => `<h4>${STATUS_GROUP_LABEL[g]}</h4><div class="tablewrap"><table class="grid">
      <thead><tr><th>Value in the file</th><th>Lines</th><th>Meaning</th></tr></thead><tbody>${SV[g].map((v) => {
        const current = v.chosen || v.auto || "";
        return `<tr class="${v.chosen && v.chosen !== v.auto ? "manual" : ""}"><td class="mono">${esc(v.value)}</td><td>${v.count}</td>
          <td><select data-group="${g}" data-value="${esc(v.value)}" data-auto="${v.auto || ""}" aria-label="Meaning of ${esc(v.value)}">
          ${v.auto ? "" : `<option value="" ${current ? "" : "selected"}>Not recognised (needs review)</option>`}
          ${STATUS_OPTIONS[g].map(([s, label]) => `<option value="${s}" ${s === current ? "selected" : ""}>${label}${s === v.auto ? " (automatic)" : ""}</option>`).join("")}
          </select></td></tr>`;
      }).join("")}</tbody></table></div>`).join("");
}

$("#mapStatuses").addEventListener("change", (ev) => {
  if (!ev.target.closest("select")) return;
  mapCtx.before = null;
  loadMapPreview(collectMapping()); // the result below shows the new reading
});

/* ------------------------------------------------------------------ equivalents from other manufacturers
   Opened from a BOM line (replacement possible) or from a database component. */
const dlgAlt = $("#dlgAlt");
let altCtx = null; // { line?: <BOM line>, compId?: <component id> }

async function openAlternatives(ctx) {
  altCtx = ctx;
  $("#altBody").innerHTML = `<p class="muted">Searching for equivalents…</p>`;
  $("#altRef").innerHTML = "";
  if (!dlgAlt.open) dlgAlt.showModal();
  const l = ctx.line;
  const compId = ctx.compId ?? (l && l.component ? l.component.id : null);
  let url;
  if (compId) url = `/api/components/${compId}/alternatives`;
  else {
    const p = new URLSearchParams({ mpn: l.bom_mpn, manufacturer: l.bom_manufacturer, description: l.bom_description });
    url = "/api/alternatives?" + p;
  }
  try {
    renderAlternatives(await (await api(url)).json(), compId);
  } catch (e) {
    $("#altBody").innerHTML = `<div class="alert error">Error: ${esc(e.message)}</div>`;
  }
}

function renderAlternatives(data, compId) {
  const ref = data.reference, attr = data.attributes, l = altCtx.line;
  const status = ref.rohs2 ? `${badge(ref.rohs2)} ${badge(ref.rohs3)} ${badge(ref.reach)}` : `<span class="muted">not in the database</span>`;
  const attrs = [attr.category_label, ...Object.values(attr.values).flat(), attr.package, attr.dielectric,
    attr.tolerance != null ? attr.tolerance + " %" : "", attr.channel ? attr.channel + "-channel" : ""].filter(Boolean);
  $("#altRef").innerHTML = `
    <div class="altref-main"><span class="mono">${esc(ref.mpn)}</span> · ${esc(ref.manufacturer || "manufacturer not given")}</div>
    <div class="muted">${esc(ref.description || "")}</div>
    <div class="altref-status">RoHS 2 / RoHS 3 / REACH: ${status}</div>
    <div class="muted small">Recognised characteristics: ${attrs.length ? attrs.map(esc).join(" · ") : "none (description too vague)"}</div>
    ${l && l.original_mpn ? `<div class="swapnote">This line replaces <span class="mono">${esc(l.original_mpn)}</span>.
      <button type="button" class="btn small" id="altRevert">Go back to the original part number</button></div>` : ""}`;
  const rev = $("#altRevert");
  if (rev) rev.addEventListener("click", () => { delete state.substitutions[l.line]; dlgAlt.close(); runAnalysis(); });

  const alts = data.alternatives;
  if (!alts.length) {
    $("#altBody").innerHTML = `<p class="muted">No equivalent from another manufacturer found in the database.
      ${attr.category ? "" : "The description does not identify the component type."}
      ${["microcontroller", "DC-DC converter", "transceiver"].includes(attr.category_label) ? "For this component type, only confirmed equivalents are offered." : ""}</p>`;
    return;
  }
  $("#altBody").innerHTML = `<div class="tablewrap"><table class="grid" id="altTable"><thead><tr>
      <th>Part number</th><th>Manufacturer</th><th>RoHS 2</th><th>RoHS 3</th><th>REACH</th><th>ECCN</th><th>Similarity</th><th></th>
    </tr></thead><tbody>${alts.map((a) => {
      const c = a.component;
      const sim = (a.confirmed ? `<span class="badge b-compliant">Confirmed equivalent</span>` : `<span class="score">${Math.min(100, a.score)} / 100</span>`) +
        (a.matches.length ? `<div class="small ok">✓ ${a.matches.map(esc).join(" · ")}</div>` : "") +
        (a.differences.length ? `<div class="small warn">⚠ ${a.differences.map(esc).join(" · ")}</div>` : "");
      const use = l ? `<button type="button" class="btn small primary" data-use="${c.id}">Use in the BOM</button>` : "";
      const conf = compId ? (a.confirmed
        ? `<button type="button" class="btn small" data-unconfirm="${c.id}">Remove the equivalence</button>`
        : `<button type="button" class="btn small" data-confirm="${c.id}">Confirm the equivalence</button>`) : "";
      // data-label: column title shown when the table switches to cards (narrow window)
      return `<tr class="${a.compliant ? "" : "notcompliant"}">
        <td class="mono">${compLink(c)}<div class="muted small nomono">${esc(c.description)}</div></td>
        <td data-label="Manufacturer">${mfrLink(c.manufacturer, c.manufacturer_key)}</td>
        <td data-label="RoHS 2">${badge(c.rohs2)}</td><td data-label="RoHS 3">${badge(c.rohs3)}</td>
        <td data-label="REACH">${badge(c.reach)}</td><td data-label="ECCN">${eccnCell(c.eccn)}</td>
        <td class="simcell" data-label="Similarity">${sim}</td><td class="actions-cell">${use}${conf}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

$("#altBody").addEventListener("click", async (ev) => {
  const use = ev.target.closest("[data-use]");
  const confirmBtn = ev.target.closest("[data-confirm]");
  const unconfirm = ev.target.closest("[data-unconfirm]");
  if (ev.target.closest("a.mfr-link, a.eccn-link, a.ex-link, a.comp-link")) { dlgAlt.close(); return; }
  if (use && altCtx.line) {
    state.substitutions[altCtx.line.line] = Number(use.dataset.use);
    dlgAlt.close();
    runAnalysis();
    return;
  }
  if ((confirmBtn || unconfirm) && !ensureEditor()) return;
  const compId = altCtx.compId ?? altCtx.line?.component?.id;
  try {
    if (confirmBtn) {
      await api(`/api/components/${compId}/equivalents`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ other_id: Number(confirmBtn.dataset.confirm) }),
      });
    } else if (unconfirm) {
      await api(`/api/components/${compId}/equivalents/${unconfirm.dataset.unconfirm}`, { method: "DELETE" });
    } else return;
    openAlternatives(altCtx);
  } catch (e) {
    $("#altBody").insertAdjacentHTML("afterbegin", `<div class="alert error">${esc(e.message)}</div>`);
  }
});
$("#btnAltClose").addEventListener("click", () => dlgAlt.close());

/* ------------------------------------------------------------------ statuses given by hand ("Needs review")
   For this analysis only (exports and history included): the database is not changed. Lines that need review
   for the same reason can be given the same status at once. */
const dlgClassify = $("#dlgClassify");
let classifyCtx = null;
const ROHS_CHOICES = [["compliant", "Compliant"], ["exemption", "Compliant with an exemption"], ["non_compliant", "Non-compliant"]];
const CLASSIFY_CHOICES = {
  rohs2: ROHS_CHOICES, rohs3: ROHS_CHOICES,
  reach: [["compliant", "Compliant (no SVHC above 0.1 %)"], ["svhc", "Contains an SVHC above 0.1 %"], ["non_compliant", "Non-compliant"]],
};
const NORM_NAMES = { rohs2: "RoHS 2", rohs3: "RoHS 3", reach: "REACH" };

function openClassify(lineNo, norm) {
  const l = state.report.lines.find((x) => x.line === lineNo);
  if (!l) return;
  const manual = (l.manual || []).includes(norm);
  const reason = (l.review || {})[norm] || "";
  classifyCtx = { line: l, norm };
  $("#classifyTitle").textContent = `${NORM_NAMES[norm]} — line ${l.line}`;
  $("#classifyLine").innerHTML = `<span class="mono">${esc(l.bom_mpn || "(no part number)")}</span>` +
    (l.bom_manufacturer ? ` · ${esc(l.bom_manufacturer)}` : "") + (l.designator ? ` · <span class="mono">${esc(l.designator)}</span>` : "");
  $("#classifyReason").textContent = reason ? `Needs review: ${reason}.` : "";
  $("#classifyReason").hidden = !reason;
  $("#classifyChoices").innerHTML = CLASSIFY_CHOICES[norm].map(([v, label]) =>
    `<label class="choice"><input type="radio" name="classify" value="${v}" ${manual && l[norm] === v ? "checked" : ""}> ${badge(v)} <span>${label}</span></label>`).join("") +
    (manual ? `<label class="choice"><input type="radio" name="classify" value=""> <span>Back to “Needs review”</span></label>` : "");
  const same = reason ? state.report.lines.filter((x) => x !== l && x[norm] === "needs_review" && (x.review || {})[norm] === reason) : [];
  $("#classifySame").hidden = !same.length;
  $("#classifySameTitle").textContent = `Same status for the ${plural(same.length, "other line")} that need${same.length > 1 ? "" : "s"} review for the same reason`;
  $("#classifyAll").checked = true;
  $("#classifySameList").innerHTML = same.map((x) => `<label class="choice"><input type="checkbox" value="${x.line}" checked>
    Line ${x.line} · <span class="mono">${esc(x.bom_mpn || "—")}</span>${x.designator ? ` <span class="muted mono">${esc(x.designator)}</span>` : ""}</label>`).join("");
  $("#classifyError").hidden = true;
  dlgClassify.showModal();
}

$("#classifyAll").addEventListener("change", (ev) => $$("#classifySameList input").forEach((c) => { c.checked = ev.target.checked; }));
$("#btnClassifyCancel").addEventListener("click", () => dlgClassify.close());
$("#btnClassifyApply").addEventListener("click", () => {
  const choice = $("#classifyChoices input:checked");
  if (!choice) {
    $("#classifyError").textContent = "Choose a status.";
    $("#classifyError").hidden = false;
    return;
  }
  const { line, norm } = classifyCtx;
  const others = choice.value ? $$("#classifySameList input:checked").map((c) => Number(c.value)) : [];
  for (const n of [line.line, ...others]) {
    const entry = { ...(state.classifications[n] || {}) };
    if (choice.value) entry[norm] = choice.value; else delete entry[norm];
    if (Object.keys(entry).length) state.classifications[n] = entry; else delete state.classifications[n];
  }
  dlgClassify.close();
  runAnalysis();
});

// Banner counting the statuses given by hand
function renderClassifyBar() {
  const n = state.report ? state.report.lines.filter((l) => (l.manual || []).length).length : 0;
  $("#classifyBar").hidden = n === 0;
  $("#classifyCount").textContent = `${plural(n, "line")} with a status given by hand (this analysis only, the database is unchanged)`;
}
$("#btnClassifyReset").addEventListener("click", () => { state.classifications = {}; runAnalysis(); });

// Banner listing the replacements in the BOM
function renderSwapBar() {
  const n = Object.keys(state.substitutions).length;
  $("#swapBar").hidden = n === 0;
  $("#swapCount").textContent = `${plural(n, "line")} replaced by an equivalent`;
}
$("#btnSwapReset").addEventListener("click", () => { state.substitutions = {}; runAnalysis(); });
$("#btnSwapExport").addEventListener("click", () => runAnalysis("bom"));

// "Equivalents" buttons in the Database tab and the manufacturer pages
for (const sel of ["#comps", "#mfrComps", "#svhcComps"]) {
  $(sel).addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-alt]");
    if (b) openAlternatives({ compId: Number(b.dataset.alt) });
  });
}

/* ------------------------------------------------------------------ SVHC: REACH Candidate List (ECHA) */
let SVHC = null;
const svhcIds = (s) => [...s.ec.map((x) => `EC ${x}`), ...s.cas.map((x) => `CAS ${x}`)];
const longDate = (d) => /^\d{4}-\d{2}-\d{2}$/.test(d || "")
  ? new Date(d + "T00:00:00").toLocaleDateString(DATE_LOCALE, { dateStyle: "medium" }) : esc(d || "");
const svhcHref = (id) => "#svhc/" + encodeURIComponent(id);

async function loadSvhcList() {
  try {
    SVHC = await (await api("/api/svhc")).json();
    renderSvhcList();
  } catch (e) {
    $("#svhcs tbody").innerHTML = `<tr><td colspan="5">Error: ${esc(e.message)}</td></tr>`;
  }
}
$("#svhcSearch").addEventListener("input", () => SVHC && renderSvhcList());
$("#svhcOnlyUsed").addEventListener("change", () => SVHC && renderSvhcList());

function renderSvhcList() {
  const S = SVHC;
  $("#svhcEmpty").hidden = S.available;
  $("#svhcTools").hidden = $("#svhcTableWrap").hidden = !S.available;
  if (!S.available) return;
  const q = $("#svhcSearch").value.trim().toLowerCase();
  const used = $("#svhcOnlyUsed").checked;
  const rows = S.substances.filter((s) => (!used || s.components) &&
    (!q || [s.name, s.description, ...s.ec, ...s.cas, ...s.aliases, s.reason].join(" ").toLowerCase().includes(q)));
  $("#svhcs tbody").innerHTML = rows.map((s) => `<tr>
      <td><a class="svhc-link" href="${svhcHref(s.id)}">${esc(s.name)}</a></td>
      <td class="small mono nowrap">${svhcIds(s).map(esc).join("<br>") || `<span class="muted">—</span>`}</td>
      <td class="nowrap">${longDate(s.date)}</td>
      <td class="small">${esc(s.reason)}</td>
      <td>${s.components || `<span class="muted">0</span>`}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">No substance found.</td></tr>`;
  $("#svhcCount").textContent = rows.length === S.substances.length ? plural(rows.length, "substance")
    : `${rows.length} of ${plural(S.substances.length, "substance")}`;
  $("#svhcSource").innerHTML = `Source: <a href="${esc(S.url)}" target="_blank" rel="noopener noreferrer">${esc(S.source)} ↗</a>,
    latest inclusion on ${longDate(S.version)}.` + (S.unmatched_components
    ? ` ${plural(S.unmatched_components, "database component")} list${S.unmatched_components > 1 ? "" : "s"} an SVHC that could not be linked to the list (no CAS number or English name).` : "");
}

async function openSvhc(id) {
  $("#svhcList").hidden = true;
  $("#svhcDetail").hidden = false;
  $("#svhcName").textContent = "Loading…";
  $("#svhcInfo").innerHTML = "";
  $("#svhcComps tbody").innerHTML = "";
  let s;
  try {
    s = await (await api("/api/svhc/" + encodeURIComponent(id))).json();
  } catch (e) {
    $("#svhcName").textContent = "Error: " + e.message;
    return;
  }
  const none = `<span class="muted">—</span>`;
  $("#svhcName").textContent = s.name;
  $("#svhcInfo").innerHTML = `
    ${s.description ? `<dt>Description</dt><dd>${esc(s.description)}</dd>` : ""}
    <dt>EC number</dt><dd class="mono">${s.ec.map(esc).join(", ") || none}</dd>
    <dt>CAS number</dt><dd class="mono">${s.cas.map(esc).join(", ") || none}</dd>
    <dt>In the list since</dt><dd>${longDate(s.date)}</dd>
    <dt>Reason for inclusion</dt><dd>${s.reason ? esc(s.reason) : none}</dd>
    <dt>Duties</dt><dd>Above 0.1 % by weight in an article: information allowing its safe use (REACH article 33), to customers and to consumers on request, and notification to ECHA's SCIP database.</dd>
    <dt>Official source</dt><dd><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">ECHA Candidate List ↗</a></dd>`;
  $("#svhcComps tbody").innerHTML = s.components.map((c) => `<tr>
    <td class="mono">${compLink(c)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
    <td class="small">${esc(c.svhc_list)}</td><td>${badge(c.reach)}</td><td>${altButton(c.id, c.equivalents)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">No database component lists this substance.</td></tr>`;
}

/* ------------------------------------------------------------------ component page */
const REG_TEXT = {
  rohs2: "Directive 2011/65/EU limits six substances in each homogeneous material of electrical and electronic " +
    "equipment: lead, mercury, hexavalent chromium, polybrominated biphenyls (PBB) and polybrominated diphenyl " +
    "ethers (PBDE) to 0.1 % by weight, and cadmium to 0.01 %.",
  rohs3: "Directive (EU) 2015/863 adds four phthalates, limited to 0.1 % each: DEHP, BBP, DBP and DIBP. It applies " +
    "to equipment placed on the market since 22 July 2019 (since 22 July 2021 for medical devices and monitoring " +
    "and control instruments). A RoHS 3 compliant component is also RoHS 2 compliant.",
  reach: "Regulation (EC) No 1907/2006. The supplier of an article containing more than 0.1 % by weight of a " +
    "substance of very high concern (SVHC) from the Candidate List must give enough information to allow its safe " +
    "use (article 33): to its customers, and to consumers on request within 45 days. The article must also be " +
    "notified to ECHA's SCIP database.",
};

function statusText(norm, status) {
  if (status === "compliant") return norm === "reach"
    ? "The manufacturer declares no SVHC above 0.1 % and no substance restricted by REACH."
    : "The manufacturer declares the component compliant: no restricted substance above the limits.";
  if (status === "exemption") return "Compliant thanks to an exemption: a restricted substance is allowed for this " +
    "use until the exemption expires. Check its dates below to plan a replacement.";
  if (status === "svhc") return "Contains a substance of the Candidate List above 0.1 % by weight: the information " +
    "duty and the SCIP notification apply.";
  if (status === "non_compliant") return norm === "reach"
    ? "Does not meet REACH, for example because of a substance restricted by Annex XVII."
    : "Contains a restricted substance above the limit, without a valid exemption: not allowed in equipment " +
      "placed on the EU market.";
  return "No information in the database: ask the manufacturer for its declaration.";
}

async function openComponent(id) {
  const box = $("#compSheet");
  box.innerHTML = `<p class="muted">Loading…</p>`;
  let s;
  try {
    s = await (await api(`/api/components/${encodeURIComponent(id)}/sheet`)).json();
  } catch (e) {
    box.innerHTML = `<div class="alert error">${e.status === 404 ? "This component is not in the database." : esc("Error: " + e.message)}</div>`;
    return;
  }
  const c = s.component, none = `<span class="muted">—</span>`;
  const exRows = s.exemptions.map((e) => `<tr>
      <td class="nowrap"><a class="ex-link" href="${exHref(e.key)}">${esc(e.found ? e.code : e.input)}</a>
        ${e.annex ? `<div class="muted small">Annex ${esc(e.annex)}</div>` : ""}</td>
      <td class="small">${e.found ? esc(((e.parent ? e.parent + " " : "") + e.description).slice(0, 260)) +
        (((e.parent || "") + e.description).length > 260 ? "…" : "") : `<span class="muted">Code not in Annexes III and IV.</span>`}</td>
      <td>${statusBadge(e.status)}</td></tr>`).join("");
  const exTable = exRows ? `<h4>Exemptions used</h4><div class="tablewrap"><table class="grid">
      <thead><tr><th>Exemption</th><th>Official text</th><th>Validity</th></tr></thead><tbody>${exRows}</tbody></table></div>` : "";
  const svhcRows = s.svhc.map((x) => `<tr>
      <td><a class="svhc-link" href="${svhcHref(x.id)}">${esc(x.name)}</a></td>
      <td class="small mono nowrap">${svhcIds(x).map(esc).join("<br>") || none}</td>
      <td class="nowrap">${longDate(x.date)}</td><td class="small">${esc(x.reason)}</td></tr>`).join("");
  const svhcPart = !c.svhc_list ? "" : `<h4>SVHC declared</h4><p>${esc(c.svhc_list)}</p>` + (svhcRows
    ? `<div class="tablewrap"><table class="grid"><thead><tr><th>Substance (Candidate List)</th><th>EC / CAS</th>
        <th>In the list since</th><th>Reason for inclusion</th></tr></thead><tbody>${svhcRows}</tbody></table></div>`
    : `<p class="muted small">${s.svhc_list.available ? "Not linked to a substance of the Candidate List: give its CAS number or its English name."
      : "The Candidate List is not loaded on this server: the details of the substances are not available."}</p>`);
  const card = (norm, title, extra = "") => `<div class="card regcard">
      <div class="reghead"><h3>${title}</h3>${badge(c[norm])}</div>
      <p>${statusText(norm, c[norm])}</p>
      <p class="muted small">${REG_TEXT[norm]}</p>${extra}</div>`;
  box.innerHTML = `
    <div class="mfrhead">
      <div><h2 class="mono">${esc(c.mpn)}</h2>
        <div class="muted">${mfrLink(c.manufacturer, c.manufacturer_key)}${c.description ? ` · ${esc(c.description)}` : ""}</div></div>
      <div class="headbtns"><button class="btn" data-edit="${c.id}">Edit</button> ${altButton(c.id, c.equivalents, "btn")}</div>
    </div>
    <dl class="card mfrinfo">
      <dt>ECCN (export control)</dt><dd>${c.eccn ? eccnCell(c.eccn) + (s.eccn ? `<div class="muted small">${esc(s.eccn.summary)}</div>` : "") : none}</dd>
      <dt>Country of origin</dt><dd>${c.country_of_origin ? `${esc(COUNTRIES[c.country_of_origin] || c.country_of_origin)} (${esc(c.country_of_origin)})` : none}</dd>
      <dt>Source of the information</dt><dd>${c.source ? esc(c.source) : none}</dd>
      <dt>Last checked</dt><dd>${c.last_checked ? esc(c.last_checked) : none}</dd>
    </dl>
    <h3 class="section">Regulations</h3>
    <div class="regcards">
      ${card("rohs2", "RoHS 2")}
      ${card("rohs3", "RoHS 3", exTable)}
      ${card("reach", "REACH", svhcPart)}
    </div>
    <p class="muted small">Statuses come from the database. Check the manufacturer's declarations before any statement of compliance.</p>`;
}

$("#compSheet").addEventListener("click", (ev) => {
  const e = ev.target.closest("[data-edit]");
  const a = ev.target.closest("[data-alt]");
  if (e) editComponent(e.dataset.edit);
  else if (a) openAlternatives({ compId: Number(a.dataset.alt) });
});

/* ------------------------------------------------------------------ ECCN codes */
let ECCNS = { codes: [], unknown_components: 0 };
let currentEccn = null;

async function loadEccnList() {
  try {
    ECCNS = await (await api("/api/eccn")).json();
    renderEccnList();
  } catch (e) {
    $("#eccns tbody").innerHTML = `<tr><td colspan="5">Error: ${esc(e.message)}</td></tr>`;
  }
}
$("#eccnSearch").addEventListener("input", renderEccnList);

function renderEccnList() {
  const q = $("#eccnSearch").value.trim().toLowerCase();
  const rows = ECCNS.codes.filter((e) => !q || [e.code, e.summary, e.official_heading, e.title, ...e.manufacturers].join(" ").toLowerCase().includes(q));
  $("#eccns tbody").innerHTML = rows.map((e) => `<tr>
      <td>${eccnCell(e.code)}</td>
      <td>${esc(e.title || e.summary)}${e.official_heading ? `<div class="muted small">${esc(e.official_heading)}</div>` : ""}</td>
      <td>${e.controlled ? `<span class="badge b-eccn">Controlled</span>` : `<span class="badge b-compliant">Not listed</span>`}</td>
      <td>${e.components}</td>
      <td class="small">${e.manufacturer_links.map((m) => mfrLink(m.name, m.key)).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">No ECCN code in the database.</td></tr>`;
  const u = ECCNS.unknown_components;
  $("#eccnUnknown").textContent = u ? `${plural(u, "component")} without an ECCN.` : "";
}

async function openEccn(code) {
  $("#eccnList").hidden = true;
  $("#eccnDetail").hidden = false;
  $("#eccnCode").textContent = code;
  $("#eccnSummary").textContent = "Loading…";
  try {
    currentEccn = await (await api("/api/eccn/" + encodeURIComponent(code))).json();
  } catch (e) {
    currentEccn = null;
    $("#eccnSummary").textContent = "Code not found: " + e.message;
    $("#eccnParts").innerHTML = $("#eccnInfo").innerHTML = $("#eccnComps tbody").innerHTML = "";
    $("#btnEditEccn").hidden = true;
    return;
  }
  renderEccn();
}

function renderEccn() {
  const e = currentEccn;
  $("#btnEditEccn").hidden = false;
  $("#eccnCode").innerHTML = `${esc(e.code)} ${e.controlled ? `<span class="badge b-svhc">Subject to export control</span>` : `<span class="badge b-compliant">Not listed (EAR99)</span>`}`;
  $("#eccnSummary").textContent = e.title || e.summary;
  $("#eccnParts").innerHTML = e.parts.length ? e.parts.map((p) => `<div class="vcard part">
      <div class="norm">${esc(p.label)}</div><div class="partcode mono">${esc(p.code)}</div>
      <div class="partname">${esc(p.name)}</div><div class="detail">${esc(p.description)}</div></div>`).join("") : "";
  const links = e.links.map((l) => `<div><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)} ↗</a>${l.hint ? `<div class="muted small">${esc(l.hint)}</div>` : ""}</div>`).join("");
  const mfrs = e.manufacturer_links.map((m) => `${mfrLink(m.name, m.key)} (${m.count})`).join(", ");
  $("#eccnInfo").innerHTML = `
    ${e.official_heading ? `<dt>Official heading of entry ${esc(e.entry)}</dt><dd><q>${esc(e.official_heading)}</q><div class="muted small">Commerce Control List text (eCFR${e.as_of ? `, version of ${esc(e.as_of)}` : ""}).</div></dd>` : ""}
    <dt>Scope</dt><dd>${esc(e.explanation)}</dd>
    ${e.control_scope ? `<dt>Extent of control</dt><dd>${esc(e.control_scope)}</dd>` : ""}
    <dt>Official definition</dt><dd>${links}</dd>
    ${e.related_codes.length ? `<dt>Related codes in the database</dt><dd>${e.related_codes.map((c) => eccnCell(c)).join(" ")}</dd>` : ""}
    <dt>Manufacturers</dt><dd>${mfrs || `<span class="muted">—</span>`}</dd>
    <dt>Notes</dt><dd>${e.notes ? esc(e.notes) : `<span class="muted">—</span>`}</dd>`;
  $("#eccnComps tbody").innerHTML = e.components.map((c) => `<tr>
    <td class="mono">${compLink(c)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
    <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td><td>${country(c.country_of_origin)}</td>
    <td><button class="btn small" data-edit="${c.id}">Edit</button></td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">No database component has this code.</td></tr>`;
}

$("#eccnComps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  if (b) editComponent(b.dataset.edit);
});

const dlgEccn = $("#dlgEccn"), eccnForm = $("#eccnForm");
$("#btnEditEccn").addEventListener("click", () => {
  if (!ensureEditor()) return;
  eccnForm.elements.title.value = currentEccn.title || "";
  eccnForm.elements.notes.value = currentEccn.notes || "";
  $("#eccnError").hidden = true;
  dlgEccn.showModal();
});
$("#btnEccnCancel").addEventListener("click", () => dlgEccn.close());
eccnForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    currentEccn = await (await api("/api/eccn/" + encodeURIComponent(currentEccn.code), {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(eccnForm))),
    })).json();
    dlgEccn.close();
    renderEccn();
  } catch (e) { $("#eccnError").textContent = e.message; $("#eccnError").hidden = false; }
});

/* ------------------------------------------------------------------ RoHS exemptions */
let EXEMPTIONS = { exemptions: [] };
let currentExemption = null;
const STATUS_BADGE = { in_force: "b-compliant", expiring_soon: "b-svhc", expired: "b-non_compliant", no_date: "b-ex-none", unknown: "b-unknown" };
const statusBadge = (s) => `<span class="badge ${STATUS_BADGE[s.kind] || "b-unknown"}">${esc(s.label)}</span>`;

async function loadExemptionList() {
  try {
    EXEMPTIONS = await (await api("/api/exemptions")).json();
    renderExemptionList();
  } catch (e) {
    $("#exempts tbody").innerHTML = `<tr><td colspan="6">Error: ${esc(e.message)}</td></tr>`;
  }
}
$("#exemptSearch").addEventListener("input", renderExemptionList);

function renderExemptionList() {
  const q = $("#exemptSearch").value.trim().toLowerCase();
  const rows = EXEMPTIONS.exemptions.filter((e) => !q || [e.code, e.key, e.description, e.parent, ...e.substances, ...e.manufacturers].join(" ").toLowerCase().includes(q));
  $("#exempts tbody").innerHTML = rows.map((e) => `<tr>
      <td class="nowrap"><a class="ex-link" href="${exHref(e.key)}">${esc(e.found ? e.code : e.key)}</a>
        ${e.annex ? `<div class="muted small">Annex ${esc(e.annex)}</div>` : ""}</td>
      <td>${e.found ? esc(((e.parent ? e.parent + " " : "") + e.description).slice(0, 220)) + (((e.parent || "") + e.description).length > 220 ? "…" : "")
        : `<span class="muted">Code not in Annexes III and IV: check how the code is written.</span>`}</td>
      <td class="small">${e.substances.map(esc).join(", ")}</td>
      <td>${statusBadge(e.status)}</td>
      <td>${e.components}</td>
      <td class="small">${e.manufacturer_links.map((m) => mfrLink(m.name, m.key)).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">No exemption used by the database components.</td></tr>`;
  const up = EXEMPTIONS.upcoming;
  $("#exemptSource").innerHTML = EXEMPTIONS.version
    ? `Official text: Directive 2011/65/EU consolidated as of ${esc(EXEMPTIONS.version)}
       (<a href="${esc(EXEMPTIONS.source_url)}" target="_blank" rel="noopener noreferrer">EUR-Lex ↗</a>).
       ${up ? `Version of ${esc(up.version)} already published: ${up.changes ? `${plural(up.changes, "exemption")} changed` : "no change in Annexes III and IV"}.` : ""}`
    : "";
}

async function openExemption(code) {
  $("#exemptList").hidden = true;
  $("#exemptDetail").hidden = false;
  $("#exemptCode").textContent = code;
  $("#exemptSummary").textContent = "Loading…";
  try {
    currentExemption = await (await api("/api/exemptions/" + encodeURIComponent(code))).json();
  } catch (e) {
    currentExemption = null;
    $("#exemptSummary").textContent = "Error: " + e.message;
    return;
  }
  renderExemption();
}

function renderExemption() {
  const e = currentExemption;
  $("#exemptCode").innerHTML = `${e.found ? `Exemption ${esc(e.code)}` : esc(e.input)} ${statusBadge(e.status)}`;
  $("#exemptSummary").textContent = e.found ? e.annex_label : "This code does not match any exemption in Annexes III and IV.";
  const datesList = (e.dates || []).map((d) => {
    const past = d < new Date().toISOString().slice(0, 10);
    return `<span class="badge ${past ? "b-non_compliant" : "b-compliant"}">${new Date(d + "T00:00:00").toLocaleDateString(DATE_LOCALE, { dateStyle: "long" })}</span>`;
  }).join(" ");
  const links = e.links.map((l) => `<div><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)} ↗</a>${l.hint ? `<div class="muted small">${esc(l.hint)}</div>` : ""}</div>`).join("");
  const up = e.upcoming;
  const mfrs = e.manufacturer_links.map((m) => `${mfrLink(m.name, m.key)} (${m.count})`).join(", ");
  $("#exemptInfo").innerHTML = e.found ? `
    ${e.group ? `<dt>Section</dt><dd>${esc(e.group)}</dd>` : ""}
    <dt>Official text</dt><dd>${e.parent ? `<div class="muted">${esc(e.parent)}</div>` : ""}<q>${esc(e.description)}</q></dd>
    <dt>Scope and dates</dt><dd>${e.scope ? esc(e.scope) : `<span class="muted">No detail given (applies without a stated expiry date)</span>`}</dd>
    ${datesList ? `<dt>Dates mentioned</dt><dd>${datesList}</dd>` : ""}
    <dt>Substances</dt><dd>${e.substances.length ? e.substances.map(esc).join(", ") : "—"}</dd>
    <dt>Renewal</dt><dd>${esc(e.renewal_note)}</dd>
    ${up ? `<dt>Upcoming version</dt><dd>Consolidated version of ${esc(up.version)}: ${up.type === "none" ? "no change for this exemption." : `exemption ${esc(up.type)}${up.scope ? ` — ${esc(up.scope)}` : ""}.`} <a href="${esc(up.url)}" target="_blank" rel="noopener noreferrer">See ↗</a></dd>` : ""}
    <dt>Official source</dt><dd>${links}</dd>
    <dt>Manufacturers</dt><dd>${mfrs || `<span class="muted">—</span>`}</dd>
    <dt>Notes</dt><dd>${e.notes ? esc(e.notes) : `<span class="muted">—</span>`}</dd>`
    : `<dt>Code entered</dt><dd>${esc(e.input)}</dd><dt>Official source</dt><dd>${links}</dd>`;
  $("#exemptComps tbody").innerHTML = e.components.map((c) => `<tr>
    <td class="mono">${compLink(c)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
    <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td>
    <td><button class="btn small" data-edit="${c.id}">Edit</button> ${altButton(c.id, c.equivalents)}</td></tr>`).join("")
    || `<tr><td colspan="7" class="muted">No database component uses this exemption.</td></tr>`;
}

$("#exemptComps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  const a = ev.target.closest("[data-alt]");
  if (b) editComponent(b.dataset.edit);
  else if (a) openAlternatives({ compId: Number(a.dataset.alt) });
});

const dlgExempt = $("#dlgExempt"), exemptForm = $("#exemptForm");
$("#btnEditExempt").addEventListener("click", () => {
  if (!ensureEditor()) return;
  exemptForm.elements.notes.value = currentExemption.notes || "";
  $("#exemptError").hidden = true;
  dlgExempt.showModal();
});
$("#btnExemptCancel").addEventListener("click", () => dlgExempt.close());
exemptForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    currentExemption = await (await api("/api/exemptions/" + encodeURIComponent(currentExemption.key), {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: exemptForm.elements.notes.value }),
    })).json();
    dlgExempt.close();
    renderExemption();
  } catch (e) { $("#exemptError").textContent = e.message; $("#exemptError").hidden = false; }
});

/* ------------------------------------------------------------------ accounts and BOM history */
const dlgAuth = $("#dlgAuth"), authForm = $("#authForm");
let authMode = "login";
let authConfig = null;     // { email_confirmation, captcha_site_key } from the server
let authConfigLoading = null;
let confirmToken = "";     // token of the link from the sign-up confirmation e-mail
let captchaWidget = null;  // Cloudflare Turnstile widget, created on first use
let turnstileLoading = null;

function fmtDate(iso) {
  try { return new Date(iso).toLocaleString(DATE_LOCALE, { dateStyle: "short", timeStyle: "short" }); } catch { return iso; }
}

async function loadUser() {
  try { state.user = await (await api("/api/auth/me")).json(); } catch { state.user = null; }
  renderAccount();
  loadHistory();
}

function renderAccount() {
  const u = state.user;
  $("#account").innerHTML = u
    ? `<span class="who" title="${esc(u.email)}">${esc(u.name)}</span>
       <button class="btn small" id="btnMyBoms">My BOMs</button>
       <button class="btn small" id="btnLogout">Sign out</button>`
    : `<button class="btn small primary" id="btnLogin">Sign in</button>`;
  $("#historyHint").hidden = !!u;
  $("#historyBox").hidden = !u;
  renderEditBox();
}

async function refreshUser() {
  try { state.user = await (await api("/api/auth/me")).json(); } catch { /* keep the current state */ }
  renderEditBox();
}

function loadAuthConfig() {
  authConfigLoading ||= api("/api/auth/config").then((r) => r.json())
    .then((c) => { authConfig = c; showCaptcha(); renderEditBox(); }).catch(() => { authConfigLoading = null; });
  return authConfigLoading;
}

// Database tab: what the visitor may change, and the request for edit access sent to the administrator
function renderEditBox() {
  const box = $("#editBox"), html = editAccessHtml();
  box.hidden = !html;
  if (html) box.innerHTML = html;
  if (dlgEditAccess.open) $("#editAccessBody").innerHTML = html;
}

// Who may change the database, and how to get the right ("" when editing is open to everyone)
function editAccessHtml() {
  const u = state.user, c = authConfig;
  if (!c || c.open_editing) return "";
  let html;
  if (!u) {
    html = `To add or edit components, <button class="linkbtn" data-auth="login">sign in</button>
      with an account approved by the administrator.`;
  } else if (u.is_admin) {
    html = "Administrator: you can add, edit and delete components, with no daily limit.";
  } else if (u.can_edit) {
    html = `You can add and edit components: <strong>${u.additions_left}</strong> of ${u.daily_additions}
      new components left today (over the last 24 hours). Only the administrator can delete components.`;
  } else if (u.edit_access === "pending") {
    html = "Edit access requested: waiting for the administrator's answer. You will be told by e-mail.";
  } else {
    const intro = u.edit_access === "refused"
      ? "Your last request for edit access was refused."
      : "Your account can browse the database and analyse BOMs.";
    html = c.edit_requests
      ? `${intro} To add or edit components, <button class="btn small" id="btnRequestEdit">Request edit access</button>`
      : `${intro} Changing the database is reserved for accounts approved by the administrator.`;
  }
  return html;
}

// Opening a form whose changes the server would refuse is pointless: without the right to change the
// database, the sign-in form opens instead, or the explanation of how to get the right.
const dlgEditAccess = $("#dlgEditAccess");
const canEdit = () => !authConfig || authConfig.open_editing || !!state.user?.can_edit;

function ensureEditor() {
  if (canEdit()) return true;
  if (!state.user) {
    openAuth("login", "Sign in with an account approved by the administrator to add or edit components.");
  } else {
    $("#editAccessBody").innerHTML = editAccessHtml();
    if (!dlgEditAccess.open) dlgEditAccess.showModal();
  }
  return false;
}
$("#btnEditAccessClose").addEventListener("click", () => dlgEditAccess.close());

$("#editBox").addEventListener("click", onEditAccessClick);
$("#editAccessBody").addEventListener("click", onEditAccessClick);

async function onEditAccessClick(ev) {
  const box = ev.currentTarget; // no longer set once the request below has been awaited
  const login = ev.target.closest("[data-auth]");
  if (login) { openAuth(login.dataset.auth); return; }
  const btn = ev.target.closest("#btnRequestEdit");
  if (!btn) return;
  btn.disabled = true;
  try {
    state.user = await (await api("/api/edit-access/request", { method: "POST" })).json();
    renderEditBox();
  } catch (e) {
    box.insertAdjacentHTML("beforeend", `<div class="alert error">${esc(e.message)}</div>`);
    btn.disabled = false;
  }
}

// Administrator: approve or refuse a request, from the link received by e-mail
const dlgReview = $("#dlgReview");
let reviewToken = "";

function showReviewError(msg) {
  $("#reviewError").textContent = msg;
  $("#reviewError").hidden = !msg;
}

async function openReview(link) {
  reviewToken = link;
  $("#reviewInfo").textContent = "Loading the request…";
  showReviewError("");
  $("#btnApprove").hidden = $("#btnRefuse").hidden = true;
  if (!dlgReview.open) dlgReview.showModal();
  try {
    const r = await (await api("/api/edit-access/lookup", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: link }),
    })).json();
    $("#reviewInfo").innerHTML = `<strong>${esc(r.email)}</strong>${r.name ? ` (${esc(r.name)})` : ""} asks for
      permission to add and edit components. Account created on ${esc(fmtDate(r.created_at))}, request sent on
      ${esc(fmtDate(r.requested_at))}.`;
    $("#btnApprove").hidden = $("#btnRefuse").hidden = false;
  } catch (e) {
    $("#reviewInfo").textContent = "";
    showReviewError(e.message);
  }
}

async function decide(approve) {
  $("#btnApprove").disabled = $("#btnRefuse").disabled = true;
  try {
    const r = await (await api("/api/edit-access/review", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: reviewToken, approve }),
    })).json();
    $("#reviewInfo").innerHTML = approve
      ? `Approved: <strong>${esc(r.email)}</strong> can now add and edit components. They are told by e-mail.`
      : `Refused: <strong>${esc(r.email)}</strong> is told by e-mail.`;
    $("#btnApprove").hidden = $("#btnRefuse").hidden = true;
    if (state.user) refreshUser();
  } catch (e) {
    showReviewError(e.message);
  } finally {
    $("#btnApprove").disabled = $("#btnRefuse").disabled = false;
  }
}
$("#btnApprove").addEventListener("click", () => decide(true));
$("#btnRefuse").addEventListener("click", () => decide(false));
$("#btnReviewClose").addEventListener("click", () => dlgReview.close());

$("#account").addEventListener("click", async (ev) => {
  if (ev.target.closest("#btnLogin")) openAuth("login");
  else if (ev.target.closest("#btnMyBoms")) {
    location.hash = "#analysis";
    $("#historyBox").scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (ev.target.closest("#btnLogout")) {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    state.user = null;
    state.historyId = null;
    renderAccount();
  }
});
$("#historyHint").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-auth]");
  if (b) openAuth(b.dataset.auth);
});

// mode: "login", "register" or "confirm" (link from the confirmation e-mail)
function openAuth(mode, hint = "") {
  authForm.reset();
  setAuthMode(mode);
  $("#authHint").textContent = hint;
  $("#authHint").hidden = !hint;
  if (!dlgAuth.open) dlgAuth.showModal();
  (mode === "confirm" ? authForm.elements.password : authForm.elements.email).focus();
  loadAuthConfig();
}

function showAuthError(msg) {
  $("#authError").textContent = msg;
  $("#authError").hidden = !msg;
}

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "register", confirming = mode === "confirm";
  $$("#dlgAuth .authtab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
  $("#dlgAuth .authtabs").hidden = confirming;
  $("#authTitle").hidden = $("#authIntro").hidden = !confirming;
  $("#authFields").hidden = false;
  $("#authNameRow").hidden = !signup;
  $("#authEmailRow").hidden = confirming;
  authForm.elements.email.required = !confirming;
  authForm.elements.password.autocomplete = signup ? "new-password" : "current-password";
  $("#authPwHint").hidden = !signup;
  $("#authSubmit").hidden = false;
  $("#authSubmit").textContent = signup ? "Create my account" : confirming ? "Confirm my account" : "Sign in";
  $("#btnAuthCancel").textContent = "Cancel";
  $("#authInfo").hidden = true;
  showAuthError("");
  showCaptcha();
}
$$("#dlgAuth .authtab").forEach((t) => t.addEventListener("click", () => setAuthMode(t.dataset.mode)));
$("#btnAuthCancel").addEventListener("click", () => dlgAuth.close());

// Turnstile captcha on the sign-up form, when the server has one configured (script loaded on demand)
function loadTurnstile() {
  if (window.turnstile) return Promise.resolve();
  turnstileLoading ||= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    s.async = true;
    s.onload = resolve;
    s.onerror = () => { turnstileLoading = null; s.remove(); reject(new Error("The captcha could not be loaded: check your connection or content blocker")); };
    document.head.appendChild(s);
  });
  return turnstileLoading;
}

async function showCaptcha() {
  const box = $("#authCaptcha"), key = authConfig && authConfig.captcha_site_key;
  box.hidden = !(key && authMode === "register");
  if (box.hidden) return;
  try {
    await loadTurnstile();
    if (captchaWidget === null) captchaWidget = turnstile.render(box, { sitekey: key, theme: "auto", size: "flexible" });
  } catch (e) { showAuthError(e.message); }
}

function showSignupSent(data) {
  $("#authFields").hidden = true;
  $("#authSubmit").hidden = true;
  $("#btnAuthCancel").textContent = "Close";
  $("#authInfo").innerHTML = `A confirmation link was sent to <strong>${esc(data.email)}</strong>.
    Open it within ${data.hours} hours to activate your account (check the spam folder too).`;
  $("#authInfo").hidden = false;
}

authForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const password = authForm.elements.password.value;
  let url = "/api/auth/login", body = { email: authForm.elements.email.value, password };
  if (authMode === "confirm") {
    url = "/api/auth/confirm";
    body = { token: confirmToken, password };
  } else if (authMode === "register") {
    url = "/api/auth/register";
    body.name = authForm.elements.name.value;
    if (authConfig && authConfig.captcha_site_key) {
      body.captcha = (captchaWidget !== null && window.turnstile && turnstile.getResponse(captchaWidget)) || "";
      if (!body.captcha) { showAuthError("Tick the “Verify you are human” box first."); return; }
    }
  }
  $("#authSubmit").disabled = true;
  try {
    const data = await (await api(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json();
    if (data && data.pending) { showSignupSent(data); return; }
    state.user = data;
    confirmToken = "";
    dlgAuth.close();
    renderAccount();
    // A BOM that is already open is saved in the history as soon as the user signs in
    if (state.report && state.file) runAnalysis(); else loadHistory();
  } catch (e) {
    showAuthError(e.message);
  } finally {
    $("#authSubmit").disabled = false;
    // A captcha token can only be used once
    if (authMode === "register" && captchaWidget !== null && window.turnstile) turnstile.reset(captchaWidget);
  }
});

const VERDICT_SHORT = { compliant: "Compliant", exemption: "Exemption", svhc: "SVHC", non_compliant: "Non-compliant", needs_review: "Needs review" };
const verdictBadge = (v) => `<span class="badge b-${esc(v)}">${esc(VERDICT_SHORT[v] || v)}</span>`;

async function loadHistory() {
  if (!state.user) return;
  let items;
  try { items = await (await api("/api/history")).json(); } catch (e) {
    $("#historyList").innerHTML = `<p class="muted">History unavailable: ${esc(e.message)}</p>`;
    return;
  }
  $("#historyCount").textContent = items.length ? plural(items.length, "analysis", "analyses") : "";
  $("#historyList").innerHTML = items.length ? `<div class="tablewrap"><table class="grid" id="historyTable"><thead><tr>
      <th>File</th><th>Analysed on</th><th>Overall</th><th>RoHS 2</th><th>RoHS 3</th><th>REACH</th><th>Lines</th><th></th>
    </tr></thead><tbody>${items.map((h) => {
      const s = h.summary || {};
      const swaps = Object.keys(h.substitutions || {}).length;
      return `<tr class="${h.id === state.historyId ? "current" : ""}">
        <td><span class="mono">${esc(h.filename)}</span>${h.id === state.historyId ? ` <span class="tag">open</span>` : ""}</td>
        <td class="nowrap">${esc(fmtDate(h.updated_at))}</td>
        <td>${s.overall ? verdictBadge(s.overall) : ""}</td><td>${s.rohs2 ? verdictBadge(s.rohs2) : ""}</td>
        <td>${s.rohs3 ? verdictBadge(s.rohs3) : ""}</td><td>${s.reach ? verdictBadge(s.reach) : ""}</td>
        <td class="small">${s.lines ?? ""}${swaps ? `<div class="muted">${plural(swaps, "replacement")}</div>` : ""}</td>
        <td class="nowrap"><button class="btn small primary" data-reopen="${h.id}">Reopen</button>
          <button class="btn small" data-hdelete="${h.id}" title="Delete from the history">Delete</button></td></tr>`;
    }).join("")}</tbody></table></div>`
    : `<p class="muted">No BOM analysed yet: drop a file above and it will be saved here.</p>`;
  state.historyItems = items;
}

$("#historyList").addEventListener("click", async (ev) => {
  const re = ev.target.closest("[data-reopen]");
  const del = ev.target.closest("[data-hdelete]");
  if (re) {
    const h = state.historyItems.find((x) => String(x.id) === re.dataset.reopen);
    try {
      const blob = await (await api(`/api/history/${h.id}/file`)).blob();
      state.file = new File([blob], h.filename);
      state.overrides = h.mapping || {};
      state.substitutions = Object.fromEntries(Object.entries(h.substitutions || {}).map(([k, v]) => [k, Number(v)]));
      state.classifications = h.classifications || {};
      state.historyId = h.id;
      resetLineFilters();
      await runAnalysis();
      $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      $("#error").textContent = "Could not reopen this BOM: " + e.message;
      $("#error").hidden = false;
    }
  } else if (del) {
    if (!confirm("Delete this BOM from your history?")) return;
    try {
      await api(`/api/history/${del.dataset.hdelete}`, { method: "DELETE" });
      if (String(state.historyId) === del.dataset.hdelete) state.historyId = null;
      loadHistory();
    } catch (e) { alert(e.message); }
  }
});

/* ------------------------------------------------------------------ start-up */
loadStats();
loadUser();
loadAuthConfig();
api("/api/countries").then((r) => r.json()).then((c) => {
  COUNTRIES = c;
  $("#countryList").innerHTML = Object.entries(c).map(([code, name]) => `<option value="${code}">${esc(name)}</option>`).join("");
  route(); // redraw the view with the country names
}).catch(() => {});
route();

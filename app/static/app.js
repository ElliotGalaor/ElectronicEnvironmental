"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const LABELS = {
  conforme: "Conforme", exemption: "Exemption", svhc: "SVHC > 0,1 %", non_conforme: "Non conforme",
  a_verifier: "À vérifier", inconnu: "Inconnu", non_monte: "Non monté",
};
const VERDICT = {
  conforme: "Conforme", exemption: "Conforme (exemptions)", svhc: "Conforme, SVHC à déclarer",
  non_conforme: "Non conforme", a_verifier: "À vérifier",
};
const MATCH = {
  exact: ["Exacte", ""], mpn: ["Référence seule", ""], approchant: ["Approchante", ""],
  mpn_autre_fabricant: ["Autre fabricant", "warn"], partiel: ["Partielle", "warn"],
  ambigu: ["Ambiguë", "warn"], introuvable: ["Introuvable", "bad"], "-": ["—", ""],
};
const ROLES = { mpn: "Référence fabricant *", manufacturer: "Fabricant", designator: "Repères", quantity: "Quantité", description: "Description", value: "Valeur", dnp: "Non monté (DNP)" };
const NORMS = [["rohs2", "RoHS 2", "Directive 2011/65/UE"], ["rohs3", "RoHS 3", "Directive (UE) 2015/863"], ["reach", "REACH", "Règlement (CE) 1907/2006"]];

let COUNTRIES = {};
const country = (code) => code ? `<span title="${esc(COUNTRIES[code] || "")}">${esc(code)}</span>` : "";
const exHref = (code) => "#exemption/" + encodeURIComponent(code);
const exLinks = (field) => (field || "").split(/[,;/]|\s+et\s+|\s+and\s+/).map((c) => c.trim()).filter(Boolean)
  .map((c) => `<a class="ex-link" href="${exHref(c)}" title="Voir le texte officiel et la date d'expiration">${esc(c)}</a>`).join(", ");
const eccnHref = (e) => "#eccn/" + encodeURIComponent(e);
const eccnCell = (e) => !e ? "" : e === "EAR99"
  ? `<a class="eccn-link mono" href="${eccnHref(e)}" title="Voir la définition de EAR99">EAR99</a>`
  : `<a class="eccn-link" href="${eccnHref(e)}" title="Soumis au contrôle export : voir le domaine d'application et la définition"><span class="badge b-eccn">${esc(e)}</span></a>`;
const badge = (s) => `<span class="badge b-${esc(s)}">${esc(LABELS[s] || s)}</span>`;

const state = { file: null, report: null, filter: "all", overrides: {}, substitutions: {}, user: null, historyId: null, historyItems: [], page: 0, pageSize: 50, adminRequired: false };

function token() { try { return localStorage.getItem("rrc_token") || ""; } catch { return ""; } }

async function api(url, opts = {}) {
  opts.headers = { ...(opts.headers || {}), "X-Admin-Token": token() };
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r;
}

/* ------------------------------------------------------------------ onglets et navigation
   Adresses : #analyse, #base, #fabricants, #fabricant/<clé> (liens partageables, bouton Précédent) */
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  const target = "#" + t.dataset.tab;
  if (location.hash === target) route(); else location.hash = target;
}));

function showTab(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
}

function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h.startsWith("fabricant/")) {
    showTab("fabricants");
    openManufacturer(h.slice("fabricant/".length));
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
  } else if (h === "fabricants") {
    showTab("fabricants");
    $("#mfrDetail").hidden = true;
    $("#mfrList").hidden = false;
    loadManufacturers();
  } else if (h === "base") {
    showTab("base");
    loadComponents();
  } else {
    showTab("analyse");
  }
}
window.addEventListener("hashchange", route);

// Lien vers la fiche d'un fabricant ("_" = fabricant non renseigné)
const mfrHref = (key) => "#fabricant/" + encodeURIComponent(key === "" ? "_" : key);
const mfrLink = (name, key) => (key === undefined || key === null)
  ? esc(name)
  : `<a class="mfr-link" href="${mfrHref(key)}">${esc(name || "(non renseigné)")}</a>`;

async function loadStats() {
  try {
    const s = await (await api("/api/stats")).json();
    state.adminRequired = s.admin_required;
    state.aiAvailable = s.ai_available;
    $("#pdfMethodHint").textContent = s.ai_available
      ? "Analyse par IA (Claude) activée sur le serveur."
      : "Analyse par règles (aucune clé API Claude configurée sur le serveur).";
    $("#dbinfo").textContent = `Base : ${s.total} composant${s.total > 1 ? "s" : ""}`;
    $("#adminBox").hidden = !s.admin_required;
  } catch { $("#dbinfo").textContent = "Base : serveur injoignable"; }
}

/* ------------------------------------------------------------------ analyse */
const drop = $("#drop");
["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (ev) => { if (ev.dataTransfer.files[0]) startAnalysis(ev.dataTransfer.files[0]); });
$("#bomFile").addEventListener("change", (ev) => { if (ev.target.files[0]) startAnalysis(ev.target.files[0]); ev.target.value = ""; });

// Filtres du tableau des lignes : remis à zéro à chaque nouvelle BOM (un filtre resté d'une BOM
// précédente, ou rempli automatiquement par le navigateur, masquerait toutes les lignes).
function resetLineFilters() {
  $("#lineSearch").value = "";
  state.filter = "all";
  $$("#filters .chip").forEach((x) => x.classList.toggle("active", x.dataset.filter === "all"));
}

function startAnalysis(file) {
  state.file = file; state.overrides = {}; state.substitutions = {}; state.historyId = null;
  resetLineFilters();
  runAnalysis();
}

async function runAnalysis(format = "json") {
  if (!state.file) return;
  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("mapping", Object.keys(state.overrides).length ? JSON.stringify(state.overrides) : "");
  fd.append("substitutions", Object.keys(state.substitutions).length ? JSON.stringify(state.substitutions) : "");
  // Utilisateur connecté : enregistrement dans l'historique (mise à jour de l'entrée si la BOM y est déjà)
  if (format === "json" && state.user) {
    if (state.historyId) fd.append("history_id", state.historyId); else fd.append("save", "1");
  }
  $("#error").hidden = true;
  try {
    const r = await api(`/api/analyze?format=${format}`, { method: "POST", body: fd });
    if (format !== "json") {
      const blob = await r.blob();
      const a = Object.assign(document.createElement("a"), {
        href: URL.createObjectURL(blob),
        download: format === "bom"
          ? `${state.file.name.replace(/\.[^.]+$/, "")}_modifiee.csv`
          : `conformite_${state.file.name.replace(/\.[^.]+$/, "")}.${format}`,
      });
      a.click();
      URL.revokeObjectURL(a.href);
      return;
    }
    state.report = await r.json();
    if (state.report.history_id) state.historyId = state.report.history_id;
    renderReport();
    renderSwapBar();
    if (state.user) loadHistory();
  } catch (e) {
    $("#error").textContent = "Erreur : " + e.message;
    $("#error").hidden = false;
    if (format === "json") $("#result").hidden = true;
  }
}

$$("[data-export]").forEach((b) => b.addEventListener("click", () => runAnalysis(b.dataset.export)));
$("#btnMapping").addEventListener("click", () => { $("#mapping").hidden = !$("#mapping").hidden; });
$("#btnRerun").addEventListener("click", () => {
  state.overrides = {};
  $$("#mapgrid select").forEach((s) => { state.overrides[s.name] = s.value || "__none__"; });
  runAnalysis();
});

function renderReport() {
  const R = state.report, S = R.summary;
  $("#result").hidden = false;
  $("#fname").textContent = R.filename;
  $("#fmeta").textContent = `— ${S.lines} lignes, analysé avec ${R.database_size} composants en base`;

  // Colonnes
  $("#hdrRow").textContent = R.mapping.header_row;
  const opts = (sel) => `<option value="">— aucune —</option>` + R.mapping.headers.map((h) => h && `<option ${h === sel ? "selected" : ""}>${esc(h)}</option>`).join("");
  $("#mapgrid").innerHTML = Object.entries(ROLES).map(([role, label]) =>
    `<label>${label}<select name="${role}">${opts(R.mapping.columns[role])}</select></label>`).join("");

  // Verdicts
  const detail = (c) => [
    c.non_conforme && `${c.non_conforme} non conf.`, c.a_verifier && `${c.a_verifier} à vérifier`,
    c.exemption && `${c.exemption} exemption`, c.svhc && `${c.svhc} SVHC`, c.conforme && `${c.conforme} conformes`,
  ].filter(Boolean).join(" · ");
  $("#cards").innerHTML =
    `<div class="vcard global v-${S.overall}"><div class="norm">Verdict global de la BOM</div><div class="verdict">${VERDICT[S.overall]}</div>
      <div class="detail">${S.overall === "conforme" ? "Toutes les lignes sont conformes" : S.overall === "non_conforme" ? "Au moins un composant non conforme" : "Informations manquantes ou incertaines"}</div></div>` +
    NORMS.map(([k, name, ref]) => `<div class="vcard v-${S[k].verdict}"><div class="norm">${name} <span title="${ref}">· ${ref}</span></div>
      <div class="verdict">${VERDICT[S[k].verdict]}</div><div class="detail">${detail(S[k].counts)}</div></div>`).join("");

  $("#counts").innerHTML = `<span><b>${S.found}</b> trouvées en base</span><span><b>${S.uncertain}</b> correspondances incertaines</span>
    <span><b>${S.not_found}</b> introuvables</span><span><b>${S.dnp}</b> non montées</span>` +
    (S.export ? `<span>Export : <b>${S.export.eccn_controlled}</b> ECCN contrôlé${S.export.eccn_controlled > 1 ? "s" : ""}, <b>${S.export.eccn_unknown}</b> ECCN inconnu${S.export.eccn_unknown > 1 ? "s" : ""}</span>` +
      `<span>Origines : ${[
        ...Object.entries(S.export.countries).map(([c, n]) => `${country(c)} (${n})`),
        ...(S.export.country_unknown ? [`<b>${S.export.country_unknown}</b> inconnue${S.export.country_unknown > 1 ? "s" : ""}`] : []),
      ].join(", ") || "—"}</span>` : "");
  renderCharts(S);
  renderLines();
}

/* ------------------------------------------------------------------ camemberts par norme
   Répartition des statuts des lignes montées (les lignes non montées sont exclues). */
const PIE_STATUSES = [
  ["conforme", "Conforme", "--pie-ok"],
  ["exemption", "Exemption", "--pie-ex"],
  ["svhc", "SVHC", "--pie-svhc"],
  ["a_verifier", "À vérifier", "--pie-chk"],
  ["non_conforme", "Non conforme", "--pie-ko"],
];

function pieSvg(slices, total, centerValue, centerLabel) {
  const R = 58, r = 36, C = 60; // anneau : rayon extérieur 58, intérieur 36
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
  $("#charts").innerHTML = NORMS.map(([k, name, ref]) => {
    const counts = S[k].counts;
    const slices = PIE_STATUSES.filter(([key]) => counts[key]).map(([key, label, color]) => ({ key, label, color, n: counts[key] }));
    const total = slices.reduce((t, s) => t + s.n, 0);
    const skipped = counts.non_monte || 0;
    if (!total) return `<div class="card chart"><div><h4>${name}</h4><p class="muted">Aucune ligne montée.</p></div></div>`;
    const okShare = slices.filter((s) => s.key !== "a_verifier" && s.key !== "non_conforme").reduce((t, s) => t + s.n, 0);
    const pctOf = (n) => Math.round((100 * n) / total) + " %";
    const legend = slices.map((s) => `<li data-k="${s.key}"><span class="sw" style="background:var(${s.color})"></span>
      <span class="lbl">${s.label}</span><span class="val">${s.n} · ${pctOf(s.n)}</span></li>`).join("");
    const desc = slices.map((s) => `${s.label} : ${s.n}`).join(", ");
    return `<div class="card chart" data-norm="${k}" role="figure" aria-label="${name} : ${desc}">
      ${pieSvg(slices, total, pctOf(okShare), "conformes")}
      <div><h4>${name} <span>· ${total} ligne${total > 1 ? "s" : ""}</span></h4>
        <ul class="legend">${legend}</ul>
        ${skipped ? `<div class="foot">${skipped} non montée${skipped > 1 ? "s" : ""} exclue${skipped > 1 ? "s" : ""}</div>` : ""}</div>
    </div>`;
  }).join("");
}

// Survol : met en avant la part et sa ligne de légende, avec une infobulle
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
  chartTip.textContent = `${card.querySelector("h4").firstChild.textContent.trim()} · ${li.querySelector(".lbl").textContent} : ${li.querySelector(".val").textContent}`;
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

// Tableau vide alors que la BOM contient des lignes : on dit pourquoi et on propose de tout afficher
function emptyLinesRow() {
  if (!state.report.lines.length) return `<tr><td colspan="11" class="muted">La BOM ne contient aucune ligne.</td></tr>`;
  const q = $("#lineSearch").value.trim();
  const chip = $(`#filters .chip[data-filter="${state.filter}"]`);
  const why = [q ? `recherche « ${esc(q)} »` : "", state.filter !== "all" && chip ? `filtre « ${esc(chip.textContent)} »` : ""].filter(Boolean).join(" et ");
  return `<tr><td colspan="11" class="emptyrow">Aucune des ${state.report.lines.length} lignes ne correspond ${why ? `à : ${why}` : "aux filtres"}.
    <button class="btn small" data-clearfilters>Afficher toutes les lignes</button></td></tr>`;
}

function lineVisible(l) {
  const st = [l.rohs2, l.rohs3, l.reach];
  const f = state.filter;
  if (f === "issues" && !st.some((s) => ["non_conforme", "a_verifier"].includes(s))) return false;
  if (f === "ko" && !st.includes("non_conforme")) return false;
  if (f === "check" && !st.includes("a_verifier")) return false;
  if (f === "svhc" && !st.some((s) => ["svhc", "exemption"].includes(s))) return false;
  if (f === "eccn" && !(l.component && l.component.eccn && l.component.eccn !== "EAR99")) return false;
  const q = $("#lineSearch").value.trim().toLowerCase();
  if (q && ![l.bom_mpn, l.designator, l.bom_manufacturer, l.bom_description].join(" ").toLowerCase().includes(q)) return false;
  return true;
}

function renderLines() {
  const rows = state.report.lines.filter(lineVisible);
  $("#lines tbody").innerHTML = rows.map((l) => {
    const [mLabel, mCls] = MATCH[l.match] || [l.match, ""];
    const comp = l.component;
    const matchCell = `<span class="match ${mCls}">${mLabel}</span>` +
      (comp && l.match !== "exact" ? `<div class="mono muted">${esc(comp.mpn)}</div>` : "");
    const status = (k) => badge(l[k]) + (l[k + "_inferred"] ? `<span class="inferred">déduit</span>` : "");
    const sugg = l.suggestions.length && !comp
      ? `<div class="sugg">Proches : ${l.suggestions.map((s) => `<span class="mono">${esc(s.mpn)}</span>`).join(", ")}</div>` : "";
    const add = (l.match === "introuvable" && l.bom_mpn)
      ? `<div class="sugg"><button class="btn small" data-add="${l.line}">+ Ajouter à la base</button></div>` : "";
    const notes = l.notes.map((n) => n.startsWith("Exemption RoHS") && l.exemptions
      ? `<div>Exemption RoHS : ${exLinks(l.exemptions)} (voir la date d'expiration)</div>` : `<div>${esc(n)}</div>`).join("");
    const altBtn = (!l.dnp && l.bom_mpn) ? `<div class="sugg"><button class="btn small" data-altline="${l.line}">Équivalents</button></div>` : "";
    const ref = (!l.dnp && l.bom_mpn)
      ? `<a href="#" class="mpn-link" data-altline="${l.line}" title="Voir les équivalents d'autres fabricants">${esc(l.bom_mpn)}</a>`
      : esc(l.bom_mpn);
    return `<tr class="${l.dnp ? "dnp" : ""}${l.original_mpn ? " swapped" : ""}">
      <td>${l.line}</td><td class="mono">${esc(l.designator)}</td><td>${esc(l.quantity)}</td>
      <td class="mono">${ref}${l.original_mpn ? `<div class="muted small">au lieu de ${esc(l.original_mpn)}</div>` : ""}</td><td>${comp ? mfrLink(l.bom_manufacturer || comp.manufacturer, comp.manufacturer_key) : esc(l.bom_manufacturer)}</td><td>${matchCell}</td>
      <td>${status("rohs2")}</td><td>${status("rohs3")}</td><td>${status("reach")}</td>
      <td>${comp ? eccnCell(comp.eccn) + (comp.country_of_origin ? `<div class="muted">${country(comp.country_of_origin)}</div>` : "") : ""}</td>
      <td class="notes">${notes}${sugg}${add}${altBtn}</td></tr>`;
  }).join("") || emptyLinesRow();
  const total = state.report.lines.length;
  $("#lineCount").textContent = rows.length === total ? `${total} ligne${total > 1 ? "s" : ""}` : `${rows.length} ligne${rows.length > 1 ? "s" : ""} affichée${rows.length > 1 ? "s" : ""} sur ${total}`;
  $("#lineCount").classList.toggle("filtered", rows.length !== total);
}

$("#lines").addEventListener("click", (ev) => {
  if (ev.target.closest("[data-clearfilters]")) { resetLineFilters(); renderLines(); return; }
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

/* ------------------------------------------------------------------ base */
let searchTimer;
$("#q").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 0; loadComponents(); }, 250); });
$("#onlyIssues").addEventListener("change", () => { state.page = 0; loadComponents(); });
$("#prev").addEventListener("click", () => { if (state.page > 0) { state.page--; loadComponents(); } });
$("#next").addEventListener("click", () => { state.page++; loadComponents(); });
$("#adminToken").value = token();
$("#adminToken").addEventListener("change", (e) => { try { localStorage.setItem("rrc_token", e.target.value); } catch {} });

async function loadComponents() {
  const p = new URLSearchParams({ q: $("#q").value, limit: state.pageSize, offset: state.page * state.pageSize, status: $("#onlyIssues").checked ? "probleme" : "" });
  try {
    const { total, items } = await (await api("/api/components?" + p)).json();
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    if (state.page >= pages) { state.page = pages - 1; return loadComponents(); }
    $("#pageInfo").textContent = `${total} composant${total > 1 ? "s" : ""} — page ${state.page + 1} / ${pages}`;
    $("#prev").disabled = state.page === 0;
    $("#next").disabled = state.page >= pages - 1;
    $("#comps tbody").innerHTML = items.map((c) => `<tr>
      <td class="mono">${esc(c.mpn)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
      <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td>
      <td class="notes">${c.rohs_exemptions ? `<div>Exemption : ${exLinks(c.rohs_exemptions)}</div>` : ""}${c.svhc_list ? `<div>SVHC : ${esc(c.svhc_list)}</div>` : ""}</td>
      <td>${eccnCell(c.eccn)}</td><td>${country(c.country_of_origin)}</td>
      <td>${esc(c.last_checked)}</td><td><button class="btn small" data-edit="${c.id}">Modifier</button> <button class="btn small" data-alt="${c.id}">Équivalents</button></td></tr>`).join("")
      || `<tr><td colspan="11" class="muted">Aucun composant.</td></tr>`;
  } catch (e) {
    $("#comps tbody").innerHTML = `<tr><td colspan="11">Erreur : ${esc(e.message)}</td></tr>`;
  }
}

$("#comps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  if (b) openDialog(await (await api("/api/components/" + b.dataset.edit)).json());
});
$("#btnAdd").addEventListener("click", () => openDialog(null));

/* ------------------------------------------------------------------ fabricants */
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
    $("#mfrs tbody").innerHTML = `<tr><td colspan="7">Erreur : ${esc(e.message)}</td></tr>`;
  }
}
$("#mfrSearch").addEventListener("input", renderManufacturers);

function renderManufacturers() {
  const q = $("#mfrSearch").value.trim().toLowerCase();
  const rows = MFRS.filter((m) => !q || [m.name, m.key, ...m.aliases].join(" ").toLowerCase().includes(q));
  $("#mfrs tbody").innerHTML = rows.map((m) => {
    const s = m.stats;
    const rohs = s.rohs3_ko ? `<span class="badge b-non_conforme">${s.rohs3_ko} non conf.</span>`
      : s.rohs3_unknown ? `<span class="badge b-inconnu">${s.rohs3_unknown} inconnu${s.rohs3_unknown > 1 ? "s" : ""}</span>`
      : s.components ? `<span class="badge b-conforme">Conformes</span>` : "";
    const reach = s.reach_ko ? `<span class="badge b-non_conforme">${s.reach_ko} non conf.</span>`
      : s.reach_svhc ? `<span class="badge b-svhc">${s.reach_svhc} SVHC</span>` : "";
    return `<tr>
      <td>${mfrLink(m.name, m.key)}</td><td>${s.components}</td><td>${rohs}</td><td>${reach}</td>
      <td>${s.eccn_controlled ? `<span class="badge b-eccn">${s.eccn_controlled}</span>` : ""}</td>
      <td>${country(m.country)}</td><td>${siteLink(m.website)}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">Aucun fabricant.</td></tr>`;
}

let currentMfr = null;

async function openManufacturer(key) {
  $("#mfrList").hidden = true;
  $("#mfrDetail").hidden = false;
  $("#mfrName").textContent = "Chargement…";
  try {
    currentMfr = await (await api("/api/manufacturers/" + encodeURIComponent(key === "" ? "_" : key))).json();
  } catch (e) {
    currentMfr = null;
    $("#mfrName").textContent = "Fabricant introuvable";
    $("#mfrAliases").textContent = e.message;
    $("#mfrCards").innerHTML = $("#mfrInfo").innerHTML = $("#mfrComps tbody").innerHTML = "";
    $("#btnEditMfr").hidden = true;
    return;
  }
  renderManufacturer();
}

function renderManufacturer() {
  const m = currentMfr, s = m.stats;
  $("#mfrName").textContent = m.name || "(fabricant non renseigné)";
  $("#mfrAliases").textContent = m.aliases.length ? "Aussi écrit : " + m.aliases.join(", ") : "";
  $("#btnEditMfr").hidden = m.key === "";
  const plural = (n, word) => `${n} ${word}${n > 1 ? "s" : ""}`;
  $("#mfrCards").innerHTML = [
    ["Composants en base", s.components, ""],
    ["RoHS 3 conformes", `${s.rohs3_ok} / ${s.components}`, pct(s.rohs3_ok, s.components) + (s.rohs3_ko ? ` · ${plural(s.rohs3_ko, "non conforme")}` : "")],
    ["REACH : SVHC > 0,1 %", s.reach_svhc, s.reach_ko ? plural(s.reach_ko, "non conforme") : ""],
    ["ECCN contrôlés", s.eccn_controlled, s.eccn_unknown ? plural(s.eccn_unknown, "ECCN inconnu") : ""],
  ].map(([t, v, d]) => `<div class="vcard"><div class="norm">${t}</div><div class="verdict">${v}</div><div class="detail">${esc(d)}</div></div>`).join("");
  const origins = Object.entries(m.origins).map(([c, n]) => `${country(c)} (${n})`).join(", ");
  const dflt = `<span class="tag" title="Valeur connue pour ce fabricant, modifiable">par défaut</span>`;
  $("#mfrInfo").innerHTML = `
    <dt>Site web</dt><dd>${siteLink(m.website)}${m.website_is_default ? dflt : ""}</dd>
    <dt>Siège</dt><dd>${m.country ? `${esc(m.country_name)} (${esc(m.country)})` : `<span class="muted">—</span>`}${m.country_is_default ? dflt : ""}</dd>
    <dt>Pays d'origine des composants</dt><dd>${origins || `<span class="muted">non renseignés</span>`}</dd>
    <dt>Notes</dt><dd>${m.notes ? esc(m.notes) : `<span class="muted">—</span>`}</dd>`;
  $("#mfrComps tbody").innerHTML = m.components.map((c) => `<tr>
    <td class="mono">${esc(c.mpn)}</td><td>${esc(c.description)}</td>
    <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td>
    <td>${eccnCell(c.eccn)}</td><td>${country(c.country_of_origin)}</td><td>${esc(c.last_checked)}</td>
    <td><button class="btn small" data-edit="${c.id}">Modifier</button> <button class="btn small" data-alt="${c.id}">Équivalents</button></td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">Aucun composant de ce fabricant en base.</td></tr>`;
}

$("#mfrComps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  if (b) openDialog(await (await api("/api/components/" + b.dataset.edit)).json());
});

const dlgMfr = $("#dlgMfr"), mfrForm = $("#mfrForm");
$("#btnEditMfr").addEventListener("click", () => {
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

/* ------------------------------------------------------------------ modale */
const dlg = $("#dlg"), form = $("#compForm");
let editingId = null;

function openDialog(comp, preset = {}) {
  editingId = comp ? comp.id : null;
  const data = comp || { rohs2: "inconnu", rohs3: "inconnu", reach: "inconnu", last_checked: new Date().toLocaleDateString("sv-SE"), ...preset };
  for (const el of form.elements) if (el.name) el.value = data[el.name] ?? "";
  $("#dlgTitle").textContent = comp ? "Modifier le composant" : "Nouveau composant";
  $("#btnDelete").hidden = !comp;
  $("#dlgError").hidden = true;
  $("#pdfStatus").hidden = true;
  $("#mpnList").innerHTML = $("#mfrList").innerHTML = "";
  $$(".formgrid .filled").forEach((l) => l.classList.remove("filled"));
  dlg.showModal();
}

/* ------------------------------------------------------------------ datasheet PDF */
const FIELD_LABELS = {
  mpn: "Référence", manufacturer: "Fabricant", description: "Description", rohs2: "RoHS 2", rohs3: "RoHS 3",
  reach: "REACH", rohs_exemptions: "Exemptions", svhc_list: "SVHC", eccn: "ECCN", country_of_origin: "Pays d'origine",
};
const pdfDrop = $("#pdfDrop");
["dragenter", "dragover"].forEach((e) => pdfDrop.addEventListener(e, (ev) => { ev.preventDefault(); pdfDrop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => pdfDrop.addEventListener(e, (ev) => { ev.preventDefault(); pdfDrop.classList.remove("over"); }));
pdfDrop.addEventListener("drop", (ev) => { if (ev.dataTransfer.files[0]) extractPdf(ev.dataTransfer.files[0]); });
$("#pdfFile").addEventListener("change", (ev) => { if (ev.target.files[0]) extractPdf(ev.target.files[0]); ev.target.value = ""; });
$("#btnFromPdf").addEventListener("click", () => { openDialog(null); $("#pdfFile").click(); });

async function extractPdf(file) {
  const box = $("#pdfStatus");
  box.hidden = false;
  box.innerHTML = `<div class="head">Analyse de ${esc(file.name)}…</div>`;
  pdfDrop.classList.add("busy");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await (await api("/api/datasheet/extract", { method: "POST", body: fd })).json();
    applyExtraction(r, file.name);
  } catch (e) {
    box.innerHTML = `<div class="head warn">Analyse impossible : ${esc(e.message)}</div>`;
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
    // Ne remplace un champ que si la datasheet apporte une valeur (un statut "inconnu" n'écrase rien)
    if (v && !(el.tagName === "SELECT" && v === "inconnu" && el.value !== "inconnu")) {
      el.value = v;
      if (v !== "inconnu") el.closest("label").classList.add("filled");
    }
  }
  form.elements.last_checked.value = new Date().toLocaleDateString("sv-SE");
  $("#mpnList").innerHTML = (r.candidates.mpn || []).map((c) => `<option value="${esc(c.value)}">`).join("");
  $("#mfrList").innerHTML = (r.candidates.manufacturer || []).map((c) => `<option value="${esc(c.value)}">`).join("");

  const method = r.method === "ia" ? `IA (${esc(r.model || "Claude")})` : "règles automatiques";
  const others = (r.candidates.mpn || []).slice(1, 6).map((c) => esc(c.value));
  const ev = Object.entries(r.evidence || {}).filter(([, list]) => list && list.length).map(([k, list]) =>
    `<div class="ev"><b>${FIELD_LABELS[k] || k}</b> : ${list.map((e) => `<q>${esc(e.text)}</q>${e.page ? ` (p. ${e.page})` : ""}`).join(" · ")}</div>`).join("");
  const items = [
    ...r.warnings.map((w) => `<li class="warn">${esc(w)}</li>`),
    ...r.notes.map((n) => `<li>${esc(n)}</li>`),
    others.length ? `<li>Autres références trouvées : ${others.join(", ")} (liste proposée dans le champ Référence)</li>` : "",
  ].join("");
  $("#pdfStatus").innerHTML =
    `<div class="head">${esc(fname)} analysé par ${method} — ${r.pages} page${r.pages > 1 ? "s" : ""}. Champs proposés surlignés.</div>` +
    (r.existing_id && !editingId ? `<div class="warn">Ce composant existe déjà en base. <button type="button" class="btn small" id="openExisting">Ouvrir la fiche existante</button></div>` : "") +
    (items ? `<ul>${items}</ul>` : "") +
    (ev ? `<details><summary>Extraits de la datasheet utilisés</summary>${ev}</details>` : "");
  const btn = $("#openExisting");
  if (btn) btn.addEventListener("click", async () => {
    const comp = await (await api("/api/components/" + r.existing_id)).json();
    openDialog(comp);
    // Réapplique la proposition sur la fiche existante pour comparaison
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
  if (!editingId || !confirm("Supprimer définitivement ce composant de la base ?")) return;
  try { await api(`/api/components/${editingId}`, { method: "DELETE" }); dlg.close(); afterDbChange(); }
  catch (e) { $("#dlgError").textContent = e.message; $("#dlgError").hidden = false; }
});

function afterDbChange() {
  loadStats();
  if ($("#tab-base").classList.contains("active")) loadComponents();
  if ($("#tab-fabricants").classList.contains("active")) route();
  if (state.report) runAnalysis(); // relance l'analyse avec la base à jour
}

/* ------------------------------------------------------------------ import */
$("#btnImport").addEventListener("click", () => $("#importFile").click());
$("#importFile").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const box = $("#importMsg");
  try {
    const r = await (await api("/api/components/import", { method: "POST", body: fd })).json();
    box.className = "alert " + (r.error_count ? "" : "success");
    box.innerHTML = `Import de <b>${esc(f.name)}</b> : ${r.created} créés, ${r.updated} mis à jour, ${r.error_count} erreurs.` +
      (r.errors.length ? "<br>" + r.errors.slice(0, 10).map(esc).join("<br>") : "");
    afterDbChange();
  } catch (e) { box.className = "alert error"; box.textContent = "Import impossible : " + e.message; }
  box.hidden = false;
});

/* ------------------------------------------------------------------ équivalents d'autres fabricants
   Ouvert depuis une ligne de BOM (remplacement possible) ou depuis un composant de la base. */
const dlgAlt = $("#dlgAlt");
let altCtx = null; // { line?: <ligne de BOM>, compId?: <id composant> }

async function openAlternatives(ctx) {
  altCtx = ctx;
  $("#altBody").innerHTML = `<p class="muted">Recherche d'équivalents…</p>`;
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
    $("#altBody").innerHTML = `<div class="alert error">Erreur : ${esc(e.message)}</div>`;
  }
}

function renderAlternatives(data, compId) {
  const ref = data.reference, attr = data.attributes, l = altCtx.line;
  const status = ref.rohs2 ? `${badge(ref.rohs2)} ${badge(ref.rohs3)} ${badge(ref.reach)}` : `<span class="muted">absent de la base</span>`;
  const attrs = [attr.category_label, ...Object.values(attr.values).flat(), attr.package, attr.dielectric,
    attr.tolerance != null ? attr.tolerance + " %" : "", attr.channel ? "canal " + attr.channel : ""].filter(Boolean);
  $("#altRef").innerHTML = `
    <div class="altref-main"><span class="mono">${esc(ref.mpn)}</span> · ${esc(ref.manufacturer || "fabricant non renseigné")}</div>
    <div class="muted">${esc(ref.description || "")}</div>
    <div class="altref-status">RoHS 2 / RoHS 3 / REACH : ${status}</div>
    <div class="muted small">Caractéristiques reconnues : ${attrs.length ? attrs.map(esc).join(" · ") : "aucune (description trop vague)"}</div>
    ${l && l.original_mpn ? `<div class="swapnote">Cette ligne remplace <span class="mono">${esc(l.original_mpn)}</span>.
      <button type="button" class="btn small" id="altRevert">Revenir à la référence d'origine</button></div>` : ""}`;
  const rev = $("#altRevert");
  if (rev) rev.addEventListener("click", () => { delete state.substitutions[l.line]; dlgAlt.close(); runAnalysis(); });

  const alts = data.alternatives;
  if (!alts.length) {
    $("#altBody").innerHTML = `<p class="muted">Aucun équivalent d'un autre fabricant trouvé dans la base.
      ${attr.category ? "" : "La description ne permet pas d'identifier le type de composant."}
      ${["microcontrôleur", "convertisseur DC-DC", "transceiver"].includes(attr.category_label) ? "Pour ce type de composant, seuls les équivalents confirmés sont proposés." : ""}</p>`;
    return;
  }
  $("#altBody").innerHTML = `<div class="tablewrap"><table class="grid" id="altTable"><thead><tr>
      <th>Référence</th><th>Fabricant</th><th>RoHS 2</th><th>RoHS 3</th><th>REACH</th><th>ECCN</th><th>Similarité</th><th></th>
    </tr></thead><tbody>${alts.map((a) => {
      const c = a.component;
      const sim = (a.confirmed ? `<span class="badge b-conforme">Équivalent confirmé</span>` : `<span class="score">${Math.min(100, a.score)} / 100</span>`) +
        (a.matches.length ? `<div class="small ok">✓ ${a.matches.map(esc).join(" · ")}</div>` : "") +
        (a.differences.length ? `<div class="small warn">⚠ ${a.differences.map(esc).join(" · ")}</div>` : "");
      const use = l ? `<button type="button" class="btn small primary" data-use="${c.id}">Utiliser dans la BOM</button>` : "";
      const conf = compId ? (a.confirmed
        ? `<button type="button" class="btn small" data-unconfirm="${c.id}">Retirer l'équivalence</button>`
        : `<button type="button" class="btn small" data-confirm="${c.id}">Confirmer l'équivalence</button>`) : "";
      return `<tr class="${a.compliant ? "" : "notcompliant"}">
        <td class="mono">${esc(c.mpn)}<div class="muted small nomono">${esc(c.description)}</div></td>
        <td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td>
        <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td><td>${eccnCell(c.eccn)}</td>
        <td class="simcell">${sim}</td><td class="actions-cell">${use}${conf}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

$("#altBody").addEventListener("click", async (ev) => {
  const use = ev.target.closest("[data-use]");
  const confirmBtn = ev.target.closest("[data-confirm]");
  const unconfirm = ev.target.closest("[data-unconfirm]");
  if (ev.target.closest("a.mfr-link, a.eccn-link, a.ex-link")) { dlgAlt.close(); return; }
  if (use && altCtx.line) {
    state.substitutions[altCtx.line.line] = Number(use.dataset.use);
    dlgAlt.close();
    runAnalysis();
    return;
  }
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

// Bandeau des remplacements dans la BOM
function renderSwapBar() {
  const n = Object.keys(state.substitutions).length;
  $("#swapBar").hidden = n === 0;
  $("#swapCount").textContent = `${n} ligne${n > 1 ? "s" : ""} remplacée${n > 1 ? "s" : ""} par un équivalent`;
}
$("#btnSwapReset").addEventListener("click", () => { state.substitutions = {}; runAnalysis(); });
$("#btnSwapExport").addEventListener("click", () => runAnalysis("bom"));

// Boutons « Équivalents » de l'onglet Base et des fiches fabricant
for (const sel of ["#comps", "#mfrComps"]) {
  $(sel).addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-alt]");
    if (b) openAlternatives({ compId: Number(b.dataset.alt) });
  });
}

/* ------------------------------------------------------------------ codes ECCN */
let ECCNS = { codes: [], unknown_components: 0 };
let currentEccn = null;

async function loadEccnList() {
  try {
    ECCNS = await (await api("/api/eccn")).json();
    renderEccnList();
  } catch (e) {
    $("#eccns tbody").innerHTML = `<tr><td colspan="5">Erreur : ${esc(e.message)}</td></tr>`;
  }
}
$("#eccnSearch").addEventListener("input", renderEccnList);

function renderEccnList() {
  const q = $("#eccnSearch").value.trim().toLowerCase();
  const rows = ECCNS.codes.filter((e) => !q || [e.code, e.summary, e.official_heading, e.title, ...e.manufacturers].join(" ").toLowerCase().includes(q));
  $("#eccns tbody").innerHTML = rows.map((e) => `<tr>
      <td>${eccnCell(e.code)}</td>
      <td>${esc(e.title || e.summary)}${e.official_heading ? `<div class="muted small">${esc(e.official_heading)}</div>` : ""}</td>
      <td>${e.controlled ? `<span class="badge b-eccn">Contrôlé</span>` : `<span class="badge b-conforme">Non listé</span>`}</td>
      <td>${e.components}</td>
      <td class="small">${e.manufacturers.map(esc).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">Aucun code ECCN en base.</td></tr>`;
  const u = ECCNS.unknown_components;
  $("#eccnUnknown").textContent = u ? `${u} composant${u > 1 ? "s" : ""} sans ECCN renseigné.` : "";
}

async function openEccn(code) {
  $("#eccnList").hidden = true;
  $("#eccnDetail").hidden = false;
  $("#eccnCode").textContent = code;
  $("#eccnSummary").textContent = "Chargement…";
  try {
    currentEccn = await (await api("/api/eccn/" + encodeURIComponent(code))).json();
  } catch (e) {
    currentEccn = null;
    $("#eccnSummary").textContent = "Code introuvable : " + e.message;
    $("#eccnParts").innerHTML = $("#eccnInfo").innerHTML = $("#eccnComps tbody").innerHTML = "";
    $("#btnEditEccn").hidden = true;
    return;
  }
  renderEccn();
}

function renderEccn() {
  const e = currentEccn;
  $("#btnEditEccn").hidden = false;
  $("#eccnCode").innerHTML = `${esc(e.code)} ${e.controlled ? `<span class="badge b-svhc">Soumis au contrôle export</span>` : `<span class="badge b-conforme">Non listé (EAR99)</span>`}`;
  $("#eccnSummary").textContent = e.title || e.summary;
  $("#eccnParts").innerHTML = e.parts.length ? e.parts.map((p) => `<div class="vcard part">
      <div class="norm">${esc(p.label)}</div><div class="partcode mono">${esc(p.code)}</div>
      <div class="partname">${esc(p.name)}</div><div class="detail">${esc(p.description)}</div></div>`).join("") : "";
  const links = e.links.map((l) => `<div><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)} ↗</a>${l.hint ? `<div class="muted small">${esc(l.hint)}</div>` : ""}</div>`).join("");
  const mfrs = Object.entries(e.manufacturers).map(([m, n]) => `${esc(m)} (${n})`).join(", ");
  $("#eccnInfo").innerHTML = `
    ${e.official_heading ? `<dt>Intitulé officiel de l'entrée ${esc(e.entry)}</dt><dd><q>${esc(e.official_heading)}</q><div class="muted small">Texte de la Commerce Control List (eCFR${e.as_of ? `, version du ${esc(e.as_of)}` : ""}).</div></dd>` : ""}
    <dt>Domaine d'application</dt><dd>${esc(e.explanation)}</dd>
    ${e.control_scope ? `<dt>Portée du contrôle</dt><dd>${esc(e.control_scope)}</dd>` : ""}
    <dt>Définition officielle</dt><dd>${links}</dd>
    ${e.related_codes.length ? `<dt>Codes voisins en base</dt><dd>${e.related_codes.map((c) => eccnCell(c)).join(" ")}</dd>` : ""}
    <dt>Fabricants</dt><dd>${mfrs || `<span class="muted">—</span>`}</dd>
    <dt>Notes</dt><dd>${e.notes ? esc(e.notes) : `<span class="muted">—</span>`}</dd>`;
  $("#eccnComps tbody").innerHTML = e.components.map((c) => `<tr>
    <td class="mono">${esc(c.mpn)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
    <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td><td>${country(c.country_of_origin)}</td>
    <td><button class="btn small" data-edit="${c.id}">Modifier</button></td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">Aucun composant de la base n'a ce code.</td></tr>`;
}

$("#eccnComps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  if (b) openDialog(await (await api("/api/components/" + b.dataset.edit)).json());
});

const dlgEccn = $("#dlgEccn"), eccnForm = $("#eccnForm");
$("#btnEditEccn").addEventListener("click", () => {
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

/* ------------------------------------------------------------------ exemptions RoHS */
let EXEMPTIONS = { exemptions: [] };
let currentExemption = null;
const STATUS_BADGE = { en_vigueur: "b-conforme", bientot: "b-svhc", depassee: "b-non_conforme", sans_date: "b-ex-none", inconnue: "b-inconnu" };
const statusBadge = (s) => `<span class="badge ${STATUS_BADGE[s.kind] || "b-inconnu"}">${esc(s.label)}</span>`;

async function loadExemptionList() {
  try {
    EXEMPTIONS = await (await api("/api/exemptions")).json();
    renderExemptionList();
  } catch (e) {
    $("#exempts tbody").innerHTML = `<tr><td colspan="6">Erreur : ${esc(e.message)}</td></tr>`;
  }
}
$("#exemptSearch").addEventListener("input", renderExemptionList);

function renderExemptionList() {
  const q = $("#exemptSearch").value.trim().toLowerCase();
  const rows = EXEMPTIONS.exemptions.filter((e) => !q || [e.code, e.key, e.description, e.parent, ...e.substances, ...e.manufacturers].join(" ").toLowerCase().includes(q));
  $("#exempts tbody").innerHTML = rows.map((e) => `<tr>
      <td class="nowrap"><a class="ex-link" href="${exHref(e.key)}">${esc(e.found ? e.code : e.key)}</a>
        ${e.annex ? `<div class="muted small">Annexe ${esc(e.annex)}</div>` : ""}</td>
      <td>${e.found ? esc(((e.parent ? e.parent + " " : "") + e.description).slice(0, 220)) + (((e.parent || "") + e.description).length > 220 ? "…" : "")
        : `<span class="muted">Code absent des annexes III et IV : vérifiez l'écriture du code.</span>`}</td>
      <td class="small">${e.substances.map(esc).join(", ")}</td>
      <td>${statusBadge(e.status)}</td>
      <td>${e.components}</td>
      <td class="small">${e.manufacturers.map(esc).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">Aucune exemption utilisée par les composants de la base.</td></tr>`;
  const up = EXEMPTIONS.upcoming;
  $("#exemptSource").innerHTML = EXEMPTIONS.version
    ? `Texte officiel : directive 2011/65/UE consolidée au ${esc(EXEMPTIONS.version)}
       (<a href="${esc(EXEMPTIONS.source_url)}" target="_blank" rel="noopener noreferrer">EUR-Lex ↗</a>).
       ${up ? `Version du ${esc(up.version)} déjà publiée : ${up.changes ? `${up.changes} exemption(s) modifiée(s)` : "aucun changement dans les annexes III et IV"}.` : ""}`
    : "";
}

async function openExemption(code) {
  $("#exemptList").hidden = true;
  $("#exemptDetail").hidden = false;
  $("#exemptCode").textContent = code;
  $("#exemptSummary").textContent = "Chargement…";
  try {
    currentExemption = await (await api("/api/exemptions/" + encodeURIComponent(code))).json();
  } catch (e) {
    currentExemption = null;
    $("#exemptSummary").textContent = "Erreur : " + e.message;
    return;
  }
  renderExemption();
}

function renderExemption() {
  const e = currentExemption;
  $("#exemptCode").innerHTML = `${e.found ? `Exemption ${esc(e.code)}` : esc(e.input)} ${statusBadge(e.status)}`;
  $("#exemptSummary").textContent = e.found ? e.annex_label : "Ce code ne correspond à aucune exemption des annexes III et IV.";
  const datesList = (e.dates || []).map((d) => {
    const past = d < new Date().toISOString().slice(0, 10);
    return `<span class="badge ${past ? "b-non_conforme" : "b-conforme"}">${new Date(d + "T00:00:00").toLocaleDateString("fr-FR", { dateStyle: "long" })}</span>`;
  }).join(" ");
  const links = e.links.map((l) => `<div><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)} ↗</a>${l.hint ? `<div class="muted small">${esc(l.hint)}</div>` : ""}</div>`).join("");
  const up = e.upcoming;
  const mfrs = Object.entries(e.manufacturers).map(([m, n]) => `${esc(m)} (${n})`).join(", ");
  $("#exemptInfo").innerHTML = e.found ? `
    ${e.group ? `<dt>Rubrique</dt><dd>${esc(e.group)}</dd>` : ""}
    <dt>Texte officiel</dt><dd>${e.parent ? `<div class="muted">${esc(e.parent)}</div>` : ""}<q>${esc(e.description)}</q></dd>
    <dt>Champ d'application et dates</dt><dd>${e.scope ? esc(e.scope) : `<span class="muted">Aucune précision (s'applique sans date d'expiration indiquée)</span>`}</dd>
    ${datesList ? `<dt>Dates citées</dt><dd>${datesList}</dd>` : ""}
    <dt>Substances concernées</dt><dd>${e.substances.length ? e.substances.map(esc).join(", ") : "—"}</dd>
    <dt>Renouvellement</dt><dd>${esc(e.renewal_note)}</dd>
    ${up ? `<dt>Version à venir</dt><dd>Version consolidée du ${esc(up.version)} : ${up.type === "aucun" ? "pas de changement pour cette exemption." : `exemption ${esc(up.type)}${up.scope ? ` — ${esc(up.scope)}` : ""}.`} <a href="${esc(up.url)}" target="_blank" rel="noopener noreferrer">Voir ↗</a></dd>` : ""}
    <dt>Source officielle</dt><dd>${links}</dd>
    <dt>Fabricants</dt><dd>${mfrs || `<span class="muted">—</span>`}</dd>
    <dt>Notes</dt><dd>${e.notes ? esc(e.notes) : `<span class="muted">—</span>`}</dd>`
    : `<dt>Code saisi</dt><dd>${esc(e.input)}</dd><dt>Source officielle</dt><dd>${links}</dd>`;
  $("#exemptComps tbody").innerHTML = e.components.map((c) => `<tr>
    <td class="mono">${esc(c.mpn)}</td><td>${mfrLink(c.manufacturer, c.manufacturer_key)}</td><td>${esc(c.description)}</td>
    <td>${badge(c.rohs2)}</td><td>${badge(c.rohs3)}</td><td>${badge(c.reach)}</td>
    <td><button class="btn small" data-edit="${c.id}">Modifier</button> <button class="btn small" data-alt="${c.id}">Équivalents</button></td></tr>`).join("")
    || `<tr><td colspan="7" class="muted">Aucun composant de la base n'utilise cette exemption.</td></tr>`;
}

$("#exemptComps").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-edit]");
  const a = ev.target.closest("[data-alt]");
  if (b) openDialog(await (await api("/api/components/" + b.dataset.edit)).json());
  else if (a) openAlternatives({ compId: Number(a.dataset.alt) });
});

const dlgExempt = $("#dlgExempt"), exemptForm = $("#exemptForm");
$("#btnEditExempt").addEventListener("click", () => {
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

/* ------------------------------------------------------------------ comptes et historique des BOM */
const dlgAuth = $("#dlgAuth"), authForm = $("#authForm");
let authMode = "login";

function fmtDate(iso) {
  try { return new Date(iso).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }); } catch { return iso; }
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
       <button class="btn small" id="btnMyBoms">Mes BOM</button>
       <button class="btn small" id="btnLogout">Se déconnecter</button>`
    : `<button class="btn small primary" id="btnLogin">Se connecter</button>`;
  $("#historyHint").hidden = !!u;
  $("#historyBox").hidden = !u;
}

$("#account").addEventListener("click", async (ev) => {
  if (ev.target.closest("#btnLogin")) openAuth("login");
  else if (ev.target.closest("#btnMyBoms")) {
    location.hash = "#analyse";
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

function openAuth(mode) {
  setAuthMode(mode);
  authForm.reset();
  $("#authError").hidden = true;
  dlgAuth.showModal();
  authForm.elements.email.focus();
}

function setAuthMode(mode) {
  authMode = mode;
  $$("#dlgAuth .authtab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
  const signup = mode === "register";
  $("#authNameRow").hidden = !signup;
  authForm.elements.password.autocomplete = signup ? "new-password" : "current-password";
  $("#authPwHint").hidden = !signup;
  $("#authSubmit").textContent = signup ? "Créer mon compte" : "Se connecter";
  $("#authError").hidden = true;
}
$$("#dlgAuth .authtab").forEach((t) => t.addEventListener("click", () => setAuthMode(t.dataset.mode)));
$("#btnAuthCancel").addEventListener("click", () => dlgAuth.close());

authForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { email: authForm.elements.email.value, password: authForm.elements.password.value };
  if (authMode === "register") body.name = authForm.elements.name.value;
  $("#authSubmit").disabled = true;
  try {
    state.user = await (await api(authMode === "register" ? "/api/auth/register" : "/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json();
    dlgAuth.close();
    renderAccount();
    // Une BOM déjà ouverte est enregistrée dans l'historique dès la connexion
    if (state.report && state.file) runAnalysis(); else loadHistory();
  } catch (e) {
    $("#authError").textContent = e.message;
    $("#authError").hidden = false;
  } finally {
    $("#authSubmit").disabled = false;
  }
});

const VERDICT_SHORT = { conforme: "Conforme", exemption: "Exemption", svhc: "SVHC", non_conforme: "Non conforme", a_verifier: "À vérifier" };
const verdictBadge = (v) => `<span class="badge b-${esc(v)}">${esc(VERDICT_SHORT[v] || v)}</span>`;

async function loadHistory() {
  if (!state.user) return;
  let items;
  try { items = await (await api("/api/history")).json(); } catch (e) {
    $("#historyList").innerHTML = `<p class="muted">Historique indisponible : ${esc(e.message)}</p>`;
    return;
  }
  $("#historyCount").textContent = items.length ? `${items.length} analyse${items.length > 1 ? "s" : ""}` : "";
  $("#historyList").innerHTML = items.length ? `<div class="tablewrap"><table class="grid" id="historyTable"><thead><tr>
      <th>Fichier</th><th>Analysée le</th><th>Global</th><th>RoHS 2</th><th>RoHS 3</th><th>REACH</th><th>Lignes</th><th></th>
    </tr></thead><tbody>${items.map((h) => {
      const s = h.summary || {};
      const swaps = Object.keys(h.substitutions || {}).length;
      return `<tr class="${h.id === state.historyId ? "current" : ""}">
        <td><span class="mono">${esc(h.filename)}</span>${h.id === state.historyId ? ` <span class="tag">ouverte</span>` : ""}</td>
        <td class="nowrap">${esc(fmtDate(h.updated_at))}</td>
        <td>${s.overall ? verdictBadge(s.overall) : ""}</td><td>${s.rohs2 ? verdictBadge(s.rohs2) : ""}</td>
        <td>${s.rohs3 ? verdictBadge(s.rohs3) : ""}</td><td>${s.reach ? verdictBadge(s.reach) : ""}</td>
        <td class="small">${s.lines ?? ""}${swaps ? `<div class="muted">${swaps} remplacement${swaps > 1 ? "s" : ""}</div>` : ""}</td>
        <td class="nowrap"><button class="btn small primary" data-reopen="${h.id}">Rouvrir</button>
          <button class="btn small" data-hdelete="${h.id}" title="Supprimer de l'historique">Supprimer</button></td></tr>`;
    }).join("")}</tbody></table></div>`
    : `<p class="muted">Aucune BOM analysée pour l'instant : déposez un fichier ci-dessus, il sera enregistré ici.</p>`;
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
      state.historyId = h.id;
      resetLineFilters();
      await runAnalysis();
      $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      $("#error").textContent = "Impossible de rouvrir cette BOM : " + e.message;
      $("#error").hidden = false;
    }
  } else if (del) {
    if (!confirm("Supprimer cette BOM de votre historique ?")) return;
    try {
      await api(`/api/history/${del.dataset.hdelete}`, { method: "DELETE" });
      if (String(state.historyId) === del.dataset.hdelete) state.historyId = null;
      loadHistory();
    } catch (e) { alert(e.message); }
  }
});

/* ------------------------------------------------------------------ démarrage */
loadStats();
loadUser();
api("/api/countries").then((r) => r.json()).then((c) => {
  COUNTRIES = c;
  $("#countryList").innerHTML = Object.entries(c).map(([code, name]) => `<option value="${code}">${esc(name)}</option>`).join("");
  route(); // ré-affiche la vue avec les noms de pays
}).catch(() => {});
route();

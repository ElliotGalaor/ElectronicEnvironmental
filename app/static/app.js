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
const eccnCell = (e) => !e ? "" : e === "EAR99" ? `<span class="mono">EAR99</span>` : `<span class="badge b-eccn" title="Soumis au contrôle export">${esc(e)}</span>`;
const badge = (s) => `<span class="badge b-${esc(s)}">${esc(LABELS[s] || s)}</span>`;

const state = { file: null, report: null, filter: "all", overrides: {}, substitutions: {}, page: 0, pageSize: 50, adminRequired: false };

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

function startAnalysis(file) { state.file = file; state.overrides = {}; state.substitutions = {}; runAnalysis(); }

async function runAnalysis(format = "json") {
  if (!state.file) return;
  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("mapping", Object.keys(state.overrides).length ? JSON.stringify(state.overrides) : "");
  fd.append("substitutions", Object.keys(state.substitutions).length ? JSON.stringify(state.substitutions) : "");
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
    renderReport();
    renderSwapBar();
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
  renderLines();
}

$$("#filters .chip").forEach((c) => c.addEventListener("click", () => {
  state.filter = c.dataset.filter;
  $$("#filters .chip").forEach((x) => x.classList.toggle("active", x === c));
  renderLines();
}));
$("#lineSearch").addEventListener("input", renderLines);

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
    const notes = l.notes.map((n) => `<div>${esc(n)}</div>`).join("");
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
  }).join("") || `<tr><td colspan="11" class="muted">Aucune ligne pour ce filtre.</td></tr>`;
}

$("#lines").addEventListener("click", (ev) => {
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
      <td class="notes">${c.rohs_exemptions ? `<div>Exemption : ${esc(c.rohs_exemptions)}</div>` : ""}${c.svhc_list ? `<div>SVHC : ${esc(c.svhc_list)}</div>` : ""}</td>
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
  if (ev.target.closest("a.mfr-link")) { dlgAlt.close(); return; }
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

/* ------------------------------------------------------------------ démarrage */
loadStats();
api("/api/countries").then((r) => r.json()).then((c) => {
  COUNTRIES = c;
  $("#countryList").innerHTML = Object.entries(c).map(([code, name]) => `<option value="${code}">${esc(name)}</option>`).join("");
  route(); // ré-affiche la vue avec les noms de pays
}).catch(() => {});
route();

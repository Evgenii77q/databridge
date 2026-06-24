/* ── helpers ─────────────────────────────────────────────────────────────────── */
const $  = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");

let apiBase = "";

function normalizeBase(url) {
  return String(url || "").trim().replace(/\/$/, "");
}

function apiCandidates() {
  const list = [];
  if (location.origin.startsWith("http")) list.push(location.origin);
  list.push("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:3737", "http://localhost:3737");
  return [...new Set(list.map(normalizeBase).filter(Boolean))];
}

function getApiBase() {
  const manual = normalizeBase($("backendUrl")?.value);
  if (manual) return manual;
  if (apiBase) return apiBase;
  if (location.origin.startsWith("http")) return normalizeBase(location.origin);
  return "http://127.0.0.1:8000";
}

async function probeHealth(base, timeoutMs = 2500) {
  const ctrl = new AbortController();
  const tm = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${normalizeBase(base)}/health`, { signal: ctrl.signal });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(tm);
  }
}

async function ensureApiBase() {
  const manual = normalizeBase($("backendUrl")?.value);
  if (manual) {
    apiBase = manual;
    return apiBase;
  }
  if (apiBase) return apiBase;
  const candidates = apiCandidates();
  for (const c of candidates) {
    if (await probeHealth(c)) {
      apiBase = c;
      if ($("backendUrl")) $("backendUrl").value = c;
      return apiBase;
    }
  }
  apiBase = candidates[0] || "http://127.0.0.1:8000";
  if ($("backendUrl")) $("backendUrl").value = apiBase;
  return apiBase;
}

async function readApiError(resp) {
  let txt = await resp.text().catch(() => "");
  if (!txt) return `Server ${resp.status}`;
  try {
    const obj = JSON.parse(txt);
    if (typeof obj?.detail === "string") return obj.detail;
    if (obj?.detail) return JSON.stringify(obj.detail);
    return JSON.stringify(obj);
  } catch {
    return txt.length > 1000 ? txt.slice(0, 1000) + "…" : txt;
  }
}

/* ── toast ───────────────────────────────────────────────────────────────────── */
const ICONS = { success:"✓", error:"✕", warning:"⚠", info:"ℹ" };
function toast(msg, type="info", ms=4500) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="toast-icon">${ICONS[type]||ICONS.info}</span><span class="toast-msg">${esc(msg)}</span><button class="toast-close">✕</button>`;
  el.querySelector(".toast-close").onclick = () => dismiss(el);
  $("toastContainer").appendChild(el);
  if (ms > 0) setTimeout(() => dismiss(el), ms);
}
function dismiss(el) {
  if (!el || el._gone) return; el._gone = true;
  el.classList.add("fade-out");
  setTimeout(() => el.remove(), 350);
}

/* ── connection status ───────────────────────────────────────────────────────── */
function setConn(state, label) {
  const p = $("statusPill"), d = $("statusDot");
  p.className = `conn-badge ${state}`;
  d.className = `conn-dot${state==="checking"?" pulse":""}`;
  $("statusText").textContent = label;
}

async function checkHealth(silent=false) {
  setConn("checking","Проверка…");
  try {
    const base = await ensureApiBase();
    const ok = await probeHealth(base, 4000);
    if (ok) {
      setConn("ok","Подключено");
      if(!silent) toast(`Backend доступен (${base})`,"success",3000);
    } else {
      setConn("error","Нет соединения");
      if(!silent) toast(`Backend недоступен (${base})`,"error");
    }
  } catch {
    setConn("error","Нет соединения");
    if(!silent) toast("Backend недоступен. Проверь URL и запуск сервера.","error");
  }
}

$("checkConn").addEventListener("click", () => checkHealth(false));

/* ── tab switching ───────────────────────────────────────────────────────────── */
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const id = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    $(`tab-${id}`)?.classList.add("active");
    if (id === "schema")       loadSchema();
    if (id === "migration")    loadHistory();
    if (id === "integrations") loadConnectors();
  });
});

/* ── state ───────────────────────────────────────────────────────────────────── */
let currentFile   = null;
let currentFileId = null;
let currentSheet  = "";
let mappingRows   = [];   // [{excel, db, matches}]
let migPoller     = null;

/* ── step state helpers ──────────────────────────────────────────────────────── */
const LABELS = { idle:"Ожидание", running:"Выполняется", success:"Готово", error:"Ошибка" };
function setStep(cardId, chipId, state, label) {
  const card = $(cardId), chip = $(chipId);
  card.className = `card${state!=="idle" ? ` state-${state}` : ""}`;
  chip.textContent = label ?? LABELS[state] ?? state;
}

/* ── progress bar ────────────────────────────────────────────────────────────── */
function setProgress(pct) {
  $("step1Bar").classList.remove("indeterminate");
  $("step1Bar").style.width = `${Math.max(0,Math.min(100,pct))}%`;
}
function startProgress() { $("step1Bar").classList.add("indeterminate"); $("step1Bar").style.width=""; }
function stopProgress(ok) { $("step1Bar").classList.remove("indeterminate"); $("step1Bar").style.width = ok?"100%":"0%"; }

/* ── confidence badge ────────────────────────────────────────────────────────── */
function confBadge(n) {
  n = parseInt(n,10)||0;
  let cls,lbl,fill,pct;
  if (n>=7)     {cls="high";  lbl="Высокое";fill="#10b981";pct=Math.min(n*10,100);}
  else if(n>=4) {cls="medium";lbl="Среднее";fill="#f59e0b";pct=n*10;}
  else if(n>0)  {cls="low";   lbl="Низкое"; fill="#ef4444";pct=n*10;}
  else          {cls="none";  lbl="—";      fill="#4e5669";pct=0;}
  return `<span class="conf-badge ${cls}"><span class="conf-mini"><span class="conf-mini-f" style="width:${pct}%;background:${fill}"></span></span>${lbl}</span>`;
}

/* ── reset ───────────────────────────────────────────────────────────────────── */
function resetOutputs() {
  mappingRows = [];
  $("mappingBody").innerHTML = '<tr><td colspan="5" class="empty-cell">Нет данных — запустите шаг 01.</td></tr>';
  $("tableName").textContent = "—";
  $("tableMeta").textContent = "Ожидание шага 01";
  $("dbMeta").textContent    = "— / —";
  $("downloadCsv").disabled  = true;
  $("downloadJson").disabled = true;
  $("downloadCsv").dataset.url  = "";
  $("downloadJson").dataset.url = "";
  $("runStep2").disabled = true;
  $("step2Log").textContent = "Шаг 2 не запущен.";
  $("applyOverride").style.display = "none";
  currentFileId = null; currentSheet = "";
  $("sheetSelect").innerHTML = '<option value="">Выберите лист</option>';
  $("sheetSelect").disabled  = true;
  stopProgress(false);
  setStep("step1","step1StateTag","idle");
  setStep("step2card","step2StateTag","idle");
  setStep("step3card","step3StateTag","idle");
  $("step1Log").textContent = "Нет вывода.";
  $("offlineBanner").style.display = "none";
}

/* ── file pick ───────────────────────────────────────────────────────────────── */
function handleFile(file) {
  if (!file) return;
  currentFile = file;
  const pill = $("fileMetaPill");
  $("fileName").textContent = file.name;
  pill.style.display = "flex";
  resetOutputs();
  $("runStep1").disabled = false;
  setStep("step1","step1StateTag","idle","Готов к запуску");
  $("step1Log").textContent = "Файл выбран. Нажмите «Запустить сканирование».";
  loadSheets();
}

$("excelInput").addEventListener("change", e => handleFile(e.target.files[0]));

const dz = $("dropZone");
dz.addEventListener("dragover",  e => { e.preventDefault(); dz.classList.add("drag-over"); });
dz.addEventListener("dragleave", ()  => dz.classList.remove("drag-over"));
dz.addEventListener("drop",      e  => {
  e.preventDefault(); dz.classList.remove("drag-over");
  const f = e.dataTransfer?.files?.[0];
  if (f && /\.(xlsx|xls)$/i.test(f.name)) handleFile(f);
  else toast("Только .xlsx / .xls файлы","warning");
});

/* ── load sheets ─────────────────────────────────────────────────────────────── */
async function loadSheets() {
  const sel = $("sheetSelect");
  sel.innerHTML = '<option value="">Загрузка…</option>';
  sel.disabled  = true;
  try {
    const api  = await ensureApiBase();
    const form = new FormData(); form.append("file", currentFile);
    const up   = await fetch(`${api}/api/upload`, {method:"POST", body:form});
    if (!up.ok) throw new Error(await readApiError(up));
    const ud   = await up.json();
    currentFileId = ud.file_id;

    const sr = await fetch(`${api}/api/sheets?file_id=${currentFileId}`);
    if (!sr.ok) throw new Error(await readApiError(sr));
    const sd = await sr.json();
    const sheets = sd.sheets || [];

    sel.innerHTML = '<option value="">Все листы</option>';
    sheets.forEach(n => { const o=document.createElement("option"); o.value=n; o.textContent=n; sel.appendChild(o); });
    sel.disabled = sheets.length === 0;
    if (sheets.length === 1) { sel.value = sheets[0]; currentSheet = sheets[0]; }
    setConn("ok","Подключено");
  } catch(e) {
    sel.innerHTML = '<option value="">Выберите лист</option>';
    sel.disabled  = true;
    setConn("error","Нет соединения");
  }
}

$("sheetSelect").addEventListener("change", e => { currentSheet = e.target.value || ""; });

/* ── run step 1 ──────────────────────────────────────────────────────────────── */
$("runStep1").addEventListener("click", async () => {
  if (!currentFile) return;
  $("mappingBody").innerHTML = '<tr><td colspan="5" class="empty-cell">Сканирование…</td></tr>';
  mappingRows = [];
  setStep("step1","step1StateTag","running");
  setStep("step2card","step2StateTag","idle");
  setStep("step3card","step3StateTag","idle");
  $("step1Log").textContent = "Читаю Excel, извлекаю структуру…";
  $("downloadCsv").disabled = $("downloadJson").disabled = true;
  $("runStep2").disabled = true;
  $("applyOverride").style.display = "none";
  $("offlineBanner").style.display = "none";
  startProgress();
  $("runStep1").disabled = true;

  try {
    const res = await runStep1Real();
    stopProgress(true);
    setStep("step1","step1StateTag","success");
    $("step1Log").textContent = `Маппинг получен. Колонок: ${res.rows.length}.`;

    // render mapping
    mappingRows = res.rows;
    renderMappingTable(res.rows);
    setStep("step2card","step2StateTag","success");

    // step 3
    $("tableName").textContent = res.table || "Не определена";
    $("tableMeta").textContent = res.table ? "Авто-выбор по результатам маппинга" : "Ожидание шага 01";
    $("dbMeta").textContent    = `${res.db || "—"} / ${res.sheet || "—"}`;
    setStep("step3card","step3StateTag", res.table?"success":"idle", res.table?"Таблица выбрана":"Ожидание");

    $("downloadCsv").disabled  = !res.csvUrl;
    $("downloadJson").disabled = !res.jsonUrl;
    $("downloadCsv").dataset.url  = res.csvUrl  || "";
    $("downloadJson").dataset.url = res.jsonUrl || "";
    $("runStep2").disabled = false;

    if (res.warning) {
      $("offlineBannerText").textContent = res.warning;
      $("offlineBanner").style.display = "flex";
      toast("Offline режим: " + res.warning,"warning",7000);
    } else {
      toast("Шаг 01 выполнен","success");
    }
  } catch(e) {
    stopProgress(false);
    setStep("step1","step1StateTag","error");
    $("step1Log").textContent = e.message || "Ошибка шага 01.";
    toast(e.message || "Ошибка шага 01","error");
  } finally {
    $("runStep1").disabled = false;
  }
});

async function runStep1Real() {
  const api = await ensureApiBase();
  if (!currentFileId) {
    setProgress(20);
    const form = new FormData(); form.append("file", currentFile);
    const up = await fetch(`${api}/api/upload`,{method:"POST",body:form});
    if (!up.ok) throw new Error(await readApiError(up));
    currentFileId = (await up.json()).file_id;
    setProgress(45);
  }
  setProgress(60);
  const r = await fetch(`${api}/api/run-step1`,{
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({file_id:currentFileId, sheet_name:currentSheet||null})
  });
  if (!r.ok) throw new Error(await readApiError(r));
  setProgress(90);
  const d = await r.json();
  return {
    table:   d.table || "",
    sheet:   d.sheet_name || "",
    db:      d.db_name || "",
    rows:    (d.step2||[]).map(row=>({ excel:row.excel_column||"", db:row.db_column||"", matches:String(row.match_count??0) })),
    csvUrl:  d.csv_url  ? `${api}${d.csv_url}`  : "",
    jsonUrl: d.json_url ? `${api}${d.json_url}` : "",
    warning: d.warning || "",
  };
}

/* ── render mapping table (with editable DB cells) ───────────────────────────── */
function renderMappingTable(rows) {
  const body = $("mappingBody");
  if (!rows.length) { body.innerHTML='<tr><td colspan="5" class="empty-cell">Колонки не найдены.</td></tr>'; return; }
  body.innerHTML = "";
  rows.forEach((row, i) => {
    const unresolved = !row.db || row.db.startsWith("(");
    const tr = document.createElement("tr");
    tr.dataset.idx = i;
    tr.innerHTML = `
      <td class="row-num">${i+1}</td>
      <td class="excel-col">${esc(row.excel)}</td>
      <td class="db-col-cell">
        <input class="db-col-input${unresolved?" unresolved":""}"
               value="${esc(unresolved ? row.db.replace(/^\(|\)$/g,"") : row.db)}"
               placeholder="введите имя колонки"
               data-original="${esc(row.db)}"
               data-idx="${i}" />
      </td>
      <td class="match-num">${esc(row.matches)}</td>
      <td>${confBadge(row.matches)}</td>
    `;
    body.appendChild(tr);
  });

  // listen for edits
  body.querySelectorAll(".db-col-input").forEach(inp => {
    inp.addEventListener("input", () => {
      const idx = parseInt(inp.dataset.idx, 10);
      mappingRows[idx].db = inp.value.trim() || inp.dataset.original;
      inp.classList.toggle("unresolved", !inp.value.trim());
      $("applyOverride").style.display = "inline-flex";
    });
  });
}

/* ── apply mapping override ──────────────────────────────────────────────────── */
$("applyOverride").addEventListener("click", async () => {
  if (!currentFileId) { toast("Сначала запустите шаг 01","warning"); return; }
  const api = await ensureApiBase();
  const rows = mappingRows.map(r => ({ excel_column:r.excel, db_column:r.db, match_count:parseInt(r.matches,10)||0 }));
  try {
    const r = await fetch(`${api}/api/mapping/override`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ file_id:currentFileId, sheet_name:currentSheet||null, rows })
    });
    if (!r.ok) throw new Error(await readApiError(r));
    $("applyOverride").style.display = "none";
    toast("Маппинг обновлён","success");
  } catch(e) {
    toast(e.message || "Ошибка применения маппинга","error");
  }
});

/* ── run step 2 (simple migrate) ─────────────────────────────────────────────── */
$("runStep2").addEventListener("click", async () => {
  setStep("step3card","step3StateTag","running");
  $("step2Log").textContent = "Запуск миграции данных…";
  $("runStep2").disabled = true;
  try {
    const api = await ensureApiBase();
    const r = await fetch(`${api}/api/run-step2`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({file_id:currentFileId})
    });
    if (!r.ok) throw new Error(await readApiError(r));
    const d = await r.json();
    setStep("step3card","step3StateTag","success","Мигрировано");
    $("step2Log").textContent = d.output || "Миграция завершена.";
    toast("Миграция данных завершена","success");
  } catch(e) {
    setStep("step3card","step3StateTag","error");
    $("step2Log").textContent = e.message || "Ошибка миграции.";
    toast(e.message || "Ошибка шага 02","error");
  } finally {
    $("runStep2").disabled = false;
  }
});

/* ── downloads ───────────────────────────────────────────────────────────────── */
$("downloadCsv").addEventListener("click",  () => { const u=$("downloadCsv").dataset.url;  if(u) window.open(u,"_blank"); });
$("downloadJson").addEventListener("click", () => { const u=$("downloadJson").dataset.url; if(u) window.open(u,"_blank"); });

/* ── reset all ───────────────────────────────────────────────────────────────── */
$("resetAll").addEventListener("click", () => {
  currentFile = null;
  $("excelInput").value = "";
  $("fileMetaPill").style.display = "none";
  $("runStep1").disabled = true;
  resetOutputs();
  toast("Сброшено","info",2500);
});

/* ═══════════════════════════════════════════════════════════════════════════════
   SCHEMA tab
═══════════════════════════════════════════════════════════════════════════════ */
$("refreshSchema").addEventListener("click", loadSchema);

async function loadSchema() {
  $("schemaContent").innerHTML = '<div class="empty-state"><p>Загрузка…</p></div>';
  try {
    const api = await ensureApiBase();
    // /api/schema/tables does NOT require file_id — works standalone against the DB
    const r = await fetch(`${api}/api/schema/tables`);
    if (!r.ok) throw new Error(await readApiError(r));
    const tables = await r.json(); // returns plain array [{schema, table, rows}]
    if (!Array.isArray(tables) || !tables.length) {
      $("schemaContent").innerHTML = '<div class="empty-state"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" opacity="0.3"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg><p>Таблицы не найдены или Postgres недоступен.</p></div>';
      return;
    }
    renderSchemaTables(tables);
  } catch(e) {
    $("schemaContent").innerHTML = `<div class="empty-state"><p style="color:#f87171">${esc(e.message)}</p></div>`;
  }
}

function renderSchemaTables(tables) {
  const el = $("schemaContent");
  el.innerHTML = `<div style="display:flex;gap:12px;align-items:flex-start">
    <div class="schema-tables" id="schemaTblList" style="min-width:220px;max-width:260px"></div>
    <div class="schema-detail" id="schemaDetail" style="flex:1"></div>
  </div>`;
  const list = $("schemaTblList");
  tables.forEach(t => {
    const fullName = `${t.schema||"public"}.${t.table||t.name||t}`;
    const rowCount = t.rows != null ? `${Number(t.rows).toLocaleString()} rows` : "";
    const item = document.createElement("div");
    item.className = "schema-tbl-item";
    item.innerHTML = `
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
      <span class="schema-tbl-name">${esc(fullName)}</span>
      <span class="schema-tbl-cnt">${esc(rowCount)}</span>
    `;
    item.addEventListener("click", async () => {
      list.querySelectorAll(".schema-tbl-item").forEach(i=>i.classList.remove("active"));
      item.classList.add("active");
      await loadTableDetail(t.schema||"public", t.table||t.name||t);
    });
    list.appendChild(item);
  });
}

function buildSchemaDependencyPanel(schema, table, outgoing, incoming) {
  const outgoingNodes = (outgoing || []).slice(0, 80).map((fk) => `
    <div class="schema-rel-node out">
      <div class="schema-rel-node-table">${esc((fk.ref_schema || "public") + "." + (fk.ref_table || "—"))}</div>
      <div class="schema-rel-node-map">${esc((fk.column || "—") + " → " + (fk.ref_column || "—"))}</div>
    </div>
  `).join("");
  const incomingNodes = (incoming || []).slice(0, 80).map((fk) => `
    <div class="schema-rel-node in">
      <div class="schema-rel-node-table">${esc((fk.source_schema || "public") + "." + (fk.source_table || "—"))}</div>
      <div class="schema-rel-node-map">${esc((fk.source_column || "—") + " → " + (fk.target_column || "—"))}</div>
    </div>
  `).join("");

  const outList = (outgoing || []).slice(0, 200).map((fk, i) => `
    <tr>
      <td class="row-num">${i + 1}</td>
      <td class="excel-col mono">${esc(fk.column || "—")}</td>
      <td class="excel-col mono">${esc((fk.ref_schema || "public") + "." + (fk.ref_table || "—"))}</td>
      <td class="excel-col mono">${esc(fk.ref_column || "—")}</td>
    </tr>
  `).join("");

  const inList = (incoming || []).slice(0, 200).map((fk, i) => `
    <tr>
      <td class="row-num">${i + 1}</td>
      <td class="excel-col mono">${esc((fk.source_schema || "public") + "." + (fk.source_table || "—"))}</td>
      <td class="excel-col mono">${esc(fk.source_column || "—")}</td>
      <td class="excel-col mono">${esc(fk.target_column || "—")}</td>
    </tr>
  `).join("");

  return `
    <div class="schema-rel-wrap">
      <div class="schema-rel-head">
        <div class="schema-rel-title">Зависимости таблицы</div>
        <div class="schema-rel-badges">
          <span class="schema-rel-badge out">Исходящие: ${(outgoing || []).length}</span>
          <span class="schema-rel-badge in">Входящие: ${(incoming || []).length}</span>
        </div>
      </div>

      <div class="schema-graph">
        <div class="schema-graph-col incoming">
          ${incomingNodes || '<div class="empty-state"><p>Входящих связей нет</p></div>'}
        </div>
        <div class="schema-graph-center">
          <div class="schema-main-node">
            <div class="schema-main-caption">Текущая таблица</div>
            <div class="schema-main-name mono">${esc(schema + "." + table)}</div>
          </div>
        </div>
        <div class="schema-graph-col outgoing">
          ${outgoingNodes || '<div class="empty-state"><p>Исходящих связей нет</p></div>'}
        </div>
      </div>

      <div class="schema-fk-grid">
        <div class="schema-fk-card">
          <div class="schema-fk-title">Исходящие FK</div>
          <div class="tbl-scroll schema-fk-scroll">
            <table class="data-tbl">
              <thead><tr><th>#</th><th>Колонка</th><th>Ссылка на таблицу</th><th>Колонка</th></tr></thead>
              <tbody>${outList || '<tr><td colspan="4" class="empty-cell">Нет исходящих внешних ключей</td></tr>'}</tbody>
            </table>
          </div>
        </div>
        <div class="schema-fk-card">
          <div class="schema-fk-title">Входящие FK</div>
          <div class="tbl-scroll schema-fk-scroll">
            <table class="data-tbl">
              <thead><tr><th>#</th><th>Источник</th><th>Колонка источника</th><th>Колонка текущей</th></tr></thead>
              <tbody>${inList || '<tr><td colspan="4" class="empty-cell">Нет входящих внешних ключей</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function loadTableDetail(schema, table) {
  const el = $("schemaDetail");
  el.innerHTML = '<div class="empty-state"><p>Загрузка колонок…</p></div>';
  try {
    const api = await ensureApiBase();
    const r = await fetch(`${api}/api/schema/table?name=${encodeURIComponent(table)}&schema=${encodeURIComponent(schema)}`);
    if (!r.ok) throw new Error(await readApiError(r));
    const d = await r.json();
    const cols = d.columns || [];
    const outgoing = d.foreign_keys || [];
    const incoming = d.incoming_foreign_keys || [];
    if (!cols.length) { el.innerHTML = '<div class="empty-state"><p>Нет данных о колонках.</p></div>'; return; }
    el.innerHTML = `
      <div class="schema-detail-layout">
        <div class="schema-fk-card">
          <div class="schema-fk-title">Колонки таблицы</div>
          <div class="tbl-scroll schema-cols-scroll">
            <table class="data-tbl">
              <thead><tr><th>#</th><th>Колонка</th><th>Тип</th><th>Nullable</th><th>Default</th></tr></thead>
              <tbody>${cols.map((c,i)=>`
                <tr>
                  <td class="row-num">${i+1}</td>
                  <td class="excel-col">${esc(c.name||c.column_name||"")}</td>
                  <td style="font-family:var(--mono);font-size:.74rem;color:var(--ink-2)">${esc(c.type||c.data_type||c.udt_name||"—")}</td>
                  <td style="font-size:.74rem;color:var(--ink-3)">${(c.nullable==="YES"||c.is_nullable==="YES")?"✓":"—"}</td>
                  <td style="font-family:var(--mono);font-size:.72rem;color:var(--ink-3)">${esc(c.default||c.column_default||"—")}</td>
                </tr>`).join("")}
              </tbody>
            </table>
          </div>
        </div>
        ${buildSchemaDependencyPanel(schema, table, outgoing, incoming)}
      </div>`;
  } catch(e) {
    el.innerHTML = `<div class="empty-state"><p style="color:#f87171">${esc(e.message)}</p></div>`;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════════
   MIGRATION tab (full run with polling)
═══════════════════════════════════════════════════════════════════════════════ */
$("refreshHistory").addEventListener("click", loadHistory);

$("runMigration").addEventListener("click", async () => {
  if (!currentFileId) { toast("Загрузите файл и запустите шаг 01 в Mapper","warning"); return; }
  $("runMigration").disabled = true;
  $("cancelMigration").style.display = "inline-flex";
  $("migChip").textContent = "Запуск…";
  $("migLog").textContent  = "Инициализация миграции…";
  $("migProgressWrap").style.display = "block";
  setStep("step3card","step3StateTag","running");

  try {
    const api = await ensureApiBase();
    const r = await fetch(`${api}/api/migration/run`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ file_id:currentFileId, mode:"upsert", tables:[], dry_run:false })
    });
    if (!r.ok) throw new Error(await readApiError(r));
    toast("Миграция запущена","info",3000);
    startMigrationPoller();
  } catch(e) {
    $("runMigration").disabled = false;
    $("cancelMigration").style.display = "none";
    $("migChip").textContent = "Ошибка";
    $("migLog").textContent  = e.message;
    toast(e.message,"error");
  }
});

$("cancelMigration").addEventListener("click", async () => {
  if (!currentFileId) return;
  try {
    const api = await ensureApiBase();
    await fetch(`${api}/api/migration/cancel`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file_id:currentFileId})});
    toast("Запрос отмены отправлен","warning",3000);
  } catch {}
});

function startMigrationPoller() {
  if (migPoller) clearInterval(migPoller);
  migPoller = setInterval(pollMigrationStatus, 1500);
}

async function pollMigrationStatus() {
  if (!currentFileId) return;
  try {
    const api = await ensureApiBase();
    const r = await fetch(`${api}/api/migration/status?file_id=${currentFileId}`);
    if (!r.ok) return;
    const d = await r.json();
    const p = d.progress || {};
    updateMigrationUI(p, d);

    const state = (p.state||"").toLowerCase();
    if (state === "completed" || state === "error" || state === "cancelled") {
      clearInterval(migPoller); migPoller = null;
      $("runMigration").disabled    = false;
      $("cancelMigration").style.display = "none";
      loadHistory();
      if (state === "completed") toast("Миграция завершена успешно","success");
      else if (state === "error") toast(p.message||"Ошибка миграции","error");
    }
  } catch {}
}

function updateMigrationUI(p, d) {
  const pct   = Math.max(0, Math.min(100, parseInt(p.percent)||0));
  const state = (p.state||"idle").toLowerCase();
  $("migProgBar").style.width = pct + "%";
  $("migProgPct").textContent = pct + "%";
  $("migProgStage").textContent = p.stage || "—";
  $("migRows").textContent   = (p.processed_rows||0).toLocaleString();
  $("migTables").textContent = p.processed_tables||0;
  $("migRps").textContent    = (parseFloat(p.rows_per_sec)||0).toFixed(1);
  if (p.eta_sec != null) $("migEta").textContent = `ETA ${Math.ceil(p.eta_sec)}s`;
  else $("migEta").textContent = "";

  const CHIP_LABELS = { idle:"Ожидание", running:"Выполняется", completed:"Готово", error:"Ошибка", cancelled:"Отменено" };
  $("migChip").textContent = CHIP_LABELS[state] || state;

  // log last events
  const events = (d.events||[]).slice(-20).map(e => {
    const ts = (e.ts||"").slice(11,19);
    const lbl = (e.level||"info").toUpperCase().padEnd(5);
    return `[${ts}] ${lbl}  ${e.message}`;
  }).join("\n");
  $("migLog").textContent = events || p.message || "Нет событий.";
}

async function loadHistory() {
  if (!currentFileId) { $("historyContent").innerHTML='<div class="empty-state"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" opacity="0.3"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg><p>Загрузите файл и запустите миграцию.</p></div>'; return; }
  try {
    const api = await ensureApiBase();
    const r = await fetch(`${api}/api/migration/history?file_id=${currentFileId}`);
    if (!r.ok) return;
    const d = await r.json();
    const items = d.history || [];
    if (!items.length) { $("historyContent").innerHTML='<div class="empty-state"><p>Нет истории запусков.</p></div>'; return; }
    const el = $("historyContent");
    el.innerHTML = '<div class="hist-list"></div>';
    const list = el.querySelector(".hist-list");
    items.slice(0,30).forEach(item => {
      const state = (item.state||"idle").toLowerCase();
      const pct   = parseInt(item.percent||0);
      const ts    = (item.started_at||"").slice(0,19).replace("T"," ");
      const div = document.createElement("div");
      div.className = "hist-item";
      div.innerHTML = `
        <div class="hist-state ${state}"></div>
        <span class="hist-time">${esc(ts)}</span>
        <span style="color:var(--ink-2);flex:1">${esc(item.stage||state)}</span>
        <span style="color:var(--ink-3);font-size:.72rem">${item.processed_rows||0} строк</span>
        <span class="hist-pct">${pct}%</span>
      `;
      list.appendChild(div);
    });
  } catch {}
}

/* ═══════════════════════════════════════════════════════════════════════════════
   INTEGRATIONS tab
═══════════════════════════════════════════════════════════════════════════════ */
$("refreshConnectors").addEventListener("click", loadConnectors);

const CAT_ICONS = {
  BI: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 12v4M12 8v8M16 16v-4"/></svg>`,
  ITSM:      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
  CRM:       `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`,
  ERP:       `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>`,
};

// Static fallback connector list (mirrors CONNECTORS in server.py)
const STATIC_CONNECTORS = [
  {id:"powerbi",   name:"Power BI",  category:"BI"},
  {id:"jira",      name:"Jira",      category:"ITSM"},
  {id:"bitrix24",  name:"Bitrix24",  category:"CRM"},
  {id:"sap",       name:"SAP",       category:"ERP"},
  {id:"onec",      name:"1C",        category:"ERP"},
];

const connectorDrafts = {};

function connStatusLabel(status) {
  const s = String(status || "disconnected").toLowerCase();
  if (s === "connected") return { cls: "available", text: "Подключен" };
  if (s === "testing") return { cls: "testing", text: "Тестируется" };
  if (s === "error") return { cls: "error", text: "Ошибка" };
  return { cls: "offline", text: "Отключен" };
}

function saveConnectorDraft(id, patch) {
  connectorDrafts[id] = { ...(connectorDrafts[id] || {}), ...(patch || {}) };
}

function readConnectorDraft(c) {
  const d = connectorDrafts[c.id] || {};
  const cfg = c.config || {};
  return {
    endpoint: d.endpoint ?? cfg.endpoint ?? "",
    token: d.token ?? cfg.token ?? "",
    direction: d.direction ?? "push",
  };
}

async function integrationPost(path, payload) {
  const api = await ensureApiBase();
  const r = await fetch(`${api}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await readApiError(r));
  return await r.json();
}

async function loadIntegrationEvents() {
  const box = $("integrationEvents");
  if (!box) return;
  if (!currentFileId) {
    box.textContent = "Нет событий. Загрузите файл и выполните шаг 01.";
    return;
  }
  try {
    const api = await ensureApiBase();
    const r = await fetch(`${api}/api/integrations/events?file_id=${currentFileId}&limit=80`);
    if (!r.ok) throw new Error(await readApiError(r));
    const d = await r.json();
    const lines = (d.events || []).slice(-60).map((e) => {
      const ts = String(e.ts || "").slice(11, 19);
      const connector = (e.connector_id || "").toUpperCase();
      const action = (e.action || "").toUpperCase();
      const status = (e.status || "").toUpperCase();
      return `[${ts}] ${connector} ${action} ${status}  ${e.message || ""}`;
    });
    box.textContent = lines.length ? lines.join("\n") : "Событий пока нет.";
  } catch (e) {
    box.textContent = e.message || "Не удалось загрузить журнал интеграций.";
  }
}

function setConnectorCardBusy(card, busy) {
  card.querySelectorAll("button, input, select").forEach((el) => {
    el.disabled = !!busy || (!!el.dataset.needFile && !currentFileId);
  });
}

function connectorCardHtml(c) {
  const icon = CAT_ICONS[c.category] || CAT_ICONS.ERP;
  const st = connStatusLabel(c.status);
  const d = readConnectorDraft(c);
  const test = c.last_test || {};
  const sync = c.last_sync || {};
  const testTxt = test.checked_at
    ? `${test.ok ? "OK" : "ERR"} · ${test.message || ""}`
    : "Тест не запускался";
  const syncTxt = sync.finished_at
    ? `${sync.direction || "push"} · rows≈${sync.estimated_rows || 0}`
    : "Синхронизация не запускалась";
  return `
    <div class="conn-card-head">
      <div class="conn-icon">${icon}</div>
      <div>
        <div class="conn-name">${esc(c.name)}</div>
        <div class="conn-cat">${esc(c.category || "")}</div>
      </div>
    </div>
    <div class="conn-status">
      <span class="conn-status-badge ${st.cls}">${st.text}</span>
    </div>
    <div class="conn-form">
      <input class="field-inp conn-endpoint" placeholder="https://api.example.com" value="${esc(d.endpoint)}" />
      <input class="field-inp conn-token" placeholder="Token (optional)" value="${esc(d.token)}" />
      <div class="conn-controls">
        <select class="field-sel conn-direction">
          <option value="push"${d.direction === "push" ? " selected" : ""}>push</option>
          <option value="pull"${d.direction === "pull" ? " selected" : ""}>pull</option>
          <option value="bidirectional"${d.direction === "bidirectional" ? " selected" : ""}>bidirectional</option>
        </select>
        <button class="ico-btn xsm conn-test" data-need-file="1">Test</button>
        <button class="ico-btn xsm conn-connect" data-need-file="1">Connect</button>
        <button class="ico-btn xsm conn-sync accent" data-need-file="1">Sync</button>
        <button class="ico-btn xsm conn-disconnect" data-need-file="1">Disconnect</button>
      </div>
      <div class="conn-meta">
        <div><strong>Test:</strong> ${esc(testTxt)}</div>
        <div><strong>Sync:</strong> ${esc(syncTxt)}</div>
      </div>
    </div>
  `;
}

async function loadConnectors() {
  const grid = $("connectorsGrid");
  const hint = $("integrationsHint");
  grid.innerHTML = '<div class="empty-state"><p>Загрузка…</p></div>';
  if (hint) hint.style.display = currentFileId ? "none" : "block";
  try {
    const api = await ensureApiBase();
    let connectors;
    if (currentFileId) {
      // With file_id — get live statuses from backend
      const r = await fetch(`${api}/api/integrations/connectors?file_id=${currentFileId}`);
      if (!r.ok) throw new Error(await readApiError(r));
      connectors = await r.json(); // plain array
    } else {
      // No file loaded yet — show static list with "available" status
      connectors = STATIC_CONNECTORS.map(c => ({...c, status:"available"}));
    }
    if (!connectors.length) { grid.innerHTML='<div class="empty-state"><p>Нет доступных коннекторов.</p></div>'; return; }
    grid.innerHTML = "";
    connectors.forEach(c => {
      const card = document.createElement("div");
      card.className = "conn-card";
      card.innerHTML = connectorCardHtml(c);
      const endpointEl = card.querySelector(".conn-endpoint");
      const tokenEl = card.querySelector(".conn-token");
      const directionEl = card.querySelector(".conn-direction");
      const cfg = () => ({
        endpoint: endpointEl.value.trim(),
        token: tokenEl.value.trim(),
      });
      const persistDraft = () => saveConnectorDraft(c.id, {
        endpoint: endpointEl.value.trim(),
        token: tokenEl.value.trim(),
        direction: directionEl.value,
      });
      endpointEl.addEventListener("input", persistDraft);
      tokenEl.addEventListener("input", persistDraft);
      directionEl.addEventListener("change", persistDraft);

      card.querySelector(".conn-test").addEventListener("click", async () => {
        if (!currentFileId) { toast("Сначала шаг 01 (нужен file_id)", "warning"); return; }
        setConnectorCardBusy(card, true);
        try {
          const res = await integrationPost("/api/integrations/test", {
            file_id: currentFileId,
            connector_id: c.id,
            config: cfg(),
          });
          toast(res.message || (res.ok ? "Тест OK" : "Тест не пройден"), res.ok ? "success" : "warning");
          await loadConnectors();
          await loadIntegrationEvents();
        } catch (e) {
          toast(e.message || "Ошибка тестирования", "error");
        } finally {
          setConnectorCardBusy(card, false);
        }
      });

      card.querySelector(".conn-connect").addEventListener("click", async () => {
        if (!currentFileId) { toast("Сначала шаг 01 (нужен file_id)", "warning"); return; }
        setConnectorCardBusy(card, true);
        try {
          await integrationPost("/api/integrations/connect", {
            file_id: currentFileId,
            connector_id: c.id,
            config: cfg(),
          });
          toast("Интеграция подключена", "success");
          await loadConnectors();
          await loadIntegrationEvents();
        } catch (e) {
          toast(e.message || "Ошибка подключения", "error");
        } finally {
          setConnectorCardBusy(card, false);
        }
      });

      card.querySelector(".conn-sync").addEventListener("click", async () => {
        if (!currentFileId) { toast("Сначала шаг 01 (нужен file_id)", "warning"); return; }
        setConnectorCardBusy(card, true);
        try {
          const res = await integrationPost("/api/integrations/sync", {
            file_id: currentFileId,
            connector_id: c.id,
            direction: directionEl.value,
            options: {},
          });
          toast(res?.result ? `Sync OK: rows≈${res.result.estimated_rows}` : "Sync завершен", "success");
          await loadConnectors();
          await loadIntegrationEvents();
        } catch (e) {
          toast(e.message || "Ошибка синхронизации", "error");
        } finally {
          setConnectorCardBusy(card, false);
        }
      });

      card.querySelector(".conn-disconnect").addEventListener("click", async () => {
        if (!currentFileId) { toast("Сначала шаг 01 (нужен file_id)", "warning"); return; }
        setConnectorCardBusy(card, true);
        try {
          await integrationPost("/api/integrations/disconnect", {
            file_id: currentFileId,
            connector_id: c.id,
            config: {},
          });
          toast("Интеграция отключена", "info");
          await loadConnectors();
          await loadIntegrationEvents();
        } catch (e) {
          toast(e.message || "Ошибка отключения", "error");
        } finally {
          setConnectorCardBusy(card, false);
        }
      });
      grid.appendChild(card);
      setConnectorCardBusy(card, false);
    });
    await loadIntegrationEvents();
  } catch(e) {
    grid.innerHTML = `<div class="empty-state"><p style="color:#f87171">${esc(e.message)}</p></div>`;
    const ev = $("integrationEvents");
    if (ev) ev.textContent = e.message || "Ошибка загрузки интеграций.";
  }
}

/* ── init ────────────────────────────────────────────────────────────────────── */
checkHealth(true);
loadConnectors();

// Enable migration button once file_id is available
Object.defineProperty(window, "_migBtnCheck", {
  get() {
    $("runMigration").disabled = !currentFileId;
    return currentFileId;
  }
});
setInterval(() => { $("runMigration").disabled = !currentFileId; }, 1000);

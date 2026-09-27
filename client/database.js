// Read-only SQLite viewer. The server owns both the table allow-list and row limit.
const ui = {
  tables: document.querySelector("#tables"), title: document.querySelector("#table-title"),
  meta: document.querySelector("#meta"), error: document.querySelector("#error"),
  head: document.querySelector("#records thead"), body: document.querySelector("#records tbody"),
  refresh: document.querySelector("#refresh"),
};
let overview = [], selected = "Meeting";

function showError(message = "") { ui.error.hidden = !message; ui.error.textContent = message; }
function display(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
function renderTables() {
  ui.tables.replaceChildren(...overview.map(item => {
    const button = document.createElement("button"); button.type = "button";
    button.className = item.name === selected ? "selected" : "";
    button.innerHTML = `<span>${item.name}</span><b>${item.count}</b>`;
    button.onclick = () => { selected = item.name; renderTables(); loadTable(); };
    return button;
  }));
}
function renderTable(data) {
  ui.title.textContent = data.table;
  ui.meta.textContent = data.total > data.limit ? `Showing newest ${data.limit} of ${data.total} saved rows.` : `${data.total} saved row${data.total === 1 ? "" : "s"}.`;
  const header = document.createElement("tr");
  for (const column of data.columns) { const cell = document.createElement("th"); cell.textContent = column; header.append(cell); }
  ui.head.replaceChildren(header); ui.body.replaceChildren(...data.rows.map(row => {
    const tr = document.createElement("tr");
    for (const column of data.columns) { const cell = document.createElement("td"); const value = display(row[column]); cell.textContent = value; cell.title = value; tr.append(cell); }
    return tr;
  }));
}
async function request(path) {
  const response = await fetch(path); const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || "Could not read the local database.");
  return body;
}
async function loadTable() {
  ui.refresh.disabled = true; showError();
  try { const data = await request(`/api/database?table=${encodeURIComponent(selected)}`); overview = data.tables; renderTables(); renderTable(data); }
  catch (error) { showError(error.message); }
  finally { ui.refresh.disabled = false; }
}
async function start() {
  try { overview = (await request("/api/database")).tables; if (!overview.some(item => item.name === selected)) selected = overview[0]?.name; renderTables(); await loadTable(); }
  catch (error) { showError(error.message); ui.title.textContent = "Database unavailable"; }
}
ui.refresh.onclick = loadTable;
start();

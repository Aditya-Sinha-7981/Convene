// Periodic reports (CON-18). The server resolves the range, counts meetings and renders the DOCX; this page sends
// plain dates, shows the preview line it gets back, and enables the download link only when the server allows it.
const $ = (id) => document.getElementById(id);
let timer = null, request = 0;

const iso = (date) => date.toISOString().slice(0, 10);

// Presets are plain UTC dates computed here and sent as from/to; the server has no preset logic.
function preset(name) {
  const now = new Date();
  const y = now.getUTCFullYear(), m = now.getUTCMonth();
  if (name === "this-month") return [new Date(Date.UTC(y, m, 1)), new Date(Date.UTC(y, m + 1, 0))];
  if (name === "last-month") return [new Date(Date.UTC(y, m - 1, 1)), new Date(Date.UTC(y, m, 0))];
  const q = Math.floor(m / 3) * 3;
  return [new Date(Date.UTC(y, q, 1)), new Date(Date.UTC(y, q + 3, 0))];
}

function setDownload(enabled, params) {
  const link = $("download");
  link.setAttribute("aria-disabled", enabled ? "false" : "true");
  link.classList.toggle("is-disabled", !enabled);
  link.href = enabled ? `/api/reports/download?${params}` : "#";
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function describe(p) {
  if (p.meeting_count === 0) {
    return "No ended meetings in this period." + (p.excluded_not_ended_count ? ` ${plural(p.excluded_not_ended_count, "meeting")} not yet ended.` : "");
  }
  let text = `${plural(p.meeting_count, "meeting")} (${p.with_summary_count} with summaries)`;
  if (p.excluded_not_ended_count) text += ` · ${p.excluded_not_ended_count} not yet ended, excluded`;
  if (p.over_cap) text += ` · more than ${p.max_meetings}, the most one report can hold: narrow the range`;
  return text;
}

async function refresh() {
  const from = $("from").value, to = $("to").value;
  $("error").textContent = "";
  setDownload(false);
  if (!from || !to) { $("preview").textContent = "Pick a date range."; return; }
  if (from > to) { $("preview").textContent = ""; $("error").textContent = "The start date is after the end date."; return; }
  const params = new URLSearchParams({ from, to });
  const mine = ++request;
  $("preview").textContent = "Counting meetings…";
  try {
    const response = await fetch(`/api/reports/preview?${params}`);
    const body = await response.json().catch(() => ({}));
    if (mine !== request) return;
    if (!response.ok) throw new Error(body.error?.message || "The preview failed.");
    $("preview").textContent = describe(body);
    setDownload(body.meeting_count > 0 && !body.over_cap, params);
  } catch (error) {
    if (mine !== request) return;
    $("preview").textContent = "";
    $("error").textContent = error.message;
  }
}

function schedule() { clearTimeout(timer); timer = setTimeout(refresh, 200); }

document.querySelectorAll("[data-preset]").forEach((button) => button.addEventListener("click", () => {
  const [from, to] = preset(button.dataset.preset);
  $("from").value = iso(from); $("to").value = iso(to);
  refresh();
}));
$("from").addEventListener("input", schedule);
$("to").addEventListener("input", schedule);
$("range").addEventListener("submit", (event) => event.preventDefault());
$("download").addEventListener("click", (event) => {
  if ($("download").getAttribute("aria-disabled") === "true") event.preventDefault();
});

const [from, to] = preset("this-month");
$("from").value = iso(from); $("to").value = iso(to);
refresh();

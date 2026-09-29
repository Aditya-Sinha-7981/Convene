const list = document.querySelector("#list"), filter = document.querySelector("#filter"), form = document.querySelector("#upload"), message = document.querySelector("#message"), error = document.querySelector("#list-error"), empty = document.querySelector("#empty"), emptyText = document.querySelector("#empty-text");
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const label = (state) => ({ ready: "Ready to search", pending: "Processing", processing: "Processing", failed: "Needs attention" }[state] || state || "Processing");
const badge = (state) => el("span", `pol-status is-${state || "pending"}`, label(state));
async function json(response) { return response.json().catch(() => ({})); }
function setMessage(text, failed = false) { message.textContent = text; message.classList.toggle("is-error", failed); }
function row({ policy, current_version: current, latest_version: latest }) {
  const article = el("article", "pol-row"), main = el("div", "pol-row-main"), link = el("a", "pol-title", policy.title || "Untitled policy"), tagList = el("div", "pol-tags"), meta = el("div", "pol-meta");
  link.href = `/policies/${encodeURIComponent(policy.policy_id)}`;
  for (const value of Array.isArray(policy.tags) ? policy.tags : []) tagList.append(el("span", "pol-tag", value));
  if (!tagList.childElementCount) tagList.append(el("span", "pol-tag", "No tags"));
  main.append(link, tagList); meta.append(badge(latest?.status), el("span", "", current ? `Search uses version ${current.version_number}` : "No searchable version yet")); article.append(main, meta); return article;
}
async function refresh() {
  error.textContent = "";
  try { const response = await fetch(`/api/policies?q=${encodeURIComponent(filter.value.trim())}`), data = await json(response); if (!response.ok) throw new Error(data.error?.message || "Could not load policies."); const policies = data.policies || []; list.replaceChildren(...policies.map(row)); empty.hidden = policies.length > 0; emptyText.textContent = filter.value.trim() ? "No policies match that search." : "No policies yet. Upload the first one when you are ready."; }
  catch (cause) { list.replaceChildren(); empty.hidden = true; error.textContent = cause.message; }
}
form.addEventListener("submit", async (event) => { event.preventDefault(); const submit = form.querySelector("button[type=submit]"); submit.disabled = true; setMessage("Uploading…"); try { const response = await fetch("/api/policies", { method: "POST", body: new FormData(form) }), data = await json(response); if (!response.ok) throw new Error(data.error?.message || "Upload failed."); form.reset(); setMessage("Upload accepted. Convene is processing it locally in the background."); await refresh(); } catch (cause) { setMessage(cause.message, true); } finally { submit.disabled = false; } });
let timer; filter.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 180); }); refresh();

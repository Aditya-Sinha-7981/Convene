// Minimal dashboard for checking the event feed (CON-04). The full dashboard replaces it (CON-07).
const meetingId = decodeURIComponent(location.pathname.split("/").pop());
const gauges = {};
const log = document.querySelector("#log");

async function refresh() {
  const response = await fetch(`/api/meetings/${meetingId}`);
  const body = await response.json();
  if (!response.ok) { document.querySelector("#status").textContent = body.error.message; return; }
  document.querySelector("#title").textContent = body.meeting.title;
  document.querySelector("#status").textContent = body.meeting.status;
  document.querySelector("#joinUrl").textContent = body.join_url || "(none: no LAN address, or the meeting ended)";
  const qr = document.querySelector("#qr");
  if (body.qr_svg) {
    qr.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(body.qr_svg);
    qr.hidden = false;
  } else { qr.hidden = true; }
  const rows = document.querySelector("#devices");
  rows.replaceChildren();
  for (const device of body.devices) {
    const live = gauges[device.device_id] || device.gauges;
    const tr = document.createElement("tr");
    const name = device.participants.map(p => p.display_name).join(", ") || "(shared)";
    for (const text of [name, device.status, device.reconnect_count, live.last_audio_age_ms ?? "—", live.audio_duration_s ?? "—"]) {
      const td = document.createElement("td");
      td.textContent = String(text);
      tr.appendChild(td);
    }
    rows.appendChild(tr);
  }
}

function connect() {
  const ws = new WebSocket(`wss://${location.host}/ws/dashboard/${meetingId}`);
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    log.textContent = `${new Date().toLocaleTimeString()} ${event.data}\n` + log.textContent.slice(0, 20000);
    if (message.type === "device_gauges") {
      for (const g of message.gauges) gauges[g.device_id] = g;
    }
    refresh();
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

document.querySelector("#end").onclick = async () => {
  await fetch(`/api/meetings/${meetingId}/end`, { method: "POST" });
  refresh();
};
connect();
refresh();

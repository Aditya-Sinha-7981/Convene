import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// The browser serves dashboard_state.js as an ES module. This repository intentionally has no package.json or
// build system, so Node loads the same source through a data URL rather than changing the project's module mode.
const source = await readFile(new URL("../../client/dashboard_state.js", import.meta.url), "utf8");
const { createState, orderedUtterances, reduce } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const meeting = { meeting_id: "m", title: "Review", status: "live" };
const device = { device_id: "d", meeting_id: "m", status: "connected", participants: [] };
const line = (id, start, text = id) => ({ utterance_id: id, meeting_id: "m", device_id: "d", t_start: start, text,
  speaker_label: "Priya", low_confidence: false });

test("orders late utterances by server timestamp and tie-breaker", () => {
  let state = createState();
  state = reduce(state, { type: "event", event: { type: "utterance", seq: 3, utterance: line("b", "10:00:02") } });
  state = reduce(state, { type: "event", event: { type: "utterance", seq: 4, utterance: line("a", "10:00:01") } });
  assert.deepEqual(orderedUtterances(state).map(row => row.utterance_id), ["a", "b"]);
});

test("deduplicates and replaces an utterance with its complete corrected view", () => {
  let state = reduce(createState(), { type: "event", event: { type: "utterance", seq: 4, utterance: line("a", "10:00") } });
  state = reduce(state, { type: "event", event: { type: "utterance", seq: 4, utterance: line("a", "10:00", "old") } });
  assert.equal(state.utterances.a.text, "a");
  state = reduce(state, { type: "event", event: { type: "utterance_updated", seq: 7,
    utterance: { ...line("a", "10:00", "corrected"), speaker_label: "Sam", corrected: true } } });
  assert.equal(state.utterances.a.text, "corrected");
  assert.equal(state.utterances.a.speaker_label, "Sam");
});

test("snapshot does not overwrite a newer event", () => {
  let state = reduce(createState(), { type: "event", event: { type: "device_status", seq: 12,
    device: { ...device, status: "disconnected" } } });
  state = reduce(state, { type: "snapshot", meeting, devices: [device], utterances: [], as_of_seq: 10 });
  assert.equal(state.devices.d.status, "disconnected");
});

test("snapshot seeds a device gauge with a receipt time", () => {
  const state = reduce(createState(), { type: "snapshot", as_of_seq: 3,
    devices: [{ ...device, gauges: { last_audio_age_ms: 25 } }] });
  assert.equal(state.gauges.d.last_audio_age_ms, 25);
  assert.equal(typeof state.gauges.d.received_at, "number");
});

test("buffers only durable changes newer than its snapshot cursor and preserves gauges", () => {
  let state = reduce(createState(), { type: "snapshot", meeting, devices: [device], utterances: [line("a", "10:00")], as_of_seq: 10 });
  state = reduce(state, { type: "event", event: { type: "utterance_updated", seq: 9, utterance: line("a", "10:00", "old") } });
  state = reduce(state, { type: "event", event: { type: "device_gauges", seq: null,
    gauges: [{ device_id: "d", last_audio_age_ms: 22, stt_backlog: 0 }] } });
  assert.equal(state.utterances.a.text, "a");
  assert.equal(state.gauges.d.last_audio_age_ms, 22);
});

test("unknown events are harmless and a large transcript remains ordered", () => {
  let state = createState();
  state = reduce(state, { type: "event", event: { type: "future_event", seq: 1 } });
  for (let n = 1500; n > 0; n--) {
    state = reduce(state, { type: "event", event: { type: "utterance", seq: n + 1,
      utterance: line(String(n).padStart(4, "0"), `10:00:${String(n % 60).padStart(2, "0")}`) } });
  }
  assert.equal(orderedUtterances(state).length, 1500);
});

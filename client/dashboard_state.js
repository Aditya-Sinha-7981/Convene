// Pure, server-authoritative dashboard state.  This module deliberately contains no DOM, network, or confidence
// threshold logic: the server supplies `speaker_label` and `low_confidence`.

export function createState() {
  return {
    meeting: null,
    meetingSeq: 0,
    devices: {},
    deviceSeq: {},
    gauges: {},
    utterances: {},
    utteranceSeq: {},
    asOfSeq: 0,
    lastSeq: 0,
  };
}

export function orderedUtterances(state) {
  return Object.values(state.utterances).sort((a, b) =>
    a.t_start.localeCompare(b.t_start) || a.utterance_id.localeCompare(b.utterance_id));
}

function putIfNewer(values, versions, id, value, seq) {
  if ((versions[id] || 0) >= seq) return [values, versions];
  return [{ ...values, [id]: value }, { ...versions, [id]: seq }];
}

function snapshot(state, action) {
  const { meeting, devices = [], utterances = [], as_of_seq: asOfSeq = 0 } = action;
  let next = { ...state, asOfSeq: Math.max(state.asOfSeq, asOfSeq), lastSeq: Math.max(state.lastSeq, asOfSeq) };
  if (meeting && state.meetingSeq <= asOfSeq) next = { ...next, meeting, meetingSeq: asOfSeq };
  for (const device of devices) {
    const [values, versions] = putIfNewer(next.devices, next.deviceSeq, device.device_id, device, asOfSeq);
    const gauges = device.gauges ? { ...next.gauges, [device.device_id]: { ...device.gauges, received_at: Date.now() } } : next.gauges;
    next = { ...next, devices: values, deviceSeq: versions, gauges };
  }
  for (const utterance of utterances) {
    // A transcript snapshot contains each line's creation seq. The snapshot cursor is its complete-state boundary.
    const version = Math.max(utterance.seq || 0, asOfSeq);
    const [values, versions] = putIfNewer(next.utterances, next.utteranceSeq, utterance.utterance_id, utterance, version);
    next = { ...next, utterances: values, utteranceSeq: versions };
  }
  return next;
}

function dashboardEvent(state, event) {
  if (!event || typeof event.type !== "string") return state;
  if (event.type === "device_gauges") {
    const gauges = { ...state.gauges };
    for (const gauge of event.gauges || []) gauges[gauge.device_id] = { ...gauge, received_at: Date.now() };
    return { ...state, gauges };
  }
  const seq = Number.isInteger(event.seq) ? event.seq : 0;
  if (seq && seq <= state.asOfSeq) return state;
  let next = { ...state, lastSeq: Math.max(state.lastSeq, seq) };
  if (event.type === "meeting_status" && event.meeting) {
    if (seq < state.meetingSeq) return state;
    return { ...next, meeting: event.meeting, meetingSeq: seq };
  }
  if (event.type === "device_status" && event.device) {
    const [devices, deviceSeq] = putIfNewer(state.devices, state.deviceSeq, event.device.device_id, event.device, seq);
    return { ...next, devices, deviceSeq };
  }
  if ((event.type === "utterance" || event.type === "utterance_updated") && event.utterance) {
    const [utterances, utteranceSeq] = putIfNewer(
      state.utterances, state.utteranceSeq, event.utterance.utterance_id, event.utterance, seq);
    return { ...next, utterances, utteranceSeq };
  }
  // Connection events are audit evidence, but the complete Device event is the dashboard's current state.
  return next;
}

export function reduce(state, action) {
  if (!action || typeof action.type !== "string") return state;
  if (action.type === "snapshot") return snapshot(state, action);
  if (action.type === "local_utterance" && action.utterance) {
    return { ...state, utterances: { ...state.utterances, [action.utterance.utterance_id]: action.utterance } };
  }
  if (action.type === "local_meeting" && action.meeting) return { ...state, meeting: action.meeting };
  if (action.type === "event") return dashboardEvent(state, action.event);
  return state;
}

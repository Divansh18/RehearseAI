import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/rehearse_ai/web/app.js', import.meta.url), 'utf8');
const mediaSource = readFileSync(new URL('../src/rehearse_ai/web/media_setup.js', import.meta.url), 'utf8');

class TextNode {
  constructor(value) { this.textContent = value; this.parent = null; }
}

class Element {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.parent = null;
    this._text = '';
    this.className = '';
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.scrollHeight = 400;
    this.clientHeight = 100;
    this.scrollTop = 0;
    this.attributes = {};
    this.readyState = 0;
    this.srcObject = null;
    this.classList = {
      add: (value) => { this.className = [...new Set([...this.className.split(' '), value])].filter(Boolean).join(' '); },
      remove: (value) => { this.className = this.className.split(' ').filter((item) => item !== value).join(' '); },
    };
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this._text = value; this.children = []; }
  append(...nodes) {
    for (const node of nodes) { node.parent = this; this.children.push(node); }
  }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child instanceof Element) {
        if (selector === child.tag) return child;
        if (child.className.split(' ').includes(selector.slice(1))) return child;
        const nested = child.querySelector(selector);
        if (nested) return nested;
      }
    }
    return null;
  }
  querySelectorAll(selector) {
    return this.children.filter((child) => child instanceof Element && child.tag === selector);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
  addEventListener(name, handler) { this[`on${name}`] = handler; }
  async play() {}
}

function fixture({ active = true, fetchResponse } = {}) {
  const ids = ['start', 'end', 'connection', 'microphone', 'status-dot', 'phase-label',
    'question', 'question-count', 'question-progress', 'guidance',
    'elapsed', 'error', 'setup-error', 'remote-audio', 'session-id',
    'admin-link', 'logout', 'ai-state', 'ai-presence', 'camera-state',
    'screen-share-state', 'media-loss-overlay', 'media-loss-reason',
    'room-camera-preview', 'room-screen-preview', 'restore-devices',
    'restore-screen', 'room-title', 'return-overview', 'snapshot-consent', 'snapshot-status'];
  ids.push('begin-interview', 'resume', 'setup-panel', 'interview-workspace',
    'camera-preview', 'screen-preview', 'mic-level', 'setup-message',
    'enable-devices', 'share-screen', 'test-speaker', 'confirm-speaker', 'cancel-setup',
    'check-microphone', 'check-camera', 'check-screen', 'check-speaker');
  const nodes = Object.fromEntries(ids.map((id) => [id, new Element()]));
  for (let i = 0; i < 4; i += 1) nodes['question-progress'].append(new Element('span'));
  nodes['question-count'].hidden = true;
  nodes['admin-link'].hidden = true;
  nodes.error.hidden = true;
  nodes['media-loss-overlay'].hidden = true;
  nodes['interview-workspace'].hidden = !active;
  nodes['setup-panel'].hidden = active;
  nodes.start.disabled = false;
  nodes.end.disabled = !active;
  for (const name of ['microphone', 'camera', 'screen', 'speaker']) {
    nodes[`check-${name}`].append(new Element('span'));
  }
  nodes.connection.textContent = 'Ready';
  nodes.microphone.textContent = 'Microphone off';
  nodes['remote-audio'].pause = () => {};
  nodes['interview-workspace'].showModal = function () { this.open = true; };
  nodes['interview-workspace'].close = function () { this.open = false; };
  nodes['interview-workspace'].open = active;
  const document = {
    documentElement: { style: { overflow: '' } },
    querySelector: (selector) => nodes[selector.slice(1)],
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => new TextNode(text),
  };
  const timeouts = new Map();
  const intervals = new Map();
  let nextTimer = 1;
  const context = vm.createContext({
    document, window: { isSecureContext: true, addEventListener(name, fn) { this[`on${name}`] = fn; } }, navigator: {},
    MediaStream: class {
      constructor(tracks) { this.tracks = tracks; }
      getTracks() { return this.tracks; }
      getAudioTracks() { return this.tracks.filter((track) => track.kind === 'audio'); }
      getVideoTracks() { return this.tracks.filter((track) => track.kind === 'video'); }
    },
    location: { assigned: null, assign(path) { this.assigned = path; } },
    fetch: fetchResponse || (async () => ({
      ok: true, status: 200, json: async () => ({ self_reported_name: 'Nyla Example', question_count: 4, can_begin: true, window_state: 'open', status: 'assigned' }),
    })),
    AbortController,
    setTimeout: (fn) => { const id = nextTimer++; timeouts.set(id, fn); return id; },
    clearTimeout: (id) => timeouts.delete(id),
    setInterval: (fn) => { const id = nextTimer++; intervals.set(id, fn); return id; },
    clearInterval: (id) => intervals.delete(id),
  });
  vm.runInContext(mediaSource, context);
  vm.runInContext(source, context);
  if (active) vm.runInContext('sessionClosed = false;', context);
  return {
    nodes,
    context,
    timeouts,
    intervals,
    flushTimeouts: () => { for (const [id, fn] of [...timeouts]) { timeouts.delete(id); fn(); } },
    send: (event) => context.handleEvent({ type: 'rehearseai', event }),
  };
}

test('prepared speech does not present a question; output and interruption update room state', () => {
  const { nodes, send, context } = fixture();
  vm.runInContext('questionTotal = 4;', context);
  send({ event: 'agent_prepared', utterance_id: 1, utterance_kind: 'question', question_number: 1, text: 'Question one.' });
  assert.equal(nodes['question-count'].hidden, true);
  assert.equal(nodes.connection.textContent, 'Preparing');
  send({ event: 'agent_output_started', utterance_id: 1 });
  assert.equal(nodes['question-count'].textContent, '01 / 04');
  assert.equal(nodes.connection.textContent, 'Speaking');
  send({ event: 'agent_interrupted', utterance_id: 1 });
  assert.equal(nodes.connection.textContent, 'Listening');
  send({ event: 'candidate_final', answer_number: 1, text: 'A project answer.' });
  assert.equal(nodes['question-count'].textContent, '01 / 04');
});

test('database write failure stays visible and does not claim the interview was saved', () => {
  const { nodes, send } = fixture();
  send({ event: 'persistence_error' });
  assert.match(nodes.error.textContent, /not being saved/);
  assert.equal(nodes.error.hidden, false);
  assert.equal(nodes.connection.textContent, 'Error');
  send({ event: 'agent_output_started', utterance_id: 1 });
  assert.equal(nodes.error.hidden, false);
});

test('room is modal, locks background scrolling, and Escape cannot dismiss it', () => {
  const { nodes, context } = fixture({ active: false });
  context.openRoom();
  assert.equal(nodes['interview-workspace'].open, true);
  assert.equal(nodes['room-title'].focused, true);
  assert.equal(context.document.documentElement.style.overflow, 'hidden');
  let prevented = false;
  nodes['interview-workspace'].oncancel({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  context.closeRoom();
  assert.equal(context.document.documentElement.style.overflow, '');
  assert.equal(nodes['begin-interview'].focused, true);
});

test('one acknowledged started interview captures camera only and never captures setup or screen', async () => {
  const uploads = [];
  const draws = [];
  const { nodes, context, send } = fixture({ fetchResponse: async (url, options) => {
    uploads.push({ url, options });
    return { ok: true, status: 201 };
  } });
  const camera = { getVideoTracks: () => [fakeTrack('video')] };
  const screen = { getVideoTracks: () => [fakeTrack('video')] };
  context.syntheticSetup = { camera, screen, live: () => true };
  vm.runInContext('mediaSetup = syntheticSetup; csrfToken = "synthetic";', context);
  nodes['snapshot-consent'].checked = true;
  const video = nodes['room-camera-preview'];
  video.readyState = 2;
  video.videoWidth = 1280;
  video.videoHeight = 720;
  context.document.createElement = (tag) => tag === 'canvas' ? {
    getContext: () => ({ drawImage(source) { draws.push(source); } }),
    toBlob(callback) { callback(new Blob(['synthetic jpeg'], { type: 'image/jpeg' })); },
  } : new Element(tag);
  send({ event: 'session_started', session_id: 'synthetic-session' });
  await new Promise(setImmediate);
  assert.equal(uploads.length, 0);
  vm.runInContext('beginSent = true;', context);
  send({ event: 'session_started', session_id: 'synthetic-session' });
  send({ event: 'session_started', session_id: 'synthetic-session' });
  await new Promise(setImmediate);
  assert.deepEqual(draws, [video]);
  assert.equal(uploads.length, 1);
  assert.match(uploads[0].url, /synthetic-session\/snapshot$/);
  assert.equal(nodes['snapshot-status'].textContent, 'Camera snapshot saved.');
});

test('no-audio warning stays visible during preparation and clears when server output starts', () => {
  const { nodes, send } = fixture();
  send({ event: 'response_error', error_code: 'tts_context_no_audio' });
  assert.equal(nodes.connection.textContent, 'Error');
  assert.equal(nodes.error.hidden, false);
  assert.match(nodes.error.textContent, /returned no audio/);
  send({ event: 'agent_prepared', utterance_id: 3, utterance_kind: 'question', question_number: 1, text: 'Question one.' });
  assert.equal(nodes.error.hidden, false);
  send({ event: 'agent_output_started', utterance_id: 3 });
  assert.equal(nodes.error.hidden, true);
  assert.equal(nodes.connection.textContent, 'Speaking');
  send({ event: 'interview_complete' });
  assert.equal(nodes.connection.textContent, 'Finishing');
});

test('Begin remains blocked until media setup is ready', async () => {
  const { nodes, context } = fixture({ active: false });
  await context.startSession();
  assert.equal(nodes.connection.textContent, 'Ready');
  assert.equal(nodes['question-count'].hidden, true);
  assert.equal(nodes.end.disabled, true);
});

test('two Begin clicks cannot create duplicate connection attempts', async () => {
  let resolveAccount;
  let requests = 0;
  const { context } = fixture({ active: false, fetchResponse: () => {
    requests += 1;
    return new Promise((resolve) => { resolveAccount = resolve; });
  } });
  vm.runInContext('invitationReady = true; mediaSetup = { ready: () => true, microphone: { getAudioTracks: () => [] }, showRoomPreviews() {}, cleanup() {} };', context);
  context.document.querySelector('#snapshot-consent').checked = true;
  const first = context.startSession();
  const second = context.startSession();
  assert.equal(requests, 1);
  resolveAccount({ ok: false, status: 401 });
  await Promise.all([first, second]);
});

test('expired invitation redirects before microphone access or WebRTC setup', async () => {
  const { context, nodes } = fixture({
    active: false,
    fetchResponse: async () => ({ ok: false, status: 401 }),
  });
  vm.runInContext('invitationReady = true; mediaSetup = { ready: () => true, microphone: { getAudioTracks: () => [] }, showRoomPreviews() {}, cleanup() {} };', context);
  nodes['snapshot-consent'].checked = true;
  await context.startSession();
  assert.equal(context.location.assigned, '/candidate/welcome');
  assert.equal(nodes.end.disabled, true);
});

test('invitation metadata sets assigned question count without exposing a plan', async () => {
  const interview = fixture({ fetchResponse: async () => ({ ok: true, status: 200,
    json: async () => ({ self_reported_name: 'Nyla Example', question_count: 3, can_begin: true, window_state: 'open', status: 'assigned' }) }) });
  await interview.context.checkAccount();
  assert.equal(interview.nodes['question-progress'].children.length, 3);
});

test('End session releases tracks and connections immediately', () => {
  const { nodes, context } = fixture();
  const closed = [];
  const track = { readyState: 'live', stop: () => { track.readyState = 'ended'; } };
  context.fakeStream = { getTracks: () => [track], getAudioTracks: () => [track] };
  context.fakePeer = { close: () => closed.push('peer') };
  context.fakeChannel = { close: () => closed.push('channel') };
  nodes['remote-audio'].pause = () => closed.push('audio');
  vm.runInContext('microphone = fakeStream; peer = fakePeer; channel = fakeChannel;', context);
  context.endSession();
  assert.deepEqual(closed, ['channel', 'peer', 'audio']);
  assert.equal(track.readyState, 'ended');
  assert.equal(nodes.microphone.textContent, 'Microphone off');
  assert.equal(nodes.connection.textContent, 'Ended');
  assert.equal(nodes['return-overview'].hidden, false);
  assert.equal(nodes.end.disabled, true);
  assert.equal(nodes.start.hidden, true);
});

test('professional closing drains before automatic cleanup and freezes timer', () => {
  const { nodes, context, send, timeouts, intervals, flushTimeouts } = fixture();
  const closed = [];
  const track = { readyState: 'live', stop: () => { track.readyState = 'ended'; } };
  context.fakeStream = { getTracks: () => [track], getAudioTracks: () => [track] };
  context.fakePeer = { close: () => closed.push('peer') };
  context.fakeChannel = { close: () => closed.push('channel') };
  nodes['remote-audio'].pause = () => closed.push('audio');
  vm.runInContext('microphone = fakeStream; peer = fakePeer; channel = fakeChannel;', context);
  send({ event: 'session_started', session_id: 'session-a' });
  assert.equal(intervals.size, 1);
  send({ event: 'agent_prepared', utterance_id: 8, utterance_kind: 'closing', text: 'Thank you for your time.' });
  send({ event: 'agent_output_started', utterance_id: 8 });
  send({ event: 'agent_delivery_reported', utterance_id: 8 });
  send({ event: 'interview_complete' });
  assert.equal(nodes['phase-label'].textContent, 'Interview complete');
  assert.equal(track.readyState, 'live');
  assert.equal(timeouts.size, 1);
  assert.equal(nodes['question-progress'].children[3].dataset.state, 'complete');
  flushTimeouts();
  assert.deepEqual(closed, ['channel', 'peer', 'audio']);
  assert.equal(track.readyState, 'ended');
  assert.equal(nodes.connection.textContent, 'Ended');
  assert.equal(nodes.microphone.textContent, 'Microphone off');
  assert.equal(nodes['return-overview'].hidden, false);
  assert.equal(nodes['phase-label'].textContent, 'Interview complete');
  assert.equal(intervals.size, 0);
  send({ event: 'agent_prepared', utterance_id: 9, text: 'Late question.' });
  send({ event: 'agent_output_started', utterance_id: 9 });
});

test('manual End cancels pending automatic cleanup and Start again returns to setup', () => {
  const { nodes, context, send, timeouts, flushTimeouts } = fixture();
  send({ event: 'interview_ended_early' });
  assert.equal(timeouts.size, 1);
  context.endSession();
  assert.equal(timeouts.size, 0);
  flushTimeouts();
  assert.equal(nodes.connection.textContent, 'Ended');
  context.showSetup();
  assert.equal(nodes.connection.textContent, 'Ready');
  assert.equal(nodes['setup-panel'].hidden, false);
  assert.equal(nodes['interview-workspace'].hidden, true);
});

function fakeTrack(kind) {
  return {
    kind, readyState: 'live',
    addEventListener(name, handler) { this[`on${name}`] = handler; },
    stop() { this.readyState = 'ended'; },
    end() { this.stop(); this.onended?.(); },
  };
}

test('setup checks live tracks and preview frames; silent mic remains valid', async () => {
  const { nodes, context } = fixture({ active: false });
  const mic = fakeTrack('audio');
  const camera = fakeTrack('video');
  const screen = fakeTrack('video');
  context.navigator.mediaDevices = {
    getUserMedia: async () => new context.MediaStream([mic, camera]),
    getDisplayMedia: async (options) => {
      assert.equal(options.video, true);
      assert.equal(options.audio, false);
      return new context.MediaStream([screen]);
    },
  };
  const setup = new context.window.RehearseMediaSetup({ onChange() {}, onLoss() {} });
  await setup.enableDevices();
  await setup.shareScreen();
  setup.speakerTonePlayed = true;
  setup.confirmSpeaker();
  assert.equal(setup.ready(), false);
  nodes['camera-preview'].readyState = 2;
  nodes['screen-preview'].readyState = 2;
  setup.update();
  assert.equal(setup.ready(), true);
  assert.equal(nodes['room-camera-preview'].srcObject, setup.camera);
  assert.equal(nodes['room-screen-preview'].srcObject, setup.screen);
  assert.equal(nodes['mic-level'].value || 0, 0);
  camera.end();
  assert.equal(setup.ready(), false);
  setup.cleanup();
  assert.equal(mic.readyState, 'ended');
  assert.equal(screen.readyState, 'ended');
  assert.equal(nodes['room-camera-preview'].srcObject, null);
  assert.equal(nodes['room-screen-preview'].srcObject, null);
});

test('Begin keeps the existing camera and screen streams in the room and sends only microphone audio', async () => {
  let deviceRequests = 0;
  let screenRequests = 0;
  let peer;
  let channel;
  const signals = [];
  let offerBody;
  const { nodes, context } = fixture({ active: false, fetchResponse: async (url, options) => {
    if (url === '/api/candidate/interview') return { ok: true, status: 200, json: async () => ({ self_reported_name: 'Nyla Example', question_count: 4, can_begin: true, window_state: 'open', status: 'assigned' }) };
    if (url === '/api/csrf') return { ok: true, json: async () => ({ token: 'synthetic' }) };
    if (url === '/api/offer') {
      offerBody = JSON.parse(options.body);
      return { ok: true, status: 200, json: async () => ({ type: 'answer', sdp: 'synthetic' }) };
    }
    throw new Error(`Unexpected request: ${url}`);
  } });
  const mic = fakeTrack('audio');
  const camera = fakeTrack('video');
  const screen = fakeTrack('video');
  context.navigator.mediaDevices = {
    getUserMedia: async () => { deviceRequests += 1; return new context.MediaStream([mic, camera]); },
    getDisplayMedia: async () => { screenRequests += 1; return new context.MediaStream([screen]); },
  };
  context.RTCPeerConnection = class {
    constructor() { this.iceGatheringState = 'complete'; this.added = []; peer = this; }
    addTrack(track) { this.added.push(track); }
    createDataChannel() {
      channel = { readyState: 'open', send(value) { signals.push(JSON.parse(value)); }, close() {} };
      return channel;
    }
    async createOffer() { return { type: 'offer', sdp: 'synthetic' }; }
    async setLocalDescription(description) { this.localDescription = description; }
    async setRemoteDescription() {}
    close() {}
  };
  const setup = new context.window.RehearseMediaSetup({
    onChange(ready) { nodes['begin-interview'].disabled = !ready || !nodes['snapshot-consent'].checked; },
    onLoss() {},
  });
  await setup.enableDevices();
  await setup.shareScreen();
  nodes['camera-preview'].readyState = 2;
  nodes['screen-preview'].readyState = 2;
  setup.speakerTonePlayed = true;
  setup.confirmSpeaker();
  context.syntheticSetup = setup;
  vm.runInContext('mediaSetup = syntheticSetup; invitationReady = true;', context);
  setup.update();
  context.updateMediaLabels();
  assert.equal(setup.ready(), true);
  assert.equal(nodes['begin-interview'].disabled, true);
  nodes['snapshot-consent'].checked = true;
  setup.update();
  assert.equal(nodes['begin-interview'].disabled, false);
  await context.startSession();
  channel.onopen();
  assert.equal(deviceRequests, 1);
  assert.equal(screenRequests, 1);
  assert.equal(nodes['setup-panel'].hidden, false);
  assert.equal(nodes['interview-workspace'].hidden, false);
  assert.equal(nodes.guidance.textContent, '');
  assert.equal(nodes['room-camera-preview'].srcObject, setup.camera);
  assert.equal(nodes['room-screen-preview'].srcObject, setup.screen);
  assert.deepEqual(peer.added, [mic]);
  assert.equal(offerBody.snapshot_acknowledged, true);
  assert.equal(signals[0].action, 'begin');
  assert.equal(nodes['camera-state'].textContent, 'Camera on');
  assert.equal(nodes['screen-share-state'].textContent, 'Sharing your screen');
  context.endSession();
  assert.equal(nodes['setup-panel'].hidden, false);
  assert.equal(mic.readyState, 'ended');
  assert.equal(camera.readyState, 'ended');
  assert.equal(screen.readyState, 'ended');
  assert.equal(nodes['room-camera-preview'].srcObject, null);
});

test('the live room does not render candidate transcript events', () => {
  const { nodes, send } = fixture();
  send({ event: 'candidate_final', answer_number: 1, text: 'Private candidate answer.' });
  assert.equal(nodes['question'].textContent.includes('Private candidate answer.'), false);
});

test('permission denial and cancelled screen picker leave setup blocked', async () => {
  const { nodes, context } = fixture({ active: false });
  context.navigator.mediaDevices = {
    getUserMedia: async () => { throw Object.assign(new Error('denied'), { name: 'NotAllowedError' }); },
    getDisplayMedia: async () => { throw Object.assign(new Error('cancelled'), { name: 'NotAllowedError' }); },
  };
  const setup = new context.window.RehearseMediaSetup({ onChange() {}, onLoss() {} });
  await setup.enableDevices();
  assert.match(nodes['setup-message'].textContent, /permission was denied/i);
  await setup.shareScreen();
  assert.match(nodes['setup-message'].textContent, /cancelled or blocked/i);
  assert.equal(setup.ready(), false);
  context.window.isSecureContext = false;
  await setup.shareScreen();
  assert.match(nodes['setup-message'].textContent, /supported desktop browser/i);
});

test('late media permission result after cleanup is stopped', async () => {
  const { context } = fixture({ active: false });
  let resolvePermission;
  context.navigator.mediaDevices = {
    getUserMedia: () => new Promise((resolve) => { resolvePermission = resolve; }),
    getDisplayMedia: async () => {},
  };
  const setup = new context.window.RehearseMediaSetup({ onChange() {}, onLoss() {} });
  const pending = setup.enableDevices();
  setup.cleanup();
  const mic = fakeTrack('audio');
  const camera = fakeTrack('video');
  resolvePermission(new context.MediaStream([mic, camera]));
  await pending;
  assert.equal(mic.readyState, 'ended');
  assert.equal(camera.readyState, 'ended');
  assert.equal(setup.ready(), false);
});

test('cleanup closes an unfinished speaker test audio context', async () => {
  const { context } = fixture({ active: false });
  let closed = 0;
  context.window.AudioContext = class {
    constructor() { this.currentTime = 0; this.destination = {}; }
    async resume() {}
    async close() { closed += 1; }
    createOscillator() { return { frequency: {}, connect: (target) => target, start() {}, stop() {} }; }
    createGain() { return { gain: {}, connect: () => ({ connect() {} }) }; }
  };
  const setup = new context.window.RehearseMediaSetup({ onChange() {}, onLoss() {} });
  await setup.testSpeaker();
  setup.cleanup();
  assert.equal(closed, 1);
});

test('track loss pauses once; explicit resume requires restored readiness', async () => {
  const { nodes, context } = fixture();
  const signals = [];
  context.fakeChannel = { readyState: 'open', send: (value) => signals.push(JSON.parse(value)), close() {} };
  vm.runInContext('channel = fakeChannel; mediaSetup = { ready: () => false, microphone: null, cleanup: () => {} };', context);
  context.mediaLost('Screen sharing');
  context.mediaLost('Camera');
  assert.equal(nodes.connection.textContent, 'Paused');
  assert.equal(nodes['setup-panel'].hidden, true);
  assert.equal(nodes['interview-workspace'].hidden, false);
  assert.equal(nodes['media-loss-overlay'].hidden, false);
  assert.equal(nodes.end.disabled, false);
  assert.equal(nodes.resume.hidden, false);
  assert.equal(signals.length, 0);
  await context.resumeInterview();
  assert.equal(signals.length, 0);
  vm.runInContext('mediaSetup.ready = () => true;', context);
  await context.resumeInterview();
  assert.equal(signals[0].action, 'begin');
  assert.equal(nodes.connection.textContent, 'Listening');
  assert.equal(nodes['media-loss-overlay'].hidden, true);
  context.mediaLost('Camera');
  assert.equal(signals[1].action, 'pause');
  vm.runInContext('mediaSetup.ready = () => true;', context);
  await context.resumeInterview();
  assert.equal(signals.at(-1).action, 'resume');
  assert.equal(nodes.connection.textContent, 'Paused');
  context.handleEvent({ type: 'rehearseai', event: { event: 'media_resumed' } });
  assert.equal(nodes.connection.textContent, 'Listening');
  assert.equal(nodes['media-loss-overlay'].hidden, true);
  context.endSession();
  assert.equal(nodes.connection.textContent, 'Ended');
  assert.equal(nodes.resume.hidden, true);
});

test('End remains available while the media-loss overlay is open', () => {
  const { nodes, context } = fixture();
  context.mediaLost('Microphone');
  nodes.end.onclick();
  assert.equal(nodes.connection.textContent, 'Ended');
  assert.equal(nodes['media-loss-overlay'].hidden, true);
  assert.equal(nodes['interview-workspace'].hidden, false);
  assert.equal(nodes['return-overview'].hidden, false);
});

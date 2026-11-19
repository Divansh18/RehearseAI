const startButton = document.querySelector('#start');
const endButton = document.querySelector('#end');
const connectionLabel = document.querySelector('#connection');
const microphoneLabel = document.querySelector('#microphone');
const statusDot = document.querySelector('#status-dot');
const phaseLabel = document.querySelector('#phase-label');
const questionLabel = document.querySelector('#question');
const questionCount = document.querySelector('#question-count');
const guidance = document.querySelector('#guidance');
const elapsed = document.querySelector('#elapsed');
const progressTrack = document.querySelector('#question-progress');
const errorLabel = document.querySelector('#error');
const audio = document.querySelector('#remote-audio');
const beginButton = document.querySelector('#begin-interview');
const resumeButton = document.querySelector('#resume');
const setupPanel = document.querySelector('#setup-panel');
const workspace = document.querySelector('#interview-workspace');
const roomTitle = document.querySelector('#room-title');
const returnButton = document.querySelector('#return-overview');
const snapshotConsent = document.querySelector('#snapshot-consent');
const snapshotStatus = document.querySelector('#snapshot-status');
const setupError = document.querySelector('#setup-error');
const aiState = document.querySelector('#ai-state');
const aiPresence = document.querySelector('#ai-presence');
const cameraState = document.querySelector('#camera-state');
const screenShareState = document.querySelector('#screen-share-state');
const mediaLossOverlay = document.querySelector('#media-loss-overlay');
const mediaLossReason = document.querySelector('#media-loss-reason');
let peer = null;
let microphone = null;
let channel = null;
let mediaSetup = null;
let mediaPaused = false;
let beginSent = false;
const prepared = new Map();
const startedUtterances = new Set();
let attempt = 0;
let offerAbort = null;
let sessionClosed = true;
let terminalPending = false;
let cleanupTimeout = null;
let timerInterval = null;
let timerStartedAt = null;
let csrfToken = null;
let snapshotAbort = null;
let snapshotAttempted = false;
let previousOverflow = '';
let questionTotal = 1;
let invitationReady = false;

let readinessTimer = null;

// Begin stays locked until the server says the interview plan is ready; this only reports it.
function showPreparation(assignment) {
  const note = document.querySelector('#prep-message');
  clearTimeout(readinessTimer);
  if (assignment.plan_state === 'preparing' && assignment.status === 'assigned') {
    note.textContent = 'Your interviewer is still preparing your interview. You can finish your device checks now. Begin unlocks on its own when it is ready.';
    note.hidden = false;
    readinessTimer = setTimeout(() => { if (sessionClosed) checkAccount().catch(() => {}); }, 5000);
  } else if (assignment.plan_state === 'unavailable') {
    note.textContent = 'Your interview is not ready to begin. Please contact the interviewer.';
    note.hidden = false;
  } else if (assignment.plan_state === 'ready' && assignment.status === 'assigned' && assignment.window_state === 'open') {
    note.textContent = 'Your interview is ready. Complete the checks above and begin when you are ready.';
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

async function checkAccount() {
  const response = await fetch('/api/candidate/interview', { credentials: 'same-origin' });
  if (response.status === 401) {
    endSession();
    location.assign('/candidate/welcome');
    return false;
  }
  if (!response.ok) throw new Error('Could not check your invitation. Reopen the private link.');
  const assignment = await response.json();
  if (!assignment.self_reported_name) { location.assign('/candidate/welcome'); return false; }
  questionTotal = assignment.question_count || 0;
  invitationReady = assignment.can_begin;
  progressTrack.replaceChildren(...Array.from({ length: questionTotal }, () => document.createElement('span')));
  showPreparation(assignment);
  if (assignment.window_state === 'too_early') {
    document.querySelector('#setup-message').textContent = 'The joining window has not opened yet. You can check devices now, then refresh when it opens.';
  } else if (assignment.window_state === 'closed' || assignment.status !== 'assigned') {
    document.querySelector('#setup-message').textContent = 'This interview can no longer be started. Contact the interviewer.';
  }
  mediaSetup?.update();
  return true;
}

function renderElapsed() {
  const seconds = timerStartedAt === null ? 0 : Math.floor((Date.now() - timerStartedAt) / 1000);
  elapsed.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function stopTimer() {
  if (timerInterval !== null) clearInterval(timerInterval);
  timerInterval = null;
  if (timerStartedAt !== null) renderElapsed();
  timerStartedAt = null;
}

function startTimer() {
  if (timerInterval !== null || timerStartedAt !== null) return;
  timerStartedAt = Date.now();
  renderElapsed();
  timerInterval = setInterval(renderElapsed, 1000);
}

function setProgress(questionNumber = 0, complete = false) {
  const segments = progressTrack.querySelectorAll('span');
  segments.forEach((segment, index) => {
    const state = complete || index + 1 < questionNumber ? 'complete'
      : index + 1 === questionNumber ? 'active' : '';
    if (state) segment.dataset.state = state;
    else delete segment.dataset.state;
  });
  progressTrack.setAttribute('aria-label', complete ? 'All questions complete'
    : questionNumber ? `Question ${questionNumber} of ${questionTotal}` : 'Interview has not started');
}

function microphoneState() {
  return [microphone, mediaSetup?.microphone].some((stream) =>
    stream?.getAudioTracks().some((track) => track.readyState === 'live'))
    ? 'Microphone on' : 'Microphone off';
}

function updateMediaLabels() {
  cameraState.textContent = mediaSetup?.live(mediaSetup.camera, 'video') ? 'Camera on' : 'Camera off';
  screenShareState.textContent = mediaSetup?.live(mediaSetup.screen, 'video')
    ? 'Sharing your screen' : 'Not sharing';
  microphoneLabel.textContent = microphoneState();
}

function setStatus(label) {
  if (mediaPaused && !['Paused', 'Error', 'Ended', 'Finishing'].includes(label)) return;
  connectionLabel.textContent = label;
  aiState.textContent = label;
  aiState.dataset.state = label.toLowerCase();
  aiPresence.dataset.state = label.toLowerCase();
  microphoneLabel.textContent = microphoneState();
  statusDot.dataset.state = label.toLowerCase();
}

function setInterviewPhase(phase, text, hint = '') {
  phaseLabel.textContent = phase;
  questionLabel.textContent = text;
  guidance.textContent = hint;
}

function showError(message) {
  const target = workspace.open ? errorLabel : setupError;
  target.textContent = message;
  target.hidden = false;
  target.dataset.kind = 'error';
  setStatus('Error');
}

function showWarning(message) {
  errorLabel.textContent = message;
  errorLabel.hidden = false;
  errorLabel.dataset.kind = 'warning';
  setStatus('Error');
}

function clearWarning() {
  if (errorLabel.dataset.kind === 'warning') {
    errorLabel.hidden = true;
    delete errorLabel.dataset.kind;
  }
}

function closeRoom() {
  const wasOpen = workspace.open;
  if (workspace.open) workspace.close();
  workspace.hidden = true;
  document.documentElement.style.overflow = previousOverflow;
  if (wasOpen) beginButton.focus();
}

function openRoom() {
  workspace.hidden = false;
  previousOverflow = document.documentElement.style.overflow;
  document.documentElement.style.overflow = 'hidden';
  workspace.showModal();
  roomTitle.focus();
}

// One image of the interview room is saved for the admin-only Conversation PDF. It is taken once,
// after Begin, when the room is on screen and the camera has a valid frame: shortly after the first
// question is shown, or after a fallback delay if the opening runs long. A failure here never
// touches the interview; it only changes the status line.
const ROOM_CAPTURE_SETTLE_MS = 500;
const ROOM_CAPTURE_FALLBACK_MS = 20000;
const ROOM_CAPTURE_PAUSED_RETRY_MS = 3000;
const ROOM_CAPTURE_RENDER_TRIES = 3;
const ROOM_CAPTURE_RETRY_MS = 800;
const ROOM_IMAGE_UNAVAILABLE = 'Room image unavailable; interview continues.';
let captureTimer = null;
let activeSessionId = null;

function clearCaptureTimer() {
  if (captureTimer !== null) clearTimeout(captureTimer);
  captureTimer = null;
}

function scheduleRoomCapture(sessionId, delay) {
  clearCaptureTimer();
  const scheduledAttempt = attempt;
  captureTimer = setTimeout(() => {
    captureTimer = null;
    captureSnapshot(sessionId, scheduledAttempt).catch(() => {
      if (!sessionClosed) snapshotStatus.textContent = ROOM_IMAGE_UNAVAILABLE;
    });
  }, delay);
}

async function captureSnapshot(sessionId, currentAttempt) {
  if (snapshotAttempted || !beginSent || !snapshotConsent.checked || sessionClosed) return;
  if (currentAttempt !== attempt) return;
  if (mediaPaused) { scheduleRoomCapture(sessionId, ROOM_CAPTURE_PAUSED_RETRY_MS); return; }
  snapshotAttempted = true;
  const stale = () => currentAttempt !== attempt || sessionClosed || mediaPaused;
  const video = document.querySelector('#room-camera-preview');
  const capture = window.RehearseRoomCapture;
  let result = { error: 'capture_unavailable' };
  if (capture && mediaSetup?.live(mediaSetup.camera, 'video')) {
    for (let tries = 0; tries < ROOM_CAPTURE_RENDER_TRIES; tries += 1) {
      result = await capture.captureRoom({
        dialog: workspace, cameraVideo: video,
        screenActive: Boolean(mediaSetup.live(mediaSetup.screen, 'video')),
      });
      if (!result.error || stale()) break;
      // A blank camera frame or an unrendered room may just be early; anything else will not improve.
      if (!['camera_frame_unavailable', 'room_not_rendered'].includes(result.error)) break;
      await new Promise((resolve) => setTimeout(resolve, ROOM_CAPTURE_RETRY_MS));
      if (stale()) return;
    }
    // The room could not be rendered here: save the camera-only fallback, labelled as such.
    if (result.error && !stale()) result = await capture.captureCameraOnly({ cameraVideo: video });
  }
  if (stale()) return;
  if (result.error || !result.blob || result.blob.type !== 'image/jpeg' || result.blob.size > 1500000) {
    snapshotStatus.textContent = ROOM_IMAGE_UNAVAILABLE;
    return;
  }
  snapshotAbort = new AbortController();
  for (let tries = 0; tries < 3; tries += 1) {
    if (stale()) return;
    try {
      const response = await fetch(`/api/interviews/${sessionId}/snapshot`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'image/jpeg', 'X-Capture-Kind': result.kind, 'X-CSRF-Token': csrfToken },
        body: result.blob, signal: snapshotAbort.signal,
      });
      if (response.ok) {
        snapshotStatus.textContent = result.kind === 'room'
          ? 'Room image saved.' : 'Camera image saved (room image unavailable).';
        return;
      }
      if (response.status !== 409 || tries === 2) break;
      await new Promise((resolve) => setTimeout(resolve, 200));
    } catch (error) {
      if (error.name === 'AbortError') return;
      break;
    }
  }
  if (currentAttempt === attempt && !sessionClosed) snapshotStatus.textContent = ROOM_IMAGE_UNAVAILABLE;
}

function handleEvent(message) {
  if (message.type !== 'rehearseai') return;
  const entry = message.event;
  if (!entry || !entry.event) return;
  if (terminalPending || sessionClosed && attempt > 0) return;
  if (entry.event === 'media_paused') {
    mediaPaused = true;
    mediaLossOverlay.hidden = false;
    resumeButton.disabled = !mediaSetup?.ready();
    setStatus('Paused');
    return;
  }
  if (entry.event === 'media_resumed') {
    mediaPaused = false;
    resumeButton.hidden = true;
    mediaLossOverlay.hidden = true;
    guidance.textContent = 'Continue when you’re ready.';
    setStatus('Listening');
    return;
  }

  if (entry.event === 'session_started') {
    document.querySelector('#session-id').textContent = `Session ${entry.session_id}`;
    startTimer();
    activeSessionId = entry.session_id;
    scheduleRoomCapture(entry.session_id, ROOM_CAPTURE_FALLBACK_MS);
  } else if (entry.event === 'candidate_started') {
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'candidate_turn_completed') {
    if (errorLabel.hidden) setStatus('Preparing');
  } else if (entry.event === 'agent_prepared') {
    prepared.set(entry.utterance_id, entry);
    if (errorLabel.hidden) setStatus('Preparing');
  } else if (entry.event === 'agent_output_started') {
    clearWarning();
    const pending = prepared.get(entry.utterance_id);
    if (!pending || startedUtterances.has(entry.utterance_id)) return;
    startedUtterances.add(entry.utterance_id);
    if (pending.utterance_kind === 'question') {
      setInterviewPhase('Current question', pending.text, 'Take your time with your answer.');
      questionCount.textContent = `${String(pending.question_number).padStart(2, '0')} / ${String(questionTotal).padStart(2, '0')}`;
      questionCount.hidden = false;
      setProgress(pending.question_number);
      // The room now shows a real question: take the image once it has painted.
      if (activeSessionId && !snapshotAttempted) scheduleRoomCapture(activeSessionId, ROOM_CAPTURE_SETTLE_MS);
    }
    setStatus('Speaking');
  } else if (entry.event === 'agent_interrupted') {
    prepared.delete(entry.utterance_id);
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'agent_delivery_reported') {
    prepared.delete(entry.utterance_id);
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'awaiting_continuation') {
    guidance.textContent = 'Take your time; keep going when you’re ready.';
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'interview_complete') {
    clearCaptureTimer();
    setInterviewPhase('Interview complete', 'That wraps up your interview.', 'Finishing the closing audio.');
    questionCount.hidden = true;
    setProgress(questionTotal, true);
    scheduleAutomaticEnd();
  } else if (entry.event === 'interview_ended_early') {
    clearCaptureTimer();
    setInterviewPhase('Ended early', 'Interview ended early.', 'Finishing the closing audio.');
    questionCount.hidden = true;
    scheduleAutomaticEnd();
  } else if (entry.event === 'clarification_limit') {
    guidance.textContent = 'Answer the current question, or say “end the interview” to stop.';
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'opening_repeat_limit') {
    guidance.textContent = entry.opening_phase === 'greeting'
      ? 'Tell RehearseAI how you’re doing to continue.'
      : 'Say “start” when ready, or “end the interview” to stop.';
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'opening_waiting_for_readiness') {
    guidance.textContent = 'Say “start” when ready, or “end the interview” to stop.';
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'no_project_waiting') {
    guidance.textContent = 'Return with a technical example, or say “end the interview”.';
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'no_project_repeat_limit' || entry.event === 'project_example_required') {
    guidance.textContent = 'Use a technical example, or say “end the interview”.';
    if (errorLabel.hidden) setStatus('Listening');
  } else if (entry.event === 'persistence_error') {
    showError('This interview is not being saved. You may continue, but check the server before relying on its record.');
  } else if (entry.event === 'response_error') {
    if (entry.error_code === 'tts_context_no_audio') {
      showWarning('The voice service returned no audio. You can speak to retry the prompt.');
    } else if (entry.failure_stage === 'opening_inference' || entry.failure_stage === 'opening_intent') {
      showWarning('The coach is recovering from a planning error. You can keep speaking.');
    } else if (entry.failure_stage === 'pipeline' && ['OpenAITTSService', 'CartesiaTTSService'].includes(entry.processor)) {
      showError('The voice provider could not generate audio. Try speaking again; end the session if it persists.');
    } else {
      showError('The coach could not complete a response. Try speaking again or end the session.');
    }
  }
}

function finishSession({ manual = false } = {}) {
  if (sessionClosed) return;
  sessionClosed = true;
  terminalPending = true;
  mediaPaused = false;
  beginSent = false;
  attempt += 1;
  if (cleanupTimeout !== null) clearTimeout(cleanupTimeout);
  cleanupTimeout = null;
  stopTimer();
  offerAbort?.abort();
  clearCaptureTimer();
  activeSessionId = null;
  snapshotAbort?.abort();
  snapshotAbort = null;
  offerAbort = null;
  channel?.close();
  channel = null;
  peer?.close();
  peer = null;
  if (mediaSetup) mediaSetup.cleanup();
  else microphone?.getTracks().forEach((track) => track.stop());
  microphone = null;
  audio.pause();
  audio.srcObject = null;
  startButton.disabled = true;
  startButton.hidden = true;
  returnButton.hidden = false;
  endButton.disabled = true;
  resumeButton.hidden = true;
  mediaLossOverlay.hidden = true;
  beginButton.hidden = false;
  workspace.hidden = false;
  if (manual) {
    setInterviewPhase('Ended', 'Session ended.');
    questionCount.hidden = true;
  } else {
    guidance.textContent = 'Your microphone is off. This interview attempt is closed.';
  }
  setStatus('Ended');
}

function endSession() { finishSession({ manual: true }); }

function sendMediaAction(action, reason) {
  if (channel?.readyState === 'open') {
    channel.send(JSON.stringify({ type: 'rehearseai_media', action, reason }));
    return true;
  }
  return false;
}

function mediaLost(kind) {
  if (sessionClosed || terminalPending) return;
  if (mediaPaused) {
    mediaLossReason.textContent = 'Restore all required media, then choose Resume interview.';
    return;
  }
  mediaPaused = true;
  snapshotAbort?.abort();
  guidance.textContent = `${kind} stopped. Restore it, then choose Resume interview.`;
  mediaLossReason.textContent = `${kind} stopped. Restore it, then choose Resume interview.`;
  mediaLossOverlay.hidden = false;
  resumeButton.hidden = false;
  resumeButton.disabled = true;
  setStatus('Paused');
  // Before Begin, readiness alone blocks the greeting; there is no server turn to pause.
  if (beginSent) sendMediaAction('pause', kind.toLowerCase().replaceAll(' ', '_'));
}

async function resumeInterview() {
  if (!mediaPaused || !mediaSetup?.ready() || sessionClosed) return;
  resumeButton.disabled = true;
  if (microphone !== mediaSetup.microphone) {
    const sender = peer?.getSenders().find((item) => item.track?.kind === 'audio');
    if (!sender) { showError('Microphone connection could not be restored. End and start again.'); resumeButton.disabled = false; return; }
    try {
      await sender.replaceTrack(mediaSetup.microphone.getAudioTracks()[0]);
    } catch {
      showError('Microphone connection could not be restored. End and start again.');
      resumeButton.disabled = false;
      return;
    }
    microphone = mediaSetup.microphone;
  }
  if (!channel || channel.readyState !== 'open') {
    mediaLossReason.textContent = 'Voice connection is not ready. Wait a moment and try Resume, or end the interview.';
    resumeButton.disabled = false;
    return;
  }
  if (beginSent) {
    sendMediaAction('resume');
    guidance.textContent = 'Restoring the voice turn. Wait for Listening before continuing.';
  } else {
    beginSent = sendMediaAction('begin');
    mediaPaused = false;
    resumeButton.hidden = true;
    mediaLossOverlay.hidden = true;
    guidance.textContent = 'Continue when you’re ready.';
    setStatus('Listening');
  }
}

function showSetup() {
  if (!sessionClosed) return;
  closeRoom();
  setupPanel.hidden = false;
  beginButton.hidden = false;
  beginButton.focus();
  mediaLossOverlay.hidden = true;
  startButton.hidden = true;
  endButton.disabled = true;
  setStatus('Ready');
  mediaSetup?.update();
}

function scheduleAutomaticEnd() {
  terminalPending = true;
  setStatus('Finishing');
  const endingAttempt = attempt;
  // Output-stop is server-side. Allow a bounded interval for browser playout to drain.
  cleanupTimeout = setTimeout(() => {
    if (endingAttempt === attempt) finishSession();
  }, 1200);
}

async function waitForIce(pc) {
  if (pc.iceGatheringState === 'complete') return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pc.removeEventListener('icegatheringstatechange', check);
      reject(new Error('WebRTC setup timed out.'));
    }, 12000);
    function check() {
      if (pc.iceGatheringState === 'complete') {
        clearTimeout(timeout);
        pc.removeEventListener('icegatheringstatechange', check);
        resolve();
      }
    }
    pc.addEventListener('icegatheringstatechange', check);
  });
}

async function startSession() {
  if (!sessionClosed || !mediaSetup?.ready() || !snapshotConsent.checked || !invitationReady) return;
  const thisAttempt = ++attempt;
  sessionClosed = false;
  terminalPending = false;
  mediaPaused = false;
  beginSent = false;
  stopTimer();
  elapsed.textContent = '00:00';
  errorLabel.hidden = true;
  setupError.hidden = true;
  delete errorLabel.dataset.kind;
  snapshotStatus.textContent = '';
  snapshotAttempted = false;
  clearCaptureTimer();
  activeSessionId = null;
  prepared.clear();
  startedUtterances.clear();
  document.querySelector('#session-id').textContent = 'Connecting to a session';
  setInterviewPhase('Opening', 'Your assigned interview will begin shortly.');
  questionCount.hidden = true;
  setProgress();
  startButton.disabled = true;
  startButton.hidden = true;
  returnButton.hidden = true;
  endButton.disabled = false;
  openRoom();
  mediaSetup.showRoomPreviews();
  mediaLossOverlay.hidden = true;
  resumeButton.hidden = true;
  beginButton.hidden = false;
  setStatus('Connecting');

  try {
    if (!await checkAccount()) return;
    if (!mediaSetup.ready()) throw new Error('A required device stopped. Restore it in setup before beginning.');
    microphone = mediaSetup.microphone;
    setStatus('Connecting');
    peer = new RTCPeerConnection();
    microphone.getAudioTracks().forEach((track) => peer.addTrack(track, microphone));
    channel = peer.createDataChannel('rehearseai');
    const activeChannel = channel;
    channel.onopen = () => {
      if (thisAttempt !== attempt || mediaPaused || !mediaSetup.ready()) return;
      beginSent = sendMediaAction('begin');
    };
    channel.onmessage = (event) => {
      if (thisAttempt !== attempt) return;
      try { handleEvent(JSON.parse(event.data)); } catch { /* Ignore unrelated data. */ }
    };
    channel.onclose = () => {
      if (thisAttempt === attempt && channel === activeChannel && peer) {
        endSession();
        showError('The session connection closed. Check your connection, then start again.');
      }
    };
    peer.ontrack = (event) => {
      if (thisAttempt !== attempt) return;
      audio.srcObject = event.streams[0] || new MediaStream([event.track]);
      audio.play().catch(() => {
        if (thisAttempt === attempt) {
          showError('Audio playback was blocked. Allow audio in your browser and retry.');
        }
      });
    };
    peer.onconnectionstatechange = () => {
      if (thisAttempt !== attempt || !peer) return;
      if (peer.connectionState === 'connected' && connectionLabel.textContent === 'Connecting') {
        setStatus('Listening');
      }
      if (['failed', 'disconnected'].includes(peer.connectionState)) {
        endSession();
        showError('Connection lost. Check your connection, then start again.');
      }
    };
    await peer.setLocalDescription(await peer.createOffer());
    if (thisAttempt !== attempt) return;
    await waitForIce(peer);
    if (thisAttempt !== attempt) return;
    offerAbort = new AbortController();
    const csrfResponse = await fetch('/api/csrf', { credentials: 'same-origin' });
    if (!csrfResponse.ok) throw new Error('Could not authorize the interview connection.');
    csrfToken = (await csrfResponse.json()).token;
    const response = await fetch('/api/offer', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ sdp: peer.localDescription.sdp, type: peer.localDescription.type, snapshot_acknowledged: snapshotConsent.checked }),
      signal: offerAbort.signal,
    });
    if (thisAttempt !== attempt) return;
    if (response.status === 401) {
      endSession();
      location.assign('/candidate/welcome');
      return;
    }
    if (response.status === 403) throw new Error('Secure interview setup was rejected. Refresh the page and try again.');
    if (response.status === 409) {
      let reason = '';
      try { reason = (await response.json()).detail; } catch { /* no detail */ }
      throw new Error(typeof reason === 'string' && reason ? reason : 'The interview cannot begin right now.');
    }
    if (!response.ok) throw new Error('The local voice server rejected the connection.');
    const answer = await response.json();
    if (thisAttempt !== attempt) return;
    await peer.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    offerAbort = null;
  } catch (error) {
    if (thisAttempt !== attempt) return;
    endSession();
    showError(error.name === 'NotAllowedError'
      ? 'Media permission was denied. Allow access in your browser and try again.'
      : error.message);
  }
}

startButton.addEventListener('click', showSetup);
returnButton.addEventListener('click', () => { closeRoom(); location.assign('/candidate/welcome'); });
workspace.addEventListener('cancel', (event) => event.preventDefault());
beginButton.addEventListener('click', startSession);
resumeButton.addEventListener('click', resumeInterview);
endButton.addEventListener('click', endSession);
window.addEventListener('DOMContentLoaded', () => {
  mediaSetup = new window.RehearseMediaSetup({
    onChange: (ready) => {
      beginButton.disabled = !ready || !sessionClosed || !snapshotConsent.checked || !invitationReady;
      if (mediaPaused) resumeButton.disabled = !ready;
      updateMediaLabels();
    },
    onLoss: mediaLost,
  });
  if (!mediaSetup.supported()) {
    document.querySelector('#setup-message').textContent = 'Screen sharing requires a supported desktop browser on localhost or HTTPS. This device cannot begin the interview.';
  }
  document.querySelector('#enable-devices').addEventListener('click', () => mediaSetup.enableDevices());
  document.querySelector('#share-screen').addEventListener('click', () => mediaSetup.shareScreen());
  document.querySelector('#test-speaker').addEventListener('click', () => mediaSetup.testSpeaker());
  document.querySelector('#confirm-speaker').addEventListener('click', () => mediaSetup.confirmSpeaker());
  snapshotConsent.addEventListener('change', () => mediaSetup.update());
  document.querySelector('#restore-devices').addEventListener('click', () => mediaSetup.enableDevices());
  document.querySelector('#restore-screen').addEventListener('click', () => mediaSetup.shareScreen());
  document.querySelector('#cancel-setup').addEventListener('click', () => mediaSetup.cleanup());
  checkAccount().catch((error) => showError(error.message));
});
window.addEventListener('pagehide', () => { endSession(); mediaSetup?.cleanup(); });

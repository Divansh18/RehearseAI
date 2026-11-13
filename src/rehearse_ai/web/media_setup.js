class RehearseMediaSetup {
  constructor({ onChange, onLoss }) {
    this.onChange = onChange;
    this.onLoss = onLoss;
    this.microphone = null;
    this.camera = null;
    this.screen = null;
    this.speakerTested = false;
    this.speakerTonePlayed = false;
    this.generation = 0;
    this.requestingDevices = false;
    this.requestingScreen = false;
    this.meterContext = null;
    this.meterFrame = null;
    this.speakerContext = null;
    this.cameraPreview = document.querySelector('#camera-preview');
    this.screenPreview = document.querySelector('#screen-preview');
    this.level = document.querySelector('#mic-level');
    this.message = document.querySelector('#setup-message');
    for (const preview of [this.cameraPreview, this.screenPreview]) {
      preview.addEventListener('loadeddata', () => this.update());
    }
    this.update();
  }

  supported() {
    return window.isSecureContext && !!navigator.mediaDevices?.getUserMedia
      && !!navigator.mediaDevices?.getDisplayMedia;
  }

  live(stream, kind) {
    return !!stream?.getTracks().some((track) => track.kind === kind && track.readyState === 'live');
  }

  ready() {
    return this.supported() && this.live(this.microphone, 'audio')
      && this.live(this.camera, 'video') && this.cameraPreview.readyState >= 2
      && this.live(this.screen, 'video') && this.screenPreview.readyState >= 2
      && this.speakerTested;
  }

  update() {
    const checks = {
      microphone: this.live(this.microphone, 'audio'),
      camera: this.live(this.camera, 'video') && this.cameraPreview.readyState >= 2,
      screen: this.live(this.screen, 'video') && this.screenPreview.readyState >= 2,
      speaker: this.speakerTested,
    };
    for (const [name, passed] of Object.entries(checks)) {
      const item = document.querySelector(`#check-${name}`);
      item.dataset.ready = String(passed);
      item.querySelector('span').textContent = passed ? 'Ready' : 'Needed';
    }
    this.onChange(this.ready());
  }

  describe(error, kind) {
    if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
      return kind === 'screen' ? 'Screen selection was cancelled or blocked. Try Share screen again; if this is a mobile browser, use a supported desktop browser.'
        : 'Camera or microphone permission was denied. Check browser permissions, then try again.';
    }
    if (error.name === 'NotFoundError') return `No ${kind === 'screen' ? 'shareable screen' : 'camera or microphone'} was found.`;
    if (error.name === 'NotReadableError') return 'The device is busy or unavailable. Close other apps using it and try again.';
    if (error.name === 'InvalidStateError') return 'Keep this tab active and use the Share screen button again.';
    return `Could not start ${kind === 'screen' ? 'screen sharing' : 'camera and microphone'}. Check device access and try again.`;
  }

  watch(stream, kind) {
    for (const track of stream.getTracks()) {
      track.addEventListener('ended', () => {
        if ([this.microphone, this.camera, this.screen].includes(stream)) {
          this.message.textContent = `${kind} stopped. Restore it to continue.`;
          this.onLoss(kind);
          this.update();
        }
      });
    }
  }

  async enableDevices() {
    if (this.requestingDevices) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      this.message.textContent = 'Camera and microphone require a supported browser on localhost or HTTPS.';
      return;
    }
    const audioNeeded = !this.live(this.microphone, 'audio');
    const videoNeeded = !this.live(this.camera, 'video');
    if (!audioNeeded && !videoNeeded) return;
    this.requestingDevices = true;
    const generation = this.generation;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: audioNeeded ? { echoCancellation: true, noiseSuppression: true } : false,
        video: videoNeeded,
      });
      if (generation !== this.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      if (audioNeeded) {
        this.microphone?.getTracks().forEach((track) => track.stop());
        this.microphone = new MediaStream(stream.getAudioTracks());
        this.watch(this.microphone, 'Microphone');
        this.startMeter();
      }
      if (videoNeeded) {
        this.camera?.getVideoTracks().forEach((track) => track.stop());
        this.camera = new MediaStream(stream.getVideoTracks());
        this.watch(this.camera, 'Camera');
        this.cameraPreview.srcObject = this.camera;
        this.cameraPreview.play().catch(() => {});
      }
      this.message.textContent = 'Camera and microphone are previewed locally.';
      this.update();
    } catch (error) {
      if (generation === this.generation) this.message.textContent = this.describe(error, 'devices');
    } finally { this.requestingDevices = false; }
  }

  async shareScreen() {
    if (this.requestingScreen) return;
    if (!this.supported()) {
      this.message.textContent = 'Screen sharing is unavailable here. Use a supported desktop browser on localhost or HTTPS.';
      return;
    }
    this.requestingScreen = true;
    const generation = this.generation;
    try {
      // Keep this call directly in the button action: the browser requires user activation.
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
      if (generation !== this.generation) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.screen?.getTracks().forEach((track) => track.stop());
      this.screen = stream;
      this.screenPreview.srcObject = stream;
      this.screenPreview.play().catch(() => {});
      this.watch(stream, 'Screen sharing');
      this.message.textContent = 'Shared screen is previewed only in this browser.';
      this.update();
    } catch (error) {
      if (generation === this.generation) this.message.textContent = this.describe(error, 'screen');
    } finally { this.requestingScreen = false; }
  }

  async testSpeaker() {
    if (this.speakerContext) return;
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) { this.message.textContent = 'Speaker testing is unavailable in this browser.'; return; }
    const context = new Context();
    this.speakerContext = context;
    try {
      await context.resume();
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = 440;
      gain.gain.value = 0.08;
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.25);
      oscillator.onended = () => {
        if (this.speakerContext === context) this.speakerContext = null;
        context.close();
      };
      this.speakerTonePlayed = true;
      document.querySelector('#confirm-speaker').disabled = false;
      this.message.textContent = 'Did you hear the test tone? Confirm below.';
      this.update();
    } catch {
      await context.close();
      this.message.textContent = 'Speaker test failed. Check your output device and try again.';
      this.speakerContext = null;
    }
  }

  confirmSpeaker() {
    if (!this.speakerTonePlayed) return;
    this.speakerTested = true;
    this.message.textContent = 'Speaker confirmed.';
    this.update();
  }

  startMeter() {
    if (this.meterFrame !== null) cancelAnimationFrame(this.meterFrame);
    this.meterContext?.close();
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context || !this.microphone) return;
    this.meterContext = new Context();
    const analyser = this.meterContext.createAnalyser();
    analyser.fftSize = 256;
    this.meterContext.createMediaStreamSource(this.microphone).connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    const tick = () => {
      analyser.getByteTimeDomainData(samples);
      const power = Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length);
      this.level.value = Math.min(1, power * 5);
      this.meterFrame = requestAnimationFrame(tick);
    };
    tick();
  }

  cleanup() {
    this.generation += 1;
    const tracks = new Set([this.microphone, this.camera, this.screen].flatMap((stream) => stream?.getTracks() || []));
    tracks.forEach((track) => track.stop());
    this.microphone = this.camera = this.screen = null;
    if (this.meterFrame !== null) cancelAnimationFrame(this.meterFrame);
    this.meterFrame = null;
    this.meterContext?.close();
    this.meterContext = null;
    this.speakerContext?.close();
    this.speakerContext = null;
    this.level.value = 0;
    this.cameraPreview.srcObject = null;
    this.screenPreview.srcObject = null;
    this.speakerTested = false;
    this.speakerTonePlayed = false;
    document.querySelector('#confirm-speaker').disabled = true;
    this.update();
  }
}

window.RehearseMediaSetup = RehearseMediaSetup;

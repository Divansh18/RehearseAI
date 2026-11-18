/*
 * One image of the interview room, for the admin-only Conversation PDF.
 *
 * What this is: the room's own DOM and stylesheets rendered by the browser (an SVG
 * <foreignObject> rasterised to a canvas), with the current frame of the candidate's camera
 * substituted for the camera <video>. It is NOT an operating-system screenshot and NOT proof of
 * what the screen looked like: it is a browser-rendered picture of the room, and the server stores
 * it labelled as such.
 *
 * Why not a real screenshot: the only pixel-exact option is getDisplayMedia() on the current tab,
 * which needs a second screen-sharing permission prompt. That is deliberately not requested here.
 *
 * What it never touches: the shared-screen preview. Its <video> is replaced in the copy by a
 * neutral label before anything is drawn, so those pixels are never read, drawn or uploaded.
 * Devices are never re-acquired; the already-running camera video element is only read.
 */
(() => {
  const MAX_OUTPUT_WIDTH = 1280;
  const MAX_OUTPUT_HEIGHT = 2000;
  const MAX_SCALE = 2;
  const JPEG_QUALITY = 0.85;

  function cameraFrame(video) {
    if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return null;
    const scale = Math.min(1, 960 / video.videoWidth);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  // A frame that is entirely one colour (a black or not-yet-started camera) is not a valid frame.
  function looksBlank(canvas) {
    const probe = document.createElement('canvas');
    probe.width = 32;
    probe.height = 18;
    const context = probe.getContext('2d');
    context.drawImage(canvas, 0, 0, probe.width, probe.height);
    const data = context.getImageData(0, 0, probe.width, probe.height).data;
    let sum = 0;
    let sumSquares = 0;
    const count = data.length / 4;
    for (let i = 0; i < data.length; i += 4) {
      const luma = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      sum += luma;
      sumSquares += luma * luma;
    }
    const mean = sum / count;
    const variance = sumSquares / count - mean * mean;
    return variance < 4;
  }

  function stylesheetText() {
    const parts = [];
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        for (const rule of Array.from(sheet.cssRules)) {
          if (rule.type !== CSSRule.IMPORT_RULE) parts.push(rule.cssText);
        }
      } catch {
        /* a stylesheet we cannot read is simply left out */
      }
    }
    return parts.join('\n');
  }

  function neutralScreenLabel(original, label) {
    const box = document.createElement('div');
    box.id = original.id;
    box.className = original.className;
    box.textContent = label;
    box.setAttribute(
      'style',
      'display:flex;align-items:center;justify-content:center;text-align:center;width:96px;'
        + 'height:38px;flex:none;padding:2px 4px;font-size:9px;line-height:1.2;color:#b3c1ca;'
        + 'border:1px solid #354854;border-radius:5px;background:#071016;',
    );
    return box;
  }

  function wholeRoomClone(dialog, cameraVideo, frame, screenLabel, rect, height) {
    const clone = dialog.cloneNode(true);
    clone.removeAttribute('hidden');
    clone.setAttribute('open', '');
    const force = (name, value) => clone.style.setProperty(name, value, 'important');
    force('position', 'absolute');
    force('inset', 'auto');
    force('left', `${rect.left}px`);
    force('top', `${rect.top}px`);
    force('margin', '0');
    force('width', `${rect.width}px`);
    force('height', `${height}px`);
    force('max-height', 'none');
    force('overflow', 'visible');
    force('transform', 'none');
    force('animation', 'none');
    // The candidate's camera: the frame as it is right now, mirrored by the room's own CSS rule.
    const camera = clone.querySelector('#room-camera-preview');
    if (camera) {
      const picture = document.createElement('img');
      picture.id = camera.id;
      picture.className = camera.className;
      picture.setAttribute('alt', '');
      picture.setAttribute('src', frame.toDataURL('image/jpeg', 0.92));
      // The room styles <video>, not <img>: carry over exactly how the live video is fitted.
      const live = getComputedStyle(cameraVideo);
      for (const name of ['display', 'width', 'height', 'object-fit', 'object-position', 'border-radius']) {
        picture.style.setProperty(name, live.getPropertyValue(name));
      }
      camera.replaceWith(picture);
    }
    // The shared screen is never captured: neutral label in its place.
    const screen = clone.querySelector('#room-screen-preview');
    if (screen) screen.replaceWith(neutralScreenLabel(screen, screenLabel));
    // Controls in the copy are inert pictures, never live elements.
    clone.querySelectorAll('button, a, input, details').forEach((element) => {
      element.setAttribute('tabindex', '-1');
    });
    return clone;
  }

  async function renderRoom({ dialog, cameraVideo, screenActive }) {
    const frame = cameraFrame(cameraVideo);
    if (!frame || looksBlank(frame)) return { error: 'camera_frame_unavailable' };
    const rect = dialog.getBoundingClientRect();
    if (!rect.width || !rect.height) return { error: 'room_not_rendered' };
    const height = Math.max(rect.height, dialog.scrollHeight + (dialog.offsetHeight - dialog.clientHeight));
    const viewportWidth = Math.max(window.innerWidth, Math.ceil(rect.left + rect.width));
    const viewportHeight = Math.max(window.innerHeight, Math.ceil(rect.top + height));
    const clone = wholeRoomClone(
      dialog,
      cameraVideo,
      frame,
      screenActive ? 'Screen sharing active' : 'Not sharing',
      rect,
      height,
    );
    const bodyStyle = getComputedStyle(document.body);
    const markup = new XMLSerializer().serializeToString(clone);
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${viewportWidth}" height="${viewportHeight}">`
      + '<foreignObject x="0" y="0" width="100%" height="100%">'
      + `<div xmlns="http://www.w3.org/1999/xhtml" style="position:relative;width:${viewportWidth}px;`
      + `height:${viewportHeight}px;overflow:hidden;color:${bodyStyle.color};`
      + `font-family:${bodyStyle.fontFamily.replace(/"/g, "'")};line-height:${bodyStyle.lineHeight};`
      + `background:${bodyStyle.backgroundColor};">`
      + `<style>${stylesheetText().replace(/<\/style/gi, '<\\/style')}</style>${markup}</div>`
      + '</foreignObject></svg>';
    const image = new Image();
    image.decoding = 'async';
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    await image.decode();
    const scale = Math.min(MAX_SCALE, MAX_OUTPUT_WIDTH / rect.width, MAX_OUTPUT_HEIGHT / height);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(rect.width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const context = canvas.getContext('2d');
    context.fillStyle = bodyStyle.backgroundColor;
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(
      image, rect.left, rect.top, rect.width, height, 0, 0, canvas.width, canvas.height,
    );
    return { canvas };
  }

  /** The room image as a JPEG blob, or { error } describing why it could not be made. */
  async function captureRoom(options) {
    try {
      const result = await renderRoom(options);
      if (result.error) return result;
      const blob = await new Promise((resolve) => {
        try { result.canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY); } catch { resolve(null); }
      });
      if (!blob || blob.type !== 'image/jpeg') return { error: 'encode_failed' };
      return { blob, kind: 'room', width: result.canvas.width, height: result.canvas.height };
    } catch (error) {
      // Some browsers refuse to read back a rasterised SVG; the caller then uses the fallback.
      return { error: error?.name === 'SecurityError' ? 'raster_blocked' : 'raster_failed' };
    }
  }

  /** The labelled fallback: the camera frame alone. It is never presented as the room. */
  async function captureCameraOnly({ cameraVideo }) {
    const frame = cameraFrame(cameraVideo);
    if (!frame || looksBlank(frame)) return { error: 'camera_frame_unavailable' };
    const scale = Math.min(1, 640 / frame.width, 360 / frame.height);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(frame.width * scale));
    canvas.height = Math.max(1, Math.round(frame.height * scale));
    canvas.getContext('2d').drawImage(frame, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.78));
    return blob ? { blob, kind: 'camera' } : { error: 'encode_failed' };
  }

  window.RehearseRoomCapture = { captureRoom, captureCameraOnly, looksBlank };
})();

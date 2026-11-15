import { BYTES_PER_SECOND, HEIGHT, MAX_FRAMES, WIDTH, buildStream, pickEven, rgb565ToRgba, rgb565be } from "./dlx.js";
import { UploadError, bluetoothAvailable, pickScreen, upload } from "./ble.js";

const $ = (id) => document.getElementById(id);
const MAX_DECODE = 600;          // frames kept in memory from very long GIFs
const DECODE_BUDGET = 1e9;       // bytes of decoded frames kept in memory
const MS_PER_SPEED_STEP = 15;   // preview only; the keyboard's real unit is unknown

const state = {
  name: "",
  frames: [],          // ImageBitmap per source frame
  w: 0, h: 0,
  trimStart: 0, trimEnd: 0,
  zoom: 1, cx: 0, cy: 0,
  count: MAX_FRAMES, speed: 5,
  countAuto: true,     // follow the most frames the trim allows until the user picks fewer
  countWanted: MAX_FRAMES,   // what the user picked, kept while trimming narrows the range
  prepared: [],        // RGB565 frames for the current settings
  busy: false,
};

const animated = () => state.frames.length > 1;
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ------------------------------------------------------------ decoding --
async function decodeFile(file) {
  if (animatedType(file.type) && "ImageDecoder" in window) {
    const decoder = new ImageDecoder({ data: await file.arrayBuffer(), type: file.type });
    await decoder.tracks.ready;
    await decoder.completed;
    const total = decoder.tracks.selectedTrack.frameCount;
    // Large frames: keep fewer of them, so a long full-HD GIF can't exhaust the tab's memory.
    const first = (await decoder.decode({ frameIndex: 0 })).image;
    const perFrame = first.displayWidth * first.displayHeight * 4;
    first.close();
    const limit = Math.max(MAX_FRAMES, Math.min(MAX_DECODE, Math.floor(DECODE_BUDGET / perFrame)));
    const keep = pickEven([...Array(total).keys()], limit);
    const frames = [];
    for (const i of keep) {
      const { image } = await decoder.decode({ frameIndex: i });
      frames.push(await createImageBitmap(image));
      image.close();
    }
    decoder.close();
    return { frames, total };
  }
  return { frames: [await createImageBitmap(file)], total: 1 };
}
const animatedType = (t) => ["image/gif", "image/webp", "image/png", "image/apng"].includes(t);

async function loadFile(file) {
  if (!file || !file.type.startsWith("image/")) return showMessage("error", "That isn't an image. Choose a GIF, PNG, JPEG or WebP file.");
  hideMessage();
  $("dropText").textContent = `Reading ${file.name}…`;
  let decoded;
  try {
    decoded = await decodeFile(file);
  } catch (e) {
    $("dropText").textContent = "Choose a file, or drop it here";
    return showMessage("error", `Couldn't read ${escapeHtml(file.name)}: ${escapeHtml(e.message)}`);
  }
  state.frames.forEach((b) => b.close?.());
  stripThumbs = null;
  Object.assign(state, {
    name: file.name, frames: decoded.frames,
    w: decoded.frames[0].width, h: decoded.frames[0].height,
    trimStart: 0, trimEnd: decoded.frames.length - 1, zoom: 1,
  });
  state.cx = state.w / 2; state.cy = state.h / 2;
  state.countAuto = true;   // as many frames as fit, for the best chance of the whole GIF

  const note = decoded.total > decoded.frames.length ? ` (${decoded.frames.length} of ${decoded.total} frames kept)` : "";
  $("dropText").textContent = `${file.name}${animated() ? ` · ${decoded.total} frames` : ""}${note}`;
  syncControls();
  update();
}

// ------------------------------------------------------------- cropping --
function cropBox() {
  const aspect = WIDTH / HEIGHT;
  let bw = state.w, bh = state.w / aspect;
  if (bh > state.h) { bh = state.h; bw = state.h * aspect; }
  bw /= state.zoom; bh /= state.zoom;
  const cx = Math.min(Math.max(state.cx, bw / 2), state.w - bw / 2);
  const cy = Math.min(Math.max(state.cy, bh / 2), state.h - bh / 2);
  state.cx = cx; state.cy = cy;
  return { x: cx - bw / 2, y: cy - bh / 2, w: bw, h: bh };
}

const cropCanvas = $("cropCanvas");
const cctx = cropCanvas.getContext("2d");
let view = { s: 1, ox: 0, oy: 0 };

function drawCropper() {
  const W = cropCanvas.width, H = cropCanvas.height;
  cctx.fillStyle = "#1c1f26";
  cctx.fillRect(0, 0, W, H);
  if (!state.frames.length) return;
  const s = Math.min(W / state.w, H / state.h);
  view = { s, ox: (W - state.w * s) / 2, oy: (H - state.h * s) / 2 };
  const src = state.frames[state.trimStart];
  cctx.imageSmoothingQuality = "high";
  cctx.drawImage(src, view.ox, view.oy, state.w * s, state.h * s);
  const b = cropBox();
  const bx = view.ox + b.x * s, by = view.oy + b.y * s, bw = b.w * s, bh = b.h * s;
  cctx.fillStyle = "rgba(10, 12, 16, 0.6)";
  cctx.beginPath();
  cctx.rect(0, 0, W, H);
  cctx.rect(bx, by, bw, bh);
  cctx.fill("evenodd");
  cctx.strokeStyle = "#ffffff";
  cctx.lineWidth = 2;
  cctx.strokeRect(bx + 1, by + 1, bw - 2, bh - 2);
  cctx.fillStyle = cssVar("--accent");
  for (const [x, y] of [[bx, by], [bx + bw, by], [bx, by + bh], [bx + bw, by + bh]]) {
    cctx.fillRect(x - 5, y - 5, 10, 10);
  }
}

let drag = null;
cropCanvas.addEventListener("pointerdown", (e) => {
  if (!state.frames.length) return;
  cropCanvas.setPointerCapture(e.pointerId);
  cropCanvas.classList.add("dragging");
  document.body.classList.add("cropping");
  drag = { x: e.clientX, y: e.clientY, cx: state.cx, cy: state.cy };
});
cropCanvas.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const k = cropCanvas.width / cropCanvas.getBoundingClientRect().width / view.s;
  state.cx = drag.cx + (e.clientX - drag.x) * k;
  state.cy = drag.cy + (e.clientY - drag.y) * k;
  drawCropper();
  schedulePrepare();
});
const endDrag = () => { drag = null; cropCanvas.classList.remove("dragging"); document.body.classList.remove("cropping"); };
cropCanvas.addEventListener("pointerup", endDrag);
cropCanvas.addEventListener("pointercancel", endDrag);
cropCanvas.addEventListener("wheel", (e) => {
  if (!state.frames.length) return;
  e.preventDefault();
  state.zoom = Math.min(4, Math.max(1, state.zoom * (e.deltaY < 0 ? 1.06 : 1 / 1.06)));
  syncControls();
  update();
}, { passive: false });

// --------------------------------------------------------- frame prep --
function selectedIndices() {
  if (!animated()) return [0];
  const range = [];
  for (let i = state.trimStart; i <= state.trimEnd; i++) range.push(i);
  return pickEven(range, state.count);
}

const work = Object.assign(document.createElement("canvas"), { width: WIDTH, height: HEIGHT });
const wctx = work.getContext("2d", { willReadFrequently: true });

function prepare() {
  const b = cropBox();
  wctx.imageSmoothingQuality = "high";
  state.prepared = selectedIndices().map((i) => {
    wctx.fillStyle = "#000";
    wctx.fillRect(0, 0, WIDTH, HEIGHT);
    wctx.drawImage(state.frames[i], b.x, b.y, b.w, b.h, 0, 0, WIDTH, HEIGHT);
    return rgb565be(wctx.getImageData(0, 0, WIDTH, HEIGHT).data);
  });
  previewFrames = state.prepared.map((f) => new ImageData(rgb565ToRgba(f), WIDTH, HEIGHT));
  updateReadout();
}

let prepTimer = 0;
function schedulePrepare() {
  clearTimeout(prepTimer);
  prepTimer = setTimeout(prepare, 60);
}

// -------------------------------------------------------------- preview --
const lcd = $("lcd").getContext("2d");
let previewFrames = [];
let tick = 0, lastStep = 0;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

function frameDelay() { return (11 - state.speed) * MS_PER_SPEED_STEP; }

function play(now) {
  if (previewFrames.length) {
    if (reducedMotion.matches) tick = 0;
    else if (now - lastStep >= frameDelay()) { tick = (tick + 1) % previewFrames.length; lastStep = now; }
    lcd.putImageData(previewFrames[Math.min(tick, previewFrames.length - 1)], 0, 0);
  }
  requestAnimationFrame(play);
}
requestAnimationFrame(play);

// ------------------------------------------------------------ readouts --
function streamBytes(n) { return 8 + 17 + 352 + 16 + 84 + 4 * n + n * WIDTH * HEIGHT * 2 + 4; }

function updateReadout() {
  const n = state.prepared.length;
  if (!n) return;
  const sent = n === 1 ? 2 : n;
  const bytes = streamBytes(sent);
  $("roFrames").textContent = n === 1 ? "Still" : String(n);
  $("roLoop").textContent = n === 1 ? "–" : `≈ ${(n * frameDelay() / 1000).toFixed(1)} s`;
  $("roSize").textContent = bytes > 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
  $("roTime").textContent = formatTime(bytes / BYTES_PER_SECOND);
}

const formatTime = (s) => (s < 90 ? `≈ ${Math.max(15, Math.round(s / 5) * 5)} s` : `≈ ${Math.round(s / 60 * 2) / 2} min`);

// ------------------------------------------------------------- controls --
function syncControls() {
  const last = Math.max(0, state.frames.length - 1);
  for (const id of ["trimStart", "trimEnd"]) $(id).max = last;
  $("trimStart").value = state.trimStart;
  $("trimEnd").value = state.trimEnd;
  $("trimStartOut").textContent = state.trimStart + 1;
  $("trimEndOut").textContent = state.trimEnd + 1;

  const span = state.trimEnd - state.trimStart + 1;
  const maxCount = state.frames.length ? Math.max(2, Math.min(MAX_FRAMES, span)) : MAX_FRAMES;
  $("count").max = maxCount;
  state.count = state.countAuto ? maxCount : Math.min(state.countWanted, maxCount);
  $("count").value = state.count;
  $("countOut").textContent = state.count;
  $("speed").value = state.speed;
  $("speedOut").textContent = state.speed;
  $("zoom").value = state.zoom;
  $("zoomOut").textContent = `${state.zoom.toFixed(1)}×`;

  document.querySelectorAll("[data-needs]").forEach((el) => {
    const need = el.dataset.needs;
    const on = need === "file" ? state.frames.length > 0 : animated();
    el.classList.toggle("off", !on);
    el.querySelectorAll("input, button").forEach((c) => { if (!c.closest("#stepSend")) c.disabled = !on; });
  });
  $("lcdEmpty").hidden = state.frames.length > 0;
  updateSendButton();
  drawStrip();
}

// ----------------------------------------------------------- trim strip --
// A row of thumbnails across the whole clip; the kept range is outlined and
// its ends can be dragged. The range sliders stay the accessible control.
const strip = $("strip");
const sctx = strip.getContext("2d");
let stripThumbs = null;     // thumbnails rendered at the strip's current size

function sizeStrip() {
  const dpr = devicePixelRatio || 1;
  const w = Math.max(1, Math.round(strip.clientWidth * dpr)), h = Math.round(strip.clientHeight * dpr);
  if (strip.width !== w || strip.height !== h) { strip.width = w; strip.height = h; stripThumbs = null; }
}

function buildThumbs() {
  const W = strip.width, H = strip.height, len = state.frames.length;
  const c = Object.assign(document.createElement("canvas"), { width: W, height: H });
  const x = c.getContext("2d");
  const n = Math.max(1, Math.min(len, Math.round(W / (H * 1.1))));
  const gap = Math.round(devicePixelRatio || 1) * 2;
  const cw = (W - gap * (n - 1)) / n;
  x.fillStyle = cssVar("--card");                          // gaps between thumbnails
  x.fillRect(0, 0, W, H);
  x.imageSmoothingQuality = "high";
  for (let i = 0; i < n; i++) {
    const f = state.frames[Math.min(len - 1, Math.floor(((i + 0.5) / n) * len))];
    const s = Math.max(cw / f.width, H / f.height);       // cover the cell
    const sw = cw / s, sh = H / s;
    x.drawImage(f, (f.width - sw) / 2, (f.height - sh) / 2, sw, sh, i * (cw + gap), 0, cw, H);
  }
  return c;
}

function drawStrip() {
  sizeStrip();
  const W = strip.width, H = strip.height, dpr = devicePixelRatio || 1;
  sctx.clearRect(0, 0, W, H);
  if (!animated()) { sctx.fillStyle = cssVar("--key"); sctx.fillRect(0, 0, W, H); return; }
  stripThumbs ??= buildThumbs();
  sctx.drawImage(stripThumbs, 0, 0);
  const len = state.frames.length;
  const x0 = (state.trimStart / len) * W, x1 = ((state.trimEnd + 1) / len) * W;
  sctx.globalAlpha = 0.72;                                   // fade what's trimmed away
  sctx.fillStyle = cssVar("--bg");
  sctx.fillRect(0, 0, x0, H);
  sctx.fillRect(x1, 0, W - x1, H);
  sctx.globalAlpha = 1;
  const lw = 3 * dpr, accent = cssVar("--accent");
  sctx.strokeStyle = accent;
  sctx.lineWidth = lw;
  sctx.beginPath();
  sctx.roundRect(x0 + lw / 2, lw / 2, Math.max(lw, x1 - x0 - lw), H - lw, 8 * dpr);
  sctx.stroke();
  sctx.fillStyle = accent;                                   // grab handles
  for (const hx of [x0 + lw / 2, x1 - lw / 2]) {
    sctx.beginPath();
    sctx.roundRect(hx - 3 * dpr, H * 0.28, 6 * dpr, H * 0.44, 3 * dpr);
    sctx.fill();
  }
}

let stripDrag = null;
const frameAt = (e) => {
  const r = strip.getBoundingClientRect();
  return Math.min(state.frames.length - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * state.frames.length)));
};
function moveTrim(f) {
  if (stripDrag === "start") state.trimStart = Math.min(f, state.trimEnd);
  else state.trimEnd = Math.max(f, state.trimStart);
  syncControls(); drawCropper(); schedulePrepare();
}
strip.addEventListener("pointerdown", (e) => {
  if (!animated() || state.busy) return;
  strip.setPointerCapture(e.pointerId);
  const f = frameAt(e);
  stripDrag = Math.abs(f - state.trimStart) <= Math.abs(f - state.trimEnd) && f <= state.trimEnd ? "start" : "end";
  moveTrim(f);
});
strip.addEventListener("pointermove", (e) => { if (stripDrag) moveTrim(frameAt(e)); });
const endStripDrag = () => { stripDrag = null; };
strip.addEventListener("pointerup", endStripDrag);
strip.addEventListener("pointercancel", endStripDrag);
new ResizeObserver(() => drawStrip()).observe(strip);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { stripThumbs = null; drawStrip(); drawCropper(); });

function update() {
  drawCropper();
  prepare();
}

$("trimStart").addEventListener("input", (e) => {
  state.trimStart = Math.min(+e.target.value, state.trimEnd);
  syncControls(); drawCropper(); schedulePrepare();
});
$("trimEnd").addEventListener("input", (e) => {
  state.trimEnd = Math.max(+e.target.value, state.trimStart);
  syncControls(); schedulePrepare();
});
$("count").addEventListener("input", (e) => {
  state.count = state.countWanted = +e.target.value;
  state.countAuto = state.count >= +e.target.max;   // back at the top: follow the trim again
  syncControls(); schedulePrepare();
});
$("speed").addEventListener("input", (e) => { state.speed = +e.target.value; syncControls(); updateReadout(); });
$("zoom").addEventListener("input", (e) => { state.zoom = +e.target.value; syncControls(); drawCropper(); schedulePrepare(); });
$("recenter").addEventListener("click", () => { state.cx = state.w / 2; state.cy = state.h / 2; state.zoom = 1; syncControls(); update(); });

$("file").addEventListener("change", (e) => loadFile(e.target.files[0]));
let dragDepth = 0;
addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; document.body.classList.add("dragging"); });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
addEventListener("dragover", (e) => e.preventDefault());
addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (!state.busy) loadFile(e.dataTransfer.files[0]);
});

// ---------------------------------------------------------------- send --
function updateSendButton() {
  $("send").disabled = state.busy || !state.frames.length || !bluetoothAvailable();
}

function showMessage(kind, html) {
  const m = $("message");
  m.className = `message ${kind}`;
  m.innerHTML = html;
  m.hidden = false;
}
function hideMessage() { $("message").hidden = true; }

const guard = (e) => { e.preventDefault(); e.returnValue = ""; };

$("send").addEventListener("click", async () => {
  hideMessage();
  let device;
  try {
    device = await pickScreen();            // must run straight from the click
  } catch (e) {
    if (e.name !== "NotFoundError") showMessage("error", `Bluetooth: ${escapeHtml(e.message)}`);
    return;
  }
  clearTimeout(prepTimer);
  prepare();
  const stream = buildStream(state.prepared, state.speed);

  state.busy = true;
  updateSendButton();
  document.querySelectorAll(".step input, .step button").forEach((c) => { c.dataset.was = c.disabled; c.disabled = true; });
  $("progress").hidden = false;
  addEventListener("beforeunload", guard);
  let lock = null;
  try { lock = await navigator.wakeLock?.request("screen"); } catch { /* optional */ }

  const status = $("status"), bar = $("bar");
  try {
    const secs = await upload(device, stream, {
      onProgress: ({ phase, block, blocks, seconds }) => {
        bar.style.width = `${blocks ? (100 * block) / blocks : 0}%`;
        if (phase === "connecting") status.textContent = "Connecting to the keyboard…";
        else if (phase === "handshake") status.textContent = "Starting the upload…";
        else if (phase === "sending") {
          const left = Math.max(0, (seconds / block) * (blocks - block));
          status.textContent = `Sending block ${block} of ${blocks} · about ${formatTime(left).replace("≈ ", "")} left`;
        } else if (phase === "finishing") status.textContent = "Saving on the keyboard…";
      },
    });
    bar.style.width = "100%";
    status.textContent = `Sent in ${Math.round(secs)} s.`;
    showMessage("ok", "Done. If the screen doesn't show it, change page with <kbd>Fn</kbd> <kbd>PgUp</kbd> / <kbd>PgDn</kbd>.");
  } catch (e) {
    const after = e instanceof UploadError && e.afterControl2;
    status.textContent = "Upload stopped.";
    showMessage(after ? "warn" : "error", after
      ? `The upload stopped partway (${escapeHtml(e.message)}). The screen may look stuck: wait two minutes and send again. Is the keyboard on USB power? See “If an upload fails” below.`
      : `${escapeHtml(e.message)}`);
  } finally {
    removeEventListener("beforeunload", guard);
    lock?.release?.();
    state.busy = false;
    document.querySelectorAll(".step input, .step button").forEach((c) => { c.disabled = c.dataset.was === "true"; });
    syncControls();
  }
});

const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------ pinned preview --
// The preview sticks while the steps scroll. Pin it low enough that, at the
// bottom of the page, its card ends level with the last step's card.
const studio = document.querySelector(".studio"), stage = document.querySelector(".stage");
function pinStage() {
  const tail = document.documentElement.scrollHeight - (studio.getBoundingClientRect().bottom + scrollY);
  stage.style.setProperty("--pin", `${Math.max(24, innerHeight - tail - stage.offsetHeight)}px`);
}
new ResizeObserver(pinStage).observe(document.body);
addEventListener("resize", pinStage);

// ---------------------------------------------------------------- start --
if (!bluetoothAvailable() || !("ImageDecoder" in window)) $("unsupported").hidden = false;
syncControls();
drawCropper();

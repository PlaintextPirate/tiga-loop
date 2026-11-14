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
  prepared: [],        // RGB565 frames for the current settings
  busy: false,
};

const animated = () => state.frames.length > 1;

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
  Object.assign(state, {
    name: file.name, frames: decoded.frames,
    w: decoded.frames[0].width, h: decoded.frames[0].height,
    trimStart: 0, trimEnd: decoded.frames.length - 1, zoom: 1,
  });
  state.cx = state.w / 2; state.cy = state.h / 2;
  state.count = Math.min(Math.max(2, state.frames.length), MAX_FRAMES);   // as many as fit

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
  cctx.fillStyle = "#2a2e34";
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
  cctx.fillStyle = "#3150c4";
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
  const maxCount = Math.max(2, Math.min(MAX_FRAMES, span));
  $("count").max = maxCount;
  state.count = Math.min(state.count, maxCount);
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
}

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
$("count").addEventListener("input", (e) => { state.count = +e.target.value; syncControls(); schedulePrepare(); });
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

// ---------------------------------------------------------------- start --
if (!bluetoothAvailable() || !("ImageDecoder" in window)) $("unsupported").hidden = false;
syncControls();
drawCropper();

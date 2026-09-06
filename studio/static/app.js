"use strict";
// H3 Studio front end. No framework, no build step: the whole point is that
// this file can be read and fixed by whoever is standing in front of the Pod.

const $ = (id) => document.getElementById(id);

/* Japanese labels for values that come from the backend. Widely understood
   technical words (GPU, Pod, ComfyUI, seed, MP) are deliberately left as-is. */
const JP_STATE = { RUNNING: "起動中", EXITED: "停止中", STOPPED: "停止中", UNKNOWN: "不明" };
const JP_MODE = { image_trace: "画像から", motion_trace: "動きをまねる", prompt_cinema: "文章から" };
const JP_KIND = { draft: "下書き", final: "仕上げ" };
const state = {
  mode: "image_trace",
  quality: "draft",
  variations: 4,
  image: null,
  localPath: null,
  sourceVideo: null,
  sourceVideoMeta: null,
  refImageSize: "match",
  cfg: null,
  batch: null,       // current batch shown in RESULTS
  selected: null,
  busy: false,
  comfyReady: false,
  lastEncoder: {},   // mode -> {prompt, image, aspect_ratio, megapixels}
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

function notice(level, text, ttl = 9000) {
  const el = document.createElement("div");
  el.className = "notice " + level;
  el.textContent = text;
  $("notices").appendChild(el);
  if (ttl) setTimeout(() => el.remove(), ttl);
}

// ---------------------------------------------------------------- config
async function loadConfig() {
  const cfg = await api("/api/config");
  state.cfg = cfg;
  /* value = what ComfyUI accepts, text = what the operator reads. Never send
     the label: the node validates against its own option strings. */
  $("aspect").innerHTML = cfg.aspect_ratios
    .map((a) => `<option value="${a.value}">${a.label}</option>`).join("");

  const rs = cfg.reference_strength;
  $("refstrength").min = rs.min;
  $("refstrength").max = rs.max;
  $("refstrength").value = rs.default;
  $("ref-value").textContent = rs.default;
  /* null means "ComfyUI is not reachable, so we cannot tell" -- only an
     explicit false means the node really is missing from the Pod. */
  if (rs.available === false) {
    $("ref-unavailable").hidden = false;
    $("ref-unavailable").textContent =
      "この機能のノードが Pod に入っていないため、今は使えません。生成はそのまま行えます。";
    $("refstrength").disabled = true;
  }

  $("auto-stop").innerHTML = cfg.auto_stop_choices
    .map((m) => `<option value="${m}">${m === 0 ? "OFF" : m + " min"}</option>`).join("");

  $("finish-options").innerHTML = cfg.finish_options.map((o) => `
    <label class="opt" title="${o.note}">
      <input type="checkbox" data-finish="${o.key}" ${o.available ? "" : "disabled"}>
      <span>${o.label}${o.available ? "" : "（未導入）"}</span>
    </label>`).join("");

  renderModeHints();
}

function preset() {
  return (state.cfg && state.cfg.presets[state.mode + "/" + state.quality]) || {};
}

function renderModeHints() {
  const mode = state.mode;
  const inputs = (state.cfg && state.cfg.mode_inputs[mode]) || [];
  const has = (k) => inputs.indexOf(k) >= 0;

  $("field-image").hidden = !has("image");
  $("field-video").hidden = !has("source_video");
  $("field-refsize").hidden = !has("ref_image_size");
  $("field-refstrength").hidden = !has("reference_strength");

  $("image-label").textContent =
    mode === "motion_trace" ? "人物画像（顔・服・雰囲気をここから）" : "元画像";
  $("refstrength-label").firstChild.nodeValue =
    mode === "motion_trace" ? "元の素材への忠実度（人物と動きの両方） " : "元画像への忠実度 ";

  $("mode-hint").textContent =
    mode === "image_trace"
      ? "1枚の画像から、人物・顔・服・雰囲気をそのままに動画にします。"
      : mode === "motion_trace"
      ? "元動画から動き・テンポ・カメラを、人物画像から顔・服・雰囲気を取ります。プロンプトには「何を残して、何を変えたいか」を書いてください。"
      : "画像は使いません。プロンプトだけで世界観やカメラワークを探します（忠実度の設定はこのモードでは効きません）。";
  const p = preset();
  $("quality-hint").textContent = p.megapixels
    ? `${p.megapixels} MP・${p.steps} ステップ`
    : "";
  renderCacheHint();
}

// Mirrors studio/graph.py expected_cache(): prompt and (Image Trace only) the
// keyframe pixels + the resolution they are resized to are the encoder-visible
// inputs. Everything else — seed, steps, sampler, duration — is a HIT.
function renderCacheHint() {
  const prev = state.lastEncoder[state.mode];
  const cur = {
    prompt: $("prompt").value.trim(),
    image: state.image,
    source_video: state.sourceVideo,
    aspect_ratio: $("aspect").value,
    megapixels: preset().megapixels,
    duration: Number($("duration").value),
    ref_image_size: state.refImageSize,
  };
  /* Mirrors studio/graph.py expected_cache() for all three modes. Motion Trace
     also folds duration in: the reference video is truncated to the generated
     frame count, so a different duration is a different encode. */
  const KEYS = {
    image_trace: ["image", "aspect_ratio", "megapixels"],
    motion_trace: ["image", "source_video", "aspect_ratio", "megapixels",
                   "duration", "ref_image_size"],
    prompt_cinema: [],
  };
  let verdict = "MISS";
  if (prev && prev.prompt === cur.prompt) {
    verdict = (KEYS[state.mode] || []).every((k) => prev[k] === cur[k]) ? "HIT" : "MISS";
  }
  const n = state.variations;
  $("cache-hint").innerHTML = verdict === "HIT"
    ? `1本目から <span class="tag HIT">キャッシュ再利用</span> — 待ち時間の長い解析は起きません。`
    : `1本目だけ <span class="tag MISS">解析あり</span>（約60秒）、` +
      `残り ${Math.max(0, n - 1)} 本は <span class="tag HIT">キャッシュ再利用</span>で速く出ます。`;
}

// ---------------------------------------------------------------- status
let statusTimer = null;
async function pollStatus() {
  try {
    const s = await api("/api/status");
    renderGpu(s.gpu);
    renderStats(s.stats);
    if (state.batch) {
      const fresh = s.batches.find((b) => b.id === state.batch.id);
      if (fresh) renderResults(fresh);
    }
      state.comfyReady = Boolean(s.gpu.comfy_ready);
    renderReadiness();
    state.busy = s.gpu.active_jobs > 0;
    $("btn-cancel").hidden = !state.busy;
    $("btn-generate").disabled = state.busy;
    state.comfyReady = false;
    renderReadiness();
  } catch (e) {
    /* The Studio backend itself is unreachable (stopped, or never started).
       That is a different condition from "RunPod said no", and the UI must not
       silently look like a missing API key. */
    renderGpu({ pod_state: "UNKNOWN", studio_down: true, pod_error: e.message });
  } finally {
    statusTimer = setTimeout(pollStatus, state.busy ? 1500 : 5000);
  }
}

function renderGpu(g) {
  const chip = $("gpu-chip");
  const st = g.pod_state || "UNKNOWN";
  chip.textContent = g.studio_down
    ? "H3 Studio に接続できません"
    : "GPU: " + (JP_STATE[st] || st) + (g.comfy_ready ? " · ComfyUI 準備OK" : "");
  chip.className = "chip " + (g.studio_down ? "chip-unknown"
    : st === "RUNNING" ? "chip-running"
    : st === "EXITED" || st === "STOPPED" ? "chip-exited" : "chip-unknown");
  $("offline-banner").hidden = !g.studio_down;

  const cost = [];
  if (g.hourly_usd) cost.push(`$${Number(g.hourly_usd).toFixed(3)}/時`);
  if (g.session_cost_usd != null) {
    const m = Math.round((g.session_seconds || 0) / 60);
    cost.push(`今回の料金: $${g.session_cost_usd.toFixed(2)}（${m}分）`);
  }
  $("cost").textContent = cost.join("\n");

  const rows = [
    ["Pod", g.pod_id], ["状態", JP_STATE[st] || st], ["GPU", g.gpu],
    ["予備のPod", g.previous_pod_id],
    ["料金", g.hourly_usd ? "$" + Number(g.hourly_usd).toFixed(3) + "/時" : "-"],
    ["保存領域", g.network_volume],
    ["ComfyUI", g.comfy_ready ? "準備OK" : "停止中"],
    ["進捗の受信", g.ws_connected ? "接続済み" : "ポーリング"],
    ["実行中", g.active_jobs + " 本"],
  ];
  if (g.auto_stop && g.auto_stop.enabled) {
    rows.push(["自動停止まで", Math.round(g.auto_stop.remaining_seconds / 60) + " 分"]);
  }
  $("gpu-info").innerHTML = rows
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");

  /* START is enabled whenever the Pod is provably not already running.
     It is disabled only for reasons we can actually name:
       - the Studio backend is down, so there is nothing to ask;
       - RunPod reports no usable API key, so a start would fail anyway;
       - the Pod is already RUNNING.
     A transient RunPod error with a valid key must NOT latch the button off --
     that is exactly what made it unclickable while the backend was stopped. */
  const noKey = g.have_api_key === false;
  const unknown = st === "UNKNOWN";
  $("btn-gpu-start").disabled = Boolean(g.studio_down) || noKey || st === "RUNNING";
  $("btn-gpu-stop").disabled = Boolean(g.studio_down) || noKey || st !== "RUNNING";

  let why = "";
  if (g.studio_down) why = "H3 Studio backend が停止しています（bash scripts/start_studio.sh）。";
  else if (noKey) why = "RunPod API キーを Keychain から読めません。";
  else if (unknown && g.pod_error) why = "RunPod に問い合わせできません: " + g.pod_error;
  $("gpu-why").textContent = why;
  $("gpu-why").hidden = !why;
}

/* Files are always selectable. If ComfyUI is not up yet the Studio keeps the
   file locally and forwards it automatically when you press 生成する, so you
   can pick an image and write your prompt while the GPU is still booting. */
function renderReadiness() {
  const waiting = !state.comfyReady && (state.image || state.sourceVideo);
  const note = $("upload-pending");
  if (note) note.hidden = !waiting;
}

function renderStats(s) {
  if (!s) return;
  $("stats").textContent =
    `${s.generations} 本生成 ／ キャッシュ再利用 ${s.cache_hits}・再計算 ${s.cache_misses}` +
    ` ／ GPU ${Math.round(s.gpu_seconds)} 秒`;
}

// ---------------------------------------------------------------- results
function renderResults(batch) {
  state.batch = batch;
  const sel = batch.generations.find((g) => g.selected);
  state.selected = sel ? sel.id : null;

  $("results").innerHTML = batch.generations.map((g, i) => {
    const body = g.output_url
      ? `<video src="${g.output_url}" controls preload="metadata" playsinline></video>`
      : `<div class="ph">${g.status === "error" ? "エラー<br>" + esc(g.error || "") : esc(g.progress_text || g.status)}</div>`;
    const tag = g.cache_result
      ? `<span class="tag ${g.cache_result}">${g.cache_result}</span>`
      : (g.status === "error" ? `<span class="tag err">エラー</span>` : "");
    return `<div class="card ${g.selected ? "sel" : ""}" data-gen="${g.id}">
      ${body}
      <div class="bar-progress"><i style="width:${Math.round((g.progress || 0) * 100)}%"></i></div>
      <div class="meta">
        <span>#${i + 1} · seed ${g.seed}</span>
        <span>${tag}${g.seconds ? " " + g.seconds + "s" : ""}</span>
      </div>
      <div class="acts">
        <button class="btn ${g.selected ? "btn-primary" : ""}" data-act="select" ${g.status !== "done" ? "disabled" : ""}>
          ${g.selected ? "★ 選択中" : "これを選ぶ"}</button>
        <button class="btn" data-act="export" ${g.status !== "done" ? "disabled" : ""}>書き出し</button>
      </div>
    </div>`;
  }).join("");

  $("finish").hidden = !state.selected;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

$("results").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const genId = btn.closest(".card").dataset.gen;
  try {
    if (btn.dataset.act === "select") {
      await api("/api/select", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ generation_id: genId, selected: true }),
      });
      const b = await api("/api/batch/" + state.batch.id);
      renderResults(b);
    } else {
      const r = await api("/api/export", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ generation_id: genId }),
      });
      notice("info", "書き出しました: " + r.path);
    }
  } catch (err) { notice("error", err.message); }
});

// ---------------------------------------------------------------- actions
$("mode-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.mode = b.dataset.mode;
  [...$("mode-seg").children].forEach((c) => c.classList.toggle("on", c === b));
  renderModeHints();
  maybeSuggestTags();
});

$("refsize-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.refImageSize = b.dataset.refsize;
  [...$("refsize-seg").children].forEach((c) => c.classList.toggle("on", c === b));
  renderCacheHint();
});

/* Motion Trace addresses its references by tag: <Picture 1> is the identity
   image, <Video 1> the source video. Offer the scaffold as a placeholder once;
   never overwrite what the operator has typed. */
function maybeSuggestTags() {
  if (state.mode !== "motion_trace") return;
  const t = $("prompt");
  if (t.value.trim()) return;
  const mt = (state.cfg && state.cfg.motion_trace) || {};
  t.placeholder =
    "keep the person of " + (mt.identity_tag || "<Picture 1>") +
    ", follow the motion and camera of " + (mt.motion_tag || "<Video 1>") +
    "; change the background to a sunlit street";
}
$("quality-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.quality = b.dataset.quality;
  [...$("quality-seg").children].forEach((c) => c.classList.toggle("on", c === b));
  renderModeHints();
});
$("var-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.variations = Number(b.dataset.var);
  [...$("var-seg").children].forEach((c) => c.classList.toggle("on", c === b));
  renderCacheHint();
});
$("prompt").addEventListener("input", renderCacheHint);
$("aspect").addEventListener("change", renderCacheHint);
$("duration").addEventListener("change", renderCacheHint);
$("refstrength").addEventListener("input", (e) => { $("ref-value").textContent = e.target.value; });

$("video-file").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  $("video-status").textContent = "アップロード中…";
  $("video-notes").textContent = "";
  try {
    const fd = new FormData();
    fd.append("image", f, f.name);
    const r = await api("/api/upload?kind=video", { method: "POST", body: fd });
    state.sourceVideo = r.video;
    state.sourceVideoMeta = r.probe;
    const p = r.probe || {};
    const bits = [r.video, (Math.round(r.bytes / 1024 / 1024 * 10) / 10) + " MB"];
    if (p.duration) bits.push(p.duration + "s");
    if (p.fps) bits.push(p.fps + " fps");
    if (p.frames) bits.push(p.frames + " frames");
    if (!r.sent_to_comfyui) bits.push("GPU起動後に自動で転送します");
    $("video-status").textContent = bits.join(" · ");
    renderReadiness();
    $("video-notes").textContent = (p.notes || []).join("\n");
    if (p.ok === false) notice("warn", (p.notes || []).join(" / "));
    const v = $("video-preview");
    v.src = URL.createObjectURL(f);
    v.hidden = false;
    renderCacheHint();
  } catch (err) {
    $("video-status").textContent = "";
    notice("error", err.message);
  }
});

$("image-file").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  $("image-status").textContent = "アップロード中…";
  try {
    const fd = new FormData();
    fd.append("image", f, f.name);
    const r = await api("/api/upload", { method: "POST", body: fd });
    state.image = r.image;
    state.localPath = r.local_path;
    $("image-status").textContent =
      `${r.image}（${Math.round(r.bytes / 1024)} KB）` +
      (r.sent_to_comfyui ? "" : " · GPU起動後に自動で転送します");
    renderReadiness();
    const img = $("image-preview");
    img.src = URL.createObjectURL(f);
    img.hidden = false;
    renderCacheHint();
  } catch (err) {
    $("image-status").textContent = "";
    notice("error", err.message);
  }
});

$("btn-generate").addEventListener("click", async () => {
  const payload = {
    mode: state.mode,
    prompt: $("prompt").value,
    quality: state.quality,
    variations: state.variations,
    duration: Number($("duration").value),
    aspect_ratio: $("aspect").value,
  };
  const inputs = (state.cfg && state.cfg.mode_inputs[state.mode]) || [];
  if (inputs.indexOf("image") >= 0) {
    payload.image = state.image;
    payload.local_path = state.localPath;
  }
  if (inputs.indexOf("source_video") >= 0) {
    payload.source_video = state.sourceVideo;
    payload.source_video_meta = state.sourceVideoMeta;
  }
  if (inputs.indexOf("ref_image_size") >= 0) payload.ref_image_size = state.refImageSize;
  if (inputs.indexOf("reference_strength") >= 0 && !$("refstrength").disabled) {
    payload.reference_strength = Number($("refstrength").value);
  }
  $("btn-generate").disabled = true;
  try {
    const batch = await api("/api/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.lastEncoder[state.mode] = {
      prompt: payload.prompt.trim(), image: payload.image,
      source_video: payload.source_video,
      aspect_ratio: payload.aspect_ratio, megapixels: preset().megapixels,
      duration: payload.duration, ref_image_size: state.refImageSize,
    };
    renderResults(batch);
    renderCacheHint();
  } catch (e) {
    notice("error", e.message);
    $("btn-generate").disabled = false;
  }
});

$("btn-cancel").addEventListener("click", async () => {
  try { await api("/api/cancel", { method: "POST", body: "{}" }); }
  catch (e) { notice("error", e.message); }
});

$("btn-finalize").addEventListener("click", async () => {
  if (!state.selected) return;
  const finish = {};
  document.querySelectorAll("#finish-options input[data-finish]").forEach((c) => {
    if (c.checked) finish[c.dataset.finish] = true;
  });
  try {
    const b = await api("/api/finalize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ generation_id: state.selected, finish_options: finish }),
    });
    notice("info", "Final を投入しました（選択した1本のみ）。");
    renderResults(b);
  } catch (e) { notice("error", e.message); }
});

$("btn-gpu-start").addEventListener("click", async () => {
  $("btn-gpu-start").disabled = true;
  notice("info", "GPU を起動しています… Pod start → ComfyUI 起動 → health check", 0);
  try {
    const r = await api("/api/gpu/start", { method: "POST", body: "{}" });
    notice(r.ok ? "info" : "error", (r.ok ? "READY: " : "失敗: ") + (r.error || r.steps.join(" → ")));
  } catch (e) { notice("error", e.message); }
});

$("btn-gpu-stop").addEventListener("click", async () => {
  if (!confirm("Pod を停止します（課金停止）。Network Volume とモデル・キャッシュ・出力は保持されます。")) return;
  try {
    const r = await api("/api/gpu/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: false }),
    });
    notice(r.ok ? "info" : "warn", r.ok ? "Pod を停止しました。Volume は保持されています。" : r.error);
  } catch (e) { notice("error", e.message); }
});

$("auto-stop").addEventListener("change", async (e) => {
  try {
    await api("/api/gpu/auto_stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: Number(e.target.value) }),
    });
    notice("info", "AUTO STOP: " + (e.target.value === "0" ? "OFF" : e.target.value + " 分"));
  } catch (err) { notice("error", err.message); }
});

async function loadHistory() {
  try {
    const h = await api("/api/history?limit=15");
    $("history").innerHTML = h.batches.map((b) => {
      const done = b.generations.filter((g) => g.status === "done").length;
      const sel = b.generations.find((g) => g.selected);
      return `<div class="h-item"><b>${JP_KIND[b.kind] || b.kind} · ${JP_MODE[b.mode] || b.mode}</b><br>
        ${esc((b.prompt || "").slice(0, 70))}<br>
        ${done}/${b.generations.length} 本${sel ? " · ★選択済み" : ""}
        ${b.reference_strength ? " · 忠実度 " + b.reference_strength : ""}</div>`;
    }).join("") || "<div class='hint'>まだ履歴はありません。</div>";
  } catch (e) { /* history is non-critical */ }
}

(async function init() {
  try { await loadConfig(); maybeSuggestTags(); }
  catch (e) { notice("error", "設定の読み込みに失敗: " + e.message); }
  pollStatus();
  loadHistory();
  setInterval(loadHistory, 30000);
})();

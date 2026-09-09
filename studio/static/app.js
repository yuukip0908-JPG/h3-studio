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
  /* One take. 46 batches, 42 of them one take: generating four candidates is
     the exception, and at 22 minutes each it is an expensive default. */
  variations: 1,
  image: null,
  localPath: null,
  extraImages: [],          // more photographs of the same person (Motion Trace)
  sourceVideo: null,
  sourceVideoMeta: null,
  sourceVideoLocal: null,   // the file on this Mac; Long Draft cuts from it
  longDraft: false,         // Motion Trace only
  longDraftPlan: null,
  longDraftRun: null,       // the run currently shown
  longDraftWatched: null,   // a run this page saw going, so it may stay when done
  longDraftDismissed: false,
  longDraftRuns: [],
  refImageSize: "match",
  draftQuality: "standard",
  cfg: null,
  screen: "studio",
  /* Per-run departures from the preset. Empty means "the preset, exactly" --
     which is what makes the echo beside 生成設定 truthful rather than decorative. */
  ov: { steps: null, megapixels: null },
  seedFixed: null,
  batches: [],
  events: [],
  batch: null,       // current batch shown in RESULTS
  resultsDismissed: false,  // emptied on purpose; do not restore it behind them
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
  if (!r.ok) {
    /* Keep the whole payload on the error: some refusals carry a fix with them
       (a suggested prompt, for instance) and a bare message throws it away. */
    const err = new Error(data.error || ("HTTP " + r.status));
    err.data = data;
    err.status = r.status;
    throw err;
  }
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

  /* 30 min is marked as the recommendation and is NOT selected for you:
     an auto-stop that appears without being asked for is a surprise, and this
     one ends a paid session. */
  const rec = cfg.auto_stop_recommended || 30;
  $("auto-stop").innerHTML = cfg.auto_stop_choices
    .map((m) => `<option value="${m}">${m === 0 ? "OFF" : m + " 分"}${m === rec ? "（推奨）" : ""}</option>`)
    .join("");

  const dq = cfg.draft_quality || { choices: [] };
  $("draftq-seg").innerHTML = dq.choices.map((c) => {
    const off = c.available === false;
    return `<button data-dq="${c.value}" ${off ? "disabled" : ""}
      class="${c.value === state.draftQuality && !off ? "on" : ""}"
      title="${c.reason || ""}">${c.label}<span class="sub">${c.sub}</span></button>`;
  }).join("");
  const turbo = dq.choices.find((c) => c.value === "turbo") || {};
  $("draftq-hint").textContent = turbo.available === false
    ? turbo.reason
    : "「高速」は Turbo LoRA を使い、Draft のステップ数を下げます。仕上げ（Final）は常に高品質です。";

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

/* What will actually be sent. The server does preset.update(overrides), so the
   UI has to mirror that or the cache forecast under the button predicts the
   preset's resolution while the request carries another one. */
function overrides() {
  const o = {};
  if (state.ov.steps != null) o.steps = state.ov.steps;
  if (state.ov.megapixels != null) o.megapixels = state.ov.megapixels;
  return o;
}
function effective() { return Object.assign({}, preset(), overrides()); }

/* The whole point of decision 3: a value that no longer matches the preset is
   never silently in force. It is named, with what it was and what it is now. */
function renderPresetEcho() {
  const p = preset(), e = effective(), bits = [];
  if (e.steps !== p.steps) bits.push(`Steps ${p.steps}→${e.steps}`);
  if (e.megapixels !== p.megapixels) bits.push(`解像度 ${p.megapixels}→${e.megapixels} MP`);
  if (state.seedFixed != null) bits.push(`Seed 固定`);
  const el = $("preset-echo");
  if (el) el.textContent = bits.length ? "（変更あり: " + bits.join(" / ") + "）" : "";
  const q = $("quality-hint");
  if (q) q.textContent = e.megapixels ? `${e.megapixels} MP・${e.steps} ステップ` : "";
  const sd = $("steps-default");
  if (sd) sd.textContent = p.steps ? "プリセット " + p.steps : "";
  if ($("steps") && document.activeElement !== $("steps")) $("steps").value = e.steps || "";
}

function renderModeHints() {
  const mode = state.mode;
  const inputs = (state.cfg && state.cfg.mode_inputs[mode]) || [];
  const has = (k) => inputs.indexOf(k) >= 0;

  $("field-image").hidden = !has("image");
  $("field-video").hidden = !has("source_video");
  $("field-refsize").hidden = !has("ref_image_size");
  $("field-refstrength").hidden = !has("reference_strength");
  renderLongDraftField();

  $("image-label").textContent =
    mode === "motion_trace" ? "人物画像（顔・服・雰囲気をここから）" : "元画像";
  $("refstrength-label").firstChild.nodeValue =
    mode === "motion_trace" ? "元の素材への忠実度（人物と動きの両方） " : "元画像への忠実度 ";

  /* One line. The cards under it already carry the description, the prompt
     helper already explains what to write, and 忠実度 is hidden outright in
     Prompt Cinema -- so a third telling of each was pushing 生成本数 off the
     bottom of a 212px column. */
  $("mode-hint").textContent =
    mode === "image_trace"
      ? "画像の人物のまま動かします。"
      : mode === "motion_trace"
      ? "動きは動画から、人物は画像から取ります。"
      : "文章だけで作ります（画像は使いません）。";
  renderPresetEcho();
  markCostly();
  renderCacheHint();
}

/* Mirrors studio/graph.py expected_cache(). Kept in one place because two
   things read it: the forecast under the button, and which tier each control
   is filed under. */
const CACHE_KEYS = {
  image_trace: ["image", "aspect_ratio", "megapixels"],
  motion_trace: ["image", "source_video", "aspect_ratio", "megapixels",
                 "duration", "ref_image_size"],
  prompt_cinema: [],
};

/* Which controls cost an encode is not fixed: a setting is expensive only if
   it is in THIS mode's fingerprint. Duration re-encodes a Motion Trace (the
   reference video is truncated to the frame count) but is free for Image Trace,
   and Prompt Cinema passes images=[] so nothing costs an encode at all. Marking
   長さ permanently would have warned an Image Trace operator away from a change
   that is free. */
function markCostly() {
  const keys = CACHE_KEYS[state.mode] || [];
  const mark = (id, keyName) => {
    const el = $(id);
    if (el) el.hidden = keys.indexOf(keyName) < 0;
  };
  mark("cost-duration", "duration");
  mark("cost-aspect", "aspect_ratio");
  mark("cost-megapixels", "megapixels");
  mark("cost-refsize", "ref_image_size");
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
    megapixels: effective().megapixels,
    duration: Number($("duration").value),
    ref_image_size: state.refImageSize,
  };
  /* Mirrors studio/graph.py expected_cache() for all three modes. Motion Trace
     also folds duration in: the reference video is truncated to the generated
     frame count, so a different duration is a different encode. */
  const KEYS = CACHE_KEYS;
  /* Three answers, not two. lastEncoder lives only in this tab's memory and is
     written after a successful generation, so a freshly opened Studio knows
     nothing -- and answering "解析あり" there stated a cost nobody had measured
     every single time the page was reloaded. */
  let verdict = "UNKNOWN";
  if (prev && prev.prompt === cur.prompt) {
    verdict = (KEYS[state.mode] || []).every((k) => prev[k] === cur[k]) ? "HIT" : "MISS";
  } else if (prev) {
    verdict = "MISS";
  }
  const n = state.variations;
  $("cache-hint").innerHTML = verdict === "UNKNOWN"
    ? `<span class="tag UNKNOWN">解析の有無は不明</span> この画面を開き直したので、`
      + `前回と同じ設定かどうかを判断できません。`
    : verdict === "HIT"
    ? `<span class="tag HIT">キャッシュ再利用</span> 解析は起きません。`
    : n > 1
    ? `1本目だけ <span class="tag MISS">解析あり</span>（約60秒）、` +
      `残り ${n - 1} 本は <span class="tag HIT">キャッシュ再利用</span>で速く出ます。`
    : `<span class="tag MISS">解析あり</span> 素材か解像度が変わったので、約60秒の解析が入ります。`;
}

// ------------------------------------------------------- Long Draft (Phase 0)
/* A long Motion Trace is dominated by sampling, and sampling grows faster than
   the frame count (measured here: 18 s ran at 203.8 s per step, ~46 minutes).
   Long Draft cuts the reference video into short pieces and generates them one
   after another so the direction can be judged in a fraction of that time.

   It is a Draft tool. Segments are generated independently -- no shared latent,
   no overlap -- so a boundary can jump, and Final is never split. */
function longDraftCfg() {
  return (state.cfg && state.cfg.long_draft) || {};
}

function longDraftApplies() {
  const cfg = longDraftCfg();
  return (cfg.modes || []).indexOf(state.mode) >= 0;
}

function longDraftOn() {
  return state.longDraft && longDraftApplies();
}

function renderLongDraftField() {
  const field = $("field-longdraft");
  if (!field) return;
  const cfg = longDraftCfg();
  field.hidden = !longDraftApplies();
  /* Long Draft decides the length of every segment from the source video, and
     produces one video per segment rather than N variations of one. Leaving
     those two controls live would let the operator set values that are then
     silently ignored. */
  const on = longDraftOn();
  $("btn-generate").textContent = on ? "長尺Draftを生成する" : "生成する";
  $("field-duration").hidden = on;
  $("field-variations").hidden = on;
  if (field.hidden) return;
  const un = $("longdraft-unavailable");
  un.hidden = cfg.available !== false;
  un.textContent = cfg.available === false ? (cfg.reason || "長尺Draftは使えません。") : "";
  document.querySelectorAll("#longdraft-seg button").forEach((b) => {
    b.disabled = cfg.available === false;
  });
  $("ld-warning").textContent = cfg.warning || "";
  renderLongDraftPlan();
}

let ldPlanToken = 0;
async function refreshLongDraftPlan() {
  if (!longDraftOn()) { renderLongDraftPlan(); return; }
  const meta = state.sourceVideoMeta || {};
  if (!meta.duration) { state.longDraftPlan = null; renderLongDraftPlan(); return; }
  const token = ++ldPlanToken;
  try {
    const plan = await api("/api/long_draft/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_duration: meta.duration,
        /* Without the frame rate the split falls back to seconds, and H3 reads
           frames -- that mismatch is what skipped 0.7 s at every boundary. */
        source_fps: meta.fps,
        segment_seconds: longDraftCfg().segment_seconds_default,
        params: { megapixels: preset().megapixels, steps: preset().steps,
                  aspect_ratio: $("aspect").value },
      }),
    });
    if (token !== ldPlanToken) return;   // a newer request already answered
    state.longDraftPlan = plan;
  } catch (e) {
    state.longDraftPlan = { error: e.message };
  }
  renderLongDraftPlan();
}

function renderLongDraftPlan() {
  const box = $("longdraft-plan");
  if (!box) return;
  if (!longDraftOn()) { box.hidden = true; return; }
  box.hidden = false;
  const body = $("ld-plan-body");
  const plan = state.longDraftPlan;
  if (!plan) {
    body.innerHTML = `<p class="hint">元動画を選ぶと、分割の計画をここに表示します。</p>`;
    return;
  }
  if (plan.error) {
    body.innerHTML = `<p class="hint warn">${esc(plan.error)}</p>`;
    return;
  }
  /* Three quantities, and the UI used to show one. The operator asks for
     6.0 s; H3 renders 158 frames (6.6 s of output); and 158 frames of a 30 fps
     source is 5.3 s of their video. The middle column is the one that used to
     be wrong. */
  const rows = plan.segments.map((sg) =>
    `<tr><td>Segment ${sg.index + 1}</td><td>元動画 ${sg.start.toFixed(1)} - ${sg.end.toFixed(1)} 秒</td>
     <td>${(sg.cut_seconds || sg.duration).toFixed(1)} 秒ぶん</td>
     <td>${sg.frames} フレーム</td></tr>`).join("");
  /* Both numbers are estimates and are labelled as such. They come from
     measured history when this machine has comparable runs, and from the
     duration-scaled baseline when it does not. */
  const saved = plan.single_estimated_seconds && plan.estimated_seconds
    ? plan.single_estimated_seconds / plan.estimated_seconds : null;
  body.innerHTML = `
    <div class="ld-head">元動画 <b>${plan.source_duration.toFixed(1)} 秒</b> ·
      分割 <b>${plan.segment_count}</b> · 1 segment <b>${plan.segment_seconds} 秒</b>${
        plan.source_covered ? ` · 使用 <b>${plan.source_covered.toFixed(1)} 秒</b>` : ""}</div>
    <table class="ld-table"><tbody>${rows}</tbody></table>
    <div class="ld-eta">
      <div>通常生成（1本）: <b>推定 約${fmtDuration(plan.single_estimated_seconds)}</b></div>
      <div>長尺Draft（${plan.segment_count} 分割合計）: <b>推定 約${fmtDuration(plan.estimated_seconds)}</b>
        ${saved ? ` · <span class="tag HIT">推定 ${saved.toFixed(1)}倍速</span>` : ""}</div>
      <div class="hint">どちらも<b>推定</b>です。実測ではありません。長尺Draftを実行するとその実測が履歴に入り、次回からの推定が実測ベースになります。</div>
    </div>
    ${(plan.notes || []).map((n) => `<p class="hint warn">${esc(n)}</p>`).join("")}`;
}

document.querySelectorAll("#longdraft-seg button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#longdraft-seg button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    state.longDraft = b.dataset.longdraft === "on";
    renderLongDraftField();
    refreshLongDraftPlan();
  });
});

const JP_SEG = { planned: "準備中", pending: "待機", queued: "待機", submitting: "送信中",
                 running: "生成中", done: "完了", cancelled: "中止", error: "エラー" };
/* The tag printed the raw enum. history.py also allows NODE_NOT_EXECUTED and
   UNKNOWN, which had no style at all -- a colourless pill with a developer
   word in it. These are the same words the cache hint already uses. */
const JP_CACHE = { HIT: "キャッシュ再利用", MISS: "解析あり",
                   NODE_NOT_EXECUTED: "解析なし", UNKNOWN: "不明" };
const SEG_ICON = { done: "✓", running: "●", queued: "●", pending: "○",
                   cancelled: "×", error: "!" };

function buildLongDraftShell() {
  const el = document.createElement("div");
  el.innerHTML = '<button class="ld-close" data-ld-close title="閉じる">×</button>'
    + '<h2 class="ld-title"></h2>'
    + '<div class="ld-run-head"></div>'
    + '<div class="ld-segs"></div>'
    + '<div class="ld-preview-wrap" hidden>'
    + '<h2>結合プレビュー（確認用）</h2><div class="vid ld-preview"></div>'
    + '<p class="hint">segment を順番につないだだけの確認用プレビューです。'
    + '個々の segment 動画はそのまま残っています。</p>'
    + '<div class="acts"><a class="btn ld-download" download>結合動画をダウンロード</a></div></div>'
    + '<p class="hint warn ld-warning"></p>'
    + '<div class="ld-actions"></div>'
    + '<p class="hint ld-manifest"></p>';
  return el;
}

function buildSegmentCard() {
  const el = document.createElement("div");
  el.className = "ld-seg";
  el.innerHTML = '<div class="ld-seg-head"></div><div class="ld-seg-state"></div>'
    + '<div class="vid"></div><div class="ld-seg-error"></div>';
  return el;
}

/* Same reconciliation as the results grid, and for the same reason: a Long
   Draft shows one video per segment plus the joined preview, and rebuilding
   the panel每 poll tore all of them down. */
function renderLongDraftRun(run) {
  const box = $("longdraft-run");
  if (!box) return;
  if (!run) {
    box.hidden = true;
    box.innerHTML = "";
    state.longDraftRun = null;
    return;
  }
  state.longDraftRun = run;
  state.longDraftDismissed = false;
  box.hidden = false;

  const shell = ensureChild(box, run.id, buildLongDraftShell);
  pruneChildren(box, new Set([run.id]));

  setText(shell.querySelector(".ld-title"),
          `長尺Draft ${run.done_count} / ${run.segment_count}`);
  const cur = run.current_index;
  const running = run.status === "running" || run.status === "planned";
  setHtml(shell.querySelector(".ld-run-head"),
    (cur != null ? `現在: Segment ${cur + 1} / ${run.segment_count} · ` : "")
    + `状態: <b>${JP_SEG[run.status] || run.status}</b>`
    + (running && run.remaining_seconds
       ? ` · 全体推定残り <b>約${fmtDuration(run.remaining_seconds)}</b>（推定）` : ""));

  const segsRoot = shell.querySelector(".ld-segs");
  const keys = new Set();
  run.segments.forEach((sg) => {
    const key = String(sg.index);
    keys.add(key);
    const card = ensureChild(segsRoot, key, buildSegmentCard);
    card.className = "ld-seg " + sg.status;
    const live = sg.status === "running" && sg.sampler && sg.sampler.max
      ? ` Sampling ${Math.round(sg.sampler.value || 0)} / ${Math.round(sg.sampler.max)}`
      : (sg.progress_text ? " " + sg.progress_text : "");
    setHtml(card.querySelector(".ld-seg-head"),
      `${SEG_ICON[sg.status] || "·"} Segment ${sg.index + 1}`
      + `<span class="dim">${sg.start.toFixed(1)}-${sg.end.toFixed(1)} 秒 · seed ${sg.seed}</span>`);
    setHtml(card.querySelector(".ld-seg-state"),
      esc((JP_SEG[sg.status] || sg.status) + live)
      + (sg.cache_result ? `<span class="tag ${sg.cache_result}">${sg.cache_result}</span>` : "")
      + (sg.generation_seconds ? `<span class="dim">${fmtDuration(sg.generation_seconds)}</span>` : ""));
    ensureVideo(card.querySelector(".vid"), sg.output_url);
    setHtml(card.querySelector(".ld-seg-error"),
            sg.error ? `<div class="hint warn">${esc(sg.error)}</div>` : "");
  });
  pruneChildren(segsRoot, keys);

  const previewWrap = shell.querySelector(".ld-preview-wrap");
  previewWrap.hidden = !run.preview_url;
  ensureVideo(shell.querySelector(".ld-preview"), run.preview_url || null);
  const ldDl = shell.querySelector(".ld-download");
  if (run.preview_url) {
    ldDl.href = run.preview_url;
    ldDl.download = `H3_long_draft_${run.id}.mp4`;
  }

  setText(shell.querySelector(".ld-warning"), run.warning || "");
  setHtml(shell.querySelector(".ld-actions"), run.resumable
    ? `<button class="btn" id="btn-ld-resume" data-ld="${run.id}">未完了の segment だけ再開する</button>`
    : "");
  /* An absolute filesystem path with an English key. The folder is worth
     knowing; the path is not worth a permanent line of the panel. */
  const man = shell.querySelector(".ld-manifest");
  setText(man, run.manifest_path ? "保存先: " + run.manifest_path.split("/").slice(-2, -1)[0] : "");
  if (run.manifest_path) man.title = run.manifest_path;
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("#btn-ld-resume");
  if (!btn) return;
  btn.disabled = true;
  try {
    const run = await api(`/api/long_draft/${btn.dataset.ld}/resume`,
                          { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    notice("info", "未完了の segment から再開しました。");
    renderLongDraftRun(run);
  } catch (err) { notice("error", err.message); btn.disabled = false; }
});

async function startLongDraft() {
  const payload = {
    mode: state.mode,
    prompt: $("prompt").value,
    image: state.image,
    local_path: state.localPath,
    source_video: state.sourceVideo,
    source_video_local: state.sourceVideoLocal,
    source_video_meta: state.sourceVideoMeta,
    source_duration: (state.sourceVideoMeta || {}).duration,
    segment_seconds: longDraftCfg().segment_seconds_default,
    aspect_ratio: $("aspect").value,
    ref_image_size: state.refImageSize,
    quality: "draft",
  };
  if (!$("refstrength").disabled) payload.reference_strength = Number($("refstrength").value);
  const run = await api("/api/long_draft", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.batch = null;
  $("results").innerHTML = "";
  renderLongDraftRun(run);
  notice("info", `長尺Draft を開始しました（${run.segment_count} segment を順番に生成します）。`);
}

// ---------------------------------------------------------------- status
let statusTimer = null;
async function pollStatus() {
  try {
    const s = await api("/api/status");
    state.pollFails = 0;
    state.lastGpu = s.gpu;
    state.batches = s.batches || [];
    state.events = s.events || [];
    renderGpu(s.gpu);
    renderGpuScreen(s);
    renderSettingsScreen(s);
    renderDelivery();
    renderStats(s.stats);
    /* A reload used to empty the results panel: state.batch only ever got set
       by submitting, so the finished videos vanished even though they were on
       disk and in this very payload. Adopt the newest batch when we have none. */
    /* Third instance of the same rule in this file, and the one that bites
       hardest: the status poll runs every two seconds, so after any generation
       in this session the × would have undone itself almost immediately. */
    if (!state.batch && !state.resultsDismissed && s.batches && s.batches.length) {
      state.batch = s.batches[0];
      renderResults(s.batches[0]);
    }
    if (state.batch) {
      const fresh = s.batches.find((b) => b.id === state.batch.id);
      if (fresh) renderResults(fresh);
      tickResults();
    }
    /* Same rule as the batch panel: a reload must not lose a Long Draft that is
       running or finished. The run is in this payload either way. */
    state.longDraftRuns = s.long_drafts || [];
    /* Showing the most recent run whatever its state put a cancelled one on
       screen labelled "生成中", with live cancel buttons, above a single take
       that was the thing actually running -- three phantom segments in the
       largest panel on the page. A run the operator opened from the shelf
       stays open; otherwise only a run that is genuinely going gets the
       panel, and a finished one only while nothing else is. */
    const LD_ACTIVE = ["running", "planned", "queued", "submitting", "paused"];
    const active = state.longDraftRuns.find((r) => LD_ACTIVE.includes(r.status));
    if (active) state.longDraftWatched = active.id;
    /* "Do not lose a finished run on reload" turned into a finished run from
       hours ago reappearing the moment the GPU went idle -- 3882px of it,
       under the take the operator was actually looking at, with no way to
       close it. A finished run is kept on screen only if this page watched it
       finish; otherwise it lives in the shelf, which is where finished work
       belongs. Dismissing it sticks. */
    const kept = state.longDraftWatched && !state.longDraftDismissed
      ? state.longDraftRuns.find((r) => r.id === state.longDraftWatched) : null;
    const pinned = state.longDraftRun && !state.longDraftDismissed
      && state.longDraftRuns.find((r) => r.id === state.longDraftRun.id);
    renderLongDraftRun(active || pinned || kept || null);
    state.comfyReady = Boolean(s.gpu.comfy_ready);
    renderReadiness();
    state.busy = s.gpu.active_jobs > 0;
    $("btn-cancel").hidden = !state.busy;
    $("btn-generate").disabled = state.busy;
  } catch (e) {
    /* One failed poll is not a dead backend. Reloading the page, restarting the
       Studio, or any hiccup aborts the in-flight request, and treating that as
       "the Studio is down" used to grey out STOP GPU -- while a Pod was running
       and billing, which is precisely when the button has to work.
       So: keep showing the last known state, and only declare the backend gone
       after several consecutive failures. */
    state.pollFails = (state.pollFails || 0) + 1;
    if (state.pollFails < 3 && state.lastGpu) {
      renderGpu(Object.assign({}, state.lastGpu, { poll_stale: true }));
    } else {
      renderGpu(Object.assign({}, state.lastGpu || {}, {
        pod_state: (state.lastGpu && state.lastGpu.pod_state) || "UNKNOWN",
        studio_down: true, pod_error: e.message,
      }));
    }
  } finally {
    statusTimer = setTimeout(pollStatus, state.busy ? 1500 : 5000);
  }
}

function renderGpu(g) {
  const chip = $("gpu-chip");
  const st = g.pod_state || "UNKNOWN";
  /* "unknown" is not "down": while a generation runs, /system_stats answers 502
     through the RunPod proxy and only /queue is reliable, and a transport
     failure says nothing about ComfyUI at all. Saying "停止" for either is how
     a working GPU gets reported as broken. */
  const JP_LIVENESS = { live: " · ComfyUI 準備OK", dead: " · ComfyUI 停止中",
                        unknown: " · ComfyUI 状態不明" };
  chip.textContent = g.studio_down
    ? "H3 Studio に接続できません"
    : "GPU: " + (JP_STATE[st] || st)
      + (g.comfy_liveness ? (JP_LIVENESS[g.comfy_liveness] || "")
                          : (g.comfy_ready ? " · ComfyUI 準備OK" : ""));
  chip.className = "chip " + (g.studio_down ? "chip-unknown"
    : st === "RUNNING" ? "chip-running"
    : st === "EXITED" || st === "STOPPED" ? "chip-exited" : "chip-unknown");
  $("offline-banner").hidden = !g.studio_down;

  /* While the Pod is stopped the last Pod's rate is not what a start would
     cost -- showing it invited exactly the mistake this panel exists to stop.
     Stopped: what the recommended card would cost. Running: what it is costing. */
  const cost = [];
  const rate = st === "RUNNING" ? g.hourly_usd : (g.recommended || {}).usd_per_hour;
  if (rate) cost.push(`$${Number(rate).toFixed(2)}/時${st === "RUNNING" ? "" : "（起動したら）"}`);
  if (g.session_cost_usd != null) {
    const m = Math.round((g.session_seconds || 0) / 60);
    cost.push(`今回の料金: 推定 $${g.session_cost_usd.toFixed(2)}（${m}分）`);
  }
  $("cost").textContent = cost.join("\n");

  renderGpuCard(g);
  renderGpuProgress(g.start);
  renderGpuOffer(g);
  renderGpuCost(g);

  const rows = [
    ["Pod", g.pod_id], ["状態", JP_STATE[st] || st], ["GPU", g.gpu],
    ["保存領域", g.network_volume || g.network_volume_id],
    ["データセンター", g.datacenter],
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

  const checks = (g.start && g.start.online_checks) || [];
  $("gpu-checks").innerHTML = !checks.length ? "" :
    "起動後チェック：" + checks.map((c) =>
      `${c.ok === true ? "OK" : c.ok === false ? "FAIL" : "不明"} ${c.label}`).join(" ／ ");

  const as = g.auto_stop || {};
  const sel = $("auto-stop");
  if (sel && document.activeElement !== sel) sel.value = String(as.idle_minutes || 0);
  const chk = $("stop-after-gen");
  if (chk && document.activeElement !== chk) chk.checked = Boolean(as.stop_after_generation);

  /* Offered only when the Pod is RUNNING and ComfyUI is not answering -- the
     situation that cost an unattended half hour of billing and a hand-run SSH
     session. The server checks the same two conditions itself. */
  const comfyDown = $("comfy-down");
  if (comfyDown) comfyDown.hidden = !g.comfy_start_available;

  /* Two separate facts, because reading one as the other is what let a Pod bill
     unattended on 2026-09-07: the heartbeat says the STUDIO is alive (the Studio
     writes it), the pulse says the WATCHDOG is alive (only the watchdog writes
     it). "enabled" has only ever meant "configured". */
  const wd = g.watchdog || {};
  const note = $("watchdog-note");
  if (!wd.enabled) {
    note.textContent = "独立ウォッチドッグ: OFF";
    note.classList.remove("warn");
  } else if (wd.running === true) {
    note.textContent = `独立ウォッチドッグ: 稼働中（${wd.pulse_age_seconds} 秒前に確認`
      + (wd.pulse_pid ? ` / pid ${wd.pulse_pid}` : "") + "）。"
      + `Studio が落ちても最大 ${wd.stale_minutes} 分で、通し起動でも ${wd.max_minutes} 分でGPUを停止します`;
    note.classList.remove("warn");
  } else if (wd.running === null) {
    note.textContent = "独立ウォッチドッグ: 状態を確認できません（pulse ファイルが読めません）。守られているか不明です。";
    note.classList.add("warn");
  } else if (st === "RUNNING") {
    note.textContent = "独立ウォッチドッグ: 動いていません。Studio が落ちても Pod は止まりません — 終わったら STOP GPU を押してください。";
    note.classList.add("warn");
  } else {
    note.textContent = "独立ウォッチドッグ: 待機（Pod 起動時に開始します）";
    note.classList.remove("warn");
  }

  /* START is enabled whenever the Pod is provably not already running.
     It is disabled only for reasons we can actually name:
       - the Studio backend is down, so there is nothing to ask;
       - RunPod reports no usable API key, so a start would fail anyway;
       - the Pod is already RUNNING, or a start is already in flight.
     A transient RunPod error with a valid key must NOT latch the button off. */
  const noKey = g.have_api_key === false;
  const unknown = st === "UNKNOWN";
  const starting = Boolean(g.start && g.start.busy);
  $("btn-gpu-start").disabled = Boolean(g.studio_down) || noKey || st === "RUNNING" || starting;
  /* STOP is the button that ends a bill, so it is disabled only when the Pod is
     *known* to be stopped. Not knowing is a reason to let the operator try, not
     a reason to take the control away -- the server refuses safely if there is
     nothing to stop. */
  $("btn-gpu-stop").disabled = noKey || st === "EXITED";

  let why = "";
  if (g.poll_stale) why = "状態の取得に一度失敗しました（表示は直前の値）。STOP GPU は押せます。";
  else if (g.studio_down) why = "H3 Studio backend が停止しています（bash scripts/start_studio.sh）。";
  else if (noKey) why = "RunPod API キーを Keychain から読めません。";
  else if (unknown && g.pod_error) why = "RunPod に問い合わせできません: " + g.pod_error;
  else if (g.start && g.start.error && !starting) why = g.start.error;
  $("gpu-why").textContent = why;
  $("gpu-why").hidden = !why;
}

/* The card answers three questions and nothing else: is it on, which GPU, what
   does it cost. Anything a person has to compute themselves is a number they
   will get wrong at the wrong moment. */
function renderGpuCard(g) {
  const st = g.pod_state || "UNKNOWN";
  const running = st === "RUNNING";
  const starting = Boolean(g.start && g.start.busy);
  const card = running ? (g.price || {}) : (g.recommended || {});
  const money = (v) => (v == null ? "—" : "$" + Number(v).toFixed(2));

  let head;
  if (starting) head = '<div class="state">起動中…</div>';
  else if (running) head = '<div class="state on">実行中</div>';
  else if (st === "UNKNOWN") head = '<div class="state">状態不明</div>';
  else head = '<div class="state off">停止中</div>';

  const label = card.label || g.gpu || "—";
  const hourly = card.usd_per_hour != null ? card.usd_per_hour : g.hourly_usd;
  const prefix = running ? "" : "おすすめ： ";
  const price = hourly == null
    ? '<div class="price none">価格を取得できません</div>'
    : `<div class="price"><b>${money(hourly)} / 時</b>　30分：約 ${money(hourly * 0.5)}　1時間：約 ${money(hourly)}</div>`;

  $("gpu-card").innerHTML = head
    + `<div class="gpu-name">${prefix}${label}</div>`
    + price
    + (card.warning && !running ? `<p class="hint warn">${card.warning}</p>` : "");
}

let progressFolded = false;   // once folded, a later poll must not reopen it

function renderGpuProgress(start) {
  const el = $("gpu-progress");
  const wrap = $("gpu-progress-wrap");
  const stages = (start && start.stages) || [];
  const show = stages.some((s) => s.state !== "pending");
  wrap.hidden = !show;
  if (!show) { progressFolded = false; return; }
  const MARK = { pending: "·", active: "▶", done: "✓", failed: "✗" };
  el.innerHTML = stages.map((s) =>
    `<li class="${s.state}"><span class="mark">${MARK[s.state] || "·"}</span>${s.label}`
    + (s.detail ? `<span class="detail">${s.detail}</span>` : "") + "</li>").join("");

  const done = stages.filter((s) => s.state === "done").length;
  const failed = stages.some((s) => s.state === "failed");
  const running = stages.some((s) => s.state === "active");
  setText($("gpu-progress-summary"), failed
    ? `起動に失敗しました（${done}/${stages.length} 完了）`
    : running ? `起動中… ${done}/${stages.length}`
    : `✓ 起動の記録（${done}/${stages.length}）`);
  /* Open while it is working, because that is the only place the stages are
     visible; folded once, and it stays folded -- the poll runs every couple of
     seconds and would otherwise reopen it under the operator's hands. */
  if (running || failed) {
    if (!progressFolded) wrap.open = true;
  } else if (!progressFolded) {
    wrap.open = false;
    progressFolded = true;
  }
}

$("gpu-progress-wrap").addEventListener("toggle", () => {
  progressFolded = true;      // whichever way they moved it, it was on purpose
});

/* An expensive card is never taken automatically. It is shown with its price
   and three ways out, and nothing starts until one of them is pressed. */
function renderGpuOffer(g) {
  const el = $("gpu-offer");
  const start = g.start || {};
  const offers = start.offers || [];
  const show = start.state === "needs_confirmation" && offers.length;
  el.hidden = !show;
  if (!show) return;
  const money = (v) => (v == null ? "価格不明" : "$" + Number(v).toFixed(2) + " / 時");
  el.innerHTML =
    "<h3>RTX 6000 Ada 48GB は現在確保できませんでした</h3>"
    + '<p class="hint">高額な GPU は確認なしでは起動しません。空き状況によっては、選んでも確保できないことがあります。</p>'
    + offers.map((o) => `
      <div class="opt">
        <span class="p">${o.label}　<b>${money(o.usd_per_hour)}</b>
          ${o.usd_30min != null ? `　30分：約 $${o.usd_30min.toFixed(2)}` : ""}</span>
        <button class="btn" data-confirm-gpu="${o.gpu}" data-label="${esc(o.label)}"
          data-hour="${o.usd_per_hour != null ? o.usd_per_hour : ""}"
          data-half="${o.usd_30min != null ? o.usd_30min : ""}">これで開始</button>
      </div>`).join("")
    + '<div class="actions">'
    + '<button class="btn" id="btn-gpu-wait">待つ（もう一度 RTX 6000 Ada を探す）</button>'
    + '<button class="btn" id="btn-gpu-cancel">キャンセル</button>'
    + "</div>";
}

/* Real charges are labelled as real; arithmetic on an hourly rate is labelled
   an estimate; anything unreadable says so instead of showing a zero. */
function renderGpuCost(g) {
  const b = g.billing || {};
  const rows = [];
  const money = (v) => "$" + Number(v).toFixed(2);

  if (g.session_cost_usd != null) {
    const m = Math.round((g.session_seconds || 0) / 60);
    rows.push(`<div><span>現在のセッション（${m}分）</span><span class="est">推定 ${money(g.session_cost_usd)}</span></div>`);
  } else {
    rows.push('<div><span>現在のセッション</span><span class="est">$0.00</span></div>');
  }
  if (b.available && b.pods_today_usd != null) {
    rows.push(`<div><span>今日の GPU 料金（実データ）</span><span>${money(b.pods_today_usd)}</span></div>`);
  } else {
    rows.push('<div><span>今日の GPU 料金</span><span class="none">取得できません</span></div>');
  }
  if (b.volume_today_usd != null) {
    rows.push(`<div><span>今日の保存領域（実データ）</span><span>${money(b.volume_today_usd)}</span></div>`);
  } else {
    rows.push('<div><span>今日の保存領域</span><span class="none">取得できません</span></div>');
  }
  rows.push(`<div><span>Network Volume</span><span>${g.network_volume_id || "—"} / 保持中</span></div>`);
  $("gpu-cost").innerHTML = rows.join("");
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
function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? `${m}分${String(s).padStart(2, "0")}秒` : `${s}秒`;
}

/* How far along one generation is.
   The sampler only reports progress during its own phase, and the Qwen3-VL
   encode before it can take a minute on its own -- so a bar driven purely by
   the sampler sits at 0% for the part of the run that feels slowest. Elapsed
   time against the measured estimate always moves; when real sampler numbers
   arrive we take whichever is further along. Capped below 1 so it never looks
   finished before it is. */
/* Is there a real step count to draw, or only the knowledge that it is working?

   ComfyUI emits no progress at all during the Qwen3-VL encode -- 75-90 s on a
   cache MISS, and every Long Draft segment is a MISS -- and a recovered
   generation gets no events at all. Both used to render as a bar pinned near
   zero, which reads as "stuck" rather than "working". */
function genIndeterminate(g) {
  if (g.status !== "running" && g.status !== "queued" && g.status !== "submitting") return false;
  return !(g.sampler && g.sampler.max > 1);
}

function genStateText(g) {
  if (g.recovered) return "再接続して監視中…";
  if (g.status === "queued") return g.progress_text || "順番待ち";
  if (g.status === "submitting") return "投入中…";
  if (g.phase === "encode") return "エンコード中…";
  if (g.phase_label) return g.phase_label + "…";
  return "実行中…";
}

function genProgress(g, now) {
  if (g.status === "done") return 1;
  if (g.status === "error" || g.status === "cancelled") return 0;
  /* Only a measured step count draws a percentage. A time-against-estimate bar
     is a guess wearing a progress bar's clothes. */
  if (genIndeterminate(g)) return 0;
  return Math.max(0, Math.min(0.97, g.sampler.value / g.sampler.max));
}

/* Remaining seconds, preferring the rate this run is actually achieving. */
function genRemaining(g, now) {
  const s = g.sampler;
  if (s && s.seconds_per_step && s.max) {
    const stepsLeft = Math.max(0, s.max - s.value);
    const sinceStep = s.last_step_at ? now / 1000 - s.last_step_at : 0;
    return Math.max(0, stepsLeft * s.seconds_per_step - sinceStep);
  }
  if (g.started_at && g.expected_seconds) {
    return g.expected_seconds - (now / 1000 - g.started_at);
  }
  return null;
}

/* What the wait looks like.

   The numbers were all here already -- percentage, remaining, step count,
   which phase, how long the finished phases took -- rendered at 12px in the
   corner of the panel while two buttons nobody can press took the top of it.
   The figure checked every few minutes should be the largest thing on screen.

   Nothing new is computed: every value comes from the same helper the rail
   already used, so there is one answer per question rather than two. */
function renderProgress(box, g, now) {
  if (!box) return;
  if (g.status !== "running" && g.status !== "queued" && g.status !== "submitting") {
    box.hidden = true;
    setHtml(box, "");
    return;
  }
  box.hidden = false;
  const vague = genIndeterminate(g);
  const left = genRemaining(g, now);
  const s = g.sampler || {};
  /* No percentage during the encode: ComfyUI sends no progress at all there,
     and a bar driven by elapsed-against-estimate is a guess in a bar's
     clothing. Say what it is doing instead of inventing a number. */
  /* The encode runs for two minutes with no progress events at all, and then
     sampling starts. That used to swap a 19px phase name for a 34px
     percentage and fill an until-then empty slot beside it, moving everything
     below down by 14px -- measured -- after two minutes of a motionless
     panel. Both halves of the top line are now occupied in both states and
     the row keeps its height, so the change is a change of numbers rather
     than of layout.

     What is deliberately not done is showing a percentage during the encode:
     there is nothing to count, and a bar driven by elapsed-against-estimate
     is a guess wearing a progress bar's clothes. The clock is honest -- it is
     the only number that exists yet -- so it takes the same slot the
     percentage will take. */
  const elapsed = g.started_at ? fmtDuration(now / 1000 - g.started_at) : "";
  const hero = vague
    ? `<span class="p-pct p-clock">${esc(elapsed || "--")}</span>`
    : `<span class="p-pct">${Math.round(genProgress(g, now) * 100)}<i>%</i></span>`;
  const eta = vague
    ? `<span class="p-left">${esc(genStateText(g))}</span>`
    : (left != null && left > 3
       ? `<span class="p-left">残り約 ${esc(fmtDuration(left))}</span>` : "");
  const sub = vague
    ? `${esc(g.phase_label || "準備中")} · 経過時間`
    : `${esc(g.phase_label || "Sampling")} ${s.value | 0} / ${s.max | 0}`
      + (elapsed ? ` · ${elapsed} 経過` : "");

  const done = g.phase_seconds || {};
  /* The server sends these so a rename cannot leave the checklist naming a
     stage that no longer exists. The literal list is only a fallback for a
     Studio that has not been restarted since the field was added -- config
     wins whenever it is there. Mirrors jobs.PHASE_ORDER / PHASE_LABELS. */
  const order = ((state.cfg && state.cfg.phases) || []).length ? state.cfg.phases
    : [{ key: "transfer", label: "素材転送" }, { key: "encode", label: "Encoding" },
       { key: "sampling", label: "Sampling" }, { key: "decode", label: "VAE Decode" },
       { key: "save", label: "Save" }];
  const at = order.findIndex((p) => p.key === g.phase);
  const steps = order.map((p, i) => {
    /* A phase counts as finished when it has recorded seconds, or when the run
       has moved past it -- a phase can be skipped entirely (a cache HIT does
       no encode) and would otherwise sit unticked for the whole run. */
    const secs = done[p.key];
    const past = at >= 0 && i < at;
    const cls = p.key === g.phase ? "now" : (secs != null || past) ? "ok" : "";
    const mark = cls === "now" ? "◐" : cls === "ok" ? "✓" : "○";
    const val = p.key === g.phase && !vague && s.max
      ? `${s.value | 0}/${s.max | 0}`
      : secs != null ? fmtDuration(secs) : "";
    return `<li class="${cls}"><span class="p-mark">${mark}</span>`
      + `<span class="p-name">${esc(p.label)}</span>`
      + `<b>${esc(val)}</b></li>`;
  }).join("");
  setHtml(box, `<div class="p-hero">${hero}${eta}</div>`
    + `<div class="p-sub">${sub}</div><ol class="p-steps">${steps}</ol>`);
}

function genTiming(g, now) {
  /* `seconds` on a live generation; `generation_seconds` on a history row --
     the same measurement under two names, and the rail was blank for one. */
  if (g.status === "done") {
    const secs = g.seconds != null ? g.seconds : g.generation_seconds;
    return secs != null ? fmtDuration(secs) : "";
  }
  if (g.status !== "running" || !g.started_at) return "";
  const elapsed = now / 1000 - g.started_at;
  const left = genRemaining(g, now);
  /* The estimate is a median of past runs, so a run can outlast it. Saying
     "まもなく完了" for another ten minutes would be worse than saying nothing:
     admit it is running long instead. */
  let eta = "";
  if (left != null) {
    if (left > 3) eta = ` · 残り約${fmtDuration(left)}`;
    else if (left > -30) eta = " · まもなく完了";
    else eta = " · 予想より長くかかっています";
  }
  const rate = g.sampler && g.sampler.seconds_per_step
    ? ` · ${g.sampler.seconds_per_step}秒/step` : "";
  /* Say where the estimate came from: a median of comparable past runs, or a
     formula because nothing comparable has been measured here yet. */
  const src = g.expected_source && g.expected_source.indexOf("history") === 0
    ? "" : "（目安）";
  return `${fmtDuration(elapsed)}経過${eta}${src}${rate}`;
}

/* Re-render only the changing numbers, once a second, so the screen visibly
   moves between the 1.5 s status polls instead of appearing frozen. */
function tickResults() {
  if (!state.batch) return;
  const now = Date.now();
  state.batch.generations.forEach((g) => {
    const card = document.querySelector(`.card[data-gen="${g.id}"]`);
    if (!card) return;
    const wrap = card.querySelector(".bar-progress");
    if (wrap) wrap.classList.toggle("indeterminate", genIndeterminate(g));
    const bar = card.querySelector(".bar-progress > i");
    if (bar) bar.style.width = Math.round(genProgress(g, now) * 100) + "%";
    const timing = card.querySelector(".timing");
    if (timing) timing.textContent = genTiming(g, now);
  });
  const head = $("batch-progress");
  if (head && state.batch.total_count) {
    const b = state.batch;
    const busy = b.generations.some((g) => g.status === "queued" || g.status === "running");
    /* Prefer the measured rate of whatever is running now; fall back to the
       server's sum of placeholder estimates. */
    const running = b.generations.find((g) => g.status === "running");
    const live = running ? genRemaining(running, now) : null;
    const queued = b.generations.filter((g) => g.status === "queued").length;
    let total = null;
    if (live != null && live >= 0) {
      const per = running.sampler && running.sampler.seconds_per_step && running.sampler.max
        ? running.sampler.seconds_per_step * running.sampler.max
        : running.expected_seconds || 0;
      total = live + queued * per;
    } else if (b.remaining_seconds) {
      total = b.remaining_seconds;
    }
    const left = !busy ? "" : total != null
      ? ` · 全体の残り約${fmtDuration(total)}` : "";
    const JP_ST = { done: "完了", running: "生成中", queued: "待機",
                    error: "エラー", cancelled: "中止" };
    const per = b.generations
      .map((g, i) => `${i + 1}/${b.total_count} ${JP_ST[g.status] || g.status}`)
      .join(" · ");
    head.textContent = `${per}${left}`;
    head.hidden = false;
  } else if (head) {
    head.hidden = true;
  }
}
setInterval(tickResults, 1000);

/* ---- DOM reconciliation -------------------------------------------------
   The results panel and the Long Draft panel used to be rebuilt with
   innerHTML on every 1.5 s poll. Measured in the live page during a
   generation: 16 <video> elements were constructed in 12 seconds for the four
   on screen -- every poll tore all of them down and built them again. Each
   rebuild re-requested the file (the route sent Cache-Control: no-store),
   re-read its metadata, and threw away currentTime and any playback.

   So elements are keyed by their id in the DATA and reused. Never by comparing
   element.src: the browser normalises that to an absolute URL, so the
   comparison against the relative one is always false and the node gets
   replaced anyway while looking like it was checked. */
function ensureChild(parent, key, build) {
  let el = parent.querySelector(':scope > [data-key="' + key + '"]');
  if (!el) {
    el = build();
    el.dataset.key = key;
    parent.appendChild(el);
  }
  return el;
}

function pruneChildren(parent, keys) {
  Array.from(parent.children).forEach((c) => {
    if (!keys.has(c.dataset.key)) c.remove();
  });
}

function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function setHtml(el, html) {
  if (el && el.innerHTML !== html) el.innerHTML = html;
}

function setClass(el, name, on) {
  if (el) el.classList.toggle(name, Boolean(on));
}

/* One <video> per source, created once and left alone. Reassigning src -- even
   to the identical string -- restarts the load algorithm and loses playback
   position, so the URL is remembered in a data attribute instead. */
function ensureVideo(slot, url) {
  if (!slot) return null;
  if (!url) {
    const gone = slot.querySelector("video");
    if (gone) gone.remove();
    return null;
  }
  const existing = slot.querySelector("video");
  if (existing && existing.dataset.src === url) return existing;
  if (existing) existing.remove();
  slot.querySelectorAll(".ph").forEach((n) => n.remove());
  const v = document.createElement("video");
  v.controls = true;
  v.preload = "metadata";
  v.playsInline = true;
  v.dataset.src = url;
  v.src = url;
  slot.appendChild(v);
  return v;
}

function buildResultCard() {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = '<div class="vid"></div>'
    + '<div class="bar-progress"><i></i></div>'
    + '<div class="prog" hidden></div>'
    + '<div class="meta"><span class="who"></span><span class="tagwrap"></span></div>'
    + '<div class="meta"><span class="stage"></span><span class="timing"></span></div>'
    + '<div class="acts">'
    + '<button class="btn" data-act="zoom" hidden>大きく見る</button>'
    + '<button class="btn" data-act="select"></button>'
    + '<button class="btn" data-act="export">書き出し</button>'
    /* An anchor, not a button: /api/video/<id> already falls back to the
       history row (server.py _video), so this works for a generation this
       process never saw -- which is exactly when 書き出し fails. The action
       handler below matches button[data-act], so this is not caught by it. */
    + '<a class="btn" data-act="download" download>ダウンロード</a></div>';
  return card;
}

function renderResults(batch) {
  state.batch = batch;
  /* Anything that puts a batch on screen is a decision to look at one, so the
     dismissal is spent. */
  state.resultsDismissed = false;
  const sel = batch.generations.find((g) => g.selected);
  state.selected = sel ? sel.id : null;
  const root = $("results");
  const now = Date.now();
  const keys = new Set();
  /* One take fills the widest area on the screen; several are compared in a
     grid. Measured before this: a single take used 12% of the centre column
     and the video somebody waited 22 minutes for was shown at 268px. */
  root.classList.toggle("solo", batch.generations.length === 1);

  batch.generations.forEach((g, i) => {
    keys.add(g.id);
    const card = ensureChild(root, g.id, buildResultCard);
    card.dataset.gen = g.id;
    setClass(card, "sel", g.selected);
    setClass(card, "done", g.status === "done");
    setClass(card, "err", g.status === "error");
    setClass(card, "cancelled", g.status === "cancelled");

    const slot = card.querySelector(".vid");
    if (g.output_url) {
      ensureVideo(slot, g.output_url);
    } else {
      ensureVideo(slot, null);
      /* A generation takes eighteen minutes, and for all of them the largest
         area on the screen was a black rectangle with a word in it. The
         material being traced is the one picture that is certainly available
         and certainly relevant, so it waits there instead. */
      const src = g.status === "error" ? null : sourcePoster(batch);
      const body = g.status === "error"
        ? "エラー<br>" + esc(g.error || "")
        : esc(JP_SEG[g.status] || g.progress_text || g.status);
      setHtml(slot, src
        ? `<div class="ph src" style="background-image:url('${src}')">
             <span class="ph-label">${body}</span></div>`
        : '<div class="ph">' + body + "</div>");
    }

    const bar = card.querySelector(".bar-progress");
    setClass(bar, "indeterminate", genIndeterminate(g));
    const fill = card.querySelector(".bar-progress > i");
    const width = Math.round(genProgress(g, now) * 100) + "%";
    if (fill && fill.style.width !== width) fill.style.width = width;

    /* A live generation carries params as an object; a row opened from history
       carries the same facts as columns and params as a JSON string. Read
       whichever is there rather than showing "#1" for half the shelf. */
    const p = (g.params && typeof g.params === "object") ? g.params : {};
    const bits = [];
    const dur = p.duration || g.duration;
    const mp = p.megapixels || g.resolution_mp;
    const steps = p.steps || g.steps;
    if (dur) bits.push(dur + "秒");
    if (mp) bits.push(mp + " MP");
    if (steps) bits.push(steps + " steps");
    setHtml(card.querySelector(".who"),
      (bits.length ? esc(bits.join(" · ")) : `#${i + 1}`) +
      `<span class="seed">seed ${g.seed}</span>`);
    setHtml(card.querySelector(".tagwrap"), g.cache_result
      ? `<span class="tag ${esc(g.cache_result)}">${esc(JP_CACHE[g.cache_result] || g.cache_result)}</span>`
      : (g.status === "error" ? '<span class="tag err">エラー</span>' : ""));
    setText(card.querySelector(".stage"), genIndeterminate(g)
      ? genStateText(g)
      /* A history row has no progress_text, so it fell through to the raw
         status -- the rail read "done" in an otherwise Japanese panel. */
      /* progress_text is initialised to the literal "queued" server-side, so
         preferring it over the map printed English into a Japanese panel. */
      : (g.phase_label ? g.phase_label + " · " : "") +
        (g.status === "queued" || g.status === "submitting"
          ? JP_SEG[g.status] : (g.progress_text || JP_SEG[g.status] || g.status)));
    setText(card.querySelector(".timing"), genTiming(g, now));
    renderProgress(card.querySelector(".prog"), g, now);
    renderResultsClear();
    /* A portrait clip in a landscape card is bounded by the card's height, not
       its column: 473px tall at 9:16 is 264px wide however much room the panel
       has sideways. Widening the column does nothing; the video needs the
       screen. Only offered once there is something to look at. */
    const zoom = card.querySelector('[data-act="zoom"]');
    if (zoom) zoom.hidden = !g.output_url;

    const pick = card.querySelector('[data-act="select"]');
    setText(pick, g.selected ? "★ 選択中" : "これを選ぶ");
    pick.title = g.selected ? "クリックで選択を解除します" : "";
    setClass(pick, "btn-primary", g.selected);
    pick.disabled = g.status !== "done";
    card.querySelector('[data-act="export"]').disabled = g.status !== "done";
    const dl = card.querySelector('[data-act="download"]');
    dl.hidden = g.status !== "done";
    if (!dl.hidden) {
      dl.href = "/api/video/" + g.id;
      /* A live batch carries meta.mode; a history row carries mode. */
      dl.download = `H3_${(batch.meta && batch.meta.mode) || batch.mode || "video"}_${g.id}.mp4`;
    }
  });

  pruneChildren(root, keys);
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
      /* A toggle, not a one-way door. /api/select has taken a boolean all
         along; the page only ever sent true, so a star could be put on and
         never taken off. */
      const on = btn.closest(".card").classList.contains("sel");
      await api("/api/select", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ generation_id: genId, selected: !on }),
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
  renderExtraImages();
});

$("draftq-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled) return;
  state.draftQuality = b.dataset.dq;
  [...$("draftq-seg").children].forEach((c) => c.classList.toggle("on", c === b));
  renderCacheHint();
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
  /* The helper panel replaces the old placeholder-only hint. A placeholder
     disappears the moment anyone types, which is exactly when the advice was
     needed -- an untagged prompt re-renders the source video and the operator
     finds out 46 minutes later. */
  const helper = $("mt-prompt-helper");
  if (helper) helper.hidden = state.mode !== "motion_trace";
  if (state.mode !== "motion_trace") return;
  const t = $("prompt");
  if (t.value.trim()) return;
  const mt = (state.cfg && state.cfg.motion_trace) || {};
  t.placeholder =
    "下の「プロンプトを作る」を押すと、" + (mt.identity_tag || "<Picture 1>") +
    " と " + (mt.motion_tag || "<Video 1>") + " を使った形で入ります";
}

async function buildMotionPrompt() {
  try {
    const r = await api("/api/motion_prompt", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        person: $("mtp-person").value,
        clothing: $("mtp-clothing").value,
        background: $("mtp-background").value,
        props: $("mtp-props").value,
        extra: $("mtp-extra").value,
      }),
    });
    const t = $("prompt");
    if (t.value.trim() && !confirm("いま書かれているプロンプトを置き換えます。よろしいですか？")) return;
    t.value = r.prompt;
    t.focus();
    notice("info", "プロンプトを作りました。そのまま編集できます。");
  } catch (e) { notice("error", e.message); }
}

const btnMtp = $("btn-mtp-build");
if (btnMtp) btnMtp.addEventListener("click", buildMotionPrompt);
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
$("duration").addEventListener("change", () => {
  renderCacheHint(); renderDurationHint(); renderSourceBudget();
});

/* H3 is trained on roughly 124-362 frames at 24 fps (about 5-15 s). Cost scales
   with the frame count, so a long clip is dramatically slower -- an 18 s Motion
   Trace measured 212 s per sampling step, about 43 minutes for one draft. */
/* H3 reads a COUNT OF FRAMES at 24 fps, whatever the source's real rate is.
   Three quantities follow from that and the UI used to show one: the frames
   rendered, the seconds of output, and the seconds of SOURCE actually
   consumed. A 12 s cut of a 30 fps video supplies only 9.8 s of action, played
   over 12.25 s -- 0.8x, with 2.2 s of the cut never read. No post-processing
   recovers the part that was not read, so it is said before the GPU starts. */
const H3_FPS = 24;

function h3Frames(seconds) {
  let n = Math.max(5, Math.round(seconds * H3_FPS));
  return n + (5 - (n % 17)) % 17;
}

function renderSourceBudget() {
  const el = $("source-budget");
  if (!el) return;
  const meta = state.sourceVideoMeta || {};
  const wanted = Number($("duration").value) || 0;
  const inputs = (state.cfg && state.cfg.mode_inputs[state.mode]) || [];
  if (!state.sourceVideo || inputs.indexOf("source_video") < 0 || !wanted) {
    el.hidden = true;
    return;
  }
  const frames = h3Frames(wanted);
  const out = frames / H3_FPS;
  const lines = [];
  if (!meta.fps) {
    lines.push(`${wanted}秒 = ${frames}フレーム → 出力 ${out.toFixed(1)}秒。` +
               `元動画のフレームレートが読めないため、何秒ぶん使われるかは分かりません。`);
  } else {
    const usedSec = frames / meta.fps;
    const have = meta.frames || (meta.duration ? Math.round(meta.duration * meta.fps) : null);
    lines.push(`${wanted}秒 = ${frames}フレーム → 出力 ${out.toFixed(1)}秒。` +
               `元動画（${meta.fps} fps）からは先頭 ${usedSec.toFixed(1)}秒を使います。`);
    if (have !== null && have < frames) {
      const need = frames / meta.fps;
      lines.push(`⚠ 元動画が足りません（${have}フレームしかありません）。` +
                 `${need.toFixed(2)}秒以上に切り直してください。`);
    } else if (meta.duration && meta.duration - usedSec > 0.05) {
      lines.push(`残り ${(meta.duration - usedSec).toFixed(1)}秒は使われません。` +
                 (Math.abs(meta.fps - H3_FPS) > 1
                   ? `24 fps に変換すれば同じ${frames}フレームで ${out.toFixed(1)}秒ぶんを等速で使えます。`
                   : ""));
    }
  }
  el.textContent = lines.join(" ");
  el.hidden = false;
}

function renderDurationHint() {
  const el = $("duration-hint");
  if (!el) return;
  const seconds = Number($("duration").value) || 0;
  let frames = Math.max(5, Math.round(seconds * 24));
  frames += (5 - (frames % 17)) % 17;
  let text = "";
  if (frames > 362) {
    text = `${seconds}秒 = ${frames}フレーム。H3 の学習範囲（約5〜15秒）を超えており、` +
           `1本あたり数十分かかることがあります。`;
  } else if (seconds > 8) {
    text = `${seconds}秒 = ${frames}フレーム。長いほど生成時間は比例して伸びます。`;
  }
  el.textContent = text;
  el.hidden = !text;
}
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
    state.sourceVideoLocal = r.local_path;
    const p = r.probe || {};
    /* f.name is what the operator chose; r.video is the name ComfyUI assigned
       to it, which means nothing to them. Units in Japanese, like the rest. */
    const bits = [f.name, (Math.round(r.bytes / 1024 / 1024 * 10) / 10) + " MB"];
    if (p.duration) bits.push(p.duration + "秒");
    if (p.fps) bits.push(p.fps + " fps");
    if (p.frames) bits.push(p.frames + "フレーム");
    if (!r.sent_to_comfyui) bits.push("GPU起動後に自動で転送します");
    $("video-status").textContent = bits.join(" · ");
    renderMaterialClears();
    renderReadiness();
    $("video-notes").textContent = (p.notes || []).join("\n");
    if (p.ok === false) notice("warn", (p.notes || []).join(" / "));
    const v = $("video-preview");
    v.src = URL.createObjectURL(f);
    v.hidden = false;
    renderCacheHint();
    renderSourceBudget();
    refreshLongDraftPlan();
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
      `${f.name}（${Math.round(r.bytes / 1024)} KB）` +
      (r.sent_to_comfyui ? "" : " · GPU起動後に自動で転送します");
    renderReadiness();
    renderMaterialClears();
    const img = $("image-preview");
    img.src = URL.createObjectURL(f);
    img.hidden = false;
    renderCacheHint();
  } catch (err) {
    $("image-status").textContent = "";
    notice("error", err.message);
  }
});

/* Additional photographs of the same person.

   The identity image enters the node as img[:1] -- one frame -- while the
   reference video enters as one frame every half second. Measured on a real
   run: 1 against 21, and the output kept the video subject's face and took
   only the background from the photograph. Reference Strength cannot fix it
   (it noises both references together) and shortening the clip costs the
   clip, so more photographs is the only lever that leaves the brief intact. */
const MAX_EXTRA_IMAGES = 8;

function renderExtraImages() {
  const wrap = $("field-extra-images");
  if (wrap) wrap.hidden = state.mode !== "motion_trace";
  const box = $("extra-thumbs");
  if (!box) return;
  setHtml(box, (state.extraImages || []).map((x, i) =>
    `<span class="thumb"><img src="/api/input/${encodeURIComponent(x)}" alt="">`
    + `<button class="thumb-x" data-drop="${i}" title="外す">×</button></span>`).join(""));
  const n = (state.extraImages || []).length;
  setText($("extra-images-status"), n
    ? `${n} 枚（人物 ${n + 1} : 動画の見え方が変わります）` : "");
}

$("extra-thumbs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-drop]");
  if (!b) return;
  state.extraImages.splice(Number(b.dataset.drop), 1);
  renderExtraImages();
  renderCacheHint();
});

/* Choosing a photograph or a video was a one-way door: there was no way to
   put it back, and the only route to a clean slate was reloading the page --
   which, until this session, also discarded every other setting. */
function clearMaterial(kind) {
  if (kind === "image") {
    state.image = null;
    state.localPath = null;
    $("image-preview").hidden = true;
    $("image-preview").removeAttribute("src");
    $("image-file").value = "";
    setText($("image-status"), "");
  } else {
    state.sourceVideo = null;
    state.sourceVideoMeta = null;
    state.sourceVideoLocal = null;
    const v = $("video-preview");
    v.hidden = true;
    v.removeAttribute("src");
    v.removeAttribute("poster");
    v.load();
    $("video-file").value = "";
    setText($("video-status"), "");
    setText($("video-notes"), "");
    refreshLongDraftPlan();
  }
  renderMaterialClears();
  renderReadiness();
  renderCacheHint();
  renderSourceBudget();
  loadHistory();          // the shelf is scoped to the reference video
}

function renderMaterialClears() {
  const i = $("image-clear"), v = $("video-clear");
  if (i) i.hidden = !state.image;
  if (v) v.hidden = !state.sourceVideo;
}

$("image-clear").addEventListener("click", () => clearMaterial("image"));
$("video-clear").addEventListener("click", () => clearMaterial("video"));

$("extra-images-file").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (!files.length) return;
  const room = MAX_EXTRA_IMAGES - (state.extraImages || []).length;
  if (room <= 0) {
    notice("warn", `追加写真は ${MAX_EXTRA_IMAGES} 枚までです。`);
    return;
  }
  setText($("extra-images-status"), "アップロード中…");
  for (const f of files.slice(0, room)) {
    try {
      const fd = new FormData();
      fd.append("image", f, f.name);
      const r = await api("/api/upload", { method: "POST", body: fd });
      state.extraImages.push(r.image);
    } catch (err) { notice("error", `${f.name}: ${err.message}`); }
  }
  if (files.length > room) {
    notice("warn", `${MAX_EXTRA_IMAGES} 枚を超えたぶんは読み込んでいません。`);
  }
  renderExtraImages();
  renderCacheHint();
});

$("btn-generate").addEventListener("click", async () => {
  /* Long Draft is a different submission entirely: one parent run of N
     sequential segments, not N variations of one clip. */
  if (longDraftOn()) {
    $("btn-generate").disabled = true;
    try { await startLongDraft(); }
    catch (e) { notice("error", e.message); }
    finally { $("btn-generate").disabled = false; }
    return;
  }
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
  if (state.mode === "motion_trace" && (state.extraImages || []).length) {
    payload.extra_images = state.extraImages.slice(0, MAX_EXTRA_IMAGES);
  }
  /* Turbo only applies to a Draft, and only in the FL2VA modes. Final is never
     Turbo -- the server enforces that too. */
  const turboModes = (state.cfg.draft_quality || {}).turbo_modes || [];
  payload.turbo = state.quality === "draft" && state.draftQuality === "turbo" &&
                  turboModes.indexOf(state.mode) >= 0;
  if (inputs.indexOf("reference_strength") >= 0 && !$("refstrength").disabled) {
    payload.reference_strength = Number($("refstrength").value);
  }
  /* graph.build does preset.update(overrides), so anything omitted here keeps
     the preset's value. An empty object is sent as nothing at all. */
  const ov = overrides();
  if (Object.keys(ov).length) payload.quality_overrides = ov;
  /* A fixed seed is what makes two runs comparable; it is also a cache HIT, so
     it is the one re-run that costs nothing but sampling. */
  if (state.seedFixed != null) {
    payload.seeds = Array.from({ length: state.variations }, (_, i) => state.seedFixed + i);
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
      aspect_ratio: payload.aspect_ratio, megapixels: effective().megapixels,
      duration: payload.duration, ref_image_size: state.refImageSize,
    };
    renderResults(batch);
    renderCacheHint();
  } catch (e) {
    /* The one refusal that comes with its own remedy: Motion Trace without
       reference tags. Offer the working prompt rather than just saying no. */
    if (e.data && e.data.kind === "motion_prompt_untagged") {
      notice("warn", e.data.error, 0);
      if (confirm("プロンプトに <Picture 1> / <Video 1> がありません。\n\n"
                  + "このままだと元動画をなぞるだけになります。\n"
                  + "使える形のプロンプトに置き換えますか？\n\n" + e.data.suggested_prompt)) {
        $("prompt").value = e.data.suggested_prompt;
        $("prompt").focus();
        notice("info", "置き換えました。内容を確認してからもう一度「生成する」を押してください。");
      }
      $("btn-generate").disabled = false;
      return;
    }
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

async function startGpu(confirmGpu, label) {
  $("btn-gpu-start").disabled = true;
  /* The label is the card's name; confirmGpu is the machine id the API wants. */
  notice("info", confirmGpu ? `${label || confirmGpu} で起動します…` : "GPU を起動しています…", 4000);
  try {
    /* The server answers as soon as the sequence is accepted, not when it
       finishes: Pod start plus ComfyUI boot is minutes, and a request left open
       that long is one the browser eventually abandons -- which used to leave
       the page saying "error" while the Pod came up and billed. Progress
       arrives through /api/status, so a reload picks it up unchanged. */
    const body = JSON.stringify(confirmGpu ? { confirm_gpu: confirmGpu } : {});
    const r = await api("/api/gpu/start", {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    });
    if (r.needs_confirmation) {
      notice("warn", "RTX 6000 Ada を確保できませんでした。GPU パネルで選んでください。", 0);
    } else if (r.retryable) {
      notice("warn", "GPU の空き待ちです。Pod・データは無事で、課金も発生していません。"
                   + "数分おいて START GPU をもう一度押してください。", 0);
    }
  } catch (e) {
    notice("error", "起動リクエストを受け付けられませんでした（" + e.message + "）。"
                  + "状態表示を確認し、動いていて不要なら STOP GPU を押してください。", 0);
  }
  pollStatus();
}

$("btn-gpu-start").addEventListener("click", () => startGpu(null));

$("gpu-offer").addEventListener("click", (e) => {
  const pick = e.target.closest("[data-confirm-gpu]");
  if (pick) {
    /* Built from the offer's own values. Reading parentElement.textContent
       swept up the button's caption, so the one dialog that authorises
       unplanned GPU spend read "…30分：約 $0.60これで開始". */
    const d = pick.dataset;
    const lines = [d.label || d.confirmGpu];
    if (d.hour) lines.push(`1時間 約$${Number(d.hour).toFixed(2)}`);
    if (d.half) lines.push(`30分 約$${Number(d.half).toFixed(2)}`);
    if (!confirm("高額な GPU を起動します。\n\n" + lines.join("\n") + "\n\nよろしいですか？")) return;
    return startGpu(pick.dataset.confirmGpu, d.label);
  }
  if (e.target.id === "btn-gpu-wait") return startGpu(null);
  if (e.target.id === "btn-gpu-cancel") {
    $("gpu-offer").hidden = true;
    notice("info", "起動をキャンセルしました。GPU は起動していません。");
  }
});

$("btn-gpu-stop").addEventListener("click", async () => {
  if (!confirm("Pod を停止します（課金停止）。Network Volume とモデル・キャッシュ・出力は保持されます。")) return;

  const stop = (force) => api("/api/gpu/stop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force: force }),
  });

  try {
    let r = await stop(false);
    /* A busy Pod refuses, and the refusal is the only route to the force path.
       The escalation is never automatic: the operator is told what will be
       interrupted and has to say yes. Declining sends nothing, so the
       generation keeps running and the Pod keeps billing -- their call. */
    if (!r.ok && r.needs_force) {
      const n = r.active_jobs || 0;
      const ok = confirm(
        `生成中の処理が ${n} 件あります。\n\n`
        + "生成を中断して GPU を強制停止しますか？\n"
        + "・実行中の生成は中止され、完了していない動画は失われます\n"
        + "・完了済みの動画・Network Volume・モデル・キャッシュは保持されます");
      if (!ok) {
        notice("info", "停止をやめました。生成は続いていて、GPU は課金中のままです。", 0);
        pollStatus();
        return;
      }
      r = await stop(true);
    }
    /* "Stop requested" and "stopped" are different states and the difference is
       money, so say which one this was. */
    if (r.ok && r.verified) notice("info", "GPU を停止しました（RunPod で EXITED を確認済み）。Volume は保持されています。");
    else if (r.ok) notice("warn", r.error || "停止要求は通りましたが EXITED を確認できていません。", 0);
    else notice("warn", r.error || "GPU を停止できませんでした。", 0);
  } catch (e) {
    /* Stopping runs stop_h3.sh over SSH and then waits for RunPod to report
       EXITED, which can outlast the browser's patience. The server does not
       abandon it when the request is dropped. */
    notice("warn", "停止リクエストの応答を受け取れませんでした（" + e.message + "）。"
                 + "サーバ側では停止処理が続いている可能性があります。数十秒後に状態表示を確認し、"
                 + "まだ実行中ならもう一度 STOP GPU を押してください。", 0);
  }
  pollStatus();
});

$("btn-comfy-start").addEventListener("click", async () => {
  const btn = $("btn-comfy-start");
  btn.disabled = true;
  btn.textContent = "ComfyUI を起動中…";
  try {
    const r = await api("/api/comfyui/start", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (r.ok) notice("info", "ComfyUI を起動しました。生成を再開できます。");
    else notice("warn", r.error || "ComfyUI を起動できませんでした。", 0);
  } catch (e) {
    notice("warn", "起動要求の応答を受け取れませんでした（" + e.message + "）。"
                 + "サーバ側では続いている可能性があります。状態表示を確認してください。", 0);
  } finally {
    btn.disabled = false;
    btn.textContent = "ComfyUI を起動";
    pollStatus();
  }
});

$("stop-after-gen").addEventListener("change", async (e) => {
  try {
    await api("/api/gpu/auto_stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stop_after_generation: e.target.checked }),
    });
    notice("info", e.target.checked ? "生成が終わったらGPUを停止します。" : "生成後の自動停止をやめました。");
  } catch (err) { notice("error", err.message); }
});

$("auto-stop").addEventListener("change", async (e) => {
  try {
    await api("/api/gpu/auto_stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: Number(e.target.value) }),
    });
    notice("info", "自動停止: " + (e.target.value === "0" ? "OFF" : e.target.value + " 分"));
  } catch (err) { notice("error", err.message); }
});

/* History rows come from SQLite and carry output_path but no output_url; the
   video route now serves from history too, so the URL can be derived. This is
   what makes a finished clip survive a Studio restart, not just a reload. */
/* /api/poster 404s for anything made before posters existed, so the tile falls
   back to a label rather than a broken image icon. */
function thumb(genId, why) {
  /* "NO PREVIEW" on a cancelled attempt and on a finished take that simply
     predates posters looked identical, so a shelf of abandoned runs read the
     same as a shelf of work. Say which it is. */
  const label = why || "映像なし";
  if (!genId) return `<div class="h-thumb none">${esc(label)}</div>`;
  return `<img class="h-thumb" loading="lazy" alt="" src="/api/poster/${genId}"
    onerror="this.outerHTML='&lt;div class=\'h-thumb none\'&gt;${esc(label)}&lt;/div&gt;'">`;
}

/* When the shelf was last looked at, so a take finished since then can be
   marked. Kept per browser: it is a reading aid, not project state. */
function shelfSeen() {
  try {
    const v = localStorage.getItem("h3_shelf_seen");
    if (v !== null) return Number(v) || 0;
    /* First run marks everything already made as seen. Opening the Studio and
       finding a year of work flagged "new" tells the operator nothing; the
       badge is only worth having if it means "since you last looked". */
    const now = Date.now();
    localStorage.setItem("h3_shelf_seen", String(now));
    return now;
  } catch (e) { return Date.now(); }
}
function markShelfSeen(t) {
  try {
    const cur = shelfSeen();
    if (t > cur) localStorage.setItem("h3_shelf_seen", String(t));
  } catch (e) { /* private window: badges simply stay on */ }
}
/* History stores these as Unix seconds, not as a date string, and Date.parse
   of a number is NaN -- which silently made every take look equally old and
   the badge never appear. */
function toMs(raw) {
  if (typeof raw === "number") return raw * 1000;
  return raw ? (Date.parse(raw) || 0) : 0;
}
function finishedAt(b) {
  let t = 0;
  (b.generations || []).forEach((g) => {
    if (g.status !== "done") return;
    const v = toMs(g.finished_at || g.created_at);
    if (v > t) t = v;
  });
  return t;
}

/* The still that stands for what is being made: the photograph for Image
   Trace, a frame of the reference for Motion Trace, and nothing for Prompt
   Cinema, which genuinely has no source. Falls back to what the material
   panel currently holds, because a live batch carries less than a history
   row does. */
function sourcePoster(batch) {
  const b = batch || {};
  /* A running batch carries what it was asked for under meta; a history row
     carries the same facts as columns. Read both before giving up and
     showing a black rectangle. */
  const m = b.meta || {};
  const img = b.image || m.image || state.image;
  if (img) return "/api/input/" + encodeURIComponent(img);
  const vid = b.source_video || m.source_video || state.sourceVideo;
  if (vid) return "/api/input/" + encodeURIComponent(vid) + "?poster=1";
  return null;
}

function historyBatchToView(b) {
  return Object.assign({}, b, {
    generations: (b.generations || []).map((g) => Object.assign({}, g, {
      output_url: (g.status === "done" && g.output_path) ? "/api/video/" + g.id : null,
    })),
  });
}

let historyBatches = [];
let longDraftHistory = [];

async function loadHistory() {
  try {
    const h = await api("/api/history?limit=15");
    historyBatches = h.batches;
    /* "These three videos were one Long Draft" is a fact that has to survive a
       Studio restart, so it is read back from history, not from memory. */
    longDraftHistory = h.long_drafts || [];
    /* A shelf is looked at, not read. Each tile is one picture and two short
       lines; the prompt excerpt that used to be the whole entry is the title
       attribute, for the one time somebody needs it. */
    const ldRows = longDraftHistory.map((d) => {
      const segs = d.segments || [];
      const done = segs.filter((x) => x.status === "done").length;
      const shot = segs.find((x) => x.status === "done" && x.generation_id);
      /* A Long Draft is the longest thing this program makes, so it is the
         one most likely to have finished while nobody was watching. */
      const ldFin = done ? toMs(d.finished_at || d.created_at) : 0;
      const ldNew = done > 0 && ldFin > shelfSeen();
      return `<div class="h-item h-ld${ldNew ? " h-new" : ""}" data-longdraft="${d.id}"
        data-finished="${ldFin}" title="${esc(d.prompt || "")}">
        ${ldNew ? '<span class="h-badge">新着</span>' : ""}
        ${thumb(shot && shot.generation_id, done ? "映像なし" : "中止した")}
        <div class="h-meta"><b>長尺 · ${JP_MODE[d.mode] || d.mode}</b>
        <span class="h-sub">${(d.source_duration || 0).toFixed(1)}秒 · ${done}/${segs.length} 本</span></div></div>`;
    }).join("");
    /* Decision 4: the shelf is the work in progress, not the archive. Scoped
       to the reference video being used, so swapping the photograph keeps the
       takes and swapping the video changes them. Everything ever made is the
       History screen, which is where "search all of it" belongs. */
    /* Scoping to nothing is worse than not scoping: with the page limited to
       15 batches, a run of Image Trace work empties a Motion Trace shelf even
       though the takes exist. Fall back, and say which list is on screen. */
    const mine = state.sourceVideo
      ? h.batches.filter((b) => b.source_video === state.sourceVideo) : [];
    const scoped = mine.length ? mine : h.batches;
    setText($("shelf-title"), mine.length ? "この素材で作ったもの" : "最近作ったもの");
    $("history").innerHTML = ldRows + scoped.map((b) => {
      const done = b.generations.filter((g) => g.status === "done").length;
      const sel = b.generations.find((g) => g.selected);
      const shot = sel || b.generations.find((g) => g.status === "done");
      /* A batch where everything errored rendered "0/4本" -- identical to one
         still queued. The rail says which, without reading the numbers. */
      const running = b.generations.some((g) => ["running", "queued", "submitting"].includes(g.status));
      const st = running
        ? "h-run" : b.generations.some((g) => g.status === "error")
        ? "h-err" : done === 0 ? "h-cancelled" : "";
      /* The thing the operator came back to look at is the one that finished
         while they were away, and it used to sit in the row looking exactly
         like the thirty before it. */
      const fin = finishedAt(b);
      const isNew = done > 0 && fin > shelfSeen();
      const why = running ? "生成中" : b.generations.some((g) => g.status === "error")
        ? "失敗" : done === 0 ? "中止した" : "映像なし";
      return `<div class="h-item ${st}${isNew ? " h-new" : ""}" data-batch="${b.id}"
        data-finished="${fin}" title="${esc(b.prompt || "")}">
        ${isNew ? '<span class="h-badge">新着</span>' : ""}
        ${thumb(shot && shot.id, why)}
        <div class="h-meta"><b>${sel ? "★ " : ""}${JP_MODE[b.mode] || b.mode}</b>
        <span class="h-sub">${JP_KIND[b.kind] || b.kind} · ${done}/${b.generations.length}本${
          b.duration ? " · " + b.duration + "秒" : ""}</span></div></div>`;
    }).join("") || (ldRows ? "" :
      `<div class='hint'>${state.sourceVideo
        ? "この参照動画で作ったものはまだありません。" : "まだ履歴はありません。"}</div>`);

    /* Nothing on screen and nothing in this process's memory (a restarted
       Studio): show the most recent batch from history so the work is still
       there. */
    /* Restoring the last batch is for an empty panel nobody emptied -- a
       reopened tab, a restarted Studio. Emptying it on purpose is not that,
       and this ran every thirty seconds, so the × put the take back rather
       than taking it away. The same shape of mistake as the Long Draft panel:
       a rule written to not lose work, undoing a decision to let it go. */
    if (!state.batch && !state.resultsDismissed && h.batches.length) {
      const view = historyBatchToView(h.batches[0]);
      if (view.generations.some((g) => g.output_url)) renderResults(view);
    }
  } catch (e) { /* history is non-critical */ }
}

$("longdraft-run").addEventListener("click", (e) => {
  if (!e.target.closest("[data-ld-close]")) return;
  /* Dismissal has to outlast the next poll, which would otherwise put it
     straight back. Reopening is one click in the shelf. */
  state.longDraftDismissed = true;
  state.longDraftWatched = null;
  renderLongDraftRun(null);
});

$("history").addEventListener("click", (e) => {
  /* Monotonic: opening the newest clears every badge, opening an older one
     leaves the newer ones marked. */
  const t = e.target.closest("[data-finished]");
  if (t) markShelfSeen(Number(t.dataset.finished || 0));
}, true);

$("history").addEventListener("click", async (e) => {
  const ld = e.target.closest("[data-longdraft]");
  if (ld) {
    try {
      const run = await api("/api/long_draft/" + ld.dataset.longdraft);
      state.longDraftDismissed = false;
      state.longDraftWatched = run.id;
      renderLongDraftRun(run);
      notice("info", "履歴からこの長尺Draftを開きました。");
    } catch (err) { notice("error", err.message); }
    return;
  }
  const item = e.target.closest("[data-batch]");
  if (!item) return;
  const b = historyBatches.find((x) => x.id === item.dataset.batch);
  if (!b) return;
  renderResults(historyBatchToView(b));
  notice("info", "履歴からこのバッチを開きました。");
});

/* The source video is picked once and the photograph is what changes, so the
   video the operator was working with is put back rather than asked for again
   -- as a picture, not as a filename. /api/input serves the file itself out of
   the upload directory, and ?poster=1 returns one frame of a video, because a
   video cannot be an <img> and a black player is not a preview. */
async function restoreLastInputs() {
  let inputs;
  try { inputs = (await api("/api/last_inputs")).inputs || {}; }
  catch (e) { return; }
  if (inputs.video) {
    state.sourceVideo = inputs.video.name;
    state.sourceVideoMeta = inputs.video.meta || null;
    state.sourceVideoLocal = inputs.video.local;
    const p = state.sourceVideoMeta || {};
    const bits = [];
    if (p.duration) bits.push(p.duration + "秒");
    if (p.fps) bits.push(p.fps + " fps");
    if (p.frames) bits.push(p.frames + "フレーム");
    $("video-status").textContent = bits.join(" · ") || inputs.video.name;
    $("video-notes").textContent = (p.notes || []).join("\n");
    const v = $("video-preview");
    v.src = "/api/input/" + encodeURIComponent(inputs.video.name);
    v.poster = "/api/input/" + encodeURIComponent(inputs.video.name) + "?poster=1";
    v.hidden = false;
  }
  if (inputs.image) {
    state.image = inputs.image.name;
    state.localPath = inputs.image.local;
    $("image-status").textContent = inputs.image.name;
    const img = $("image-preview");
    img.src = "/api/input/" + encodeURIComponent(inputs.image.name);
    img.hidden = false;
  }
  renderReadiness();
  renderMaterialClears();
  renderCacheHint();
  renderSourceBudget();
  refreshLongDraftPlan();
  /* The shelf is scoped to the reference video, and until this resolves there
     is no reference video to scope to -- so the first paint has to be redone
     rather than left showing everything. */
  loadHistory();
}

/* The Pod's detail is a screen, not an overlay. 詳細 in the bar navigates to
   it; setDrawer is kept because it is the one entry point the rest of the file
   already knows about. */
function setDrawer(open) { if (open) go("gpu"); }
$("btn-drawer").addEventListener("click", () => go("gpu"));
$("btn-drawer-close").addEventListener("click", () => go("studio"));

/* ---------------------------------------------------------------- settings
   A reload used to bring the material back and nothing else, so the mode, the
   preset, the length, the aspect and the prompt all reverted to defaults --
   and the operator, who had reloaded because they were told to, silently lost
   a 10 second 9:16 1MP setup and a written prompt. Kept in this browser
   because it is what this browser was last showing, not project state. */
const REMEMBER_FIELDS = ["prompt", "duration", "aspect", "refstrength", "steps",
                         "megapixels", "seed",
                         "mtp-person", "mtp-clothing", "mtp-background",
                         "mtp-props", "mtp-extra"];

function rememberInputs() {
  try {
    const v = { mode: state.mode, variations: state.variations,
                refImageSize: state.refImageSize };
    const on = document.querySelector("#preset-seg button.on");
    if (on) v.preset = on.dataset.preset;
    REMEMBER_FIELDS.forEach((id) => {
      const el = $(id);
      if (!el || el.disabled) return;
      /* A <select> reads as "" until loadConfig fills its options. Saving that
         as a choice, and then restoring it, emptied the aspect ratio -- which
         graph.build refuses, so the next generation failed validation after a
         Pod had already been started. */
      if (el.tagName === "SELECT" && el.value === "" && el.options.length) return;
      v[id] = el.value;
    });
    localStorage.setItem("h3_inputs", JSON.stringify(v));
  } catch (e) { /* private window: settings simply do not persist */ }
}

function recallInputs() {
  let v;
  try { v = JSON.parse(localStorage.getItem("h3_inputs") || "null"); }
  catch (e) { return; }
  if (!v) return;
  /* Replayed as clicks on the real buttons rather than by assigning to state,
     so every side effect the handlers have -- hints, the preset echo, the
     cache estimate -- happens exactly as it does for a person. */
  const click = (sel) => { const b = document.querySelector(sel); if (b) b.click(); };
  if (v.mode) click(`#mode-seg button[data-mode="${v.mode}"]`);
  if (v.preset && typeof applyPreset === "function") applyPreset(v.preset);
  if (v.variations) click(`#var-seg button[data-var="${v.variations}"]`);
  if (v.refImageSize) state.refImageSize = v.refImageSize;
  /* Empty in one of these means "no override, use the preset", and the preset
     has just been applied. Writing the empty value back and firing change
     would clear what the preset set -- a restored 高解像度 that silently
     generated at 0.4 MP. */
  const OVERRIDES = ["steps", "megapixels", "seed"];
  REMEMBER_FIELDS.forEach((id) => {
    if (v[id] === undefined) return;
    if (OVERRIDES.includes(id) && v[id] === "") return;
    const el = $(id);
    if (!el) return;
    /* Never write a value the control cannot hold: a <select> silently goes
       blank instead of rejecting it. */
    if (el.tagName === "SELECT"
        && ![...el.options].some((o) => o.value === v[id])) return;
    el.value = v[id];
    /* The hints below these fields are driven by events, not by reading the
       DOM, so a restored value that never fired one shows the old hint. */
    el.dispatchEvent(new Event("change", { bubbles: true }));
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
}

let rememberTimer = null;
["click", "change", "input"].forEach((ev) =>
  document.addEventListener(ev, () => {
    clearTimeout(rememberTimer);
    rememberTimer = setTimeout(rememberInputs, 400);
  }));

(async function init() {
  try { await loadConfig(); maybeSuggestTags(); renderDurationHint(); }
  catch (e) { notice("error", "設定の読み込みに失敗: " + e.message); }
  recallInputs();
  renderExtraImages();
  renderMaterialClears();
  restoreLastInputs();
  pollStatus();
  loadHistory();
  setInterval(loadHistory, 30000);
  pollPremiere();
  setInterval(pollPremiere, 8000);
})();

/* ═══════════════════════════════════════════════════════════════════════════
   Five screens
   ═══════════════════════════════════════════════════════════════════════════
   The router only toggles which section is shown. Every screen is in the DOM
   from load, because the listeners above are bound by id at load: a screen
   built lazily is a screen whose buttons do nothing. Nothing here re-renders
   the Studio -- pollStatus keeps owning that. */

const SCREENS = ["studio", "history", "assets", "gpu", "settings"];

/* The live payload carries params as an object; the history DB carries the same
   thing as the TEXT column it was stored in. Reading one shape and being handed
   the other is why 解像度 and Steps rendered as em-dashes for every past run. */
function paramsOf(g) {
  const p = g && g.params;
  if (!p) return {};
  if (typeof p === "object") return p;
  try { return JSON.parse(p) || {}; } catch (e) { return {}; }
}

function go(screen) {
  if (SCREENS.indexOf(screen) < 0) screen = "studio";
  state.screen = screen;
  document.querySelectorAll("[data-screen]").forEach((el) => {
    el.hidden = el.dataset.screen !== screen;
  });
  document.querySelectorAll("#nav button").forEach((b) => {
    b.classList.toggle("on", b.dataset.nav === screen);
  });
  try { localStorage.setItem("h3.screen", screen); } catch (e) { /* private window */ }
  if (location.hash !== "#/" + screen) history.replaceState(null, "", "#/" + screen);
  if (screen === "history") loadHistoryScreen();
  if (screen === "assets") renderAssets();
}

$("nav").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-nav]");
  if (b) go(b.dataset.nav);
});
window.addEventListener("hashchange", () => go((location.hash || "").replace("#/", "")));

// ---------------------------------------------------------------- presets
/* Decision 3: the preset is the entry point and rewrites every value under it.
   Picking one clears the departures, so "標準" always means standard. */
const PRESET_MAP = {
  standard: { quality: "draft", megapixels: null },
  hires:    { quality: "draft", megapixels: 1.0 },
  final:    { quality: "final", megapixels: null },
};

function applyPreset(key) {
  const p = PRESET_MAP[key];
  if (!p) return;
  state.quality = p.quality;
  state.ov = { steps: null, megapixels: p.megapixels };
  document.querySelectorAll("#preset-seg button").forEach((b) =>
    b.classList.toggle("on", b.dataset.preset === key));
  /* quality-seg is hidden but still the thing the rest of the file reads. */
  document.querySelectorAll("#quality-seg button").forEach((b) =>
    b.classList.toggle("on", b.dataset.quality === p.quality));
  $("megapixels").value = p.megapixels == null ? "" : String(p.megapixels);
  renderPresetEcho();
  renderCacheHint();
  renderDurationHint();
}
$("preset-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-preset]");
  if (b) applyPreset(b.dataset.preset);
});

/* Resolution is offered as megapixels rather than as pixels: the width and
   height come from ComfyUI's own resolution node, and printing a w×h this file
   computed itself would be a number nobody measured. */
const MP_CHOICES = [
  { v: "",    label: "プリセットのまま" },
  { v: "0.4", label: "0.4 MP（検証済み・baseline）" },
  { v: "0.6", label: "0.6 MP（実験）" },
  { v: "1.0", label: "1.0 MP / 768p（実験・VRAM未検証）" },
];
$("megapixels").innerHTML = MP_CHOICES
  .map((c) => `<option value="${c.v}">${c.label}</option>`).join("");
$("megapixels").addEventListener("change", (e) => {
  state.ov.megapixels = e.target.value === "" ? null : Number(e.target.value);
  renderPresetEcho();
  renderCacheHint();   // resolution IS the fingerprint: this one costs an encode
});
$("steps").addEventListener("input", (e) => {
  const n = parseInt(e.target.value, 10);
  state.ov.steps = Number.isFinite(n) && n > 0 ? n : null;
  renderPresetEcho();  // steps are not in the fingerprint, so no forecast change
});
$("seed").addEventListener("input", (e) => {
  const n = parseInt(e.target.value, 10);
  /* Clamped here as well as refused by graph.build: a spinner that can reach
     -1 should not be able to spend a Pod start to find that out. */
  state.seedFixed = Number.isFinite(n) ? Math.max(0, Math.floor(n)) : null;
  const sh = $("seed-hint");
  sh.hidden = state.seedFixed == null;
  setText(sh, "Seed 固定中。この状態で seed だけ変えると、解析なしで別の絵が出ます。");
  renderPresetEcho();
});
$("btn-seed-dice").addEventListener("click", () => {
  $("seed").value = "";
  state.seedFixed = null;
  $("seed-hint").hidden = true;
  renderPresetEcho();
});

// -------------------------------------------------------------- 出力・納品
/* What actually came out, and what was done to it afterwards. Every line here
   is a fact the pipeline recorded -- the audio and the re-encode lines describe
   what postprocess.deliver() really did, not an option somebody could set. */
function renderDelivery() {
  const el = $("delivery-body");
  if (!el) return;
  const b = state.batch;
  const gens = (b && b.generations) || [];
  const g = gens.find((x) => x.selected && x.status === "done")
         || gens.slice().reverse().find((x) => x.status === "done");
  if (!g) {
    setHtml(el, '<p class="hint">まだ書き出したものがありません。</p>');
    return;
  }
  const pr = paramsOf(g);
  const rows = [
    ["解像度", pr.megapixels ? pr.megapixels + " MP" : "—"],
    ["長さ", pr.duration ? pr.duration + " 秒" : "—"],
    ["fps", "24（H3 の出力）"],
    ["Steps", pr.steps != null ? String(pr.steps) : "—"],
    ["Seed", g.seed != null ? String(g.seed) : "—"],
    ["解析", g.cache_result === "HIT" ? "なし（キャッシュ再利用）"
           : g.cache_result === "MISS" ? "あり" : "—"],
    ["生成時間", g.seconds ? fmtDuration(g.seconds) : "—"],
  ];
  const delivered = g.delivered;
  setHtml(el,
    `<video class="detail-media" controls preload="metadata" src="/api/video/${g.id}"
       poster="/api/poster/${g.id}"></video>`
    + '<dl class="kv">' + rows.map(([k, v]) =>
        `<div><dt>${k}</dt><dd>${esc(String(v))}</dd></div>`).join("") + "</dl>"
    + (delivered
        ? '<p class="hint">納品版を再生しています — 元動画の音声と再生速度を戻し、'
          + '映像は再エンコードしていません。</p>'
        : '<p class="hint">生成そのままの出力です（納品版はまだありません）。</p>')
    + '<div class="detail-acts">'
    + `<a class="btn" href="/api/video/${g.id}" download>ダウンロード</a>`
    + `<button class="btn" data-export="${g.id}">書き出し先に保存</button>`
    + `<button class="btn btn-primary" data-premiere="${g.id}">Premiere に送る</button>`
    + (premiereOn ? "" : '<p class="hint">Premiere は起動していません。押すと予約され、'
        + '次に Premiere を開いたときに自動で入ります。</p>')
    + "</div>");
}

/* Premiere on its own slow timer, not inside /api/status: the answer is a file
   the bridge panel rewrites every couple of seconds, and it changes only when
   an application is opened or closed. */
let premiereOn = false;
let premiereWhy = "";
let premierePending = 0;
async function pollPremiere() {
  let p = {};
  try { p = await api("/api/premiere"); }
  catch (e) { p = { available: false, reason: "Premiere の状態を確認できません。" }; }
  const was = premiereOn, wasPending = premierePending;
  premiereOn = Boolean(p.available);
  premierePending = p.pending || 0;
  premiereWhy = p.reason || "";
  const el = $("premiere-status");
  if (el) {
    setText(el, premiereOn
      ? ("接続しています。送ったものはプロジェクトの「H3 Studio」ビンに入ります。"
         + (premierePending ? `（${premierePending} 本を送信中）` : ""))
      : (premiereWhy + (premierePending ? ` 予約 ${premierePending} 本。` : "")));
    setClass(el, "warn", !premiereOn && premierePending > 0);
  }
  /* The hand-over happens in the Studio, so the first the page hears of it is
     the count reaching zero while Premiere is up. Say so once. */
  if (premiereOn && wasPending > 0 && premierePending === 0) {
    notice("info", `予約していた ${wasPending} 本を Premiere に入れました。`);
  }
  if (was !== premiereOn) renderDelivery();
}
/* Fills the window with the take and nothing else. Escape or a click on the
   backdrop puts it back -- the complaint that prompted this was not only that
   the video was small but that things in this panel were hard to undo. */
/* Emptying the panel. A finished take stayed in the largest area on the
   screen with nothing to dismiss it, which is the other half of "it is in the
   way once it is done". Never offered while something is running: the panel is
   the only place that shows it. */
function renderResultsClear() {
  const b = $("results-clear");
  if (!b) return;
  /* Anything that is not still going can be put away -- finished, errored,
     cancelled alike. Requiring a finished take was wrong in the one case where
     emptying the panel matters most: a batch that only errored has nothing
     "done" in it, so the × disappeared exactly when there was nothing worth
     looking at. Only a generation still in flight holds the panel, because
     this is the only place it is shown -- and it is read per batch rather than
     from the global busy flag, so another batch running elsewhere does not
     pin this one. */
  const live = (state.batch && state.batch.generations || []).some(
    (g) => ["running", "queued", "submitting"].includes(g.status));
  b.hidden = !state.batch || live;
}

$("results-clear").addEventListener("click", () => {
  state.batch = null;
  state.selected = null;
  state.resultsDismissed = true;
  setHtml($("results"), "");
  $("batch-progress").hidden = true;
  $("finish").hidden = true;
  renderResultsClear();
});

function setZoom(on) {
  document.body.classList.toggle("zoomed", on);
  if (!on) {
    const host = $("zoom-host");
    if (host) host.remove();
    return;
  }
}

$("results").addEventListener("click", (e) => {
  const b = e.target.closest('[data-act="zoom"]');
  if (!b) return;
  const card = b.closest(".card");
  const src = card && card.querySelector("video") && card.querySelector("video").src;
  if (!src) return;
  const host = document.createElement("div");
  host.id = "zoom-host";
  host.innerHTML = `<video src="${esc(src)}" controls autoplay loop playsinline></video>`
    + '<button class="zoom-x" title="閉じる（Esc）">×</button>';
  host.addEventListener("click", (ev) => {
    if (ev.target === host || ev.target.closest(".zoom-x")) setZoom(false);
  });
  document.body.appendChild(host);
  setZoom(true);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("zoomed")) setZoom(false);
});

$("delivery-body").addEventListener("click", async (e) => {
  const send = e.target.closest("[data-premiere]");
  if (send) {
    send.disabled = true;
    try {
      const r = await api("/api/premiere/send", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ generation_id: send.dataset.premiere }),
      });
      notice(r.ok ? "info" : "warn", r.queued
        ? `予約しました。次に Premiere を開いたときに自動で入ります（待ち ${r.pending} 本）。`
        : r.ok ? `Premiere に入れました（${r.project || "プロジェクト"} の「H3 Studio」ビン）。`
        : (r.error || "Premiere に送れませんでした。"));
    } catch (err) { notice("error", err.message); }
    finally { send.disabled = false; pollPremiere(); }
    return;
  }
  const b = e.target.closest("[data-export]");
  if (!b) return;
  try {
    const r = await api("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ generation_id: b.dataset.export }),
    });
    notice("info", "保存しました: " + r.path);
  } catch (err) { notice("error", err.message); }
});

// ------------------------------------------------------------- 2. History
/* Its own fetch, and a bigger page than the shelf: the poll payload carries a
   full workflow graph per generation, so the 30 s loop is left at 15 and the
   archive is only paid for when the archive is opened. */
let histBatches = [];
let histSel = null;
const histFilter = { q: "", mode: "all", st: "all", sort: "new" };
let histLoaded = false;

async function loadHistoryScreen(force) {
  if (histLoaded && !force) { renderHist(); return; }
  try {
    const h = await api("/api/history?limit=60");
    histBatches = h.batches || [];
    histLoaded = true;
  } catch (e) { notice("error", "履歴を読めませんでした: " + e.message); }
  renderHist();
}

function histRows() {
  const q = histFilter.q.trim().toLowerCase();
  return histBatches.filter((b) => {
    if (histFilter.mode !== "all" && b.mode !== histFilter.mode) return false;
    const done = b.generations.some((g) => g.status === "done");
    const err = b.generations.some((g) => g.status === "error");
    const run = b.generations.some((g) => ["running", "queued", "submitting"].includes(g.status));
    if (histFilter.st === "done" && !done) return false;
    if (histFilter.st === "error" && !err) return false;
    if (histFilter.st === "running" && !run) return false;
    if (q && (b.prompt || "").toLowerCase().indexOf(q) < 0) return false;
    return true;
  }).sort((a, b) => histFilter.sort === "old" ? a.created_at - b.created_at
    : histFilter.sort === "long" ? (b.duration || 0) - (a.duration || 0)
    : b.created_at - a.created_at);
}

function renderHist() {
  const facet = (id, opts, key) => {
    setHtml($(id), opts.map(([v, label, n]) =>
      `<button data-f="${v}" class="${histFilter[key] === v ? "on" : ""}">${label}<i>${n}</i></button>`
    ).join(""));
  };
  const countMode = (m) => histBatches.filter((b) => m === "all" || b.mode === m).length;
  facet("hist-modes", [["all", "すべて", countMode("all")],
    ["motion_trace", JP_MODE.motion_trace, countMode("motion_trace")],
    ["image_trace", JP_MODE.image_trace, countMode("image_trace")],
    ["prompt_cinema", JP_MODE.prompt_cinema, countMode("prompt_cinema")]], "mode");
  const cs = (f) => histBatches.filter(f).length;
  facet("hist-states", [["all", "すべて", histBatches.length],
    ["done", "完成", cs((b) => b.generations.some((g) => g.status === "done"))],
    ["running", "生成中", cs((b) => b.generations.some((g) => ["running", "queued", "submitting"].includes(g.status)))],
    ["error", "失敗", cs((b) => b.generations.some((g) => g.status === "error"))]], "st");

  const rows = histRows();
  setText($("hist-count"), `${rows.length} 件 / 全 ${histBatches.length} 件`);
  setHtml($("hist-grid"), rows.map((b) => {
    const sel = b.generations.find((g) => g.selected);
    const shot = sel || b.generations.find((g) => g.status === "done");
    const err = !shot && b.generations.some((g) => g.status === "error");
    const d = new Date((b.created_at || 0) * 1000);
    const when = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    return `<div class="tile ${histSel === b.id ? "on" : ""}" data-hb="${b.id}"
        title="${esc(b.prompt || "")}">
      ${thumb(shot && shot.id)}
      <div class="t-meta"><b>${esc((b.prompt || "（プロンプトなし）").slice(0, 28))}</b>
        <span>${when} · ${b.duration ? b.duration + "秒" : "—"}</span>
        <div class="badges"><span class="badge">${JP_MODE[b.mode] || b.mode}</span>
          <span class="badge ${b.kind === "final" ? "final" : ""}">${JP_KIND[b.kind] || b.kind}</span>
          ${err ? '<span class="badge err">失敗</span>' : ""}</div>
      </div></div>`;
  }).join("") || '<p class="hint">条件に合う履歴がありません。</p>');
}

$("hist-q").addEventListener("input", (e) => { histFilter.q = e.target.value; renderHist(); });
$("hist-sort").addEventListener("change", (e) => { histFilter.sort = e.target.value; renderHist(); });
$("hist-modes").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]"); if (!b) return;
  histFilter.mode = b.dataset.f; renderHist();
});
$("hist-states").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]"); if (!b) return;
  histFilter.st = b.dataset.f; renderHist();
});

$("hist-grid").addEventListener("click", (e) => {
  const t = e.target.closest("[data-hb]"); if (!t) return;
  /* Clicking the same tile again lets go of it. Every selection in this file
     should have a way back that is not a page reload. */
  histSel = histSel === t.dataset.hb ? null : t.dataset.hb;
  renderHist();
  renderHistDetail(histBatches.find((b) => b.id === histSel));
});

function renderHistDetail(b) {
  if (!b) return;
  const sel = b.generations.find((g) => g.selected);
  const shot = sel || b.generations.find((g) => g.status === "done") || b.generations[0] || {};
  const pr = paramsOf(shot);
  const d = new Date((b.created_at || 0) * 1000);
  const kv = [
    ["生成モード", JP_MODE[b.mode] || b.mode],
    ["種類", JP_KIND[b.kind] || b.kind],
    ["作成", d.toLocaleString("ja-JP")],
    ["長さ", b.duration ? b.duration + " 秒" : "—"],
    ["縦横比", b.aspect_ratio || "—"],
    ["解像度", pr.megapixels ? pr.megapixels + " MP" : "—"],
    ["Steps", pr.steps != null ? String(pr.steps) : "—"],
    ["Seed", shot.seed != null ? String(shot.seed) : "—"],
    ["忠実度", b.reference_strength != null ? String(b.reference_strength) : "—"],
    ["再現精度", b.ref_image_size || "—"],
    ["解析", shot.cache_result === "HIT" ? "なし（再利用）"
           : shot.cache_result === "MISS" ? "あり" : "—"],
  ];
  const mat = [];
  if (b.image) mat.push(`<img class="mat" src="/api/input/${encodeURIComponent(b.image)}" alt="" title="${esc(b.image)}">`);
  if (b.source_video) mat.push(`<img class="mat" src="/api/input/${encodeURIComponent(b.source_video)}?poster=1" alt="" title="${esc(b.source_video)}">`);

  setHtml($("hist-detail"),
    (shot.status === "done"
      ? `<video class="detail-media" controls preload="metadata" src="/api/video/${shot.id}" poster="/api/poster/${shot.id}"></video>`
      : '<p class="hint">再生できる出力がありません。</p>')
    + `<p class="hint">${esc(b.prompt || "")}</p>`
    + (mat.length ? '<div class="mats">' + mat.join("") + "</div>" : "")
    + '<dl class="kv">' + kv.map(([k, v]) =>
        `<div><dt>${k}</dt><dd>${esc(String(v))}</dd></div>`).join("") + "</dl>"
    + '<div class="detail-acts">'
    + `<button class="btn btn-primary" data-open="${b.id}">Studio で開く</button>`
    + (shot.status === "done" ? `<a class="btn" href="/api/video/${shot.id}" download>ダウンロード</a>` : "")
    + "</div>");
}

/* The reason this screen exists (指示書 §5): a past generation comes back as a
   starting point, not as a video to look at. Everything the form can hold is
   restored; the material is restored by name, which is what /api/input serves. */
$("hist-detail").addEventListener("click", (e) => {
  const b = e.target.closest("[data-open]"); if (!b) return;
  const batch = histBatches.find((x) => x.id === b.dataset.open);
  if (batch) openInStudio(batch);
});

function setMode(mode) {
  if (!mode) return;
  state.mode = mode;
  document.querySelectorAll("#mode-seg button").forEach((x) =>
    x.classList.toggle("on", x.dataset.mode === mode));
  renderModeHints();
}

function openInStudio(b) {
  setMode(b.mode);
  applyPreset(b.kind === "final" ? "final" : "standard");
  /* applyPreset clears the departures, which is right when a preset is picked
     and wrong here: a take made at 1.0 MP or 20 steps came back as the preset's
     0.4 MP / 12, so "この設定を戻す" quietly returned different settings. The
     values are in params; put them back after the preset has been applied. */
  const shot = (b.generations || []).find((g) => g.selected)
            || (b.generations || []).find((g) => g.status === "done")
            || (b.generations || [])[0];
  const pr = paramsOf(shot || {});
  if (pr.megapixels != null) {
    state.ov.megapixels = pr.megapixels;
    $("megapixels").value = String(pr.megapixels);
  }
  if (pr.steps != null) {
    state.ov.steps = pr.steps;
    $("steps").value = String(pr.steps);
  }
  renderPresetEcho();
  $("prompt").value = b.prompt || "";
  if (b.aspect_ratio) $("aspect").value = b.aspect_ratio;
  if (b.duration) $("duration").value = b.duration;
  if (b.reference_strength != null && !$("refstrength").disabled) {
    $("refstrength").value = b.reference_strength;
    setText($("ref-value"), String(b.reference_strength));
  }
  if (b.ref_image_size) {
    state.refImageSize = b.ref_image_size;
    document.querySelectorAll("#refsize-seg button").forEach((x) =>
      x.classList.toggle("on", x.dataset.refsize === b.ref_image_size));
  }
  if (b.image) {
    state.image = b.image;
    state.localPath = b.image_source || null;
    $("image-preview").src = "/api/input/" + encodeURIComponent(b.image);
    $("image-preview").hidden = false;
    setText($("image-status"), b.image);
  }
  if (b.source_video) {
    state.sourceVideo = b.source_video;
    try { state.sourceVideoMeta = JSON.parse(b.source_video_meta || "null"); } catch (err) { /* older row */ }
    const v = $("video-preview");
    v.src = "/api/input/" + encodeURIComponent(b.source_video);
    v.poster = "/api/input/" + encodeURIComponent(b.source_video) + "?poster=1";
    v.hidden = false;
  }
  go("studio");
  /* pollStatus locks onto whatever state.batch holds, so painting a past batch
     into the centre while something is generating replaces the running take
     with a finished one and the operator loses sight of the work they are
     paying for. Restore the settings either way; only take over the preview
     when nothing is in flight. */
  if (state.busy) {
    notice("info", "設定を戻しました。生成中なので、プレビューは動いている方を表示したままにします。");
  } else {
    renderResults(historyBatchToView(b));
  }
  renderDurationHint();
  renderSourceBudget();
  renderCacheHint();
  loadHistory();
  if (!state.busy) {
    notice("info", "この生成の設定を Studio に戻しました。素材とプロンプトはそのまま使えます。");
  }
}

// -------------------------------------------------------------- 3. Assets
/* Derived from history, not from a new table: an asset here is a file this
   Studio actually generated with. Nothing is written, nothing is indexed, and
   there is no second place for material to live. Deletion is honestly absent
   -- there is no endpoint that removes a file. */
let assetKind = "all";
let assetSel = null;

function assetIndex() {
  const src = histLoaded ? histBatches : (state.batches || []);
  const m = new Map();
  for (const b of src) {
    for (const [name, kind] of [[b.image, "image"], [b.source_video, "video"]]) {
      if (!name) continue;
      const cur = m.get(name) || { name, kind, uses: 0, last: 0, gen: null };
      cur.uses += 1;
      if ((b.created_at || 0) > cur.last) {
        cur.last = b.created_at || 0;
        const shot = (b.generations || []).find((g) => g.status === "done");
        cur.gen = shot ? shot.id : cur.gen;
        cur.batch = b;
      }
      m.set(name, cur);
    }
  }
  return Array.from(m.values()).sort((a, b) => b.last - a.last);
}

function renderAssets() {
  const all = assetIndex();
  setText($("ac-all"), all.length + " 点");
  setText($("ac-image"), all.filter((a) => a.kind === "image").length + " 点");
  setText($("ac-video"), all.filter((a) => a.kind === "video").length + " 点");
  const rows = all.filter((a) => assetKind === "all" || a.kind === assetKind);
  setHtml($("asset-grid"), rows.map((a) => {
    const url = "/api/input/" + encodeURIComponent(a.name) + (a.kind === "video" ? "?poster=1" : "");
    const d = new Date(a.last * 1000);
    return `<div class="tile ${assetSel === a.name ? "on" : ""}" data-asset="${esc(a.name)}">
      <img src="${url}" alt="" loading="lazy"
        onerror="this.outerHTML='&lt;div class=\\'h-thumb none\\'&gt;読み込めません&lt;/div&gt;'">
      <div class="t-meta"><b>${esc(a.name)}</b>
        <span>${a.kind === "video" ? "参照動画" : "人物画像"} · ${a.uses} 回使用</span>
        <span>${d.getMonth() + 1}/${d.getDate()} 最終使用</span>
      </div></div>`;
  }).join("") || '<p class="hint">まだ素材がありません。生成すると、ここに使った素材が並びます。</p>');
}

$("asset-kinds").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-kind]"); if (!b) return;
  assetKind = b.dataset.kind;
  document.querySelectorAll("#asset-kinds button").forEach((x) =>
    x.classList.toggle("on", x === b));
  renderAssets();
});

$("asset-grid").addEventListener("click", (e) => {
  const t = e.target.closest("[data-asset]"); if (!t) return;
  assetSel = assetSel === t.dataset.asset ? null : t.dataset.asset;
  renderAssets();
  const a = assetIndex().find((x) => x.name === assetSel);
  if (!a) return;
  const url = "/api/input/" + encodeURIComponent(a.name);
  setHtml($("asset-detail"),
    (a.kind === "video"
      ? `<video class="detail-media" controls preload="metadata" src="${url}" poster="${url}?poster=1"></video>`
      : `<img class="detail-media" src="${url}" alt="">`)
    + '<dl class="kv">'
    + `<div><dt>種類</dt><dd>${a.kind === "video" ? "参照動画" : "人物画像"}</dd></div>`
    + `<div><dt>使用回数</dt><dd>${a.uses} 回</dd></div>`
    + `<div><dt>最後に使用</dt><dd>${new Date(a.last * 1000).toLocaleString("ja-JP")}</dd></div>`
    + "</dl>"
    + '<div class="detail-acts">'
    + `<button class="btn btn-primary" data-use="${esc(a.name)}">この素材で作る</button>`
    + "</div>");
});

$("asset-detail").addEventListener("click", (e) => {
  const b = e.target.closest("[data-use]"); if (!b) return;
  const a = assetIndex().find((x) => x.name === b.dataset.use);
  if (a && a.batch) openInStudio(a.batch);
});

// ----------------------------------------------------------------- 4. GPU
/* The queue and the log. The Pod card, the cost and the auto-stop are the same
   elements the drawer held -- renderGpuCard / renderGpuCost still fill them. */
function renderGpuScreen(s) {
  const el = $("gpu-queue");
  if (!el) return;
  const active = [];
  for (const b of (s.batches || [])) {
    for (const g of (b.generations || [])) {
      if (["running", "queued", "submitting"].includes(g.status)) active.push([b, g]);
    }
  }
  setHtml(el, active.length ? active.map(([b, g]) => `
    <div class="qrow">
      <b>${JP_MODE[b.mode] || b.mode} · ${JP_KIND[b.kind] || b.kind}</b>
      <div class="qsub">${esc(genStateText(g))}${
        g.seconds ? " · " + fmtDuration(g.seconds) : ""}</div>
      <div class="bar-progress ${genIndeterminate(g) ? "indeterminate" : ""}">
        <i style="width:${Math.round(genProgress(g, Date.now() / 1000) * 100)}%"></i></div>
      <div class="qsub">${esc(genTiming(g, Date.now() / 1000) || "")}</div>
    </div>`).join("")
    : '<p class="hint">生成は動いていません。</p>');

  setHtml($("gpu-events"), (s.events || []).slice().reverse().map((ev) => {
    const d = new Date((ev.t || 0) * 1000);
    return `<div class="lv-${ev.level}"><time>${String(d.getHours()).padStart(2, "0")}:${
      String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}</time>
      <span>${esc(ev.text || "")}</span></div>`;
  }).join("") || '<p class="hint">まだログはありません。</p>');
}

// ------------------------------------------------------------ 5. Settings
/* 指示書 §8 says this screen is "next time's default behaviour". H3 Studio does
   not persist per-generation defaults, so inventing switches here would be five
   controls that forget. What it shows instead is what is genuinely in force --
   with an input only where something can actually be changed. */
function renderSettingsScreen(s) {
  const el = $("settings-body");
  if (!el) return;
  const g = (s && s.gpu) || state.lastGpu || {};
  const cfg = state.cfg || {};
  const grp = (title, rows) => `<div class="setgrp"><h3>${title}</h3><dl class="kv">`
    + rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(String(v == null ? "—" : v))}</dd></div>`).join("")
    + "</dl></div>";
  setHtml(el,
    grp("納品（生成のたびに自動で実行されます）", [
      ["元動画の音声を戻す", "する"],
      ["元の再生速度に戻す", "する"],
      ["映像の再エンコード", "しない（-c:v copy）"],
      ["Raw 出力", "残す"],
      ["再生に使うファイル", "納品版があれば納品版"],
    ])
    + grp("RunPod", [
      ["接続", g.have_api_key ? "APIキーあり" : "APIキーを読めません"],
      ["Pod", g.pod_id || "—"],
      ["GPU", g.gpu || "—"],
      ["時間単価", g.hourly_usd != null ? "$" + Number(g.hourly_usd).toFixed(2) + " / 時" : "—"],
      ["Network Volume", g.network_volume_id || "—"],
      ["データセンター", g.datacenter || "—"],
    ])
    + grp("ComfyUI", [
      ["接続先", g.comfy_url || "—"],
      ["状態", g.comfy_liveness === "live" ? "応答あり"
             : g.comfy_liveness === "dead" ? "応答なし" : "不明"],
      ["Reference Strength ノード",
       (cfg.reference_strength || {}).available === false ? "未導入" : "あり"],
      ["Turbo LoRA", (cfg.draft_quality || {}).available === false ? "未導入"
        : (cfg.draft_quality || {}).available === true ? "あり" : "不明（GPU停止中）"],
    ])
    + grp("保存先", [
      ["書き出し先", cfg.output_dir || "—"],
      ["Studio", "このMac（127.0.0.1:8010）"],
    ])
    + grp("このアプリ", [
      ["バージョン", s && s.studio_version ? s.studio_version : "—"],
      ["起動からの時間", s && s.uptime_seconds ? fmtDuration(s.uptime_seconds) : "—"],
      ["生成した本数", s && s.stats ? s.stats.generations + " 本" : "—"],
    ]));
}

$("btn-doctor").addEventListener("click", async () => {
  setText($("doctor-out"), "点検中…");
  try {
    const d = await api("/api/doctor");
    setHtml($("doctor-out"), (d.checks || []).map((c) =>
      `<div class="lv-${c.status === "PASS" ? "" : c.status === "WARN" ? "warn" : "error"}">
        ${c.status === "PASS" ? "✓" : c.status === "WARN" ? "!" : "✗"} ${esc(c.check)}
        <span class="dim">${esc(c.detail || "")}</span></div>`).join(""));
  } catch (e) { setText($("doctor-out"), "点検できませんでした: " + e.message); }
});

/* Come back to the screen that was open. A reload during a 20 minute
   generation should not send you to the front page. */
(function restoreScreen() {
  let want = (location.hash || "").replace("#/", "");
  if (!want) { try { want = localStorage.getItem("h3.screen") || "studio"; } catch (e) { want = "studio"; } }
  go(want);
})();

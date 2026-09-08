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
  sourceVideoLocal: null,   // the file on this Mac; Long Draft cuts from it
  longDraft: false,         // Motion Trace only
  longDraftPlan: null,
  longDraftRun: null,       // the run currently shown
  longDraftRuns: [],
  refImageSize: "match",
  draftQuality: "standard",
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

const JP_SEG = { pending: "待機", queued: "待機", running: "生成中", done: "完了",
                 cancelled: "中止", error: "エラー" };
const SEG_ICON = { done: "✓", running: "●", queued: "●", pending: "○",
                   cancelled: "×", error: "!" };

function buildLongDraftShell() {
  const el = document.createElement("div");
  el.innerHTML = '<h2 class="ld-title"></h2>'
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
  if (!run) { box.hidden = true; box.innerHTML = ""; return; }
  state.longDraftRun = run;
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
  setText(shell.querySelector(".ld-manifest"), "manifest: " + (run.manifest_path || ""));
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
    renderGpu(s.gpu);
    renderStats(s.stats);
    /* A reload used to empty the results panel: state.batch only ever got set
       by submitting, so the finished videos vanished even though they were on
       disk and in this very payload. Adopt the newest batch when we have none. */
    if (!state.batch && s.batches && s.batches.length) {
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
    if (state.longDraftRuns.length) {
      const shown = state.longDraftRun
        && state.longDraftRuns.find((r) => r.id === state.longDraftRun.id);
      renderLongDraftRun(shown || state.longDraftRuns[0]);
    }
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

function renderGpuProgress(start) {
  const el = $("gpu-progress");
  const stages = (start && start.stages) || [];
  const show = stages.some((s) => s.state !== "pending");
  el.hidden = !show;
  if (!show) return;
  const MARK = { pending: "·", active: "▶", done: "✓", failed: "✗" };
  el.innerHTML = stages.map((s) =>
    `<li class="${s.state}"><span class="mark">${MARK[s.state] || "·"}</span>${s.label}`
    + (s.detail ? `<span class="detail">${s.detail}</span>` : "") + "</li>").join("");
}

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
        <button class="btn" data-confirm-gpu="${o.gpu}">これで開始</button>
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

function genTiming(g, now) {
  if (g.status === "done") return g.seconds != null ? `${g.seconds}秒` : "";
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
    + '<div class="meta"><span class="who"></span><span class="tagwrap"></span></div>'
    + '<div class="meta"><span class="stage"></span><span class="timing"></span></div>'
    + '<div class="acts">'
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
  const sel = batch.generations.find((g) => g.selected);
  state.selected = sel ? sel.id : null;
  const root = $("results");
  const now = Date.now();
  const keys = new Set();

  batch.generations.forEach((g, i) => {
    keys.add(g.id);
    const card = ensureChild(root, g.id, buildResultCard);
    card.dataset.gen = g.id;
    setClass(card, "sel", g.selected);

    const slot = card.querySelector(".vid");
    if (g.output_url) {
      ensureVideo(slot, g.output_url);
    } else {
      ensureVideo(slot, null);
      setHtml(slot, '<div class="ph">' + (g.status === "error"
        ? "エラー<br>" + esc(g.error || "") : esc(g.progress_text || g.status)) + "</div>");
    }

    const bar = card.querySelector(".bar-progress");
    setClass(bar, "indeterminate", genIndeterminate(g));
    const fill = card.querySelector(".bar-progress > i");
    const width = Math.round(genProgress(g, now) * 100) + "%";
    if (fill && fill.style.width !== width) fill.style.width = width;

    setText(card.querySelector(".who"), `#${i + 1} · seed ${g.seed}`);
    setHtml(card.querySelector(".tagwrap"), g.cache_result
      ? `<span class="tag ${g.cache_result}">${g.cache_result}</span>`
      : (g.status === "error" ? '<span class="tag err">エラー</span>' : ""));
    setText(card.querySelector(".stage"), genIndeterminate(g)
      ? genStateText(g)
      : (g.phase_label ? g.phase_label + " · " : "") + (g.progress_text || g.status));
    setText(card.querySelector(".timing"), genTiming(g, now));

    const pick = card.querySelector('[data-act="select"]');
    setText(pick, g.selected ? "★ 選択中" : "これを選ぶ");
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
  /* Turbo only applies to a Draft, and only in the FL2VA modes. Final is never
     Turbo -- the server enforces that too. */
  const turboModes = (state.cfg.draft_quality || {}).turbo_modes || [];
  payload.turbo = state.quality === "draft" && state.draftQuality === "turbo" &&
                  turboModes.indexOf(state.mode) >= 0;
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

async function startGpu(confirmGpu) {
  $("btn-gpu-start").disabled = true;
  notice("info", confirmGpu ? `${confirmGpu} で起動します…` : "GPU を起動しています…", 4000);
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
    const card = pick.previousElementSibling ? pick.parentElement.textContent.trim() : pick.dataset.confirmGpu;
    if (!confirm("高額な GPU を起動します。\n\n" + card + "\n\nよろしいですか？")) return;
    return startGpu(pick.dataset.confirmGpu);
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
    notice("info", "AUTO STOP: " + (e.target.value === "0" ? "OFF" : e.target.value + " 分"));
  } catch (err) { notice("error", err.message); }
});

/* History rows come from SQLite and carry output_path but no output_url; the
   video route now serves from history too, so the URL can be derived. This is
   what makes a finished clip survive a Studio restart, not just a reload. */
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
    const ldRows = longDraftHistory.map((d) => {
      const done = (d.segments || []).filter((x) => x.status === "done").length;
      return `<div class="h-item h-ld" data-longdraft="${d.id}"><b>長尺Draft · ${JP_MODE[d.mode] || d.mode}</b><br>
        ${esc((d.prompt || "").slice(0, 70))}<br>
        ${(d.source_duration || 0).toFixed(1)} 秒 · ${d.segment_count} segments ·
        ${done}/${(d.segments || []).length} 本完了 · ${JP_SEG[d.status] || d.status}</div>`;
    }).join("");
    $("history").innerHTML = ldRows + h.batches.map((b) => {
      const done = b.generations.filter((g) => g.status === "done").length;
      const sel = b.generations.find((g) => g.selected);
      return `<div class="h-item" data-batch="${b.id}"><b>${JP_KIND[b.kind] || b.kind} · ${JP_MODE[b.mode] || b.mode}</b><br>
        ${esc((b.prompt || "").slice(0, 70))}<br>
        ${done}/${b.generations.length} 本${sel ? " · ★選択済み" : ""}
        ${b.reference_strength ? " · 忠実度 " + b.reference_strength : ""}</div>`;
    }).join("") || "<div class='hint'>まだ履歴はありません。</div>";

    /* Nothing on screen and nothing in this process's memory (a restarted
       Studio): show the most recent batch from history so the work is still
       there. */
    if (!state.batch && h.batches.length) {
      const view = historyBatchToView(h.batches[0]);
      if (view.generations.some((g) => g.output_url)) renderResults(view);
    }
  } catch (e) { /* history is non-critical */ }
}

$("history").addEventListener("click", async (e) => {
  const ld = e.target.closest("[data-longdraft]");
  if (ld) {
    try {
      const run = await api("/api/long_draft/" + ld.dataset.longdraft);
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
   video the operator was working with is put back rather than asked for again.
   Preview thumbnails are not restored: the browser has no File object for a
   file it did not just read, and /api/video is for finished generations, not
   for inputs. The name, the probe and the readiness state are what matter. */
async function restoreLastInputs() {
  let inputs;
  try { inputs = (await api("/api/last_inputs")).inputs || {}; }
  catch (e) { return; }
  if (inputs.video) {
    state.sourceVideo = inputs.video.name;
    state.sourceVideoMeta = inputs.video.meta || null;
    state.sourceVideoLocal = inputs.video.local;
    const p = state.sourceVideoMeta || {};
    const bits = [inputs.video.name + "（前回の素材）"];
    if (p.duration) bits.push(p.duration + "s");
    if (p.fps) bits.push(p.fps + " fps");
    if (p.frames) bits.push(p.frames + " frames");
    $("video-status").textContent = bits.join(" · ");
    $("video-notes").textContent = (p.notes || []).join("\n");
  }
  if (inputs.image) {
    state.image = inputs.image.name;
    state.localPath = inputs.image.local;
    $("image-status").textContent = inputs.image.name + "（前回の素材）";
  }
  renderReadiness();
  renderCacheHint();
  renderSourceBudget();
  refreshLongDraftPlan();
}

(async function init() {
  try { await loadConfig(); maybeSuggestTags(); renderDurationHint(); }
  catch (e) { notice("error", "設定の読み込みに失敗: " + e.message); }
  restoreLastInputs();
  pollStatus();
  loadHistory();
  setInterval(loadHistory, 30000);
})();

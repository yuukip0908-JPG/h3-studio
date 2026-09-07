"""START GPU as a supervised sequence, not a button that blocks a browser.

Three things forced this shape:

  * the old handler did the whole start inside the HTTP request, so when the
    browser gave up the page said "error" while the Pod kept coming up and
    billing. The work now runs in a thread the request does not own, and the
    state survives a reload;
  * the old path took whatever card RunPod offered. It took an A100 at
    $1.59/hr twice. Acquisition is now allowed to start only cards that are
    priced, in budget and baseline-verified; anything else is offered and waits
    for a yes;
  * the old acquisition loop hunted for up to sixty minutes in the background.
    One restart, one create, five minutes, then it answers.

Nothing here repairs a Pod. If a post-start check fails the session says so and
stops trying: fixing an environment while a GPU bills by the second is how a
five minute session becomes an hour.
"""

import threading
import time

from . import config, gpu_plan, preflight, runpod_ctl

IDLE = "idle"
RUNNING_STATES = ("preflight", "guard", "acquiring", "starting_comfyui", "checking")
NEEDS_CONFIRMATION = "needs_confirmation"
READY = "ready"
NO_CAPACITY = "no_capacity"
FAILED = "failed"
CANCELLED = "cancelled"

STOP_IN_PROGRESS = "GPU停止処理中です。停止完了後に再度開始してください。"

STAGES = (
    ("guard", "二重起動の確認"),
    ("preflight", "事前チェック（GPU不要）"),
    ("acquire", "GPU の確保"),
    ("volume", "Network Volume の確認"),
    ("comfyui", "ComfyUI 起動"),
    ("checks", "モデル・doctor 確認"),
    ("ready", "READY"),
)

CAPACITY_MARKERS = ("not enough free GPUs", "no instances currently available",
                    "no longer any instances available")


def _is_capacity(message):
    return any(m in str(message) for m in CAPACITY_MARKERS)


def _why_unusable(info):
    """Why an acquired Pod was not accepted, in the operator's terms.

    "RUNNING にならなかった" used to cover both cases, which sent the operator
    looking for a capacity problem when RunPod had simply not published the
    address yet.
    """
    info = info or {}
    if info.get("desiredStatus") != "RUNNING":
        return "RUNNING にならなかった（%s）" % (info.get("desiredStatus") or "状態不明")
    return "RUNNING だが RunPod がまだ SSH の住所を公開していません（ip=%s port=%s）" % (
        info.get("publicIp") or "未公開", info.get("sshPort") or "未公開")


class GpuStarter:
    """One start attempt at a time, observable while it runs."""

    def __init__(self, ctl, comfy_ready, start_comfy, object_info=None,
                 on_event=None, watchdog=None, stop_in_progress=None):
        self.ctl = ctl
        self.comfy_ready = comfy_ready          # () -> bool
        self.start_comfy = start_comfy          # () -> (rc, out, err)
        self.object_info = object_info          # () -> dict | None
        self.on_event = on_event or (lambda level, text: None)
        self.watchdog = watchdog                # (pod_id) -> None, may be None
        # () -> bool. Asked under the same lock that admits a start, so a STOP
        # that begins while a START is being admitted cannot end up with both.
        self.stop_in_progress = stop_in_progress or (lambda: False)
        self._lock = threading.Lock()
        # Set by cancel(). Every wait in the sequence checks it, so a STOP does
        # not leave a start polling a Pod that is going away -- on 2026-09-07 a
        # STOP at 14:19 left the sequence "starting ComfyUI" until 14:23.
        self._cancel = threading.Event()
        self._thread = None
        self.reset()

    # -------------------------------------------------------------- state
    def reset(self):
        self._cancel.clear()
        self.state = IDLE
        self.stages = [{"key": k, "label": l, "state": "pending", "detail": ""} for k, l in STAGES]
        self.offers = []
        self.error = None
        # True when an attempt was refused for host capacity. That is a wait,
        # not a fault, and it stays true even when paid alternatives are on
        # offer -- "come back in a few minutes" is still a valid answer.
        self.capacity_blocked = False
        self.approved_gpu = None
        self.pod_id = None
        self.cancel_reason = None
        self._last_pod_check = 0.0
        self._pod_check_warned = False
        # A Pod this sequence started or created but has not accepted. It bills
        # from the moment it exists, so it is tracked from that moment too.
        self.accepted = False
        self.price_card = None
        self.online = []
        self.started_at = None
        self.finished_at = None

    POD_CHECK_SECONDS = 10.0

    def cancel(self, reason="停止が要求されました"):
        """Ask the running sequence to stop at its next check.

        Cheap and idempotent: it sets a flag. The sequence is what decides
        where it is safe to give up, and it never leaves a Pod running as a
        side effect of being cancelled -- stopping the Pod is STOP GPU's job,
        and STOP GPU is what calls this.
        """
        with self._lock:
            self.cancel_reason = reason
            self._cancel.set()

    # Checked inside every wait. Three ways a start stops being worth
    # finishing, all of them cheap to detect and expensive to ignore.
    def _abort_reason(self):
        if self._cancel.is_set():
            return getattr(self, "cancel_reason", "停止が要求されました")
        if self.pod_id:
            # Free: the active Pod id is a local file.
            if config.pod_id() and config.pod_id() != self.pod_id:
                return ("この起動は %s のものですが、アクティブな Pod が %s に変わりました"
                        % (self.pod_id, config.pod_id()))
            # Costs a RunPod call, so it is asked at most every POD_CHECK_SECONDS
            # rather than on every pass of a 5-second loop.
            now = time.time()
            if now - self._last_pod_check >= self.POD_CHECK_SECONDS:
                self._last_pod_check = now
                try:
                    if self.ctl.describe(self.pod_id).get("desiredStatus") == "EXITED":
                        return "Pod %s が停止しました" % self.pod_id
                except Exception as e:  # noqa: BLE001 - unknown is not a reason to give up
                    # Not fatal, but not silent either: while this is failing
                    # the "Pod went away" half of the abort check is not being
                    # made, and the sequence is back to waiting out a timeout.
                    if not self._pod_check_warned:
                        self._pod_check_warned = True
                        self.on_event("warn", "Pod の状態を確認できません（停止の検知が遅れます）: %s"
                                      % str(e)[:150])
        return None

    def _abort(self, stage, reason):
        # "pending", not "failed": a start the operator stopped is not a broken
        # start, and the panel must not show a red cross for one until the next
        # START clears it.
        self._stage(stage, "pending", reason)
        self._release(reason)
        self.error = reason
        self.state = CANCELLED
        self.finished_at = time.time()
        self.on_event("warn", "GPU 起動を中断しました: %s" % reason)
        return None

    def _claim(self, info):
        """Take ownership of a Pod the moment it exists.

        Acquisition can produce a Pod it then rejects -- cancelled mid-wait, or
        RUNNING without a published address. Recording the id here, before the
        decision, is what makes an abandoned Pod impossible: everything that
        gives up afterwards knows what it has to stop.
        """
        pid = (info or {}).get("podId")
        if not pid or pid == self.pod_id:
            return
        self.pod_id = pid
        if self.watchdog:
            try:
                self.watchdog(pid)
            except Exception as e:  # noqa: BLE001 - the valve is best-effort
                self.on_event("warn", "ウォッチドッグを起動できませんでした: %s" % str(e)[:150])

    def _release(self, why):
        """Stop a Pod this sequence obtained but will not use.

        Only for one it never accepted: once accepted, stopping is STOP GPU's
        job, and STOP GPU is what asked to cancel.
        """
        if not self.pod_id or self.accepted:
            return
        pid, self.pod_id = self.pod_id, None
        try:
            self.ctl.stop(pid)
            self.on_event("warn", "使わない Pod %s を停止しました（%s）" % (pid, why))
        except Exception as e:  # noqa: BLE001
            self.on_event("error", "Pod %s を停止できませんでした: %s。"
                                   "課金が続いている可能性があるので状態表示で確認してください。"
                          % (pid, str(e)[:150]))

    def _stage(self, key, state, detail=""):
        for s in self.stages:
            if s["key"] == key:
                s["state"] = state
                if detail:
                    s["detail"] = detail
                return

    def _fail(self, stage, message, state=FAILED):
        self._release(message)
        self._stage(stage, "failed", message)
        self.error = message
        self.state = state
        self.finished_at = time.time()
        self.on_event("error" if state == FAILED else "warn", message)

    def status(self):
        return {
            "state": self.state,
            "busy": self.state in RUNNING_STATES,
            "stages": [dict(s) for s in self.stages],
            "offers": list(self.offers),
            "capacity_blocked": self.capacity_blocked,
            "error": self.error,
            "approved_gpu": self.approved_gpu,
            "pod_id": self.pod_id,
            "price": self.price_card,
            "online_checks": list(self.online),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": None if not self.started_at
            else round((self.finished_at or time.time()) - self.started_at),
        }

    # --------------------------------------------------------------- run
    def begin(self, confirm_gpu=None):
        """Kick off a start. Returns immediately; poll status()."""
        with self._lock:
            if self.state in RUNNING_STATES:
                return self.status()
            if self.stop_in_progress():
                # Refuse, and do nothing else: no attempt is created, no Pod is
                # touched, the running STOP is not cancelled, and the previous
                # attempt's state is left exactly as it was. Starting a Pod
                # while one is being stopped would leave the two disagreeing
                # about which Pod the Studio is pointing at.
                return dict(self.status(), blocked=STOP_IN_PROGRESS)
            self.reset()
            self.approved_gpu = confirm_gpu
            self.state = "guard"
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="gpu-start", daemon=True)
            self._thread.start()
        return self.status()

    def _run(self):
        try:
            self._sequence()
        except Exception as e:  # noqa: BLE001 - a crashed starter must still report
            self._fail("acquire", "起動処理が例外で終了: %s" % str(e)[:300])

    def _sequence(self):
        # ---- guard: never two Pods on one network volume ------------------
        self.state = "guard"
        try:
            pods = self.ctl.list_pods()
        except Exception as e:  # noqa: BLE001
            return self._fail("guard", "RunPod に接続できません: %s" % str(e)[:200])
        running = [p for p in pods if p.get("desiredStatus") == "RUNNING"]
        if running:
            names = ", ".join("%s ($%.2f/時)" % (p.get("id"), p.get("costPerHr") or 0) for p in running)
            return self._fail("guard",
                              "別の H3 Pod がすでに動いています: %s。"
                              "同じ Network Volume を2つの ComfyUI が使うことになるため起動しません。" % names)
        self._stage("guard", "done", "RUNNING な Pod は無い（%d基すべて停止）" % len(pods))

        # ---- preflight: everything knowable with the GPU off --------------
        self.state = "preflight"
        checks = preflight.offline_checks(
            list_pods=lambda: pods,
            get_volume=self.ctl.volume,
            have_api_key=self.ctl.have_api_key)
        summary = preflight.summarize(checks)
        if not summary["ok"]:
            bad = [c for c in checks if c["ok"] is False]
            return self._fail("preflight", "事前チェックで %d 件 FAIL: %s"
                              % (len(bad), "; ".join("%s (%s)" % (c["label"], c["detail"]) for c in bad))[:400])
        self._stage("preflight", "done", "%d 件 PASS / 不明 %d" % (summary["pass"], summary["unknown"]))

        # ---- acquire: one restart, one create, five minutes ---------------
        self.state = "acquiring"
        if self._cancel.is_set():
            return self._abort("acquire", self.cancel_reason)
        deadline = time.time() + config.ACQUIRE_DEADLINE_SECONDS
        info, why = self._acquire(pods, deadline)
        if self._cancel.is_set():
            return self._abort("acquire", self.cancel_reason)
        if info is None:
            if self.offers:
                # Offering a paid card is still a sequence that ends without a
                # Pod: anything acquisition obtained on the way must be let go
                # here too, or it bills while the panel waits for a yes.
                self._release(why)
                self._stage("acquire", "failed", why)
                self.error = why
                self.state = NEEDS_CONFIRMATION
                self.finished_at = time.time()
                self.on_event("warn", why)
                return
            return self._fail("acquire", why, state=NO_CAPACITY)

        self.pod_id = info.get("podId")     # already claimed; kept for clarity
        live_price = info.get("costPerHr")
        self.price_card = gpu_plan.price_card(info.get("gpu"), live_price)

        # A card can only bill what it costs; make sure that is what was agreed.
        budget = gpu_plan.AUTO_START_MAX_USD_PER_HOUR
        if self.approved_gpu:
            approved = gpu_plan.price_card(self.approved_gpu)
            budget = max(budget, approved.get("usd_per_hour") or budget)
        if live_price and live_price > budget + 1e-9:
            self.on_event("error", "承認された上限 $%.2f/時 を超える $%.2f/時 の Pod が起動したため停止します"
                          % (budget, live_price))
            try:
                self.ctl.stop(self.pod_id)
            except Exception:  # noqa: BLE001
                pass
            return self._fail("acquire", "起動した Pod が $%.2f/時 で、承認範囲 $%.2f/時 を超えていたため即停止しました。"
                                         % (live_price, budget))
        self._stage("acquire", "done", "%s / %s / $%.2f/時"
                    % (self.pod_id, self.price_card.get("label"), live_price or 0))

        # The watchdog was armed by _claim(), the moment the Pod existed --
        # arming it only here would leave a rejected Pod unwatched.

        # ---- volume -------------------------------------------------------
        vid = config.network_volume_id()
        if info.get("networkVolumeId") and info["networkVolumeId"] != vid:
            return self._fail("volume", "起動した Pod が別のボリューム %s を見ています（期待 %s）"
                              % (info.get("networkVolumeId"), vid))
        self._stage("volume", "done", "%s → %s" % (vid, info.get("volumeMountPath") or "/workspace"))

        # ---- ComfyUI ------------------------------------------------------
        self.state = "starting_comfyui"
        stop_now = self._abort_reason()
        if stop_now:
            return self._abort("comfyui", stop_now)
        if self.comfy_ready():
            self._stage("comfyui", "done", "すでに応答している")
        else:
            rc, out, err = self.start_comfy()
            if rc == runpod_ctl.SSH_NO_ENDPOINT:
                # Not a ComfyUI problem and not a slow boot: nothing was run.
                # Waiting the seven minutes would only bill for the delay and
                # then blame start_h3.sh, which is what happened on 2026-09-07.
                return self._fail("comfyui",
                                  "Pod の SSH 接続先が確定していないため ComfyUI を起動できませんでした: %s"
                                  % (err or "").strip()[:200])
            # The wait that follows is the long one, so it gets its own budget:
            # a check spent before start_comfy() must not silence the first
            # check after it, when the Pod may have gone away in between.
            self._last_pod_check = 0.0
            stop_now = self._abort_reason()
            if stop_now:
                return self._abort("comfyui", stop_now)
            boot_deadline = time.time() + 420
            while time.time() < boot_deadline:
                if self.comfy_ready():
                    break
                # A stopped Pod, a cancelled start or a Pod that is no longer
                # ours will never answer. Waiting out the seven minutes only
                # delays the truth -- and on a Pod that is still up, bills for it.
                stop_now = self._abort_reason()
                if stop_now:
                    return self._abort("comfyui", stop_now)
                time.sleep(5)
            else:
                return self._fail("comfyui", "ComfyUI が7分以内に応答しませんでした（start_h3.sh exit=%s）: %s"
                                  % (rc, (err or out or "")[-300:]))
            self._stage("comfyui", "done", "start_h3.sh exit=%s" % rc)

        # ---- post-start checks: report, never repair ----------------------
        self.state = "checking"
        stop_now = self._abort_reason()
        if stop_now:
            return self._abort("checks", stop_now)
        try:
            real = self.ctl.actual_gpu()
            if real and real.get("name"):
                # Correct the record before anything else reads it.
                try:
                    # Scoped to the lifetime it was measured on, so it cannot
                    # later be read as a statement about a different Pod.
                    self.ctl.record_actual_gpu(real["name"], self.pod_id,
                                               info.get("lastStartedAt"))
                except Exception:  # noqa: BLE001
                    pass
                self.price_card = gpu_plan.price_card(real["name"], live_price)
            self.online = preflight.online_checks(
                ssh=self.ctl.ssh,
                object_info=self.object_info,
                actual_gpu=real,
                expected_price=budget,
                live_price=live_price)
        except Exception as e:  # noqa: BLE001
            self.online = []
            self.on_event("warn", "起動後チェックを完走できませんでした: %s" % str(e)[:200])
        blocking = [c for c in self.online if c["ok"] is False and c["key"] in preflight.BLOCKING_ONLINE]
        soft = [c for c in self.online if c["ok"] is False and c["key"] not in preflight.BLOCKING_ONLINE]
        if blocking:
            return self._fail("checks", "起動後チェックで致命的な FAIL: %s"
                              % "; ".join("%s (%s)" % (c["label"], c["detail"]) for c in blocking)[:300])
        detail = "%d 件 PASS" % sum(1 for c in self.online if c["ok"] is True)
        if soft:
            detail += " / 要注意 %d 件: %s" % (len(soft), ", ".join(c["label"] for c in soft))
        self._stage("checks", "done", detail)

        self._stage("ready", "done", "生成できます")
        self.state = READY
        self.finished_at = time.time()
        self.on_event("info", "GPU READY: %s / $%.2f/時" % (self.price_card.get("label"), live_price or 0))

    # ----------------------------------------------------------- acquire
    def _acquire(self, pods, deadline):
        """(pod_info, reason). Sets self.offers when only paid help is left."""
        vid = config.network_volume_id()
        ours = [p for p in pods if p.get("networkVolumeId") == vid]
        ours.sort(key=lambda p: str(p.get("createdAt") or ""), reverse=True)

        restart_used = create_used = 0
        tried = []

        # 1) restart an existing Pod -- but only one we are allowed to pay for.
        for pod in ours:
            if restart_used >= config.MAX_RESTART_ATTEMPTS or time.time() > deadline:
                break
            gpu = pod.get("gpuTypeIds") or []
            gpu_id = gpu[0] if gpu else None
            price = pod.get("costPerHr")
            allowed = (self.approved_gpu is not None
                       or gpu_plan.may_auto_start(gpu_id, price)
                       or (price is not None and price <= gpu_plan.AUTO_START_MAX_USD_PER_HOUR))
            if not allowed:
                self._offer(gpu_id, price, "既存 Pod %s" % pod.get("id"))
                continue
            restart_used += 1
            self._stage("acquire", "active", "既存 Pod %s を再起動" % pod.get("id"))
            try:
                info = self.ctl.start(pod.get("id"),
                                      wait_seconds=max(30, int(deadline - time.time())),
                                      should_abort=self._cancel.is_set)
                self._claim(info)
                if runpod_ctl.endpoint_ready(info, pod.get("id")):
                    self.ctl.activate_pod(info)
                    self.accepted = True
                    return info, ""
                tried.append("%s: %s" % (pod.get("id"), _why_unusable(info)))
            except Exception as e:  # noqa: BLE001
                if _is_capacity(e):
                    self.capacity_blocked = True
                    tried.append("%s: 在庫なし" % pod.get("id"))
                else:
                    tried.append("%s: %s" % (pod.get("id"), str(e)[:120]))

        # 2) create one, with only the cards we may start unattended.
        if self._cancel.is_set():
            return None, self.cancel_reason or "起動が中断されました"
        wanted = [self.approved_gpu] if self.approved_gpu else gpu_plan.auto_start_gpu_ids()
        spec = config.create_pod_body(wanted)
        if spec.get("imageName") and create_used < config.MAX_CREATE_ATTEMPTS and time.time() < deadline:
            create_used += 1
            self._stage("acquire", "active", "新規 Pod を作成: %s" % ", ".join(wanted))
            try:
                info = self.ctl.create_pod(spec, wait_seconds=max(60, int(deadline - time.time())),
                                           should_abort=self._cancel.is_set)
                self._claim(info)
                if runpod_ctl.endpoint_ready(info, (info or {}).get("podId")):
                    self.ctl.activate_pod(info)
                    self.accepted = True
                    return info, ""
                tried.append("新規作成: %s" % _why_unusable(info))
            except Exception as e:  # noqa: BLE001
                if _is_capacity(e):
                    self.capacity_blocked = True
                    tried.append("新規作成: 在庫なし")
                else:
                    tried.append("新規作成: %s" % str(e)[:120])

        # 3) nothing cheap was obtainable. Offer the paid options, never take one.
        if not self.approved_gpu:
            for gpu_id in gpu_plan.offer_gpu_ids():
                self._offer(gpu_id, None, "新規作成")
        reason = "%s は現在確保できませんでした（%s）。" % (
            "希望した GPU" if self.approved_gpu else "RTX 6000 Ada 48GB",
            "; ".join(tried) or "試行なし")
        if self.capacity_blocked:
            reason += (" 在庫が空くのを待って再試行できます（Pod・モデル・キャッシュは無傷で、"
                       "課金も発生していません）。")
        if self.offers:
            reason += " 高額な GPU なら確保できる可能性がありますが、確認なしでは起動しません。"
        elif not self.capacity_blocked:
            reason += " 現在 GPU 在庫がありません。数分おいて再試行してください。"
        return None, reason

    def _offer(self, gpu_id, price, how):
        """Offer a card, cheapest known price first.

        An option the operator can be quoted a price for is more useful than a
        cheaper-class card nobody can price, so priced options lead and the
        unpriced ones follow in preference order.
        """
        if not gpu_id:
            return
        card = gpu_plan.price_card(gpu_id, price)
        card["how"] = how
        if any(o["gpu"] == card["gpu"] for o in self.offers):
            return
        self.offers.append(card)
        self.offers.sort(key=lambda o: (o.get("usd_per_hour") is None,
                                        o.get("usd_per_hour") or 0))

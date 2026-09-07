"""Stdlib-only ComfyUI client: HTTP API + a minimal RFC 6455 WebSocket reader.

No third-party packages, so the Studio runs on a stock macOS python3 with
nothing to install. The WebSocket path gives real per-node progress; if the
handshake fails for any reason (a proxy that will not upgrade, for instance)
callers fall back to HTTP polling and the Studio keeps working, just with
coarser progress. That fallback is the reason this is worth having at all.
"""

import base64
import json
import mimetypes
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ComfyError(RuntimeError):
    pass


class WebSocketQuiet(Exception):
    """No frame arrived before the read timeout. NOT a disconnect.

    ComfyUI sends nothing at all during the Qwen3-VL encode -- 75-90 s on a
    cache MISS, and every Long Draft segment is a MISS. Treating that silence as
    a dead socket dropped the connection on every MISS, and the reconnect
    backoff (up to 30 s) then swallowed the progress events that followed, which
    is why a phase label sat on "参照を解析中" for nine minutes.
    """


class ComfyUnavailable(ComfyError):
    """ComfyUI is not there: the Pod is stopped, or ComfyUI is not running.

    This is NOT limited to connection errors. When the Pod is EXITED, RunPod's
    Cloudflare proxy still answers every path with HTTP 404 and an empty body
    (measured 2026-09-06 against https://<pod>-8188.proxy.runpod.net). Treating
    that as an ordinary HTTP error surfaces a meaningless "404" to the operator
    when the real cause is simply that the GPU is off, so it is classified here.
    """


# RunPod serves the Pod's HTTP proxy through Cloudflare, which rejects the
# default "Python-urllib/3.x" User-Agent with HTTP 403 "error code: 1010"
# (measured against https://<pod>-8188.proxy.runpod.net on 2026-09-06). Any
# non-default User-Agent is accepted, so every request the Studio makes must
# carry one -- without it the Studio cannot talk to a real Pod at all, while
# still working perfectly against a local ComfyUI or the test mock.
USER_AGENT = "H3Studio/2.0 (+https://github.com/Comfy-Org/ComfyUI)"


def _is_proxy_pod_down(status, body, headers):
    """Is this the RunPod proxy telling us the Pod is not running?

    A stopped Pod's proxy returns 404 with an empty body and Cloudflare's own
    Server header, for every path including ones ComfyUI would never 404 on.
    Requiring all three keeps a genuine ComfyUI 404 distinguishable.
    """
    if status != 404 or body:
        return False
    server = ""
    for key, value in (headers or {}).items():
        if key.lower() == "server":
            server = (value or "").lower()
    return "cloudflare" in server


def _request(url, data=None, headers=None, method=None, timeout=30):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), {k: v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        # dict(HTTPMessage) keeps the FIRST value for a repeated header name,
        # which can hide the one that matters; items() gives every pair, so a
        # comprehension keeps the last -- what a proxy in front actually set.
        status, body = e.code, e.read()
        head = {k: v for k, v in (e.headers or {}).items()}
        if _is_proxy_pod_down(status, body, head):
            down = ComfyUnavailable(
                "the RunPod proxy returned 404 for %s, which means the Pod is not running"
                % url)
            # A positive answer from the proxy that the Pod is not there, as
            # opposed to a transport failure that says nothing at all. Liveness
            # needs to tell those apart: one is "dead", the other is "unknown".
            down.pod_down = True
            raise down from e
        return status, body, head
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as e:
        raise ComfyUnavailable(str(e)) from e


class ComfyClient:
    def __init__(self, base_url, timeout=30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # ---------- plain HTTP ----------
    def _json(self, path, payload=None, method=None, timeout=None):
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, body, _ = _request(url, data, headers, method, timeout or self.timeout)
        if status >= 400:
            raise ComfyError("%s %s -> HTTP %d: %s" % (method or ("POST" if data else "GET"),
                                                       path, status, body[:800].decode("utf-8", "replace")))
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None

    def system_stats(self):
        return self._json("/system_stats", timeout=10)

    LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"

    def liveness(self):
        """live | dead | unknown -- and never "dead" for a merely busy ComfyUI.

        /queue is the primary probe. Measured on the Pod 2026-09-07: while a
        generation was executing, /system_stats answered 502 through the RunPod
        proxy on every attempt while /queue answered 200 in ~180 ms, so probing
        system_stats alone reported a working ComfyUI as down. When ComfyUI was
        genuinely dead, /queue returned 502 too -- the two states do separate.

        /system_stats stays as a second opinion, not as the verdict: if /queue
        errors but system_stats answers, ComfyUI is serving and the queue read
        was the anomaly.

        "unknown" is a real answer. A transport failure means the question could
        not be asked, and this project's rule is that unknown is reported as
        unknown -- never as absence.
        """
        try:
            self.queue_snapshot()
            return self.LIVE
        except ComfyUnavailable as e:
            # The proxy positively says the Pod is not running; nothing else to
            # ask. Anything else at this layer is a network failure.
            return self.DEAD if getattr(e, "pod_down", False) else self.UNKNOWN
        except ComfyError:
            pass
        try:
            self.system_stats()
            return self.LIVE
        except ComfyUnavailable as e:
            return self.DEAD if getattr(e, "pod_down", False) else self.UNKNOWN
        except ComfyError:
            # Something answered on both routes and neither served: ComfyUI is
            # not up, whatever is in front of it is.
            return self.DEAD

    def is_alive(self):
        """Kept as the boolean the call sites already use. Only "live" is True;
        "unknown" is not alive, because acting on a maybe is how a stopped Pod
        gets a prompt queued at it."""
        return self.liveness() == self.LIVE

    def free_models(self, unload_models=True, free_memory=True):
        """Ask ComfyUI to unload models and collect, via its own /free route.

        Verified against the pinned ComfyUI 169fcf35: /free sets flags on the
        prompt queue and set_flag() calls not_empty.notify(), which wakes the
        idle worker out of q.get() so the flags are acted on straight away --
        unload_all_models() plus e.reset() plus a forced gc. It touches only
        in-memory models; CLIPCached's on-disk cache is untouched, so cache
        HITs are unaffected.
        """
        self._json("/free", payload={"unload_models": bool(unload_models),
                                     "free_memory": bool(free_memory)},
                   method="POST", timeout=20)
        return True

    def object_info(self, node=None):
        return self._json("/object_info" + ("/" + node if node else ""))

    def has_node(self, node):
        try:
            info = self.object_info(node)
        except ComfyError:
            return False
        return bool(info)

    def queue_state(self):
        """Best-effort queue read: {} means "empty OR unreachable"."""
        try:
            return self._json("/queue") or {}
        except ComfyError:
            return {}

    def queue_snapshot(self):
        """The queue, or a raised ComfyError -- never a silent empty.

        Reconciliation decides whether a prompt is alive or forgotten from this,
        and "I could not ask" must never be read as "it is not there". That is
        the difference between waiting out a network blip and declaring a
        running generation dead.
        """
        return self._json("/queue") or {}

    @staticmethod
    def queue_ids(snapshot):
        """(running_ids, pending_ids) from a /queue payload.

        The id is found by scanning each entry for a string, not by trusting a
        fixed index: ComfyUI's entries are [number, prompt_id, prompt, ...] but
        only the pending shape is established in this project, and a wrong index
        would silently report every prompt as missing.
        """
        def ids(entries):
            out = []
            for entry in entries or []:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, (list, tuple)):
                    out.extend(x for x in entry if isinstance(x, str))
            return out
        if not isinstance(snapshot, dict):
            return [], []
        return ids(snapshot.get("queue_running")), ids(snapshot.get("queue_pending"))

    @staticmethod
    def queue_find_by_prefix(snapshot, filename_prefix):
        """The prompt id of a queued job whose SaveVideo writes `filename_prefix`.

        This is the handle back to a prompt whose id was lost -- the Studio picks
        the prefix before it submits and stores it, so it survives a crash that
        the prompt id does not.
        """
        if not filename_prefix or not isinstance(snapshot, dict):
            return None
        for key in ("queue_running", "queue_pending"):
            for entry in snapshot.get(key) or []:
                if not isinstance(entry, (list, tuple)):
                    continue
                graphs = [x for x in entry if isinstance(x, dict)]
                for graph_obj in graphs:
                    for node in graph_obj.values():
                        if not isinstance(node, dict):
                            continue
                        inputs = node.get("inputs")
                        if isinstance(inputs, dict) and \
                                inputs.get("filename_prefix") == filename_prefix:
                            found = [x for x in entry if isinstance(x, str)]
                            if found:
                                return found[0]
        return None

    def submit(self, api_prompt, client_id):
        resp = self._json("/prompt", {"prompt": api_prompt, "client_id": client_id})
        if not resp or "prompt_id" not in resp:
            raise ComfyError("no prompt_id in /prompt response: %r" % (resp,))
        return resp["prompt_id"]

    def history(self, prompt_id):
        h = self._json("/history/%s" % prompt_id) or {}
        return h.get(prompt_id)

    def interrupt(self):
        """Stop the prompt ComfyUI is executing right now.

        This does NOT touch the queue -- anything already queued starts next.
        Use cancel_queued() as well to actually stop a batch.
        """
        try:
            self._json("/interrupt", payload={}, timeout=10)
            return True
        except ComfyError:
            return False

    def cancel_queued(self, prompt_ids):
        """Remove specific prompts from ComfyUI's pending queue.

        Deleting by id rather than clearing the whole queue, so a batch this
        Studio did not submit is never discarded.
        """
        ids = [p for p in prompt_ids if p]
        if not ids:
            return True
        try:
            self._json("/queue", {"delete": ids}, timeout=15)
            return True
        except ComfyError:
            return False

    def upload_image(self, local_path, subfolder="", overwrite=True):
        """POST /upload/image as multipart/form-data (hand-rolled: no requests)."""
        name = os.path.basename(local_path)
        with open(local_path, "rb") as f:
            content = f.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        boundary = "----h3studio%s" % uuid.uuid4().hex
        parts = []

        def field(key, value):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, key, value)).encode("utf-8"))

        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
                      "Content-Type: %s\r\n\r\n" % (boundary, name, ctype)).encode("utf-8"))
        parts.append(content)
        parts.append(b"\r\n")
        field("type", "input")
        field("overwrite", "true" if overwrite else "false")
        if subfolder:
            field("subfolder", subfolder)
        parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
        body = b"".join(parts)
        status, resp, _ = _request(self.base + "/upload/image", body,
                                   {"Content-Type": "multipart/form-data; boundary=%s" % boundary},
                                   "POST", max(self.timeout, 120))
        if status >= 400:
            raise ComfyError("upload failed HTTP %d: %s" % (status, resp[:500].decode("utf-8", "replace")))
        info = json.loads(resp)
        # ComfyUI may rename on collision; the returned name is authoritative.
        return info.get("name", name), info.get("subfolder", "")

    def download_view(self, filename, subfolder="", type_="output", dest=None, timeout=300):
        q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder or "", "type": type_})
        status, body, _ = _request(self.base + "/view?" + q, timeout=timeout)
        if status >= 400:
            raise ComfyError("view failed HTTP %d for %s" % (status, filename))
        if dest:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(body)
            return dest
        return body


# --------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455), text frames only.
# --------------------------------------------------------------------------
_OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClient:
    def __init__(self, url, timeout=70):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    def connect(self):
        u = urllib.parse.urlparse(self.url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        raw = socket.create_connection((u.hostname, port), timeout=self.timeout)
        if secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
        raw.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        handshake = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
            "User-Agent: %s\r\n\r\n" % (path, u.netloc, key, USER_AGENT)
        )
        raw.sendall(handshake.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = raw.recv(4096)
            if not chunk:
                raise ComfyError("websocket handshake: connection closed")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first:
            raise ComfyError("websocket handshake failed: %s" % first)
        self._buf = rest
        self.sock = raw
        return self

    def _recv_exact(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout as e:
                # Partial bytes stay in self._buf, so the next read continues
                # the same frame. Silence is reported as silence.
                raise WebSocketQuiet("no frame within %ss" % self.timeout) from e
            if not chunk:
                raise ComfyError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def ping(self, payload=b""):
        """Prove the transport is still there without waiting for the server.

        A send failure is a real disconnect; silence on the read side is not.
        """
        self._send_frame(_OP_PING, payload)

    def _send_frame(self, opcode, payload=b""):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_message(self):
        """Return the next text message as str, or None for a non-text frame.

        Handles fragmentation and answers ping with pong. Raises on close.
        """
        data = b""
        opcode = None
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if op == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if op == _OP_PONG:
                continue
            if op == _OP_CLOSE:
                raise ComfyError("websocket closed by server")
            if op in (_OP_TEXT, _OP_BIN):
                opcode = op
                data = payload
            elif op == _OP_CONT:
                data += payload
            if fin:
                if opcode == _OP_TEXT:
                    return data.decode("utf-8", "replace")
                return None  # binary preview frame: ignore

    def close(self):
        try:
            if self.sock:
                self._send_frame(_OP_CLOSE)
        except OSError:
            pass
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


def ws_url_for(base_url, client_id):
    u = urllib.parse.urlparse(base_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return "%s://%s/ws?clientId=%s" % (scheme, u.netloc, urllib.parse.quote(client_id))


class ProgressListener(threading.Thread):
    """Streams ComfyUI execution events for one client_id into a callback.

    Runs for the lifetime of the Studio process (one socket, all jobs), and
    reconnects with backoff. `on_event(dict)` gets the decoded messages.
    """

    def __init__(self, base_url, client_id, on_event, on_state=None, ws_timeout=None):
        super().__init__(daemon=True, name="comfy-ws")
        # The read timeout is how long silence is tolerated before the transport
        # is pinged. It is a parameter so the quiet path can be exercised in a
        # test in seconds rather than in the 70 the Pod deserves.
        self.ws_timeout = ws_timeout
        self.base_url = base_url
        self.client_id = client_id
        self.on_event = on_event
        self.on_state = on_state or (lambda ok, detail: None)
        self._stop = threading.Event()
        self.connected = False

    def stop(self):
        self._stop.set()

    # Reconnect quickly. The old ceiling was 30 s, which meant that after a few
    # drops the socket could be down for half a minute at a time and the
    # progress that arrived in that window was simply lost.
    MAX_BACKOFF_SECONDS = 5.0

    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            ws = WebSocketClient(ws_url_for(self.base_url, self.client_id),
                                 **({"timeout": self.ws_timeout} if self.ws_timeout else {}))
            try:
                ws.connect()
                self.connected = True
                self.on_state(True, "connected")
                backoff = 1.0
                while not self._stop.is_set():
                    try:
                        msg = ws.recv_message()
                    except WebSocketQuiet:
                        # Nothing arrived, which is normal for the length of an
                        # encode. Prove the transport rather than assuming the
                        # worst: a ping that cannot be sent IS a disconnect and
                        # falls through to the reconnect below.
                        ws.ping()
                        continue
                    if msg is None:
                        continue
                    try:
                        self.on_event(json.loads(msg))
                    except ValueError:
                        pass
            except Exception as e:  # noqa: BLE001 - a real failure falls back to polling
                self.connected = False
                self.on_state(False, str(e))
            finally:
                ws.close()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.MAX_BACKOFF_SECONDS)

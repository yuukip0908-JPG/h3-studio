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


class ComfyUnavailable(ComfyError):
    """The server did not answer at all (Pod stopped, ComfyUI not started)."""


def _request(url, data=None, headers=None, method=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
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

    def is_alive(self):
        try:
            self.system_stats()
            return True
        except ComfyError:
            return False

    def object_info(self, node=None):
        return self._json("/object_info" + ("/" + node if node else ""))

    def has_node(self, node):
        try:
            info = self.object_info(node)
        except ComfyError:
            return False
        return bool(info)

    def queue_state(self):
        try:
            return self._json("/queue") or {}
        except ComfyError:
            return {}

    def submit(self, api_prompt, client_id):
        resp = self._json("/prompt", {"prompt": api_prompt, "client_id": client_id})
        if not resp or "prompt_id" not in resp:
            raise ComfyError("no prompt_id in /prompt response: %r" % (resp,))
        return resp["prompt_id"]

    def history(self, prompt_id):
        h = self._json("/history/%s" % prompt_id) or {}
        return h.get(prompt_id)

    def interrupt(self):
        try:
            self._json("/interrupt", payload={}, timeout=10)
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
            "User-Agent: H3Studio\r\n\r\n" % (path, u.netloc, key)
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
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ComfyError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

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

    def __init__(self, base_url, client_id, on_event, on_state=None):
        super().__init__(daemon=True, name="comfy-ws")
        self.base_url = base_url
        self.client_id = client_id
        self.on_event = on_event
        self.on_state = on_state or (lambda ok, detail: None)
        self._stop = threading.Event()
        self.connected = False

    def stop(self):
        self._stop.set()

    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            ws = WebSocketClient(ws_url_for(self.base_url, self.client_id))
            try:
                ws.connect()
                self.connected = True
                self.on_state(True, "connected")
                backoff = 1.0
                while not self._stop.is_set():
                    msg = ws.recv_message()
                    if msg is None:
                        continue
                    try:
                        self.on_event(json.loads(msg))
                    except ValueError:
                        pass
            except Exception as e:  # noqa: BLE001 - any failure means "fall back to polling"
                self.connected = False
                self.on_state(False, str(e))
            finally:
                ws.close()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

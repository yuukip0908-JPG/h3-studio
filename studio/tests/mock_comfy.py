"""A stand-in ComfyUI server for offline verification of H3 Studio.

It speaks enough of the real API -- /system_stats, /object_info, /prompt,
/history, /view, /upload/image, CLIPCached's /h3_cache_manager/check, and a
real RFC 6455 WebSocket on /ws -- to exercise every Studio code path with no
GPU and no Pod running.

Its cache model is the one verified against CLIPCached f17ba8f and ComfyUI
169fcf35: the fingerprint covers the prompt plus, only when a keyframe is
present, the resolution that keyframe is resized to. Seed, steps, sampler,
scheduler and duration never enter it. That means a bug in the Studio's own
HIT/MISS reasoning shows up here rather than on a billed GPU.
"""

import base64
import hashlib
import json
import os
import queue as _queue
import struct
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAMPLE_VIDEO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "results", "sample_MiniMax_H3_00015_.mp4")

KNOWN_NODES = {
    "MiniMaxH3CLIPCachedFL2VA", "MiniMaxH3ImageToVideo", "SaveVideo", "LoadImage",
    "ResolutionSelector", "RandomNoise", "BasicScheduler", "KSamplerSelect",
    "UNETLoader", "VAELoader", "VAEDecode", "VAEDecodeAudio", "CreateVideo",
    "SamplerCustomAdvanced", "BasicGuider", "ComfySwitchNode", "PrimitiveInt",
    "PrimitiveFloat", "PrimitiveBoolean", "ComfyMathExpression",
}

MP_TO_WH = {0.25: (672, 384), 0.4: (832, 480), 0.6: (1024, 576)}


class MockState:
    """Shared mutable state: cache entries, queued jobs, sockets, knobs."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cache = {}            # fingerprint -> {...}
        self.history = {}          # prompt_id -> history entry
        self.jobs = {}             # prompt_id -> prompt
        self.sockets = []          # open WebSocket connections
        self.uploads = {}
        self.job_seconds = float(os.environ.get("MOCK_JOB_SECONDS", "0.6"))
        self.encode_seconds = float(os.environ.get("MOCK_ENCODE_SECONDS", "0.4"))
        self.extra_nodes = set()   # e.g. H3StudioReferenceStrength once "installed"
        self.fail_next = None      # set to a message to make the next job fail
        self.cache_route = True    # flip off to simulate a Pod without the route
        self.submitted = []        # every api prompt we received, for assertions
        # Real ComfyUI drains its prompt queue with a SINGLE worker: job N+1
        # does not start until job N has finished. That serialisation is what
        # makes variation 1 a MISS and variations 2-4 real cache HITs, so the
        # mock has to reproduce it or the cache tests would pass by accident.
        self.queue = _queue.Queue()
        self.worker = threading.Thread(target=self._drain, daemon=True)
        self.worker.start()

    def _drain(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                run_job(*item)
            except Exception:  # noqa: BLE001 - a mock job must not kill the worker
                pass

    def broadcast(self, message):
        payload = json.dumps(message).encode("utf-8")
        with self.lock:
            targets = list(self.sockets)
        for sock in targets:
            try:
                sock.send_text(payload)
            except OSError:
                with self.lock:
                    if sock in self.sockets:
                        self.sockets.remove(sock)


STATE = MockState()


# --------------------------------------------------------------------------
def fingerprint_of(prompt):
    """Reproduce the encoder-visible inputs, exactly as the real node does."""
    cached = [v for v in prompt.values() if v["class_type"] == "MiniMaxH3CLIPCachedFL2VA"]
    if not cached:
        return None
    node = cached[0]
    text = node["inputs"].get("prompt", "")
    has_keyframe = "first_frame" in node["inputs"]
    image = None
    width = height = None
    if has_keyframe:
        loaders = [v for v in prompt.values() if v["class_type"] == "LoadImage"]
        image = loaders[0]["inputs"].get("image") if loaders else None
        res = [v for v in prompt.values() if v["class_type"] == "ResolutionSelector"]
        if res:
            mp = res[0]["inputs"].get("megapixels")
            ar = res[0]["inputs"].get("aspect_ratio")
            width, height = MP_TO_WH.get(mp, (832, 480))
            if "9:16" in str(ar) or "3:4" in str(ar):
                width, height = height, width
    blob = json.dumps({"prompt": text, "image": image, "w": width, "h": height,
                       "clip": node["inputs"].get("clip_name")}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def run_job(prompt_id, prompt):
    """Simulate execution and emit the same WebSocket events ComfyUI does."""
    time.sleep(0.05)
    STATE.broadcast({"type": "execution_start", "data": {"prompt_id": prompt_id}})

    if STATE.fail_next:
        message = STATE.fail_next
        STATE.fail_next = None
        STATE.broadcast({"type": "execution_error",
                         "data": {"prompt_id": prompt_id, "exception_message": message}})
        with STATE.lock:
            STATE.history[prompt_id] = {
                "status": {"status_str": "error", "completed": False,
                           "messages": [["execution_error", {"exception_message": message}]]},
                "outputs": {}}
        return

    fp = fingerprint_of(prompt)
    with STATE.lock:
        hit = fp in STATE.cache

    for node_id, node in prompt.items():
        STATE.broadcast({"type": "executing", "data": {"prompt_id": prompt_id, "node": node_id}})
        if node["class_type"] == "MiniMaxH3CLIPCachedFL2VA":
            if not hit:
                time.sleep(STATE.encode_seconds)
                with STATE.lock:
                    STATE.cache[fp] = {"fingerprint": fp, "created": time.time(),
                                       "prompt": node["inputs"].get("prompt", "")[:120]}
        elif node["class_type"] == "SamplerCustomAdvanced":
            steps = 8
            for i in range(1, steps + 1):
                STATE.broadcast({"type": "progress",
                                 "data": {"prompt_id": prompt_id, "value": i, "max": steps}})
                time.sleep(STATE.job_seconds / steps)

    save = [(k, v) for k, v in prompt.items() if v["class_type"] == "SaveVideo"]
    prefix = save[0][1]["inputs"].get("filename_prefix", "video/H3Studio") if save else "video/out"
    filename = "%s_%s.mp4" % (os.path.basename(prefix), uuid.uuid4().hex[:6])
    subfolder = os.path.dirname(prefix)
    node_id = save[0][0] if save else "0"
    with STATE.lock:
        STATE.history[prompt_id] = {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {node_id: {"images": [
                {"filename": filename, "subfolder": subfolder, "type": "output"}]}},
        }
    STATE.broadcast({"type": "executing", "data": {"prompt_id": prompt_id, "node": None}})
    STATE.broadcast({"type": "execution_success", "data": {"prompt_id": prompt_id}})


# --------------------------------------------------------------------------
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSConn:
    """Server side of one WebSocket, writing unmasked text frames."""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()

    def send_text(self, payload):
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        with self.lock:
            self.sock.sendall(bytes(header) + payload)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)

        if path == "/ws":
            return self._websocket()
        if path == "/system_stats":
            return self._json({"system": {"comfyui_version": "0.34.2", "os": "posix"},
                               "devices": [{"name": "cuda:0 NVIDIA RTX 6000 Ada Generation",
                                            "vram_total": 51527024640}]})
        if path.startswith("/object_info"):
            rest = path[len("/object_info"):].strip("/")
            known = KNOWN_NODES | STATE.extra_nodes
            if rest:
                return self._json({rest: {"input": {}, "output": []}} if rest in known else {})
            return self._json({n: {"input": {}, "output": []} for n in known})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[-1]
            with STATE.lock:
                h = STATE.history.get(pid)
            return self._json({pid: h} if h else {})
        if path == "/queue":
            return self._json({"queue_running": [], "queue_pending": [],
                               "exec_info": {"queue_remaining": STATE.queue.qsize()}})
        if path == "/h3_cache_manager/check":
            if not STATE.cache_route:
                return self._json({"error": "not found"}, 404)
            with STATE.lock:
                entries = list(STATE.cache.values())
            return self._json({"entries": entries, "total_count": len(entries),
                               "total_size_bytes": 1024 * len(entries), "last_used": {}})
        if path == "/view":
            name = (q.get("filename") or [""])[0]
            if os.path.exists(SAMPLE_VIDEO):
                with open(SAMPLE_VIDEO, "rb") as f:
                    return self._bytes(f.read(), "video/mp4")
            return self._bytes(b"MOCKMP4:" + name.encode(), "video/mp4")
        return self._json({"error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        path = urllib.parse.urlparse(self.path).path

        if path == "/prompt":
            body = json.loads(raw)
            prompt = body["prompt"]
            unknown = sorted({v["class_type"] for v in prompt.values()}
                             - (KNOWN_NODES | STATE.extra_nodes))
            if unknown:
                # Same shape ComfyUI uses when a node type is not installed.
                return self._json({"error": {"type": "prompt_outputs_failed_validation",
                                             "message": "Prompt outputs failed validation"},
                                   "node_errors": {u: {"errors": [
                                       {"type": "value_not_in_list", "message":
                                        "node type %s not found" % u}]} for u in unknown}}, 400)
            pid = uuid.uuid4().hex
            with STATE.lock:
                STATE.jobs[pid] = prompt
                STATE.submitted.append(prompt)
            STATE.queue.put((pid, prompt))
            return self._json({"prompt_id": pid, "number": len(STATE.jobs), "node_errors": {}})

        if path == "/upload/image":
            ctype = self.headers.get("Content-Type", "")
            name = "upload.png"
            if "filename=" in raw.decode("latin-1", "replace"):
                head = raw.decode("latin-1", "replace")
                name = head.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
            name = os.path.basename(name)
            with STATE.lock:
                STATE.uploads[name] = len(raw)
            return self._json({"name": name, "subfolder": "", "type": "input"})

        if path == "/interrupt":
            return self._json({})
        return self._json({"error": "not found"}, 404)

    # ---- websocket upgrade ----
    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json({"error": "not a websocket request"}, 400)
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        self.wfile.flush()
        conn = WSConn(self.connection)
        with STATE.lock:
            STATE.sockets.append(conn)
        conn.send_text(json.dumps({"type": "status", "data": {"status": {}}}).encode())
        try:
            while True:  # drain client frames until it goes away
                if not self.connection.recv(4096):
                    break
        except OSError:
            pass
        finally:
            with STATE.lock:
                if conn in STATE.sockets:
                    STATE.sockets.remove(conn)
        self.close_connection = True


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port=0):
    httpd = MockServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


if __name__ == "__main__":
    srv, url = serve(int(os.environ.get("PORT", "8199")))
    print("mock ComfyUI on", url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()

import hmac
import json
import re
import secrets
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .media import MAX_BYTES, require_tools
from .planner import autoedit
from .renderer import render
from .timeline import EngineError

CLIPS = tuple(f"clip{i}" for i in range(1, 6))
JOB = re.compile(r"/job/([0-9a-f]{32})/(clip[1-5]|logo|font|render|preview|save)")


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def span(header, size):
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", header)
    if not match or not any(match.groups()):
        raise RequestError(416, "Unsupported byte range.")
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(int(last), size - 1) if first and last else size - 1
    if not 0 <= start <= end < size:
        raise RequestError(416, "Byte range is outside the file.")
    return start, end


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, port, folder, ffmpeg, ffprobe):
        super().__init__(("127.0.0.1", port), Handler)
        self.folder, self.ffmpeg, self.ffprobe = folder, ffmpeg, ffprobe
        self.token = secrets.token_hex(32)
        self.current = None
        self.lock = threading.Lock()
        self.busy = False
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.index = Path(__file__).with_name("index.html").read_text(encoding="utf-8").replace("__REQUEST_TOKEN__", self.token)


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def setup(self):
        self.request.settimeout(20)
        super().setup()

    def send_bytes(self, status, content, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_json(self, data, status=200):
        self.send_bytes(status, json.dumps(data).encode(), "application/json")

    def length(self, limit):
        value = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,12}", value):
            raise RequestError(411, "Send a Content-Length header.")
        length = int(value)
        if not 0 < length <= limit:
            raise RequestError(413, f"File or request size must be 1–{limit:,} bytes.")
        return length

    def require_token(self):
        supplied = self.headers.get("X-Video-Engine-Token", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, self.server.token):
            raise RequestError(403, "Open the local editor page to start a render.")

    def create_job(self):
        with self.server.lock:
            if self.server.busy:
                raise RequestError(409, "Wait for the current render to finish.")
            if self.server.current:
                shutil.rmtree(self.server.folder / self.server.current)
            self.server.current = secrets.token_hex(16)
            (self.server.folder / self.server.current).mkdir()
            return self.server.current

    def job_folder(self, key):
        if key != self.server.current:
            raise RequestError(404, "This is not the active job.")
        return self.server.folder / key

    def upload(self, key, name):
        with self.server.lock:
            job = self.job_folder(key)
            if self.server.busy or (job / "export.mp4").exists():
                raise RequestError(409, "This job no longer accepts uploads.")
            length = self.length(MAX_BYTES if name in CLIPS else 16 * 1024**2)
            partial = job / f"{name}.partial"
            try:
                with partial.open("wb") as handle:
                    while length:
                        chunk = self.rfile.read(min(length, 1024**2))
                        if not chunk:
                            raise RequestError(400, "Incomplete upload.")
                        handle.write(chunk)
                        length -= len(chunk)
                partial.replace(job / name)
            finally:
                partial.unlink(missing_ok=True)

    def export(self, key):
        try:
            settings = json.loads(self.rfile.read(self.length(8192)))
        except (ValueError, UnicodeError) as exc:
            raise RequestError(400, "Expected a JSON settings object.") from exc
        if not isinstance(settings, dict):
            raise RequestError(400, "Expected a JSON settings object.")
        with self.server.lock:
            job = self.job_folder(key)
            if self.server.busy or (job / "export.mp4").exists():
                raise RequestError(409, "This job is rendering or already rendered.")
            if not all((job / clip).is_file() for clip in CLIPS):
                raise RequestError(400, "Upload all five clips before rendering.")
            self.server.busy = True
        try:
            branding = {"backgroundColor": settings.get("backgroundColor", "#102A43"),
                        "textColor": settings.get("textColor", "#F9C74F"),
                        "panelColor": settings.get("panelColor", "#102A43")}
            text = settings.get("text", "")
            if not isinstance(text, str) or len(text) > 140:
                raise RequestError(400, "Text must be at most 140 characters.")
            branding["text"] = text.strip()
            if (job / "logo").exists():
                branding["logo"] = {"path": "logo", "position": settings.get("logoPosition", "top-right"),
                                    "width": settings.get("logoWidth", 168)}
            if (job / "font").exists():
                branding["fontFile"] = "font"
            spec = autoedit([str(job / clip) for clip in CLIPS], root=job, ffprobe=self.server.ffprobe,
                            transition=settings.get("transition", "dissolve"), fit=settings.get("fit", "contain"),
                            audio=settings.get("audio", "keep"), branding=branding)
            report = render(spec, job / "export.mp4", root=job, ffmpeg=self.server.ffmpeg, ffprobe=self.server.ffprobe)
        finally:
            with self.server.lock:
                self.server.busy = False
        return report

    def video(self, key, download):
        with self.server.lock:
            path = self.job_folder(key) / "export.mp4"
            if not path.is_file():
                raise RequestError(404, "There is no finished export for this job.")
            size = path.stat().st_size
            handle = path.open("rb")
        with handle:
            range_header = self.headers.get("Range")
            try:
                start, end = span(range_header, size) if range_header else (0, size - 1)
            except RequestError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if range_header else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="finished-video.mp4"')
            self.end_headers()
            if self.command != "HEAD":
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(remaining, 1024**2))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def dispatch(self):
        try:
            if self.headers.get("Host") not in self.server.hosts:
                raise RequestError(403, "This editor is available only on localhost.")
            action = self.command
            path = urlsplit(self.path).path
            job = JOB.fullmatch(path)
            if action in ("GET", "HEAD") and path == "/":
                self.send_bytes(200, self.server.index.encode(), "text/html; charset=utf-8")
            elif action in ("GET", "HEAD") and job and job[2] in ("preview", "save"):
                self.video(job[1], job[2] == "save")
            elif action == "POST" and path == "/job":
                self.require_token()
                self.send_json({"job": self.create_job()}, 201)
            elif action == "POST" and job and job[2] == "render":
                self.require_token()
                self.send_json(self.export(job[1]))
            elif action == "PUT" and job and job[2] in (*CLIPS, "logo", "font"):
                self.require_token()
                self.upload(job[1], job[2])
                self.send_json({"ok": True})
            else:
                raise RequestError(404, "Not found.")
        except RequestError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except EngineError as exc:
            self.send_json({"error": str(exc)}, 422)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as exc:
            self.send_json({"error": f"Local I/O failed: {exc}"}, 500)

    do_GET = do_HEAD = do_POST = do_PUT = dispatch


def serve(port, *, ffmpeg, ffprobe):
    if not 1024 <= port <= 65535:
        raise EngineError("Use a local port between 1024 and 65535.")
    require_tools(ffmpeg, ffprobe)
    with tempfile.TemporaryDirectory(prefix="video-engine-ui-") as folder:
        with LocalServer(port, Path(folder), ffmpeg, ffprobe) as server:
            print(f"Open http://127.0.0.1:{server.server_port} (local only)", flush=True)
            server.serve_forever()

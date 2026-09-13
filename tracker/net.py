"""Cliente HTTP mínimo sobre la librería estándar."""
import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) nfl-odds-tracker/1.0"


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def request(url: str, params: dict | None = None, data: dict | None = None, timeout: int = 30):
    """Devuelve (texto, headers). `data` se envía como JSON por POST."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise HttpError(exc.code, detail) from exc


def get_json(url: str, params: dict | None = None, timeout: int = 30):
    text, headers = request(url, params=params, timeout=timeout)
    return json.loads(text), headers

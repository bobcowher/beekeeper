"""Thin wrapper over Runpod's REST API v2. The only module that knows Runpod URLs."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.runpod.io/v2"
_CATALOG_TTL = 30


class RunpodError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Runpod API error {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


class RunpodClient:
    def __init__(self, api_key: str, timeout: int = 60):
        self.api_key = api_key
        self.timeout = timeout
        self._catalog = {}
        self._catalog_lock = threading.Lock()

    def _req(self, method, path, body=None, query=None):
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "beekeeper",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # NOSONAR — fixed https host
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RunpodError(e.code, e.read().decode(errors="replace")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RunpodError(0, str(e)) from None

    def gpu_types(self, cloud: str = "COMMUNITY") -> list[dict]:
        """Catalog with live availability (NONE/LOW/MEDIUM/HIGH) and prices, cached briefly."""
        with self._catalog_lock:
            hit = self._catalog.get(cloud)
            if hit and time.time() - hit[0] < _CATALOG_TTL:
                return hit[1]
        gpus = self._req("GET", "/catalog/gpus", query={
            "include": "AVAILABILITY", "cloud": cloud, "product": "POD"})["gpus"]
        with self._catalog_lock:
            self._catalog[cloud] = (time.time(), gpus)
        return gpus

    def create_pod(self, body: dict) -> dict:
        return self._req("POST", "/pods", body)

    def get_pod(self, pod_id: str) -> dict | None:
        try:
            return self._req("GET", f"/pods/{pod_id}")
        except RunpodError as e:
            if e.status == 404:
                return None
            raise

    def list_pods(self) -> list[dict]:
        return self._req("GET", "/pods").get("pods", [])

    def terminate(self, pod_id: str) -> None:
        try:
            self._req("POST", f"/pods/{pod_id}/action", {"action": "terminate"})
        except RunpodError as e:
            if e.status not in (404, 409):
                raise

    def pod_billing(self, pod_id: str | None = None, last_n: int = 3) -> dict:
        query = {"bucketSize": "hour", "lastN": last_n}
        if pod_id:
            query["podId"] = pod_id
        return self._req("GET", "/billing/pods", query=query)


_client = None
_client_key = None


def get_client():
    """Shared client for the configured key, or None if no key is set. Tests monkeypatch this."""
    global _client, _client_key
    from services import runpod_config
    key = runpod_config.get_api_key()
    if not key:
        return None
    if _client is None or key != _client_key:
        _client, _client_key = RunpodClient(key), key
    return _client

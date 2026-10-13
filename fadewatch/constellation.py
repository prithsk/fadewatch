"""Constellation API client (REST) and MCP client.

Written to the documented contract:
  * `x-api-key` auth; 401/403 stop immediately, never retried.
  * 429 `rate_limited` and 503s honour Retry-After (seconds or HTTP date),
    then `retry_after_seconds`, then bounded exponential backoff with jitter.
  * 429 `auth_lockout` stops: retrying a locked credential extends the lock.
  * Telemetry writes are batched under 1,000 records and 1 MiB, and an
    ambiguous write (timeout after send) is surfaced, never auto-replayed:
    the API has no idempotency key.
  * Predictions cost one call of a small monthly allowance (10 on Free), so
    all links go in a single request and `entity_errors` is always read.
"""
from __future__ import annotations

import asyncio
import contextlib
import email.utils
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

BASE_URL = "https://api.constellation.space"
MCP_URL = "https://api.constellation.space/mcp"
MAX_BATCH = 1000
MAX_BODY = 1_048_576
TOPOLOGY_MEASUREMENTS = ("link", "satellite", "ground_station", "weather", "attenuation", "telemetry")


def _dotenv() -> dict[str, str]:
    """KEY=value lines from a .env in the working directory or the repo root (never committed)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in (os.getcwd(), here):
        path = os.path.join(d, ".env")
        if os.path.exists(path):
            out = {}
            for line in open(path, encoding="utf-8-sig"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
            return out
    return {}


def api_key_from_env() -> str | None:
    env = {**_dotenv(), **{k: v for k, v in os.environ.items() if k.startswith("CONSTELLATION_")}}
    return env.get("CONSTELLATION_API_KEY") or env.get("CONSTELLATION_API_TOKEN")


class ConstellationError(Exception):
    def __init__(self, status: int, code: str | None, body: object, retry_after: float | None = None):
        super().__init__(f"HTTP {status} {code or ''}: {body}")
        self.status, self.code, self.body, self.retry_after = status, code, body, retry_after


class AmbiguousWrite(Exception):
    """The request was sent but no response came back: it may or may not have landed."""


def _retry_after(resp: httpx.Response, body: object) -> float | None:
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return max(float(ra), 0.0)
        except ValueError:
            dt = email.utils.parsedate_to_datetime(ra)
            return max((dt - datetime.now(timezone.utc)).total_seconds(), 0.0)
    if isinstance(body, dict) and isinstance(body.get("retry_after_seconds"), (int, float)):
        return float(body["retry_after_seconds"])
    return None


@dataclass
class Constellation:
    api_key: str | None = None
    base_url: str = BASE_URL
    client: httpx.Client | None = None
    max_retries: int = 3
    sleep = staticmethod(time.sleep)

    def __post_init__(self):
        self.api_key = self.api_key or api_key_from_env()
        self.client = self.client or httpx.Client(base_url=self.base_url, timeout=30)

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, *, params=None, json_body=None, auth: bool = True,
                 retry_ambiguous: bool = True) -> object:
        headers = {}
        if auth:
            if not self.api_key:
                raise ConstellationError(401, "auth_failed", "CONSTELLATION_API_KEY is not set")
            headers["x-api-key"] = self.api_key
        content = None
        if json_body is not None:
            content = json.dumps(json_body, separators=(",", ":")).encode()
            headers["content-type"] = "application/json"
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.request(method, path, params=params, content=content, headers=headers)
            except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                if not retry_ambiguous:
                    raise AmbiguousWrite(f"{method} {path}: {e}") from e
                if attempt == self.max_retries:
                    raise
                self.sleep(delay + random.random() * 0.5)
                delay = min(delay * 2, 30)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            if resp.status_code < 400:
                return body
            code = body.get("error") if isinstance(body, dict) else None
            ra = _retry_after(resp, body)
            retryable = (resp.status_code == 429 and code != "auth_lockout") or resp.status_code == 503
            if not retryable or attempt == self.max_retries:
                raise ConstellationError(resp.status_code, code, body, ra)
            # Never shorten a server-provided delay; jitter goes on top.
            self.sleep((ra if ra is not None else delay) + random.random() * 0.5)
            delay = min(delay * 2, 30)
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------- endpoints
    def health(self) -> dict:
        return self._request("GET", "/health", auth=False)

    def account_state(self) -> dict:
        return self._request("GET", "/account/state")

    def topology(self, measurement: str | None = None, freshness_seconds: float = 300,
                 as_of_utc: str | None = None) -> dict:
        if measurement is not None and measurement not in TOPOLOGY_MEASUREMENTS:
            raise ValueError(f"measurement must be one of {TOPOLOGY_MEASUREMENTS}")
        params = {"freshness_seconds": freshness_seconds}
        if measurement:
            params["measurement"] = measurement
        if as_of_utc:
            params["as_of_utc"] = as_of_utc
        return self._request("GET", "/topology", params=params)

    def series(self, entity_ids: list[str], start: str, end: str | None = None, measurement: str = "link",
               fields: list[str] | None = None, interval: str | None = None, agg: str | None = None) -> dict:
        """GET /topology/series for up to 16 entities, following truncation and
        retrying once with the server's `min_interval` if the span needs one."""
        if not 1 <= len(set(entity_ids)) <= 16:
            raise ValueError("series takes 1 to 16 entity ids")
        params: list[tuple[str, str]] = [("entity_id", e) for e in entity_ids]
        params += [("start", start), ("measurement", measurement)]
        if end:
            params.append(("end", end))
        if fields:
            params.append(("fields", ",".join(fields)))
        if interval:
            params.append(("interval", interval))
            params.append(("agg", agg or "mean"))
        merged: dict | None = None
        while True:
            try:
                page = self._request("GET", "/topology/series", params=params)
            except ConstellationError as e:
                if e.status == 422 and isinstance(e.body, dict) and e.body.get("min_interval") and not interval:
                    return self.series(entity_ids, start, end, measurement, fields, e.body["min_interval"], agg or "last")
                raise
            if merged is None:
                merged = page
            else:
                by = {s["entity_id"]: s for s in merged["series"]}
                for s in page["series"]:
                    m = by.get(s["entity_id"])
                    if m is None:
                        merged["series"].append(s)
                        continue
                    m["times"] += s["times"]
                    for ch, vals in s["channels"].items():
                        m["channels"].setdefault(ch, []).extend(vals)
            if not page.get("truncated") or not page.get("next_start"):
                merged["truncated"] = False
                return merged
            params = [(k, v) for k, v in params if k != "start"] + [("start", page["next_start"])]

    def predictions(self, link_ids: list[str], model_family: str = "snr",
                    horizon_minutes: int | None = None) -> dict:
        """One request (= one unit of the monthly allowance) for all ids."""
        ids = list(dict.fromkeys(link_ids))
        if not ids:
            raise ValueError("at least one link id")
        params: list[tuple[str, str]] = [("model_family", model_family)] + [("link_ids", i) for i in ids]
        if horizon_minutes is not None:
            params.append(("horizon_minutes", str(horizon_minutes)))
        return self._request("GET", "/predictions", params=params)

    def send_telemetry(self, records: list[dict]) -> list[dict]:
        """POST in batches under both caps. Returns each batch's 202 ack.

        Raises AmbiguousWrite if a batch was sent but not acknowledged; the
        caller must reconcile through /topology before resending it.
        """
        acks = []
        for batch in batch_records(records):
            acks.append(self._request("POST", "/telemetry", json_body=batch, retry_ambiguous=False))
        return acks


def batch_records(records: list[dict], max_batch: int = MAX_BATCH, max_body: int = MAX_BODY - 4096) -> list[list[dict]]:
    out, cur, size = [], [], 2
    for r in records:
        n = len(json.dumps(r, separators=(",", ":"))) + 1
        if cur and (len(cur) >= max_batch or size + n > max_body):
            out.append(cur)
            cur, size = [], 2
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


@contextlib.asynccontextmanager
async def _mcp_streams(url: str, headers: dict):
    """Streamable HTTP transport across MCP Python SDK 1.x and 2.x."""
    import mcp.client.streamable_http as sh
    if hasattr(sh, "streamable_http_client"):          # 2.x: headers live on the http client
        async with sh.create_mcp_http_client(headers=headers) as http:
            async with sh.streamable_http_client(url, http_client=http) as streams:
                yield streams[0], streams[1]
    else:                                                # 1.x
        async with sh.streamablehttp_client(url, headers=headers) as (read, write, _):
            yield read, write


class ConstellationMCP:
    """Calls the Constellation MCP server's tools (Streamable HTTP, bearer auth).

    Tools: load_demo_fleet, get_topology, get_predictions, send_telemetry, get_usage.
    """

    def __init__(self, api_key: str | None = None, url: str = MCP_URL):
        self.api_key = api_key or api_key_from_env()
        self.url = url

    @contextlib.asynccontextmanager
    async def _session(self):
        from mcp import ClientSession

        async with _mcp_streams(self.url, {"Authorization": f"Bearer {self.api_key}"}) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _call(self, tool: str, args: dict) -> object:
        async with self._session() as session:
            res = await session.call_tool(tool, args)
            if getattr(res, "structuredContent", None):
                return res.structuredContent
            joined = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            try:
                return json.loads(joined)
            except ValueError:
                return joined

    async def _list(self) -> list[str]:
        async with self._session() as session:
            return [t.name for t in (await session.list_tools()).tools]

    def call(self, tool: str, **args) -> object:
        return asyncio.run(self._call(tool, args))

    def tools(self) -> list[str]:
        return asyncio.run(self._list())

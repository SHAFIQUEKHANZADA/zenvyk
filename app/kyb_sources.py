"""KYB source clients — query each authoritative source for one business.

Each returns a normalized result:
    {key, name, weight, verdict, fields, mode, detail}
  verdict: AGREES | PARTIAL | NO_MATCH | ERROR
  mode:    live   | sample  | error

A source runs LIVE only when its API key is configured; otherwise it returns
clearly-labeled SAMPLE data (so the UI works before you've bought API access).
All real calls are best-effort and defensive — a failure degrades to ERROR,
never a fabricated "match".
"""
from __future__ import annotations

import asyncio
import re

import httpx

from app import kyb_config

_SUFFIXES = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|corp|co|company|ltd|limited)\b", re.I)


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = _SUFFIXES.sub("", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _name_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) >= max(1, min(len(ta), len(tb)) - 1)


def _verdict_from_fields(fields: dict) -> str:
    core = [fields.get(f) for f in kyb_config.CORE_FIELDS if isinstance(fields.get(f), bool)]
    if core and all(core):
        return "AGREES"
    if any(core):
        return "PARTIAL"
    return "NO_MATCH"


def _sample(source: dict, scenario: str) -> dict:
    data = kyb_config.SAMPLE_SCENARIOS.get(scenario, kyb_config.SAMPLE_SCENARIOS["clean"])
    entry = data.get(source["key"], {"verdict": "NO_MATCH", "fields": {}})
    return {
        "key": source["key"],
        "name": source["name"],
        "weight": source["weight"],
        "verdict": entry["verdict"],
        "fields": entry["fields"],
        "mode": "sample",
        "detail": "sample data (no live API key configured)",
    }


def _result(source: dict, verdict: str, fields: dict, detail: str, mode: str = "live") -> dict:
    return {
        "key": source["key"],
        "name": source["name"],
        "weight": source["weight"],
        "verdict": verdict,
        "fields": fields,
        "mode": mode,
        "detail": detail,
    }


# --- individual sources -------------------------------------------------------
async def _middesk(source, biz, scenario) -> dict:
    if not kyb_config.MIDDESK_API_KEY:
        return _sample(source, scenario)
    try:
        payload = {
            "name": biz.get("name"),
            "website": {"url": biz.get("website")} if biz.get("website") else None,
            "tin": {"tin": biz.get("ein")} if biz.get("ein") else None,
            "addresses": [
                {
                    "address_line1": biz.get("address"),
                    "city": biz.get("city"),
                    "state": biz.get("state"),
                    "postal_code": biz.get("zip"),
                }
            ],
        }
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{kyb_config.MIDDESK_BASE}/v1/businesses",
                headers={"Authorization": f"Bearer {kyb_config.MIDDESK_API_KEY}"},
                json={k: v for k, v in payload.items() if v is not None},
            )
        resp.raise_for_status()
        data = resp.json()
        # Middesk KYB is asynchronous: the review may still be pending here.
        review = data.get("review") or {}
        tasks = {t.get("key"): t.get("status") for t in (review.get("tasks") or [])}
        if not tasks:
            return _result(source, "PARTIAL",
                           {"name": True, "status": data.get("status")},
                           "live: business created, review pending (poll/webhook)")
        ok = lambda k: tasks.get(k) == "success"  # noqa: E731
        fields = {
            "name": ok("name"),
            "address": ok("address"),
            "ein": ok("tin"),
            "status": "active" if ok("sos") else "unknown",
            "watchlist": "hit" if tasks.get("watchlist") == "failure" else "clear",
        }
        return _result(source, _verdict_from_fields(fields), fields, "live: Middesk review")
    except Exception as exc:  # noqa: BLE001
        return _result(source, "ERROR", {}, f"live error: {type(exc).__name__}", mode="error")


async def _opencorporates(source, biz, scenario) -> dict:
    if not kyb_config.OPENCORPORATES_API_TOKEN:
        return _sample(source, scenario)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.opencorporates.com/v0.4/companies/search",
                params={
                    "q": biz.get("name"),
                    "api_token": kyb_config.OPENCORPORATES_API_TOKEN,
                    "jurisdiction_code": f"us_{str(biz.get('state', '')).lower()}"
                    if biz.get("state")
                    else None,
                },
            )
        resp.raise_for_status()
        companies = resp.json().get("results", {}).get("companies", [])
        if not companies:
            return _result(source, "NO_MATCH", {"name": False}, "live: no registry match")
        top = companies[0].get("company", {})
        name_ok = _name_match(biz.get("name", ""), top.get("name", ""))
        status = (top.get("current_status") or "").lower()
        addr = (top.get("registered_address_in_full") or "").lower()
        addr_ok = bool(biz.get("city")) and biz["city"].lower() in addr
        fields = {
            "name": name_ok,
            "address": addr_ok,
            "status": "active" if "active" in status or "good" in status else status or "unknown",
        }
        return _result(source, _verdict_from_fields(fields), fields, "live: OpenCorporates")
    except Exception as exc:  # noqa: BLE001
        return _result(source, "ERROR", {}, f"live error: {type(exc).__name__}", mode="error")


async def _google_places(source, biz, scenario) -> dict:
    if not kyb_config.GOOGLE_PLACES_API_KEY:
        return _sample(source, scenario)
    try:
        query = " ".join(filter(None, [biz.get("name"), biz.get("address"), biz.get("city")]))
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": query, "key": kyb_config.GOOGLE_PLACES_API_KEY},
            )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return _result(source, "NO_MATCH", {"name": False}, "live: no place match")
        top = results[0]
        name_ok = _name_match(biz.get("name", ""), top.get("name", ""))
        addr_ok = bool(biz.get("city")) and biz["city"].lower() in (top.get("formatted_address", "").lower())
        fields = {"name": name_ok, "address": addr_ok}
        return _result(source, _verdict_from_fields(fields), fields, "live: Google Places")
    except Exception as exc:  # noqa: BLE001
        return _result(source, "ERROR", {}, f"live error: {type(exc).__name__}", mode="error")


async def _website(source, biz, scenario) -> dict:
    url = biz.get("website")
    if not url:
        return _sample(source, scenario)
    try:
        if not url.startswith("http"):
            url = "https://" + url
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "ZenvykGuardian/1.0"})
        resp.raise_for_status()
        text = resp.text.lower()
        name_ok = _name_match(biz.get("name", ""), "") or _norm(biz.get("name", "")) in _norm(text)
        addr_ok = bool(biz.get("city")) and biz["city"].lower() in text
        fields = {"name": name_ok, "address": addr_ok}
        verdict = "AGREES" if name_ok and addr_ok else "PARTIAL" if name_ok else "NO_MATCH"
        return _result(source, verdict, fields, "live: website scrape")
    except Exception as exc:  # noqa: BLE001
        return _result(source, "ERROR", {}, f"live error: {type(exc).__name__}", mode="error")


async def _tin(source, biz, scenario) -> dict:
    # No public real-time TIN/EIN verification API — always sample/unavailable.
    return _sample(source, scenario)


_DISPATCH = {
    "middesk": _middesk,
    "opencorporates": _opencorporates,
    "google_places": _google_places,
    "website": _website,
    "tin": _tin,
}


async def gather_sources(biz: dict, scenario: str, demo: bool) -> list[dict]:
    """Query all sources concurrently. In demo/presentation mode, force sample."""
    if demo or kyb_config.PRESENTATION_MODE:
        return [_sample(s, scenario) for s in kyb_config.KYB_SOURCES]
    tasks = [_DISPATCH[s["key"]](s, biz, scenario) for s in kyb_config.KYB_SOURCES]
    return list(await asyncio.gather(*tasks))

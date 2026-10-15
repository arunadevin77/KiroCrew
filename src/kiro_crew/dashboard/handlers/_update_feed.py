"""Bounded release-feed transport with explicit proxy selection."""

import urllib.parse
import urllib.request

import aiohttp

from kiro_crew.dashboard.handlers._shared import read_capped_response


async def fetch_feed_bytes(url: str, *, timeout_secs: float, max_bytes: int) -> tuple[int, bytes]:
    """GET the feed, returning its status and at most ``max_bytes + 1`` bytes."""
    timeout = aiohttp.ClientTimeout(total=timeout_secs)
    # Explicit proxy selection keeps aiohttp's netrc credential lookup disabled.
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if proxy and urllib.request.proxy_bypass(urllib.parse.urlparse(url).hostname or ""):
        proxy = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, proxy=proxy) as resp:
            # Drain chunked feeds to EOF; a single read can return a partial body.
            return resp.status, await read_capped_response(resp, max_bytes)

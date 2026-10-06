"""
DNS Resilience Module
Automatically provides fallback DNS resolution (DoH and Microsoft Anycast IP pool)
for speech.platform.bing.com when local ISP DNS servers fail with
[Errno 8] 'nodename nor servname provided, or not known'.
"""

import json
import logging
import socket
import threading
import urllib.request
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Verified Anycast IP pool for Microsoft Edge TTS / Bing Speech endpoints
DEFAULT_KNOWN_IPS: Dict[str, List[str]] = {
    "speech.platform.bing.com": [
        "150.171.27.10",
        "150.171.28.10",
        "20.207.73.82",
        "20.197.234.34",
        "13.107.246.10",
        "20.42.65.86"
    ]
}

_orig_getaddrinfo = socket.getaddrinfo
_hooked = False
_cache_lock = threading.Lock()
_dns_cache: Dict[str, List[str]] = {}


def _resolve_via_doh(hostname: str) -> Optional[List[str]]:
    """Resolve hostname using DNS over HTTPS (Google DoH and Cloudflare DoH)."""
    providers = [
        f"https://dns.google/resolve?name={hostname}&type=A",
        f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A"
    ]
    for url in providers:
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ips = [
                    ans["data"] for ans in data.get("Answer", [])
                    if ans.get("type") == 1 and ans.get("data")
                ]
                if ips:
                    return ips
        except Exception:
            continue
    return None


def resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """
    Drop-in replacement for socket.getaddrinfo.
    Attempts standard resolution first; if it fails on speech.platform.bing.com,
    falls back to DoH or known IP pool.
    """
    # 1. Try standard system resolution
    try:
        results = _orig_getaddrinfo(host, port, family, type, proto, flags)
        if host and isinstance(host, str) and "bing.com" in host:
            # Cache successfully resolved IPv4 addresses
            ips = [r[4][0] for r in results if r[0] == socket.AF_INET and r[4]]
            if ips:
                with _cache_lock:
                    _dns_cache[host] = ips
        return results
    except socket.gaierror as e:
        # Check if the host is speech.platform.bing.com or matching subdomain
        is_target = isinstance(host, str) and (
            host == "speech.platform.bing.com" or host.endswith(".platform.bing.com") or "bing.com" in host
        )
        if not is_target:
            raise e

        logger.debug(f"[DNS Resilience] Local DNS lookup failed for '{host}'. Initiating resilient fallback...")

        # 2. Check in-memory working cache
        fallback_ips = []
        with _cache_lock:
            if host in _dns_cache:
                fallback_ips = list(_dns_cache[host])

        # 3. Try DNS over HTTPS (Google/Cloudflare DoH)
        if not fallback_ips:
            doh_ips = _resolve_via_doh(host)
            if doh_ips:
                fallback_ips = doh_ips
                with _cache_lock:
                    _dns_cache[host] = doh_ips

        # 4. Use verified hardcoded Anycast IP pool
        if not fallback_ips:
            fallback_ips = DEFAULT_KNOWN_IPS.get(host, DEFAULT_KNOWN_IPS["speech.platform.bing.com"])

        if fallback_ips:
            target_port = port if isinstance(port, int) else (443 if port in ("https", "wss") else 80)
            target_type = type if type else socket.SOCK_STREAM
            target_proto = proto if proto else (6 if target_type == socket.SOCK_STREAM else 0)

            sock_results = []
            for ip in fallback_ips:
                sockaddr = (ip, target_port)
                sock_results.append((socket.AF_INET, target_type, target_proto, "", sockaddr))

            logger.info(f"🛡️ [DNS Resilience] Recovered '{host}' -> {fallback_ips[:2]} via fallback DNS resolver.")
            return sock_results

        raise e


def setup_dns_resilience():
    """Install the resilient DNS resolver hook globally."""
    global _hooked
    if not _hooked:
        socket.getaddrinfo = resilient_getaddrinfo
        _hooked = True
        logger.info("✅ Resilient DNS Resolver initialized (Edge-TTS protection active).")

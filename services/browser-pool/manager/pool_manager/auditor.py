"""Pre-flight proxy audit — measure the proxy's REAL exit before Chrome starts.

Goes through the profile's own upstream proxy to:
  - cloudflare.com/cdn-cgi/trace → exit IP + country as Cloudflare sees it
  - ip-api.com/json → country/region/city/timezone + proxy/hosting flags
  - (optional) proxycheck.io → fraud score / VPN detection

The returned timezone is what the pod sets as TZ at OS-process level.
"""
import asyncio

import httpx

IPAPI_FIELDS = "status,message,country,countryCode,regionName,city,timezone,query,proxy,hosting,mobile"


async def audit_proxy(
    proxy_url: str, proxycheck_key: str | None = None, timeout: int = 20
) -> dict:
    result: dict = {"ok": False}
    # empty proxy_url → audit the direct exit (dev/testing, or direct profile)
    client_kwargs = {"proxy": proxy_url} if proxy_url else {}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, verify=False, follow_redirects=True, **client_kwargs
        ) as c:
            t_trace = c.get("https://www.cloudflare.com/cdn-cgi/trace")
            t_api = c.get(f"http://ip-api.com/json/?fields={IPAPI_FIELDS}")
            r_trace, r_api = await asyncio.gather(t_trace, t_api, return_exceptions=True)
    except Exception as e:
        result["error"] = f"proxy connect failed: {e}"
        return result

    if isinstance(r_trace, httpx.Response) and r_trace.status_code == 200:
        trace = {}
        for line in r_trace.text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                trace[k.strip()] = v.strip()
        result["ip"] = trace.get("ip")
        result["cf_country"] = trace.get("loc")
        result["tls"] = trace.get("tls")
        result["http"] = trace.get("http")

    if isinstance(r_api, httpx.Response) and r_api.status_code == 200:
        try:
            d = r_api.json()
        except Exception:
            d = {}
        if d.get("status") == "success":
            result.update(
                {
                    "ip": result.get("ip") or d.get("query"),
                    "country": d.get("country"),
                    "country_code": d.get("countryCode"),
                    "region": d.get("regionName"),
                    "city": d.get("city"),
                    "timezone": d.get("timezone"),
                    "proxy_flag": d.get("proxy"),
                    "hosting_flag": d.get("hosting"),
                    "mobile_flag": d.get("mobile"),
                }
            )
        else:
            result["ipapi_error"] = d.get("message")

    # Country signal: prefer ip-api's code, fall back to Cloudflare's loc
    result["country_code"] = result.get("country_code") or result.get("cf_country")

    if proxycheck_key and result.get("ip"):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(
                    f"https://proxycheck.io/v2/{result['ip']}",
                    params={"key": proxycheck_key, "vpn": 1, "risk": 1, "asn": 1},
                )
                if r.status_code == 200:
                    d = r.json()
                    node = d.get(result["ip"]) or {}
                    result["risk"] = node.get("risk")
                    result["proxy_type"] = node.get("type")
        except Exception as e:
            result["proxycheck_error"] = str(e)

    result["ok"] = bool(result.get("ip"))
    if not result["ok"]:
        errs = []
        for r in (r_trace, r_api):
            if isinstance(r, Exception):
                errs.append(str(r))
        result["error"] = result.get("error") or "; ".join(errs) or "audit failed"
    return result

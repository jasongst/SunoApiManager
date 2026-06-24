"""
2Captcha hCaptcha solver — token-based, no human interaction.

Given an hCaptcha ``sitekey`` + page URL (plus the enterprise ``rqdata`` blob
when Suno serves hCaptcha Enterprise), this submits the challenge to the
2Captcha worker pool and polls for the solved token.

We talk to the classic ``in.php`` / ``res.php`` HTTP API directly rather than
pulling in the ``2captcha-python`` SDK — aiohttp is already a project
dependency and the API is two endpoints.

API reference: https://2captcha.com/2captcha-api#solving_hcaptcha
"""

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("suno-manager")

_IN_URL = "https://2captcha.com/in.php"
_RES_URL = "https://2captcha.com/res.php"


class TwoCaptchaError(Exception):
    """Raised when 2Captcha rejects the task or never returns a solution."""


async def solve_hcaptcha(
    api_key: str,
    sitekey: str,
    page_url: str,
    *,
    rqdata: Optional[str] = None,
    invisible: bool = True,
    user_agent: Optional[str] = None,
    poll_interval: float = 5.0,
    timeout: float = 180.0,
) -> str:
    """Solve an hCaptcha via 2Captcha and return the token string.

    Args:
        api_key: 2Captcha account API key.
        sitekey: hCaptcha sitekey harvested from the target page.
        page_url: URL where the captcha is shown (Suno's create page).
        rqdata: Enterprise ``rqdata`` blob, when the site uses hCaptcha
            Enterprise. Required for the token to validate in that case.
        invisible: Whether the widget is invisible (Suno's is).
        user_agent: UA to bind the solve to — best matched to the client that
            will actually submit the token, so server-side validation lines up.
        poll_interval: Seconds between result polls.
        timeout: Hard cap on total wait before giving up.

    Returns:
        The solved hCaptcha token (e.g. ``P1_...``).

    Raises:
        TwoCaptchaError: on a submission error, an explicit solve failure, or a
            timeout. The caller treats any of these as "service didn't work"
            and falls back to the manual solve.
    """
    submit_data = {
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": page_url,
        "json": "1",
    }
    if invisible:
        submit_data["invisible"] = "1"
    if rqdata:
        # Enterprise payload — 2Captcha expects it in the `data` field.
        submit_data["data"] = rqdata
    if user_agent:
        submit_data["userAgent"] = user_agent

    async with aiohttp.ClientSession() as session:
        # ── Submit the task ──
        async with session.post(_IN_URL, data=submit_data) as resp:
            payload = await resp.json(content_type=None)
        if payload.get("status") != 1:
            raise TwoCaptchaError(f"submission rejected: {payload.get('request')}")
        captcha_id = payload["request"]
        logger.info(f"2Captcha: submitted hCaptcha task {captcha_id}, polling for result...")

        # ── Poll for the result ──
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        # 2Captcha always needs a few seconds before a result can be ready.
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - loop.time())))
        while loop.time() < deadline:
            params = {"key": api_key, "action": "get", "id": captcha_id, "json": "1"}
            async with session.get(_RES_URL, params=params) as resp:
                payload = await resp.json(content_type=None)
            status = payload.get("status")
            request = payload.get("request")
            if status == 1:
                logger.info("2Captcha: hCaptcha solved")
                return request
            if request != "CAPCHA_NOT_READY":  # 2Captcha's documented spelling
                raise TwoCaptchaError(f"solve failed: {request}")
            await asyncio.sleep(poll_interval)

    raise TwoCaptchaError(f"timed out after {timeout:.0f}s waiting for 2Captcha")

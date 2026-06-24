"""
CAPTCHA Solver for Suno — Handles hCaptcha challenges using Patchright.

When Suno requires CAPTCHA verification for generation, this module:
1. Checks if CAPTCHA is required via /api/c/check
2. Opens a real browser window for the user to solve the challenge
3. Intercepts the generate request to capture the hCaptcha token
4. Returns the token for use in API calls

The token is cached and reused until it expires or a new CAPTCHA is required.

We use Patchright (a drop-in, undetected fork of Playwright) instead of vanilla
Playwright: it patches the well-known automation leaks (Runtime.enable,
navigator.webdriver, console hooks, …) at the CDP level. This makes Suno's
hCaptcha / Cloudflare anti-bot present *easier* challenges to the human solver
and reduces the risk of the session being flagged. Per Patchright's guidance we
therefore do NOT pass the old `--disable-blink-features=AutomationControlled`
style flags (which are themselves detectable) and run from a persistent profile.
"""

import asyncio
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger("suno-manager")

# Persistent browser profile — keeps cookies / Cloudflare clearance between
# solves, which both reduces challenge frequency and looks less bot-like.
_PROFILE_DIR = os.path.join(os.getcwd(), ".patchright_profile")


class CaptchaSolver:
    """Manages hCaptcha token acquisition for Suno API generate calls."""

    def __init__(self, suno_client):
        """Initialize with a reference to the SunoClient instance.

        Args:
            suno_client: Initialized SunoClient with valid cookies/JWT
        """
        self.suno_client = suno_client
        self._cached_token: Optional[str] = None
        self._token_time: float = 0
        self._token_ttl: float = 120  # hCaptcha tokens typically valid ~2 min
        self._solving: bool = False
        # Serializes solves so concurrent generate calls never open two browser
        # windows — they queue on the lock and reuse the freshly solved token.
        self._lock = asyncio.Lock()

    @property
    def is_solving(self) -> bool:
        return self._solving

    @property
    def has_valid_token(self) -> bool:
        if not self._cached_token:
            return False
        return (time.time() - self._token_time) < self._token_ttl

    async def check_captcha_required(self) -> bool:
        """Check if Suno requires CAPTCHA for generation.

        POST https://studio-api.prod.suno.com/api/c/check
        """
        try:
            data = await self.suno_client._request(
                "POST", "/api/c/check", json={"ctype": "generation"}, timeout=10
            )
            required = data.get("required", False)
            logger.info(f"CAPTCHA check: required={required}")
            return required
        except Exception as e:
            logger.warning(f"CAPTCHA check failed: {e} — assuming required")
            return True

    async def get_token(self, force: bool = False) -> Optional[str]:
        """Get an hCaptcha token, solving if necessary.

        Args:
            force: If True, solve a new CAPTCHA even if cached token exists

        Returns:
            hCaptcha token string, or None if not required
        """
        # Return cached token if still valid
        if not force and self.has_valid_token:
            logger.info("Using cached CAPTCHA token")
            return self._cached_token

        # Check if CAPTCHA is actually required
        if not force:
            required = await self.check_captcha_required()
            if not required:
                logger.info("CAPTCHA not required for generation")
                self._cached_token = None
                return None

        # Only one solve at a time. Concurrent callers queue here; the lock also
        # means a second caller that was waiting will find a fresh token below.
        async with self._lock:
            # Re-check under the lock: another coroutine may have just solved it
            # while we were waiting to acquire the lock.
            if not force and self.has_valid_token:
                logger.info("Reusing CAPTCHA token solved by a concurrent request")
                return self._cached_token
            return await self._solve_captcha()

    async def _solve_captcha(self) -> Optional[str]:
        """Launch browser and let user solve hCaptcha manually.

        Must be called while holding ``self._lock``.
        """
        self._solving = True
        try:
            token = await self._browser_solve()
            if token:
                self._cached_token = token
                self._token_time = time.time()
                logger.info(f"CAPTCHA token acquired ({len(token)} chars)")
            return token
        except Exception as e:
            logger.error(f"CAPTCHA solve failed: {e}")
            raise
        finally:
            self._solving = False

    async def _browser_solve(self) -> Optional[str]:
        """Launch the Playwright solve, isolating it from uvicorn's event loop.

        On Windows uvicorn installs a ``WindowsSelectorEventLoopPolicy``, and the
        selector loop cannot spawn subprocesses — launching the browser raises
        ``NotImplementedError`` from ``create_subprocess_exec``. We therefore run
        the whole solve in a dedicated thread backed by a ``ProactorEventLoop``,
        which supports subprocesses, and await that thread without blocking
        uvicorn's loop. On non-Windows platforms the running loop is fine.
        """
        if sys.platform != "win32":
            return await self._browser_solve_impl()

        result: dict = {}

        def runner():
            # Explicitly build a Proactor loop: asyncio.new_event_loop() would
            # honour uvicorn's selector policy and reintroduce the same bug.
            loop = asyncio.ProactorEventLoop()
            try:
                asyncio.set_event_loop(loop)
                result["token"] = loop.run_until_complete(self._browser_solve_impl())
            except BaseException as e:  # propagate to the awaiting coroutine
                result["error"] = e
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=runner, name="captcha-solver", daemon=True)
        thread.start()
        # Wait for the solver thread without freezing uvicorn's event loop
        # (the user may take minutes to solve the challenge).
        await asyncio.get_running_loop().run_in_executor(None, thread.join)

        if "error" in result:
            raise result["error"]
        return result.get("token")

    async def _browser_solve_impl(self) -> Optional[str]:
        """Open browser, navigate to suno.com/create, capture hCaptcha token."""
        try:
            from patchright.async_api import async_playwright  # type: ignore[import-not-found]
        except ImportError:
            raise RuntimeError(
                "Patchright not installed. Run: pip install patchright && patchright install chromium"
            )

        logger.info("Launching browser for CAPTCHA solving...")

        token_future: asyncio.Future = asyncio.get_running_loop().create_future()
        os.makedirs(_PROFILE_DIR, exist_ok=True)

        async with async_playwright() as p:
            # Patchright recommends a persistent context with no custom args and
            # no forced viewport — adding stealth flags or a fixed viewport is
            # itself detectable. The patches that defeat hCaptcha/Cloudflare
            # automation detection are applied automatically by Patchright.
            # `channel="chrome"` (real Chrome) gives the best stealth; we fall
            # back to the bundled Chromium if Chrome isn't available locally.
            launch_kwargs = dict(
                user_data_dir=_PROFILE_DIR,
                headless=False,
                no_viewport=True,
            )
            try:
                context = await p.chromium.launch_persistent_context(
                    channel="chrome", **launch_kwargs
                )
            except Exception as e:
                logger.info(f"System Chrome unavailable ({e}); using bundled Chromium")
                context = await p.chromium.launch_persistent_context(**launch_kwargs)

            # Inject cookies
            cookies = []
            # Add __session (JWT) cookie
            if self.suno_client.token:
                cookies.append({
                    "name": "__session",
                    "value": self.suno_client.token,
                    "domain": ".suno.com",
                    "path": "/",
                    "sameSite": "Lax",
                })
            # Add all parsed cookies from SunoClient
            for name, value in self.suno_client.cookies.items():
                cookies.append({
                    "name": name,
                    "value": str(value),
                    "domain": ".suno.com",
                    "path": "/",
                    "sameSite": "Lax",
                })
            await context.add_cookies(cookies)

            # A persistent context already opens with one blank page — reuse it
            # rather than leaving an extra empty tab behind.
            page = context.pages[0] if context.pages else await context.new_page()

            # Intercept generate request to capture hCaptcha token
            async def handle_route(route):
                try:
                    request = route.request
                    post_data = request.post_data_json
                    if post_data and "token" in post_data and post_data["token"]:
                        captured_token = post_data["token"]
                        logger.info("hCaptcha token captured from generate request!")

                        # Also refresh JWT from the browser's auth header
                        auth_header = request.headers.get("authorization", "")
                        if auth_header.startswith("Bearer "):
                            new_jwt = auth_header[7:]
                            if new_jwt and new_jwt != self.suno_client.token:
                                self.suno_client.token = new_jwt
                                self.suno_client._token_refreshed_at = (
                                    asyncio.get_running_loop().time()
                                )
                                logger.info("JWT also refreshed from browser session")

                        if not token_future.done():
                            token_future.set_result(captured_token)
                    # Abort the actual generate request (we just needed the token)
                    await route.abort()
                except Exception as e:
                    logger.error(f"Route handler error: {e}")
                    if not token_future.done():
                        token_future.set_exception(e)
                    await route.abort()

            await page.route("**/api/generate/v2/**", handle_route)

            # Navigate to suno.com/create
            logger.info("Navigating to suno.com/create...")
            try:
                await page.goto(
                    "https://suno.com/create",
                    referer="https://www.google.com/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                logger.warning(f"Page load issue (may still work): {e}")

            # Wait for the page to fully load (song list API call)
            try:
                await page.wait_for_response(
                    lambda resp: "/api/project/" in resp.url, timeout=30000
                )
                logger.info("Suno interface loaded")
            except Exception:
                logger.warning("Timed out waiting for project API — page may still be usable")

            # Close any popups
            try:
                close_btn = page.get_by_label("Close")
                await close_btn.click(timeout=2000)
            except Exception:
                pass

            logger.info(
                "Browser is open. Please solve the CAPTCHA:\n"
                "  1. Type something in the prompt box\n"
                "  2. Click 'Create'\n"
                "  3. Solve the hCaptcha challenge\n"
                "  4. The token will be captured automatically"
            )

            # Wait for the token (user solves CAPTCHA manually)
            try:
                token = await asyncio.wait_for(token_future, timeout=300)  # 5 min
            except asyncio.TimeoutError:
                logger.error("CAPTCHA solve timed out after 5 minutes")
                token = None

            # Clean up
            try:
                await context.close()
            except Exception:
                pass

            return token

    def invalidate_token(self):
        """Mark the current token as invalid (e.g. after a 422 response)."""
        self._cached_token = None
        self._token_time = 0
        logger.info("CAPTCHA token invalidated")

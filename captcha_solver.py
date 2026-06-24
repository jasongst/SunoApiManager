"""
CAPTCHA Solver for Suno — Handles hCaptcha challenges using Patchright.

When Suno requires CAPTCHA verification for generation, this module:
1. Checks if CAPTCHA is required via /api/c/check
2. Opens a real browser window for the user to solve the challenge
3. Intercepts the generate request to capture the hCaptcha token
4. Returns the token for use in API calls

The token is cached and reused until it expires or a new CAPTCHA is required.

Two solving strategies are supported, tried in order:

1. **2Captcha (automatic)** — when a 2Captcha API key is configured. A browser
   is opened only to *harvest* the hCaptcha ``sitekey`` (and the enterprise
   ``rqdata`` blob, if present); those are handed to 2Captcha's worker pool,
   which returns a token with no human interaction. See ``twocaptcha_solver``.
2. **Manual (fallback)** — a real browser window opens for a human to solve the
   challenge, and the token is captured by sniffing the generate request. This
   is used when no key is set, harvesting fails, or 2Captcha errors/times out.

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
import random
import re
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger("suno-manager")

# Persistent browser profile — keeps cookies / Cloudflare clearance between
# solves, which both reduces challenge frequency and looks less bot-like.
_PROFILE_DIR = os.path.join(os.getcwd(), ".patchright_profile")

# Random prompts used to auto-trigger a generation (and thus the hCaptcha
# challenge) without manual typing. Kept generic and innocuous.
_RANDOM_PROMPTS = [
    "a mellow lo-fi beat with soft piano and gentle rain",
    "an upbeat indie pop song about a summer road trip",
    "a calm acoustic guitar ballad at sunset",
    "a dreamy synthwave track with warm retro vibes",
    "a soulful jazz tune with smooth saxophone",
    "an energetic electronic dance anthem",
    "a folk song with warm vocals and a bright banjo",
    "a cinematic orchestral piece full of hope",
]


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
        """Acquire a token: try 2Captcha first (if configured), then manual.

        Must be called while holding ``self._lock``.
        """
        self._solving = True
        try:
            token: Optional[str] = None

            # ── Strategy 1: automatic solve via 2Captcha ──
            api_key = self._get_2captcha_key()
            if api_key:
                try:
                    token = await self._run_browser_task(
                        lambda: self._auto_solve_impl(api_key)
                    )
                except Exception as e:
                    logger.warning(
                        f"2Captcha auto-solve failed ({e}); falling back to "
                        "manual solve"
                    )
                    token = None

            # ── Strategy 2: manual browser solve (fallback) ──
            if not token:
                token = await self._run_browser_task(self._browser_solve_impl)

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

    async def _run_browser_task(self, impl):
        """Run a browser-driven coroutine, isolated from uvicorn's event loop.

        ``impl`` is a zero-argument coroutine function. On Windows uvicorn
        installs a ``WindowsSelectorEventLoopPolicy`` whose selector loop cannot
        spawn subprocesses — launching the browser raises ``NotImplementedError``
        from ``create_subprocess_exec``. We therefore run the whole task in a
        dedicated thread backed by a ``ProactorEventLoop`` (which supports
        subprocesses) and await that thread without blocking uvicorn's loop. On
        non-Windows platforms the running loop is fine.
        """
        if sys.platform != "win32":
            return await impl()

        result: dict = {}

        def runner():
            # Explicitly build a Proactor loop: asyncio.new_event_loop() would
            # honour uvicorn's selector policy and reintroduce the same bug.
            loop = asyncio.ProactorEventLoop()
            try:
                asyncio.set_event_loop(loop)
                result["value"] = loop.run_until_complete(impl())
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
        # (a manual solve may take minutes; a 2Captcha solve up to ~3 min).
        await asyncio.get_running_loop().run_in_executor(None, thread.join)

        if "error" in result:
            raise result["error"]
        return result.get("value")

    # ─── 2Captcha configuration ──────────────────────────────

    def _get_2captcha_key(self) -> Optional[str]:
        """Read the 2Captcha API key from settings (DB) or env, or None."""
        key = ""
        try:
            import database as db
            key = (db.get_setting("twocaptcha_api_key", "") or "").strip()
        except Exception:
            pass
        if not key:
            key = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
        return key or None

    def _get_configured_sitekey(self) -> Optional[str]:
        """Optional manual override for the hCaptcha sitekey (settings/env)."""
        try:
            import database as db
            sk = (db.get_setting("captcha_sitekey", "") or "").strip()
            if sk:
                return sk
        except Exception:
            pass
        return os.getenv("SUNO_HCAPTCHA_SITEKEY", "").strip() or None

    def _harvest_headless(self) -> bool:
        """Whether the param-harvest browser runs headless (default: yes).

        Headless is more bot-detectable (per Patchright guidance), so if Suno
        starts blocking the harvest, flip ``captcha_harvest_headless`` to
        ``false`` to harvest in a real (but auto-closing) window.
        """
        try:
            import database as db
            return (db.get_setting("captcha_harvest_headless", "true") or "true") != "false"
        except Exception:
            return True

    # ─── Shared browser helpers ──────────────────────────────

    @staticmethod
    def _import_playwright():
        """Import Patchright's async API or raise a helpful install message."""
        try:
            from patchright.async_api import async_playwright  # type: ignore[import-not-found]
            return async_playwright
        except ImportError:
            raise RuntimeError(
                "Patchright not installed. Run: pip install patchright && patchright install chromium"
            )

    async def _launch_context(self, p, *, headless: bool):
        """Open a persistent Patchright context (real Chrome, Chromium fallback).

        Patchright recommends a persistent context with no custom args and no
        forced viewport — adding stealth flags or a fixed viewport is itself
        detectable; the patches that defeat hCaptcha/Cloudflare automation
        detection are applied automatically. ``channel="chrome"`` (real Chrome)
        gives the best stealth; we fall back to the bundled Chromium if Chrome
        isn't available locally.
        """
        launch_kwargs = dict(
            user_data_dir=_PROFILE_DIR,
            headless=headless,
            no_viewport=True,
        )
        try:
            return await p.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
        except Exception as e:
            logger.info(f"System Chrome unavailable ({e}); using bundled Chromium")
            return await p.chromium.launch_persistent_context(**launch_kwargs)

    def _build_cookies(self) -> list:
        """Build the Suno cookie list (JWT + parsed cookies) for the context."""
        cookies = []
        if self.suno_client.token:
            cookies.append({
                "name": "__session",
                "value": self.suno_client.token,
                "domain": ".suno.com",
                "path": "/",
                "sameSite": "Lax",
            })
        for name, value in self.suno_client.cookies.items():
            cookies.append({
                "name": name,
                "value": str(value),
                "domain": ".suno.com",
                "path": "/",
                "sameSite": "Lax",
            })
        return cookies

    def _attach_token_sniffer(self, page, token_future: asyncio.Future):
        """Passively harvest an hCaptcha token from outgoing requests.

        We do NOT block/abort the request: Suno's web frontend keeps changing
        the generate endpoint, and a too-specific route filter silently matched
        nothing. Observing all POSTs is robust to endpoint changes; the cost is
        that the song used to trigger the challenge is actually generated in
        your account (expected).
        """
        def _looks_like_hcaptcha(value) -> bool:
            # hCaptcha tokens are long strings prefixed with P0_/P1_/E0_/E1_.
            return (
                isinstance(value, str)
                and len(value) > 20
                and value[:3] in ("P0_", "P1_", "E0_", "E1_")
            )

        def on_request(request):
            if token_future.done() or request.method != "POST":
                return
            try:
                data = request.post_data_json
            except Exception:
                return
            if not isinstance(data, dict):
                return

            # Prefer an explicit "token" field; otherwise scan all values for
            # something that looks like an hCaptcha token.
            token = data.get("token")
            if not (isinstance(token, str) and len(token) > 20):
                token = next(
                    (v for v in data.values() if _looks_like_hcaptcha(v)), None
                )
            if not token:
                return

            logger.info(f"hCaptcha token captured from {request.url}")

            # Also refresh JWT from the browser's auth header if present.
            try:
                auth_header = request.headers.get("authorization", "")
            except Exception:
                auth_header = ""
            if auth_header.startswith("Bearer "):
                new_jwt = auth_header[7:]
                if new_jwt and new_jwt != self.suno_client.token:
                    self.suno_client.token = new_jwt
                    self.suno_client._token_refreshed_at = (
                        asyncio.get_running_loop().time()
                    )
                    logger.info("JWT also refreshed from browser session")

            if not token_future.done():
                token_future.set_result(token)

        page.on("request", on_request)

    def _attach_param_sniffer(self, page, params_future: asyncio.Future):
        """Harvest hCaptcha ``sitekey`` + enterprise ``rqdata`` from requests.

        hCaptcha's ``getcaptcha`` call carries the sitekey and, for enterprise
        sites, the ``rqdata`` blob in its form-encoded body; ``checksiteconfig``
        carries the sitekey in its query string. We capture whichever fires
        first that yields a sitekey.
        """
        from urllib.parse import urlparse, parse_qs

        def on_request(request):
            if params_future.done():
                return
            url = request.url
            if "hcaptcha.com" not in url:
                return
            if "getcaptcha" not in url and "checksiteconfig" not in url:
                return

            sitekey = None
            rqdata = None
            # Query string (checksiteconfig / getcaptcha both put sitekey here).
            try:
                q = parse_qs(urlparse(url).query)
                if q.get("sitekey"):
                    sitekey = q["sitekey"][0]
            except Exception:
                pass
            # POST body (form-encoded) — getcaptcha carries sitekey + rqdata.
            try:
                body = request.post_data
            except Exception:
                body = None
            if body:
                try:
                    form = parse_qs(body)
                    sitekey = sitekey or (form.get("sitekey", [None])[0])
                    rqdata = form.get("rqdata", [None])[0]
                except Exception:
                    pass

            if sitekey and not params_future.done():
                params_future.set_result({"sitekey": sitekey, "rqdata": rqdata})

        page.on("request", on_request)

    async def _navigate_to_create(self, page):
        """Go to suno.com/create, wait for it to load, dismiss any popup."""
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

        # Wait for the page to fully load (song list API call).
        try:
            await page.wait_for_response(
                lambda resp: "/api/project/" in resp.url, timeout=30000
            )
            logger.info("Suno interface loaded")
        except Exception:
            logger.warning("Timed out waiting for project API — page may still be usable")

        # Close any popups.
        try:
            await page.get_by_label("Close").click(timeout=2000)
        except Exception:
            pass

    async def _trigger_generation(self, page):
        """Type a random prompt and click Create to provoke the challenge."""
        random_prompt = random.choice(_RANDOM_PROMPTS)
        if not await self._fill_prompt(page, random_prompt):
            logger.warning(
                "Could not locate the prompt textarea — please type a prompt "
                "and click Create manually."
            )
            return False
        if not await self._click_create(page):
            logger.warning(
                "Could not click the Create button — please click it manually."
            )
            return False
        return True

    @staticmethod
    async def _safe_close(context):
        try:
            await context.close()
        except Exception:
            pass

    async def _read_sitekey_from_dom(self, page) -> Optional[str]:
        """Last-resort sitekey lookup: a [data-sitekey] attr or iframe src."""
        from urllib.parse import urlparse, parse_qs
        try:
            sk = await page.locator("[data-sitekey]").first.get_attribute(
                "data-sitekey", timeout=3000
            )
            if sk:
                return sk
        except Exception:
            pass
        try:
            frames = page.locator('iframe[src*="hcaptcha.com"]')
            for i in range(await frames.count()):
                src = await frames.nth(i).get_attribute("src")
                if src and "sitekey=" in src:
                    q = parse_qs(urlparse(src).query)
                    if q.get("sitekey"):
                        return q["sitekey"][0]
        except Exception:
            pass
        return None

    # ─── Strategy 1: automatic solve via 2Captcha ────────────

    async def _auto_solve_impl(self, api_key: str) -> Optional[str]:
        """Harvest hCaptcha params in a browser, then solve via 2Captcha.

        Returns the token string, or raises so the caller falls back to manual.
        If the generate request fires a token on its own (no challenge), that
        token is returned directly and 2Captcha is never called.
        """
        async_playwright = self._import_playwright()
        from twocaptcha_solver import solve_hcaptcha

        logger.info("Attempting automatic CAPTCHA solve via 2Captcha...")
        loop = asyncio.get_running_loop()
        token_future: asyncio.Future = loop.create_future()
        params_future: asyncio.Future = loop.create_future()
        os.makedirs(_PROFILE_DIR, exist_ok=True)

        sitekey: Optional[str] = None
        rqdata: Optional[str] = None

        async with async_playwright() as p:
            context = await self._launch_context(p, headless=self._harvest_headless())
            await context.add_cookies(self._build_cookies())
            page = context.pages[0] if context.pages else await context.new_page()
            self._attach_token_sniffer(page, token_future)
            self._attach_param_sniffer(page, params_future)

            await self._navigate_to_create(page)
            await self._trigger_generation(page)

            # If no challenge appears, the generate request fires a token for
            # free — capture and return it without spending a 2Captcha solve.
            captcha_needed = await self._wait_for_captcha_or_token(page, token_future)
            if not token_future.done() and not captcha_needed:
                try:
                    token = await asyncio.wait_for(asyncio.shield(token_future), timeout=15)
                    await self._safe_close(context)
                    return token
                except asyncio.TimeoutError:
                    pass
            if token_future.done():
                token = token_future.result()
                await self._safe_close(context)
                return token

            # A real challenge is up — resolve the params we need for 2Captcha.
            if not params_future.done():
                try:
                    await asyncio.wait_for(asyncio.shield(params_future), timeout=10)
                except asyncio.TimeoutError:
                    pass
            if params_future.done():
                data = params_future.result() or {}
                sitekey = data.get("sitekey")
                rqdata = data.get("rqdata")
            sitekey = sitekey or self._get_configured_sitekey()
            if not sitekey:
                sitekey = await self._read_sitekey_from_dom(page)

            await self._safe_close(context)

        if not sitekey:
            raise RuntimeError("could not determine hCaptcha sitekey for 2Captcha")

        logger.info(
            f"Solving hCaptcha via 2Captcha (sitekey={sitekey[:8]}..., "
            f"enterprise={bool(rqdata)})"
        )
        return await solve_hcaptcha(
            api_key,
            sitekey,
            "https://suno.com/create",
            rqdata=rqdata,
            invisible=True,
            user_agent=self.suno_client.user_agent,
        )

    # ─── Strategy 2: manual browser solve (fallback) ─────────

    async def _browser_solve_impl(self) -> Optional[str]:
        """Open browser, navigate to suno.com/create, capture hCaptcha token."""
        async_playwright = self._import_playwright()

        logger.info("Launching browser for manual CAPTCHA solving...")

        token_future: asyncio.Future = asyncio.get_running_loop().create_future()
        os.makedirs(_PROFILE_DIR, exist_ok=True)

        async with async_playwright() as p:
            context = await self._launch_context(p, headless=False)
            await context.add_cookies(self._build_cookies())

            # A persistent context already opens with one blank page — reuse it
            # rather than leaving an extra empty tab behind.
            page = context.pages[0] if context.pages else await context.new_page()
            self._attach_token_sniffer(page, token_future)

            await self._navigate_to_create(page)
            await self._trigger_generation(page)

            # --- Manual only if a captcha is actually demanded ---
            # After clicking Create, watch for an hCaptcha challenge. If one
            # appears, hand control to the user; otherwise the generate request
            # fires on its own and we capture the token automatically.
            captcha_needed = await self._wait_for_captcha_or_token(page, token_future)

            if captcha_needed:
                logger.info(
                    "hCaptcha challenge detected — please solve it manually in the "
                    "browser window. The token will be captured automatically once "
                    "the challenge is completed."
                )
                timeout = 300  # 5 min for a human to solve
            else:
                logger.info(
                    "No CAPTCHA challenge appeared — continuing automatically."
                )
                timeout = 30  # the generate request should fire on its own

            # Wait for the token (manual solve, or automatic generate request)
            try:
                token = await asyncio.wait_for(token_future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(f"CAPTCHA token capture timed out after {timeout}s")
                token = None

            await self._safe_close(context)
            return token

    async def _fill_prompt(self, page, text: str) -> bool:
        """Type ``text`` into the Suno prompt textarea.

        The element's Emotion class names (``css-…``) are hashed and change on
        every build, so we anchor on the stable ``maxlength="3000"`` attribute
        of the prompt textarea, with a few generic fallbacks. Typing is done
        character-by-character to look human to the anti-bot layer.

        Returns True if the prompt was entered.
        """
        selectors = [
            'textarea[maxlength="3000"]',
            'textarea[placeholder]',
            "textarea",
            'div[contenteditable="true"]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=5000)
                await loc.click()
                try:
                    await loc.fill("")  # clear any existing draft
                except Exception:
                    pass
                await loc.press_sequentially(text, delay=30)
                logger.info(f"Typed prompt into '{sel}': {text!r}")
                return True
            except Exception:
                continue
        return False

    async def _click_create(self, page) -> bool:
        """Click the 'Create' button. Returns True on success.

        The label is localized depending on the account language ("Create" in
        English, "Créer" in French), so we try the known exact labels first and
        fall back to a fuzzy accessible-name match.
        """
        for label in ("Create", "Créer"):
            try:
                btn = page.get_by_role("button", name=label, exact=True)
                await btn.wait_for(state="visible", timeout=5000)
                await btn.click()
                logger.info(f"Clicked the '{label}' button")
                return True
            except Exception:
                continue
        # Fallback: any button whose accessible name contains create/créer.
        try:
            btn = page.get_by_role("button", name=re.compile(r"cr[ée]", re.I))
            await btn.first.click(timeout=3000)
            logger.info("Clicked the create button (fuzzy match)")
            return True
        except Exception:
            return False

    async def _wait_for_captcha_or_token(
        self, page, token_future: asyncio.Future, probe_timeout: float = 15
    ) -> bool:
        """Decide whether manual captcha solving is required.

        Polls for up to ``probe_timeout`` seconds after clicking Create:
        - if the generate request already fired its token → automatic (False);
        - if a *visible, real-sized* hCaptcha challenge iframe shows up → manual
          (True). A size check rules out the always-present 1×1 invisible
          hCaptcha iframe used in passive mode.

        If nothing is decisive within the window, we err on the side of waiting
        for the user (manual) unless a token has already been captured.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + probe_timeout
        # Both the checkbox and the challenge iframes carry "hCaptcha" in their
        # title / a hcaptcha.com src; the bounding-box check below picks out the
        # actual visible challenge popup.
        captcha = page.locator(
            'iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"]'
        )
        while loop.time() < deadline:
            if token_future.done():
                return False
            try:
                count = await captcha.count()
                for i in range(count):
                    frame = captcha.nth(i)
                    if not await frame.is_visible():
                        continue
                    box = await frame.bounding_box()
                    if box and box["width"] > 50 and box["height"] > 50:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

        return not token_future.done()

    def invalidate_token(self):
        """Mark the current token as invalid (e.g. after a 422 response)."""
        self._cached_token = None
        self._token_time = 0
        logger.info("CAPTCHA token invalidated")

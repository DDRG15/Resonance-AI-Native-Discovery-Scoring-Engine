"""
cookie_vault.py — Session Cookie Injection for Login-Gated Job Boards.

Loads per-domain cookies from the local `cookies/` directory and injects them
into a Playwright BrowserContext, enabling scraping of boards that require auth.

Supported boards (once cookies are exported from your Chrome session):
    wellfound.com, arc.dev, welcometothejungle.com

Cookie file format: JSON array in Chrome DevTools / EditThisCookie export format.
    File naming: cookies/<domain>.json  (e.g., cookies/wellfound.com.json)

The cookies/ directory is gitignored — cookie files never leave your machine.

Usage:
    vault = CookieVault()
    if vault.has_cookies("wellfound.com"):
        count = await vault.inject_into_context("wellfound.com", context)
        logger.info("Injected %d cookies for wellfound.com", count)

How to export cookies from Chrome:
    1. Log into the target site in Chrome
    2. DevTools → Application → Cookies → right-click → Copy all as JSON
       (or use the "EditThisCookie" extension → Export button)
    3. Save as cookies/<domain>.json
    4. GEMA will load them automatically on the next scrape
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Fields Playwright accepts when adding cookies to a context
_PLAYWRIGHT_COOKIE_FIELDS = {
    "name", "value", "domain", "path",
    "expires", "httpOnly", "secure", "sameSite",
}


class CookieVault:
    """
    Manages per-domain session cookies for login-gated job boards.
    Thread-safe for read operations; not designed for concurrent writes.
    """

    def __init__(self, cookies_dir: Optional[str] = None) -> None:
        if cookies_dir is None:
            # Default: cookies/ sibling to this file's package root
            this_file = Path(__file__).resolve()
            package_root = this_file.parent.parent  # gema/
            cookies_dir = str(package_root / "cookies")
        self._dir = Path(cookies_dir)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def has_cookies(self, domain: str) -> bool:
        """Return True if a non-empty cookies file exists for this domain."""
        path = self._cookie_path(domain)
        if not path.exists():
            return False
        try:
            cookies = self._read_file(path)
            return len(cookies) > 0
        except Exception:
            return False

    def load_cookies(self, domain: str) -> list[dict]:
        """
        Load cookies for domain and return them as Playwright-compatible dicts.
        Returns [] if no file exists or the file is malformed — never raises.
        """
        path = self._cookie_path(domain)
        if not path.exists():
            logger.debug("[CookieVault] No cookies file for %s", domain)
            return []
        try:
            raw = self._read_file(path)
            playwright_cookies = [self._normalize(c) for c in raw]
            logger.info(
                "[CookieVault] Loaded %d cookies for %s",
                len(playwright_cookies), domain,
            )
            return playwright_cookies
        except Exception as exc:
            logger.warning("[CookieVault] Failed to load cookies for %s: %s", domain, exc)
            return []

    async def inject_into_context(self, domain: str, context) -> int:
        """
        Inject domain cookies into a Playwright BrowserContext.
        Returns the number of cookies injected (0 if none available).
        Never raises — a failed injection is logged and scraping proceeds without auth.
        """
        cookies = self.load_cookies(domain)
        if not cookies:
            return 0
        try:
            await context.add_cookies(cookies)
            logger.info(
                "[CookieVault] Injected %d cookies into context for %s",
                len(cookies), domain,
            )
            return len(cookies)
        except Exception as exc:
            logger.warning(
                "[CookieVault] Cookie injection failed for %s: %s — scraping without auth",
                domain, exc,
            )
            return 0

    def list_domains(self) -> list[str]:
        """Return list of domains that have cookie files in the vault."""
        if not self._dir.exists():
            return []
        return [
            p.stem  # filename without .json → domain name
            for p in sorted(self._dir.glob("*.json"))
            if p.is_file()
        ]

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _cookie_path(self, domain: str) -> Path:
        # Strip leading dot so ".wellfound.com" → "wellfound.com.json"
        clean = domain.lstrip(".")
        return self._dir / f"{clean}.json"

    @staticmethod
    def _read_file(path: Path) -> list[dict]:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array, got {type(data).__name__}")
        return data

    @staticmethod
    def _normalize(raw: dict) -> dict:
        """
        Convert a Chrome DevTools / EditThisCookie cookie dict to the subset
        of fields that Playwright's add_cookies() accepts.
        """
        cookie: dict = {
            "name":   raw.get("name", ""),
            "value":  raw.get("value", ""),
            "domain": raw.get("domain", ""),
            "path":   raw.get("path", "/"),
        }
        # Optional numeric expiry — Chrome exports as float (Unix timestamp)
        if "expirationDate" in raw:
            cookie["expires"] = float(raw["expirationDate"])
        elif "expires" in raw and raw["expires"] not in (-1, None):
            cookie["expires"] = float(raw["expires"])

        if "httpOnly" in raw:
            cookie["httpOnly"] = bool(raw["httpOnly"])
        if "secure" in raw:
            cookie["secure"] = bool(raw["secure"])

        # sameSite: Playwright expects "Strict" | "Lax" | "None"
        same_site = raw.get("sameSite", "")
        if same_site in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site

        return cookie

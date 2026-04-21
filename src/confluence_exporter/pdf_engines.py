"""Pluggable HTML→PDF engines (Strategy + Registry pattern).

Three engines are shipped out of the box:

* :class:`PlaywrightEngine` — headless Chromium; best fidelity (tables, CSS,
  images). Requires ``pip install playwright`` + ``playwright install chromium``.
* :class:`WeasyPrintEngine` — excellent CSS/table support, no browser needed.
  On Windows requires GTK runtime.
* :class:`XHTML2PDFEngine` — pure-Python fallback, always available.

Use :func:`render_html_to_pdf` to render using the user-chosen engine with
automatic fallback. All engines render to a short temp file first and move to
the final destination, so Windows long-paths never block output.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import tempfile
from abc import ABC, abstractmethod

from confluence_exporter.logging_utils import get_logger
from confluence_exporter.paths import is_valid_pdf, long_path, move_into_place

logger = get_logger()


class PDFEngine(ABC):
    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool: ...
    @abstractmethod
    def render(self, html: str, output: str) -> bool: ...

    def explain_unavailable(self) -> str:
        """Optional human-readable hint for why this engine isn't usable."""
        return ""


# ---------------------------------------------------------------------------
# xhtml2pdf (pure-Python, always present in our requirements)
# ---------------------------------------------------------------------------


class XHTML2PDFEngine(PDFEngine):
    name = "xhtml2pdf"

    def is_available(self) -> bool:
        try:
            import xhtml2pdf  # noqa: F401

            return True
        except ImportError:
            return False

    def explain_unavailable(self) -> str:
        return "pip install xhtml2pdf"

    def render(self, html: str, output: str) -> bool:
        from xhtml2pdf import pisa

        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="cfx_x2p_")
        os.close(fd)
        try:
            with open(tmp, "wb") as fh:
                result = pisa.CreatePDF(src=html, dest=fh, encoding="utf-8")
            if result.err:
                return False
            if not is_valid_pdf(tmp):
                return False
            return move_into_place(tmp, output) and is_valid_pdf(long_path(output))
        except Exception as e:
            logger.debug("xhtml2pdf failed: %s", e)
            return False
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# WeasyPrint
# ---------------------------------------------------------------------------


class WeasyPrintEngine(PDFEngine):
    name = "weasyprint"

    def is_available(self) -> bool:
        try:
            from weasyprint import HTML  # noqa: F401

            return True
        except (ImportError, OSError):
            return False

    def explain_unavailable(self) -> str:
        return "pip install weasyprint  (on Windows also install the GTK runtime)"

    def render(self, html: str, output: str) -> bool:
        from weasyprint import HTML

        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="cfx_wp_")
        os.close(fd)
        try:
            HTML(string=html).write_pdf(tmp)
            if not is_valid_pdf(tmp):
                return False
            return move_into_place(tmp, output) and is_valid_pdf(long_path(output))
        except Exception as e:
            logger.debug("WeasyPrint failed: %s", e)
            return False
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Playwright (headless Chromium) — uses a singleton browser for throughput
# ---------------------------------------------------------------------------


class PlaywrightEngine(PDFEngine):
    name = "playwright"

    _pw = None  # playwright driver
    _browser = None  # browser instance
    _warned_missing_binary = False
    _warned_missing_package = False

    def is_available(self) -> bool:
        try:
            import playwright  # noqa: F401

            return True
        except ImportError:
            return False

    def explain_unavailable(self) -> str:
        return (
            "pip install playwright   &&   playwright install chromium"
        )

    def _ensure_browser(self) -> bool:
        if self._browser is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if not PlaywrightEngine._warned_missing_package:
                logger.warning(
                    "Playwright Python package not installed. Run: pip install playwright"
                )
                PlaywrightEngine._warned_missing_package = True
            return False
        try:
            PlaywrightEngine._pw = sync_playwright().start()
            PlaywrightEngine._browser = PlaywrightEngine._pw.chromium.launch()
            return True
        except Exception as launch_err:
            # teardown any partially-started driver
            try:
                if PlaywrightEngine._pw:
                    PlaywrightEngine._pw.stop()
            except Exception:
                pass
            PlaywrightEngine._pw = None
            PlaywrightEngine._browser = None

            msg = str(launch_err)
            if not PlaywrightEngine._warned_missing_binary:
                if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                    logger.error(
                        "Playwright Chromium binary not installed. "
                        "Run:  playwright install chromium  "
                        "(or: python -m playwright install chromium)"
                    )
                else:
                    logger.error("Playwright failed to launch Chromium: %s", msg)
                PlaywrightEngine._warned_missing_binary = True
            return False

    def render(self, html: str, output: str) -> bool:
        if not self._ensure_browser():
            return False

        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="cfx_pw_")
        os.close(fd)
        try:
            page = PlaywrightEngine._browser.new_page()
            render_err: Exception | None = None
            try:
                page.set_content(html, wait_until="load")
                page.pdf(
                    path=tmp,
                    format="A4",
                    margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
                    print_background=True,
                )
            except Exception as e:
                render_err = e
            finally:
                with contextlib.suppress(Exception):
                    page.close()

            if render_err is not None:
                msg = str(render_err)
                logger.warning("Playwright page render error: %s", msg)
                # Reset browser only if the error indicates a dead process
                if any(k in msg for k in (
                    "Target page, context or browser has been closed",
                    "Browser closed", "has been closed",
                    "Connection closed", "has crashed",
                )):
                    self.shutdown()
                return False

            if not is_valid_pdf(tmp):
                return False
            return move_into_place(tmp, output) and is_valid_pdf(long_path(output))
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    @classmethod
    def shutdown(cls) -> None:
        try:
            if cls._browser is not None:
                cls._browser.close()
        except Exception:
            pass
        try:
            if cls._pw is not None:
                cls._pw.stop()
        except Exception:
            pass
        cls._browser = None
        cls._pw = None


# Cleanup Playwright on interpreter exit
atexit.register(PlaywrightEngine.shutdown)


# ---------------------------------------------------------------------------
# Registry + preference resolution
# ---------------------------------------------------------------------------


_ENGINES: dict[str, PDFEngine] = {
    PlaywrightEngine.name: PlaywrightEngine(),
    WeasyPrintEngine.name: WeasyPrintEngine(),
    XHTML2PDFEngine.name: XHTML2PDFEngine(),
}

# The order used for "auto" detection — best first.
_AUTO_ORDER: tuple[str, ...] = ("playwright", "weasyprint", "xhtml2pdf")


def engine_names() -> tuple[str, ...]:
    return tuple(_ENGINES.keys())


def detect_engine(preference: str = "auto") -> str:
    """Resolve ``preference`` to an actual engine name that is available.

    Returns the engine name, or ``"none"`` if nothing works.
    """
    if preference != "auto":
        eng = _ENGINES.get(preference)
        if eng and eng.is_available():
            return preference
        # Fall through to auto if the explicit choice isn't installed
    for name in _AUTO_ORDER:
        if _ENGINES[name].is_available():
            return name
    return "none"


def render_html_to_pdf(
    html_content: str, output_path: str, preference: str = "auto"
) -> tuple[bool, str]:
    """Try engines (in preference order) until one produces a valid PDF.

    Returns ``(success, engine_used_or_error)``.
    """
    if preference == "auto":
        candidates = list(_AUTO_ORDER)
    else:
        # Try the preferred engine first, then fall back through auto order
        candidates = [preference] + [
            n for n in _AUTO_ORDER if n != preference
        ]

    last_error = "no engine available"
    for name in candidates:
        engine = _ENGINES.get(name)
        if engine is None:
            continue
        if not engine.is_available():
            last_error = f"{name} not installed ({engine.explain_unavailable()})"
            continue
        if engine.render(html_content, output_path):
            return True, name
        last_error = f"{name} failed to produce a valid PDF"

    # Clean up any leftover 0-byte file
    if os.path.exists(output_path) and not is_valid_pdf(output_path):
        with contextlib.suppress(OSError):
            os.remove(output_path)
    return False, last_error


def shutdown_engines() -> None:
    """Tear down long-lived resources (e.g. Chromium). Safe to call multiple times."""
    PlaywrightEngine.shutdown()

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import undetected_chromedriver as uc  # type: ignore[import-untyped]
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)


class BrowserFetchError(RuntimeError):
    """Raised when a browser-backed page fetch fails."""


def start_undetected_chrome(profile_dir: str) -> uc.Chrome:
    """Start undetected Chrome with the project's standard scraper settings."""
    resolved_profile_dir = Path(profile_dir).expanduser().resolve()
    resolved_profile_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Starting undetected Chrome with profile: %s", resolved_profile_dir)

    options = uc.ChromeOptions()
    options.user_data_dir = str(resolved_profile_dir)
    options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

    chrome_version = detect_chrome_major_version()
    if chrome_version is not None:
        logger.info("Detected Chrome major version: %s", chrome_version)

    try:
        driver = uc.Chrome(
            options=options,
            use_subprocess=True,
            version_main=chrome_version,
        )
    except WebDriverException as error:
        logger.exception("Undetected Chrome could not start")
        raise BrowserFetchError(format_chrome_start_error(error, resolved_profile_dir)) from error
    return driver


def fetch_with_existing_browser(
    driver: uc.Chrome,
    url: str,
    *,
    wait_until: Callable[[uc.Chrome], bool],
    timeout_seconds: float = 90.0,
    page_load_timeout_seconds: float | None = None,
    return_partial_on_timeout: bool = False,
) -> str:
    """Open a page in an existing browser and return its final HTML."""
    try:
        logger.info("Opening page in undetected Chrome: %s", url)
        driver.set_page_load_timeout(page_load_timeout_seconds or timeout_seconds)
        try:
            driver.get(url)
        except TimeoutException:
            logger.warning("Browser page load timed out, checking partial page: %s", url)
        accept_cookies_if_visible(driver)
        WebDriverWait(driver, timeout_seconds).until(lambda browser: wait_until(browser))
        logger.info("Browser page is ready: %s", url)
        return str(driver.page_source)
    except TimeoutException as error:
        logger.exception("Timed out waiting for browser page: %s", url)
        if return_partial_on_timeout:
            logger.info("Returning partial browser page after timeout: %s", url)
            return str(driver.page_source)
        raise BrowserFetchError(f"Timed out waiting for page payload: {url}") from error
    except WebDriverException as error:
        logger.exception("Browser could not load page: %s", url)
        raise BrowserFetchError(f"Browser could not load page: {error}") from error


def accept_cookies_if_visible(driver: uc.Chrome) -> None:
    labels = (
        "Akceptuję wszystkie",
        "Akceptuj wszystkie",
        "Zaakceptuj wszystkie",
        "Accept all",
        "Accept All",
        "Zgadzam się",
    )
    for label in labels:
        xpath = f"//button[contains(normalize-space(.), '{label}')]"
        try:
            buttons = driver.find_elements(By.XPATH, xpath)
        except WebDriverException:
            logger.exception("Could not inspect cookie buttons")
            return

        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    logger.info("Accepting cookie dialog with button: %s", label)
                    try:
                        button.click()
                    except WebDriverException:
                        driver.execute_script("arguments[0].click();", button)
                    return
            except WebDriverException:
                logger.exception("Could not click cookie button")


def format_chrome_start_error(error: WebDriverException, profile_dir: Path) -> str:
    message = str(error)
    version_mismatch = re.search(
        r"supports Chrome version (\d+).*?Current browser version is (\d+)",
        message,
        flags=re.DOTALL,
    )
    if version_mismatch:
        supported, current = version_mismatch.groups()
        return (
            "ChromeDriver version mismatch. "
            f"Driver supports Chrome {supported}, but installed Chrome is {current}. "
            "The app tries to auto-detect installed Chrome, but the cached driver may need cleanup."
        )

    if (
        "DevToolsActivePort" in message
        or "Chrome failed to start" in message
        or "chrome not reachable" in message
    ):
        return (
            "Chrome could not start. Close Chrome windows using this scraper profile or remove "
            f"the profile directory and try again: {profile_dir}"
        )

    return f"Chrome could not start: {error}"


def detect_chrome_major_version() -> int | None:
    candidates = (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    )
    for candidate in candidates:
        if not candidate.exists():
            continue

        directory_version = detect_chrome_major_version_from_directory(candidate.parent)
        if directory_version is not None:
            return directory_version

        try:
            result = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except OSError, subprocess.SubprocessError:
            logger.exception("Could not read Chrome version from %s", candidate)
            continue

        match = re.search(r"(\d+)\.\d+\.\d+\.\d+", result.stdout or result.stderr)
        if match:
            return int(match.group(1))

    return None


def detect_chrome_major_version_from_directory(application_dir: Path) -> int | None:
    versions: list[int] = []
    for child in application_dir.iterdir():
        if not child.is_dir():
            continue
        match = re.fullmatch(r"(\d+)\.\d+\.\d+\.\d+", child.name)
        if match:
            versions.append(int(match.group(1)))

    if not versions:
        return None
    return max(versions)

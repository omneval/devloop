"""Wait for a specific PyPI package version to become fully available.

Checks three gates before declaring success:
  1. JSON metadata exists at pypi.org/pypi/{name}/{version}/json
  2. Version appears in the simple index (what uv queries for resolution)
  3. Every published file HEADs 200 on files.pythonhosted.org

Usage:
    python scripts/wait_for_pypi.py omneval-devloop==0.0.2
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import httpx


@dataclass
class MetadataResult:
    """Result of checking JSON metadata for a package version."""

    found: bool
    file_urls: list[str]


@dataclass
class CheckResult:
    """Combined result of all three availability gates."""

    metadata_ok: bool
    simple_index_ok: bool
    files_ok: bool
    file_urls: list[str]

    @property
    def passed(self) -> bool:
        return self.metadata_ok and self.simple_index_ok and self.files_ok


class PyPIChecker:
    """Checks whether a specific PyPI package version is available.

    Uses an injectable httpx client so tests can substitute a mock transport.
    """

    def __init__(self, client: httpx.Client):
        self._client = client

    def check_json_metadata(self, name: str, version: str) -> MetadataResult:
        """Check if the version exists in PyPI JSON metadata.

        Returns:
            MetadataResult with ``found=True`` and file URLs if the version exists,
            or ``found=False`` with an empty URL list otherwise.
        """
        url = f"https://pypi.org/pypi/{name}/{version}/json"
        try:
            resp = self._client.get(url)
            if resp.status_code != 200:
                return MetadataResult(found=False, file_urls=[])
        except httpx.RequestError:
            return MetadataResult(found=False, file_urls=[])

        data = resp.json()
        release_urls = [entry["url"] for entry in data.get("urls", [])]
        return MetadataResult(found=bool(release_urls), file_urls=release_urls)

    def check_simple_index(self, name: str, version: str) -> bool:
        """Check if the version appears in the simple index (what uv queries).

        Returns:
            True if the version tag is found in the simple index HTML.
        """
        url = f"https://pypi.org/simple/{name}/"
        try:
            resp = self._client.get(url)
            if resp.status_code != 200:
                return False
        except httpx.RequestError:
            return False

        return version in resp.text

    def check_file_downloadability(self, file_urls: list[str]) -> bool:
        """Verify every release file HEADs 200 on files.pythonhosted.org.

        Returns:
            True if all files are downloadable (or list is empty).
        """
        if not file_urls:
            return True

        for url in file_urls:
            try:
                resp = self._client.head(url)
                if resp.status_code != 200:
                    return False
            except httpx.RequestError:
                return False

        return True

    def check_all(self, name: str, version: str) -> CheckResult:
        """Run all three gates and return the combined result.

        Gate 1 (JSON metadata) must pass before gates 2-3, since gate 3 needs
        the file URLs from gate 1's response.
        """
        metadata = self.check_json_metadata(name, version)
        if not metadata.found:
            return CheckResult(
                metadata_ok=False,
                simple_index_ok=False,
                files_ok=False,
                file_urls=[],
            )

        simple_ok = self.check_simple_index(name, version)
        files_ok = self.check_file_downloadability(metadata.file_urls)

        return CheckResult(
            metadata_ok=True,
            simple_index_ok=simple_ok,
            files_ok=files_ok,
            file_urls=metadata.file_urls,
        )


def wait_for_package(
    client: httpx.Client,
    name: str,
    version: str,
    max_attempts: int = 40,
    sleep_seconds: float = 60.0,
) -> bool:
    """Poll PyPI until all three gates pass for the given package version.

    Args:
        client: httpx client (injectable for testing).
        name: Package name (e.g. ``"omneval-devloop"``).
        version: Version string (e.g. ``"0.0.2"``).
        max_attempts: Maximum number of check cycles before giving up.
        sleep_seconds: Seconds to wait between attempts.

    Returns:
        True if all gates passed within the retry budget, False on timeout.
    """
    checker = PyPIChecker(client)

    for attempt in range(1, max_attempts + 1):
        result = checker.check_all(name, version)

        if result.passed:
            print(f"All gates passed for {name}=={version} (attempt {attempt})")
            return True

        # Log which gate(s) failed
        if not result.metadata_ok:
            print(
                f"Attempt {attempt}/{max_attempts}: {name}=={version} "
                f"metadata not on PyPI yet"
            )
        if result.metadata_ok and not result.simple_index_ok:
            print(
                f"Attempt {attempt}/{max_attempts}: {version} "
                f"not in simple index yet"
            )
        if result.metadata_ok and result.simple_index_ok and not result.files_ok:
            print(
                f"Attempt {attempt}/{max_attempts}: "
                f"release files not downloadable yet"
            )

        if attempt < max_attempts:
            time.sleep(sleep_seconds)

    return False


def parse_package_spec(spec: str) -> tuple[str, str]:
    """Split ``name==version`` into ``(name, version)``.

    Raises:
        ValueError: If the spec doesn't contain exactly one ``==``.
    """
    parts = spec.split("==")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid package spec '{spec}'. Expected format: name==version"
        )
    return parts[0], parts[1]


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} package==version", file=sys.stderr)
        return 1

    try:
        name, version = parse_package_spec(sys.argv[1])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"Waiting for {name}=={version} to appear in PyPI...")
    client = httpx.Client()
    try:
        success = wait_for_package(client, name, version)
    finally:
        client.close()

    if success:
        print(f"{name}=={version} is now available on PyPI")
        return 0
    else:
        print(
            f"ERROR: {name}=={version} did not become fully available "
            f"within ~10 minutes",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

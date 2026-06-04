"""Tests for scripts/wait_for_pypi.py — PyPI availability checker.

Covers the package parser, the three availability gates (JSON metadata,
simple index, file downloadability), and the retry loop with CLI exit codes.
"""

import pytest

import httpx
from scripts.wait_for_pypi import PyPIChecker, parse_package_spec, wait_for_package


class TestParsePackageSpec:
    """parse_package_spec splits ``name==version`` into its components."""

    @pytest.mark.parametrize(
        "spec, expected",
        [
            ("omneval-devloop==0.0.2", ("omneval-devloop", "0.0.2")),
            ("my-package==1.2.3", ("my-package", "1.2.3")),
            ("foo-bar-baz==10.20.30rc1", ("foo-bar-baz", "10.20.30rc1")),
        ],
    )
    def test_valid_spec(self, spec, expected):
        assert parse_package_spec(spec) == expected

    def test_missing_version_raises(self):
        with pytest.raises(ValueError, match="=="):
            parse_package_spec("omneval-devloop")

    def test_multiple_equals_raises(self):
        with pytest.raises(ValueError, match="=="):
            parse_package_spec("pkg==1.0==2.0")


class TestPyPICheckerJSONMetadata:
    """Gate 1: PyPIChecker checks JSON metadata for the version."""

    def test_metadata_found(self):
        """Return file URLs when the version exists in JSON metadata."""
        handler = _FakePyPIHandler(version_found=True)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_json_metadata("omneval-devloop", "0.0.2")

        assert result.found is True
        assert len(result.file_urls) == 2
        assert result.file_urls[0].endswith("omneval_devloop-0.0.2-py3-none-any.whl")

    def test_metadata_not_found(self):
        """Return empty when the version isn't in JSON metadata."""
        handler = _FakePyPIHandler(version_found=False)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_json_metadata("omneval-devloop", "99.99.99")

        assert result.found is False
        assert result.file_urls == []

    def test_metadata_404(self):
        """Return empty when PyPI returns 404 for the version."""
        handler = _FakePyPIHandler(status_code=404)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_json_metadata("omneval-devloop", "99.99.99")

        assert result.found is False


class TestPyPICheckerSimpleIndex:
    """Gate 2: PyPIChecker checks simple index for the version."""

    def test_version_in_simple_index(self):
        """Return True when the version tag is in the simple index HTML."""
        handler = _FakePyPIHandler(simple_index_version="0.0.2")
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_simple_index("omneval-devloop", "0.0.2")

        assert result is True

    def test_version_not_in_simple_index(self):
        """Return False when the version tag is missing from simple index."""
        handler = _FakePyPIHandler(simple_index_version="0.0.1")
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_simple_index("omneval-devloop", "0.0.2")

        assert result is False

    def test_simple_index_error(self):
        """Return False on HTTP error when querying simple index."""
        handler = _FakePyPIHandler(simple_index_status=500)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_simple_index("omneval-devloop", "0.0.2")

        assert result is False


class TestPyPICheckerFileDownloadability:
    """Gate 3: PyPIChecker verifies all release files are downloadable."""

    def test_all_files_downloadable(self):
        """Return True when all files HEAD 200."""
        handler = _FakePyPIHandler(file_downloadable=True)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        urls = [
            "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2-py3-none-any.whl",
            "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2.tar.gz",
        ]
        result = checker.check_file_downloadability(urls)

        assert result is True

    def test_one_file_not_downloadable(self):
        """Return False when at least one file fails to HEAD 200."""
        handler = _FakePyPIHandler(file_downloadable=False)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        urls = [
            "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2-py3-none-any.whl",
            "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2.tar.gz",
        ]
        result = checker.check_file_downloadability(urls)

        assert result is False

    def test_empty_url_list(self):
        """Return True for an empty list (nothing to verify)."""
        handler = _FakePyPIHandler()
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_file_downloadability([])

        assert result is True


class TestPyPICheckerAllGates:
    """PyPIChecker.check_all runs all 3 gates and reports combined result."""

    def test_all_gates_pass(self):
        """Return passed=True when all 3 gates succeed."""
        handler = _FakePyPIHandler(
            version_found=True,
            simple_index_version="0.0.2",
            file_downloadable=True,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_all("omneval-devloop", "0.0.2")

        assert result.passed is True
        assert result.metadata_ok is True
        assert result.simple_index_ok is True
        assert result.files_ok is True

    def test_gate_1_fails(self):
        """Return passed=False when JSON metadata is missing."""
        handler = _FakePyPIHandler(version_found=False)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_all("omneval-devloop", "99.99.99")

        assert result.passed is False
        assert result.metadata_ok is False

    def test_gate_2_fails(self):
        """Return passed=False when simple index is missing the version."""
        handler = _FakePyPIHandler(
            version_found=True,
            simple_index_version="0.0.1",  # different version
            file_downloadable=True,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_all("omneval-devloop", "0.0.2")

        assert result.passed is False
        assert result.simple_index_ok is False

    def test_gate_3_fails(self):
        """Return passed=False when files aren't downloadable."""
        handler = _FakePyPIHandler(
            version_found=True,
            simple_index_version="0.0.2",
            file_downloadable=False,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        checker = PyPIChecker(client)

        result = checker.check_all("omneval-devloop", "0.0.2")

        assert result.passed is False
        assert result.files_ok is False


class TestWaitForPackage:
    """wait_for_package retries check_all until success or timeout."""

    def test_returns_true_on_first_try(self):
        """Return True immediately when all gates pass."""
        handler = _FakePyPIHandler(
            version_found=True,
            simple_index_version="0.0.2",
            file_downloadable=True,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))

        result = wait_for_package(
            client, "omneval-devloop", "0.0.2", max_attempts=10, sleep_seconds=0
        )

        assert result is True

    def test_returns_false_on_timeout(self):
        """Return False when gates never pass within max_attempts."""
        handler = _FakePyPIHandler(version_found=False)
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))

        result = wait_for_package(
            client, "omneval-devloop", "99.99.99", max_attempts=3, sleep_seconds=0
        )

        assert result is False

    def test_retries_until_success(self):
        """Return True when gates pass on a subsequent attempt."""
        handler = _FakePyPIHandler(
            version_found=False,
            simple_index_version=None,
            file_downloadable=False,
            flip_at=2,  # succeed on attempt 2
        )
        client = httpx.Client(transport=httpx.MockTransport(handler.handle))

        result = wait_for_package(
            client, "omneval-devloop", "0.0.2", max_attempts=10, sleep_seconds=0
        )

        assert result is True


class TestMainCLI:
    """main() returns correct exit codes from the CLI entry point."""

    def test_success_exit_code(self, monkeypatch, capsys):
        """Return 0 when all gates pass."""
        handler = _FakePyPIHandler(
            version_found=True,
            simple_index_version="0.0.2",
            file_downloadable=True,
        )

        def fake_wait_for_package(
            client, name, version, max_attempts=40, sleep_seconds=15.0
        ):
            return True

        monkeypatch.setattr(
            "scripts.wait_for_pypi.wait_for_package", fake_wait_for_package
        )

        # Inject the mock client into main by patching httpx.Client
        fake_client = httpx.Client(transport=httpx.MockTransport(handler.handle))
        monkeypatch.setattr(
            "scripts.wait_for_pypi.httpx.Client",
            lambda *a, **kw: fake_client,
        )
        monkeypatch.setattr(
            "sys.argv", ["wait_for_pypi.py", "omneval-devloop==0.0.2"]
        )

        from scripts.wait_for_pypi import main

        exit_code = main()

        assert exit_code == 0

    def test_timeout_exit_code(self, monkeypatch, capsys):
        """Return 1 when all retries are exhausted."""
        def fake_wait_for_package(
            client, name, version, max_attempts=40, sleep_seconds=15.0
        ):
            return False

        monkeypatch.setattr(
            "scripts.wait_for_pypi.wait_for_package", fake_wait_for_package
        )
        monkeypatch.setattr(
            "sys.argv", ["wait_for_pypi.py", "omneval-devloop==0.0.2"]
        )

        from scripts.wait_for_pypi import main

        exit_code = main()

        assert exit_code == 1

    def test_usage_on_missing_arg(self, monkeypatch, capsys):
        """Return 1 and print usage when no argument is provided."""
        monkeypatch.setattr("sys.argv", ["wait_for_pypi.py"])

        from scripts.wait_for_pypi import main

        exit_code = main()
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "Usage:" in captured.err


class _FakePyPIHandler:
    """Mock transport handler for PyPI API responses.

    Supports a ``flip_at`` parameter to simulate eventual consistency: the handler
    returns failing responses until the request count reaches ``flip_at``, at which
    point it switches to returning successful responses.
    """

    def __init__(
        self,
        version_found: bool = True,
        status_code: int = 200,
        simple_index_version: str | None = None,
        simple_index_status: int = 200,
        file_downloadable: bool = True,
        flip_at: int | None = None,
    ):
        self.version_found = version_found
        self.status_code = status_code
        self.simple_index_version = simple_index_version
        self.simple_index_status = simple_index_status
        self.file_downloadable = file_downloadable
        self.flip_at = flip_at
        self._request_count = 0

        # Store the failing state so we can flip to success later
        self._fail_version_found = not version_found
        self._fail_simple_index = simple_index_version is None
        self._fail_file_downloadable = not file_downloadable

    def handle(self, request: httpx.Request) -> httpx.Response:
        self._request_count += 1

        # Flip from failing to successful state at the specified request count
        if self.flip_at and self._request_count >= self.flip_at:
            self.version_found = True
            self.simple_index_version = "0.0.2"
            self.file_downloadable = True

        url = str(request.url)

        if "/json" in url:
            if not self.version_found or self.status_code == 404:
                return httpx.Response(404, text="Not Found")

            body = {
                "releases": {
                    "0.0.2": [
                        {
                            "url": "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2-py3-none-any.whl",
                            "filename": "omneval_devloop-0.0.2-py3-none-any.whl",
                        },
                        {
                            "url": "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2.tar.gz",
                            "filename": "omneval_devloop-0.0.2.tar.gz",
                        },
                    ]
                },
                "urls": [
                    {
                        "url": "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2-py3-none-any.whl",
                    },
                    {
                        "url": "https://files.pythonhosted.org/pkg/omneval_devloop-0.0.2.tar.gz",
                    },
                ],
            }
            return httpx.Response(200, json=body)

        if "/simple/" in url:
            if self.simple_index_status != 200:
                return httpx.Response(self.simple_index_status)

            if self.simple_index_version:
                html = f'<a href="#">{self.simple_index_version}</a>'
            else:
                html = "<html><body></body></html>"
            return httpx.Response(200, text=html)

        if "files.pythonhosted.org" in url:
            if request.method == "HEAD":
                code = 200 if self.file_downloadable else 404
                return httpx.Response(code)

        # Default: return 404 for any unhandled route
        return httpx.Response(404)

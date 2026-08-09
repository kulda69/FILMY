import io
from pathlib import Path
from unittest.mock import patch

from filmy.scripts import run_imdb_refresh


class _Response:
    def __init__(self):
        self._sent = False
        self.headers = {"Content-Length": "9"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        if self._sent:
            return b""
        self._sent = True
        return b"imdb-data"


def test_download_file_uses_certifi_ssl_context(tmp_path: Path) -> None:
    destination = tmp_path / "title.basics.tsv.gz"
    progress_events = []

    with patch.object(run_imdb_refresh, "urlopen", return_value=_Response()) as mocked_urlopen:
        run_imdb_refresh._download_file(
            "https://datasets.imdbws.com/title.basics.tsv.gz",
            destination,
            progress=lambda **payload: progress_events.append(payload),
        )

    mocked_urlopen.assert_called_once()
    args, kwargs = mocked_urlopen.call_args
    assert args[0].endswith("title.basics.tsv.gz")
    assert kwargs["timeout"] == 120
    assert kwargs["context"].get_ca_certs()[0]["issuer"]
    assert progress_events == [
        {"current_file_bytes": 9, "current_file_total_bytes": 9, "current_file_percent": 100.0}
    ]
    assert destination.read_bytes() == b"imdb-data"


def test_copy_stream_reports_byte_progress() -> None:
    progress_events = []
    destination = io.BytesIO()

    run_imdb_refresh._copy_stream(
        io.BytesIO(b"12345"),
        destination,
        total_bytes=5,
        progress=lambda **payload: progress_events.append(payload),
    )

    assert progress_events == [
        {"current_file_bytes": 5, "current_file_total_bytes": 5, "current_file_percent": 100.0}
    ]
    assert destination.getvalue() == b"12345"

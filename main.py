import uvicorn

from filmy.main import app


def start() -> None:
    """Start the local FastAPI app through uvicorn without an extra uv wrapper."""
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8019,
    )


if __name__ == "__main__":
    start()

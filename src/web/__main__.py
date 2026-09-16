"""python -m src.web — local dev entrypoint for the web app (T-1101)."""

from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.web.app:app", host="127.0.0.1", port=8000, reload=True)

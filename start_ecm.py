#!/usr/bin/env python3
import os

import uvicorn


def main() -> None:
    host = os.environ.get("ECM_HOST", "127.0.0.1")
    port = int(os.environ.get("ECM_PORT", "8010"))
    uvicorn.run("ecm_api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

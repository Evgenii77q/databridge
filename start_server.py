#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path

import uvicorn


def load_app():
    project_root = Path(__file__).resolve().parent
    server_path = project_root / "забыл что" / "server.py"
    spec = importlib.util.spec_from_file_location("documino_server", server_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.app



def main():
    port = int(os.environ.get("PORT", 3737))
    uvicorn.run(load_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

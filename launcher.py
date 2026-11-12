"""PyInstaller entry point; real automatic joining is never enabled by arguments."""
from binance_monitor.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())

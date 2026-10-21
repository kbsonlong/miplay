#!/usr/bin/env python3
"""Modular entry point: calls src.cli.main; same runtime logic as V14."""
from src.cli import main

if __name__ == '__main__':
    raise SystemExit(main())

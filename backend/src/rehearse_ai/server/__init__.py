"""The HTTP application: composition in ``app``, one router per capability in ``routes``."""

from rehearse_ai.server.app import create_app, main

__all__ = ["create_app", "main"]

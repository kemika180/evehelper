"""Ingestion layer: normalize ESI/SDE payloads into polars frames.

Impure. Consumes the ``esi`` client; produces plain-data snapshots for the core.
"""

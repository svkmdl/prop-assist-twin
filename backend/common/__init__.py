"""Shared logic used by both the chat API (server.py) and the ingestion worker.

These modules are intentionally free of FastAPI and global AWS client state so
they can be imported by the lightweight ingestion Lambda without pulling in the
whole web application.
"""

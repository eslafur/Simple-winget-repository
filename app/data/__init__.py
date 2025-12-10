"""
Data access and in-memory indexing for the JSON-based winget repository.

This package is responsible for:
* Determining the data directory (via env var + sensible default).
* Loading and persisting repository-level configuration.
* Crawling the on-disk tree of packages/versions into an in-memory index.
* Creating an example package that documents the expected structure.
"""



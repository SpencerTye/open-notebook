"""LOCAL: TurboVec vector index for Open Notebook.

Replaces the database's full cosine scan (``fn::vector_search``, one comparison
per stored chunk inside SurrealDB's query interpreter, 15 s per search on a
7,448-chunk library) with an in-process quantized index that returns the
nearest candidates in milliseconds. The database stays the master copy; the
final ranking always uses the exact vectors read back from it.

Modules:

- ``ids``: record id <-> 64-bit index id.
- ``store``: the three indexes (chunks, insights, notes), their files, and the
  check against the database. Owned by the API process only.
- ``search``: the engine switch and the search path (candidates, exact
  re-scoring, grouping into the rows the app already expects).
- ``client``: how the worker tells the API that vectors changed.

Switch: ``OPEN_NOTEBOOK_VECTOR_ENGINE=turbovec`` uses the index,
``scan`` (or unset) keeps the database scan. The index is maintained either
way, so flipping needs no rebuild.
"""

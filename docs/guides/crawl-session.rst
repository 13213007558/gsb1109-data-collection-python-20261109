Crawl Sessions
==============

For very large sites, :func:`~usp.tree.sitemap_tree_for_homepage` may not finish within a
single process lifetime. :class:`~usp.crawl_session.CrawlSession` offers the same discovery
as a budgeted, resumable crawl: each *round* runs under a
:class:`~usp.crawl_session.CrawlBudget` (maximum number of documents and maximum cumulative
decompressed bytes), and the session state can be saved to a versioned JSON checkpoint and
resumed later -- including from another process -- without re-fetching completed documents.

A sitemap document becomes a committable boundary only once it has been fully fetched and
parsed. If the byte budget cannot cover the document currently being read, its temporary
results are discarded (the bytes read still count towards the current round) and the
document is re-read with the next round's fresh budget.

Documents are identified by their final (post-redirect) URL. Shared sub-sitemaps are
fetched only once, but every parent/child reference is kept as an edge. Redirect loops,
index self-loops and failed documents get explicit statuses and never block other pending
branches.

.. code-block:: python

    from usp.crawl_session import CrawlBudget, CrawlSession

    # Round 1: commit at most 200 documents
    session = CrawlSession(entry_urls="https://example.com/sitemap.xml")
    session.run_round(CrawlBudget(max_documents=200, max_bytes=500_000_000))
    session.save_checkpoint("checkpoint.json")

    # Round 2 (possibly in another process): resume from the checkpoint
    session = CrawlSession.load_checkpoint("checkpoint.json")
    session.run_round(CrawlBudget(max_documents=200))

    tree = session.sitemap_tree()  # tree of everything committed so far

Checkpoints are written atomically (temporary file + rename) and contain only plain JSON
types. Resuming refuses checkpoints that are corrupted, have an unsupported format
version, or whose entry URLs / options don't match.

The same workflow is available on the command line:

.. code-block:: bash

    python -m usp.crawl_session run --entry https://example.com/sitemap.xml \
        --checkpoint checkpoint.json --max-documents 200
    python -m usp.crawl_session resume --checkpoint checkpoint.json --max-documents 200

See ``examples/crawl_session_demo.py`` for a fully offline demo and
``examples/checkpoint.sample.json`` for an inspectable checkpoint.

Out of scope (for now): fetching page content, lastmod-based incremental sync and any
business-level deletion logic.

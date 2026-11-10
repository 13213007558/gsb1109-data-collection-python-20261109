.. _cli:

CLI Reference
-------------

The CLI provides a simple command-line interface to retrieve sitemap data.

``usp``
=======

.. code-block:: none

    usage: usp [-h] [-v]  ...

    Ultimate Sitemap Parser

    options:
      -h, --help     show this help message and exit
      -v, --version  show program's version number and exit

    commands:

        ls                 List sitemap pages
        check-alternates   Check hreflang alternate links in downloaded sitemaps (offline)

``usp check-alternates``
========================

.. code-block:: none

    usage: usp check-alternates [-h] [-o OUTPUT] [-v] [-l LOG_FILE]
                                sitemaps [sitemaps ...]

    Read already-downloaded sitemap files and verify multilingual rel="alternate"
    relationships without fetching page bodies.

    positional arguments:
      sitemaps              paths to downloaded sitemap XML files (plain or gzipped)

    options:
      -h, --help            show this help message and exit
      -o OUTPUT, --output OUTPUT
                            write the JSON report to this file instead of standard output
      -v, --verbose         increase output verbosity (-v=INFO, -vv=DEBUG)
      -l LOG_FILE, --log-file LOG_FILE
                            write log to this file and suppress console logging

The command aggregates every ``rel="alternate"`` declaration by absolute ``loc`` across
all supplied files, compares hreflang labels case-insensitively (with ``x-default``
handled as its own reserved label) and verifies self-references, reciprocal links and
language-group consistency. Targets absent from the inputs are reported as unverified,
never as dead links. The JSON report uses stable, order-independent issue numbering.

Exit codes:

* ``0`` — all alternate relationships are consistent;
* ``1`` — one or more issues (including unverifiable external targets) were found;
* ``2`` — an input file could not be read or parsed.

.. rubric:: Examples

.. code-block:: shell-session

    $ usp check-alternates sitemap_a.xml sitemap_b.xml
    Scanned 6 pages and 15 alternate edges in 2 language group(s) from 2 sitemap file(s).
    Found 3 issue(s):
      [grp-ce18c1aeba54] alternate_target_unverified: Alternate target ... is not present ...

``usp ls``
==========

.. code-block:: none

    usage: usp ls [-h] [-f FORMAT] [-r] [-k] [-u] [-v/-vv] [-l LOG_FILE] url

    download, parse and list the sitemap structure

    positional arguments:
      url              URL of the site including protocol

    options:
      -h, --help       show this help message and exit
      -f FORMAT, --format FORMAT
                            set output format (default: tabtree)
                            choices:
                              tabtree: Sitemaps and pages, nested with tab indentation
                              pages: Flat list of pages, one per line
      -r, --no-robots  don't discover sitemaps through robots.txt
      -k, --no-known   don't discover sitemaps through well-known URLs
      -u, --strip-url  strip the supplied URL from each page and sitemap URL
      -v, --verbose         increase output verbosity (-v=INFO, -vv=DEBUG)
      -l LOG_FILE, --log-file LOG_FILE
                            write log to this file and suppress console output

.. rubric:: Examples

.. code-block:: shell-session

    $ usp ls https://example.org/
    https://example.org/
        https://example.org/robots.txt
            https://example.org/sitemap.xml
                https://example.org/page1.html


.. code-block:: shell-session

    $ usp ls https://example.org/ --strip-url
    https://example.org/
        /robots.txt
            /sitemap.xml
                /page1.html

.. code-block:: shell-session

    $ usp ls https://example.org/ --format pages
    https://example.org/page1.html

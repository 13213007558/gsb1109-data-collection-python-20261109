Fixed
-----

*   Stop losing JSON feed data: keep both ``content_text`` and ``content_html``
    in an XML-like ``content`` list, map ``date_modified`` to
    ``updated``/``updated_parsed``, support JSON Feed 1.1 ``authors`` lists
    (with feed-level inheritance), inherit feed/item ``language``, and keep
    attachment ``title``, ``size_in_bytes``, and ``duration_in_seconds``.
    Underscore-prefixed extension fields are preserved at their own level,
    and wrong field types are reported through ``bozo`` instead of raising.

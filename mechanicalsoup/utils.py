import bs4


class LinkNotFoundError(Exception):
    """Exception raised when mechanicalsoup fails to find something.

    This happens in situations like (non-exhaustive list):

    * :func:`~mechanicalsoup.StatefulBrowser.find_link` is called, but
      no link is found.

    * The browser was configured with raise_on_404=True and a 404
      error is triggered while browsing.

    * The user tried to fill-in a field which doesn't exist in a form
      (e.g. browser["name"] = "val" with browser being a
      StatefulBrowser).
    """
    pass


# Submittable form controls that can be associated with a form, either by
# being nested inside it or by carrying an explicit ``form`` attribute.
FORM_CONTROL_TAGS = ("input", "button", "select", "textarea")


def form_controls(form, names=FORM_CONTROL_TAGS):
    """Return the controls owned by ``form`` in document order.

    Ownership follows the HTML specification: a control carrying an
    explicit ``form`` attribute belongs to the form whose ``id`` matches
    that attribute, even if the control is nested inside another form; a
    control without a ``form`` attribute belongs to its nearest ancestor
    ``<form>``. An explicit ``form`` attribute never falls back to the
    ancestor form when no element with the referenced id exists.

    ``names`` restricts which tag names are returned, and duplicate
    controls sharing the same name are all returned, in the order in which
    they appear in the document.

    When ``form`` is an isolated :class:`bs4.element.Tag` that is not
    attached to a parsed document, only the local subtree is available:
    nested controls without an explicit ``form`` attribute are returned,
    while controls carrying a ``form`` attribute are skipped because the
    referenced form cannot be resolved without the full document.
    """
    root = next(
        (parent for parent in form.parents
         if isinstance(parent, bs4.BeautifulSoup)),
        None,
    )
    form_id = form.get("id")

    if root is None:
        # Legacy call with an isolated form Tag: the full document (and
        # therefore any externally associated controls, or the target of
        # an explicit form attribute) is not reachable.
        return [
            control
            for control in form.find_all(names)
            if not control.has_attr("form")
        ]

    controls = []
    for control in root.find_all(names):
        if control.has_attr("form"):
            # Explicit ownership wins; a dangling reference must not fall
            # back to the ancestor form.
            if form_id is not None and control["form"] == form_id:
                controls.append(control)
        elif control.find_parent("form") is form:
            controls.append(control)
    return controls


def is_multipart_file_upload(form, tag):
    return (
        form.get("enctype", "") == "multipart/form-data" and
        tag.get("type", "").lower() == "file"
    )


def is_disabled(tag):
    """Return whether a form control is disabled per the HTML specification.

    A control is disabled if it carries a ``disabled`` attribute, or if it is
    a descendant of a ``<fieldset>`` element whose ``disabled`` attribute is
    set -- except when it is inside that fieldset's first ``<legend>`` child,
    which stays enabled. Disabled controls are barred from submission, so
    browsers do not include their name/value pairs in the form data.

    https://html.spec.whatwg.org/multipage/form-elements.html#concept-fieldset-disabled
    """
    if tag.has_attr("disabled"):
        return True
    for fieldset in tag.find_parents("fieldset"):
        if not fieldset.has_attr("disabled"):
            continue
        # Controls inside the disabled fieldset's first <legend> child are
        # not disabled by that fieldset.
        legend = fieldset.find("legend", recursive=False)
        if legend is not None and any(p is legend for p in tag.parents):
            continue
        return True
    return False

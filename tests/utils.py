from urllib.parse import parse_qsl

import requests_mock

import mechanicalsoup


"""
Utilities for testing MechanicalSoup.
"""

choose_submit_form = '''
<html>
  <body>
    <!-- vaguely based on Trac edit-page form -->
    <form id="choose-submit-form" method="post" action="mock://form.com/post">
      <textarea id="text" class="wikitext trac-resizable" name="text"
        cols="80" rows="40">
      </textarea>
      <div class="field">
        <label>Comment about this change (optional):<br />
          <input id="comment" type="text" name="comment" size="80" value="" />
        </label>
      </div>
      <div class="buttons">
        <input type="submit" name="preview" value="Preview Page" />
        <input type="submit" name="diff" value="Review Changes" />
        <input type="submit" id="save" name="save" value="Submit changes" />
        <button type="submit" name="cancel" value="Cancel" />
      </div>
    </form>
  </body>
</html>
'''


# Two forms with criss-crossing ownership: controls nested in one form may
# belong to the other through an explicit form attribute, and several
# controls (including the submit buttons) live outside both forms.
cross_owned_forms = '''
<html>
  <body>
    <form id="report" method="get" action="https://form.test/report">
      <input name="page" value="1" />
      <input form="settings" name="foreign_inline" value="x" />
      <fieldset disabled>
        <legend><input name="legend_field" value="keep" /></legend>
        <input name="disabled_field" value="skip" />
      </fieldset>
    </form>
    <form id="settings" method="post"
      action="https://form.test/settings">
      <input name="setting" value="s" />
      <select form="report" name="scope">
        <option value="all" selected="selected">All</option>
        <option value="mine">Mine</option>
      </select>
    </form>
    <input form="report" name="filter" value="a" />
    <input form="report" name="filter" value="b" />
    <textarea form="report" name="note">hello</textarea>
    <input form="ghost" name="dangling" value="d" />
    <button form="report" type="submit" name="search"
      value="go">Search</button>
    <button form="report" type="submit" name="export" value="csv"
      formaction="https://form.test/export" formmethod="post"
      formenctype="multipart/form-data">Export</button>
    <button form="settings" type="submit" name="save"
      value="1">Save</button>
  </body>
</html>
'''


def setup_mock_browser(expected_post=None, text=choose_submit_form):
    url = 'mock://form.com'
    browser, mock = prepare_mock_browser()
    mock_get(mock, url, text)

    if expected_post is not None:
        mock_post(mock, url + '/post', expected_post)

    return browser, url


def prepare_mock_browser(scheme='mock'):
    mock = requests_mock.Adapter()
    browser = mechanicalsoup.StatefulBrowser(requests_adapters={scheme: mock})

    return browser, mock


def mock_get(mocked_adapter, url, reply, content_type='text/html', **kwargs):
    headers = {'Content-Type': content_type}
    if isinstance(reply, str):
        kwargs['text'] = reply
    else:
        kwargs['content'] = reply
    mocked_adapter.register_uri('GET', url, headers=headers, **kwargs)


def mock_post(mocked_adapter, url, expected, reply='Success!'):
    def text_callback(request, context):
        query = parse_qsl(request.text)
        assert query == expected
        return reply

    mocked_adapter.register_uri('POST', url, text=text_callback)


class HttpbinRemote:
    """Drop-in replacement for pytest-httpbin's httpbin fixture
    that uses the remote httpbin server instead of a local one."""
    def __init__(self):
        self.url = "http://httpbin.org"

    def __add__(self, x):
        return self.url + x


def open_legacy_httpbin(browser, httpbin):
    """Opens the start page of httpbin (given as a fixture). Tries the
    legacy page (available only on recent versions of httpbin), and if
    it fails fall back to the main page (which is JavaScript-only in
    recent versions of httpbin hence usable for us only on old
    versions).
    """
    try:
        response = browser.open(httpbin + "/legacy")
        if response.status_code == 404:
            # The line above may or may not have raised the exception
            # depending on raise_on_404. Raise it unconditionally now.
            raise mechanicalsoup.LinkNotFoundError()
        return response
    except mechanicalsoup.LinkNotFoundError:
        return browser.open(httpbin.url)

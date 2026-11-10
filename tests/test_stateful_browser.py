import copy
import io
import json
import os
import re
import sys
import tempfile
from urllib.parse import parse_qsl
import webbrowser

import pytest
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from bs4 import BeautifulSoup
from utils import (mock_get, open_legacy_httpbin, prepare_mock_browser,
                   setup_mock_browser)

import mechanicalsoup
import requests


def test_request_forward():
    data = [('var1', 'val1'), ('var2', 'val2')]
    browser, url = setup_mock_browser(expected_post=data)
    r = browser.request('POST', url + '/post', data=data)
    assert r.text == 'Success!'


def test_properties():
    """Check that properties return the same value as the getter."""
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<form></form>', url="http://example.com")
    assert browser.page == browser.get_current_page()
    assert browser.page is not None
    assert browser.url == browser.get_url()
    assert browser.url is not None
    browser.select_form()
    assert browser.form == browser.get_current_form()
    assert browser.form is not None


def test_get_selected_form_unselected():
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<form></form>')
    with pytest.raises(AttributeError, match="No form has been selected yet."):
        browser.form
    assert browser.get_current_form() is None


def test_submit_online(httpbin):
    """Complete and submit the pizza form at http://httpbin.org/forms/post """
    browser = mechanicalsoup.StatefulBrowser()
    browser.set_user_agent('testing MechanicalSoup')
    browser.open(httpbin.url)
    for link in browser.links():
        if link["href"] == "/":
            browser.follow_link(link)
            break
    browser.follow_link("forms/post")
    assert browser.url == httpbin + "/forms/post"
    browser.select_form("form")
    browser["custname"] = "Customer Name Here"
    browser["size"] = "medium"
    browser["topping"] = ("cheese", "bacon")
    # Change our mind to make sure old boxes are unticked
    browser["topping"] = ("cheese", "onion")
    browser["comments"] = "Some comment here"
    browser.form.set("nosuchfield", "new value", True)
    response = browser.submit_selected()
    json = response.json()
    data = json["form"]
    assert data["custname"] == "Customer Name Here"
    assert data["custtel"] == ""  # web browser submits "" for input left blank
    assert data["size"] == "medium"
    assert set(data["topping"]) == {"cheese", "onion"}
    assert data["comments"] == "Some comment here"
    assert data["nosuchfield"] == "new value"

    assert json["headers"]["User-Agent"] == 'testing MechanicalSoup'
    # Ensure we haven't blown away any regular headers
    expected_headers = ('Content-Length', 'Host', 'Content-Type', 'Connection',
                        'Accept', 'User-Agent', 'Accept-Encoding')
    assert set(expected_headers).issubset(json["headers"].keys())


def test_no_404(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    resp = browser.open(httpbin + "/nosuchpage")
    assert resp.status_code == 404


def test_404(httpbin):
    browser = mechanicalsoup.StatefulBrowser(raise_on_404=True)
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.open(httpbin + "/nosuchpage")
    resp = browser.open(httpbin.url)
    assert resp.status_code == 200


def test_user_agent(httpbin):
    browser = mechanicalsoup.StatefulBrowser(user_agent='007')
    resp = browser.open(httpbin + "/user-agent")
    assert resp.json() == {'user-agent': '007'}


def test_open_relative(httpbin):
    # Open an arbitrary httpbin page to set the current URL
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(httpbin + "/html")

    # Open a relative page and make sure remote host and browser agree on URL
    resp = browser.open_relative("/get")
    assert resp.json()['url'] == httpbin + "/get"
    assert browser.url == httpbin + "/get"

    # Test passing additional kwargs to the session
    resp = browser.open_relative("/basic-auth/me/123", auth=('me', '123'))
    assert browser.url == httpbin + "/basic-auth/me/123"
    assert resp.json() == {"authenticated": True, "user": "me"}


def test_links():
    browser = mechanicalsoup.StatefulBrowser()
    html = '''<a class="bluelink" href="/blue" id="blue_link">A Blue Link</a>
              <a class="redlink" href="/red" id="red_link">A Red Link</a>'''
    expected = [BeautifulSoup(html, "lxml").a]
    browser.open_fake_page(html)

    # Test StatefulBrowser.links url_regex argument
    assert browser.links(url_regex="bl") == expected
    assert browser.links(url_regex="bluish") == []

    # Test StatefulBrowser.links link_text argument
    assert browser.links(link_text="A Blue Link") == expected
    assert browser.links(link_text="Blue") == []

    # Test StatefulBrowser.links kwargs passed to BeautifulSoup.find_all
    assert browser.links(string=re.compile('Blue')) == expected
    assert browser.links(class_="bluelink") == expected
    assert browser.links(id="blue_link") == expected
    assert browser.links(id="blue") == []

    # Test returning a non-singleton
    two_links = browser.links(id=re.compile('_link'))
    assert len(two_links) == 2
    assert two_links == BeautifulSoup(html, "lxml").find_all('a')


def test_links_no_page():
    """links() must report that no page is loaded rather than failing with
    an opaque AttributeError from the underlying None."""
    browser = mechanicalsoup.StatefulBrowser()
    with pytest.raises(AttributeError, match="No page has been opened"):
        browser.links()


def test_select_form_no_page():
    browser = mechanicalsoup.StatefulBrowser()
    with pytest.raises(AttributeError, match="No page has been opened"):
        browser.select_form()


def test_absolute_url_no_page():
    browser = mechanicalsoup.StatefulBrowser()
    with pytest.raises(AttributeError, match="No page has been opened"):
        browser.absolute_url("foo")


@pytest.mark.parametrize("expected_post", [
    pytest.param(
        [
            ('text', 'Setting some text!'),
            ('comment', 'Selecting an input submit'),
            ('diff', 'Review Changes'),
        ], id='input'),
    pytest.param(
        [
            ('text', '= Heading =\n\nNew page here!\n'),
            ('comment', 'Selecting a button submit'),
            ('cancel', 'Cancel'),
        ], id='button'),
])
def test_submit_btnName(expected_post):
    '''Tests that the btnName argument chooses the submit button.'''
    browser, url = setup_mock_browser(expected_post=expected_post)
    browser.open(url)
    browser.select_form('#choose-submit-form')
    browser['text'] = dict(expected_post)['text']
    browser['comment'] = dict(expected_post)['comment']
    initial_state = browser._StatefulBrowser__state
    res = browser.submit_selected(btnName=expected_post[2][0])
    assert res.status_code == 200 and res.text == 'Success!'
    assert initial_state != browser._StatefulBrowser__state


@pytest.mark.parametrize("expected_post", [
    pytest.param(
        [
            ('text', 'Setting some text!'),
            ('comment', 'Selecting an input submit'),
        ], id='input'),
    pytest.param(
        [
            ('text', '= Heading =\n\nNew page here!\n'),
            ('comment', 'Selecting a button submit'),
        ], id='button'),
])
def test_submit_no_btn(expected_post):
    '''Tests that no submit inputs are posted when btnName=False.'''
    browser, url = setup_mock_browser(expected_post=expected_post)
    browser.open(url)
    browser.select_form('#choose-submit-form')
    browser['text'] = dict(expected_post)['text']
    browser['comment'] = dict(expected_post)['comment']
    initial_state = browser._StatefulBrowser__state
    res = browser.submit_selected(btnName=False)
    assert res.status_code == 200 and res.text == 'Success!'
    assert initial_state != browser._StatefulBrowser__state


def test_submit_dont_modify_kwargs():
    """Test that submit_selected() doesn't modify the caller's passed-in
    kwargs, for example when adding a Referer header.
    """
    kwargs = {'headers': {'Content-Type': 'text/html'}}
    saved_kwargs = copy.deepcopy(kwargs)

    browser, url = setup_mock_browser(expected_post=[], text='<form></form>')
    browser.open(url)
    browser.select_form()
    browser.submit_selected(**kwargs)

    assert kwargs == saved_kwargs


def test_submit_dont_update_state():
    expected_post = [
            ('text', 'Bananas are good.'),
            ('preview', 'Preview Page')]
    browser, url = setup_mock_browser(expected_post=expected_post)
    browser.open(url)
    browser.select_form('#choose-submit-form')
    browser['text'] = dict(expected_post)['text']
    initial_state = browser._StatefulBrowser__state
    res = browser.submit_selected(update_state=False)
    assert res.status_code == 200 and res.text == 'Success!'
    assert initial_state == browser._StatefulBrowser__state


def test_get_set_debug():
    browser = mechanicalsoup.StatefulBrowser()
    # Debug mode is off by default
    assert not browser.get_debug()
    browser.set_debug(True)
    assert browser.get_debug()


def test_list_links(capsys):
    # capsys is a pytest fixture that allows us to inspect the std{err,out}
    browser = mechanicalsoup.StatefulBrowser()
    links = '''
     <a href="/link1">Link #1</a>
     <a href="/link2" id="link2"> Link #2</a>
'''
    browser.open_fake_page(f'<html>{links}</html>')
    browser.list_links()
    out, err = capsys.readouterr()
    expected = f'Links in the current page:{links}'
    assert out == expected


def test_launch_browser(mocker):
    browser = mechanicalsoup.StatefulBrowser()
    browser.set_debug(True)
    browser.open_fake_page('<html></html>')
    mocker.patch('webbrowser.open')
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.follow_link('nosuchlink')
    # mock.assert_called_once() not available on some versions :-(
    assert webbrowser.open.call_count == 1
    mocker.resetall()
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.select_form('nosuchlink')
    # mock.assert_called_once() not available on some versions :-(
    assert webbrowser.open.call_count == 1


def test_find_link():
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<html></html>')
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.find_link('nosuchlink')


def test_verbose(capsys):
    '''Tests that the btnName argument chooses the submit button.'''
    browser, url = setup_mock_browser()
    browser.open(url)
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert browser.get_verbose() == 0
    browser.set_verbose(1)
    browser.open(url)
    out, err = capsys.readouterr()
    assert out == "."
    assert err == ""
    assert browser.get_verbose() == 1
    browser.set_verbose(2)
    browser.open(url)
    out, err = capsys.readouterr()
    assert out == "mock://form.com\n"
    assert err == ""
    assert browser.get_verbose() == 2


def test_new_control(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    browser.open(httpbin + "/forms/post")
    browser.select_form("form")
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        # The control doesn't exist, yet.
        browser["temperature"] = "cold"
    browser["size"] = "large"  # Existing radio
    browser["comments"] = "This is a comment"  # Existing textarea
    browser.new_control("text", "temperature", "warm")
    browser.new_control("textarea", "size", "Sooo big !")
    browser.new_control("text", "comments", "This is an override comment")
    fake_select = BeautifulSoup("", "html.parser").new_tag('select')
    fake_select["name"] = "foo"
    browser.form.form.append(fake_select)
    browser.new_control("checkbox", "foo", "valval", checked="checked")
    tag = browser.form.form.find("input", {"name": "foo"})
    assert tag.attrs["checked"] == "checked"
    browser["temperature"] = "hot"
    response = browser.submit_selected()
    json = response.json()
    data = json["form"]
    print(data)
    assert data["temperature"] == "hot"
    assert data["size"] == "Sooo big !"
    assert data["comments"] == "This is an override comment"
    assert data["foo"] == "valval"


submit_form_noaction = '''
<html>
  <body>
    <form id="choose-submit-form">
      <input type="text" name="text1" value="someValue1" />
      <input type="text" name="text2" value="someValue2" />
      <input type="submit" name="save" />
    </form>
  </body>
</html>
'''


def test_form_noaction():
    browser, url = setup_mock_browser()
    browser.open_fake_page(submit_form_noaction)
    browser.select_form('#choose-submit-form')
    with pytest.raises(ValueError, match="no URL to submit to"):
        browser.submit_selected()


submit_form_noname = '''
<html>
  <body>
    <form id="choose-submit-form" method="post" action="mock://form.com/post">
      <textarea>Value</textarea> <!-- no name -->
      <select> <!-- no name -->
        <option value="tofu" selected="selected">Tofu Stir Fry</option>
        <option value="curry">Red Curry</option>
        <option value="tempeh">Tempeh Tacos</option>
      </select>
    </form>
  </body>
</html>
'''


def test_form_noname():
    browser, url = setup_mock_browser(expected_post=[])
    browser.open_fake_page(submit_form_noname, url=url)
    browser.select_form('#choose-submit-form')
    response = browser.submit_selected()
    assert response.status_code == 200 and response.text == 'Success!'


submit_form_multiple = '''
<html>
  <body>
    <form id="choose-submit-form" method="post" action="mock://form.com/post">
      <select name="foo" multiple>
        <option value="tofu" selected="selected">Tofu Stir Fry</option>
        <option value="curry">Red Curry</option>
        <option value="tempeh" selected="selected">Tempeh Tacos</option>
      </select>
    </form>
  </body>
</html>
'''


def test_form_multiple():
    browser, url = setup_mock_browser(expected_post=[('foo', 'tofu'),
                                                     ('foo', 'tempeh')])
    browser.open_fake_page(submit_form_multiple, url=url)
    browser.select_form('#choose-submit-form')
    response = browser.submit_selected()
    assert response.status_code == 200 and response.text == 'Success!'


def test_upload_file(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    url = httpbin + "/post"
    file_input_form = f"""
    <form method="post" action="{url}" enctype="multipart/form-data">
        <input type="file" name="first" />
    </form>
    """

    # Create two temporary files to upload
    def make_file(content):
        path = tempfile.mkstemp()[1]
        with open(path, "w") as fd:
            fd.write(content)
        return path
    path1 = make_file("first file content")
    path2 = make_file("second file content")

    browser.open_fake_page(file_input_form)
    browser.select_form()

    # Test filling an existing input and creating a new input
    with open(path1, "rb") as value1, open(path2, "rb") as value2:
        browser["first"] = value1
        browser.new_control("file", "second", value2)
        response = browser.submit_selected()

    files = response.json()["files"]
    assert files["first"] == "first file content"
    assert files["second"] == "second file content"


def test_upload_file_with_malicious_default(httpbin):
    """Check for CVE-2023-34457 by setting the form input value directly to a
    file that the user does not explicitly consent to upload, as a malicious
    server might do.
    """
    browser = mechanicalsoup.StatefulBrowser()
    sensitive_path = tempfile.mkstemp()[1]
    with open(sensitive_path, "w") as fd:
        fd.write("Some sensitive information")
    url = httpbin + "/post"
    malicious_html = f"""
    <form method="post" action="{url}" enctype="multipart/form-data">
        <input type="file" name="malicious" value="{sensitive_path}" />
    </form>
    """
    browser.open_fake_page(malicious_html)
    browser.select_form()
    response = browser.submit_selected()
    assert response.json()["files"] == {"malicious": ""}


def test_upload_file_raise_on_string_input():
    """Check for use of the file upload API that was modified to remediate
    CVE-2023-34457. Users must now open files manually to upload them.
    """
    browser = mechanicalsoup.StatefulBrowser()
    file_input_form = """
    <form enctype="multipart/form-data">
        <input type="file" name="upload" />
    </form>
    """
    browser.open_fake_page(file_input_form)
    browser.select_form()
    with pytest.raises(ValueError, match="CVE-2023-34457"):
        browser["upload"] = "/path/to/file"
    with pytest.raises(ValueError, match="CVE-2023-34457"):
        browser.new_control("file", "upload2", "/path/to/file")


def test_with():
    """Test that __enter__/__exit__ properly create/close the browser."""
    with mechanicalsoup.StatefulBrowser() as browser:
        assert browser.session is not None
    assert browser.session is None


def test_select_form_nr():
    """Test the nr option of select_form."""
    forms = """<form id="a"></form><form id="b"></form><form id="c"></form>"""
    with mechanicalsoup.StatefulBrowser() as browser:
        browser.open_fake_page(forms)
        form = browser.select_form()
        assert form.form['id'] == "a"
        form = browser.select_form(nr=1)
        assert form.form['id'] == "b"
        form = browser.select_form(nr=2)
        assert form.form['id'] == "c"
        with pytest.raises(mechanicalsoup.LinkNotFoundError):
            browser.select_form(nr=3)


def test_select_form_tag_object():
    """Test tag object as selector parameter type"""
    forms = """<form id="a"></form><form id="b"></form><p></p>"""
    soup = BeautifulSoup(forms, "lxml")
    with mechanicalsoup.StatefulBrowser() as browser:
        browser.open_fake_page(forms)
        form = browser.select_form(soup.find("form", {"id": "b"}))
        assert form.form['id'] == "b"
        with pytest.raises(mechanicalsoup.LinkNotFoundError):
            browser.select_form(soup.find("p"))


def test_select_form_associated_elements():
    """Test associated elements outside the form tag"""
    forms = """<form id="a"><input><textarea></textarea></form><input form="a">
               <textarea form="a"/><input form="b">
               <form id="ab" action="/test.php"><input></form>
               <textarea form="ab"></textarea>
            """
    with mechanicalsoup.StatefulBrowser() as browser:
        browser.open_fake_page(forms)
        elements_form_a = set([
            "<input/>", "<textarea></textarea>",
            '<input form="a"/>', '<textarea form="a"></textarea>'])
        elements_form_ab = set(["<input/>", '<textarea form="ab"></textarea>'])
        form_by_str = browser.select_form("#a")
        form_by_tag = browser.select_form(browser.page.find("form", id='a'))
        form_by_css = browser.select_form("form[action$='.php']")
        assert set([str(element) for element in form_by_str.form.find_all((
            "input", "textarea"))]) == elements_form_a
        assert set([str(element) for element in form_by_tag.form.find_all((
            "input", "textarea"))]) == elements_form_a
        assert set([str(element) for element in form_by_css.form.find_all((
            "input", "textarea"))]) == elements_form_ab


def test_referer_follow_link(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    start_url = browser.url
    response = browser.follow_link("/headers")
    referer = response.json()["headers"]["Referer"]
    actual_ref = re.sub('/*$', '', referer)
    expected_ref = re.sub('/*$', '', start_url)
    assert actual_ref == expected_ref


submit_form_headers = '''
<html>
  <body>
    <form method="get" action="{}" id="choose-submit-form">
      <input type="text" name="text1" value="someValue1" />
      <input type="text" name="text2" value="someValue2" />
      <input type="submit" name="save" />
    </form>
  </body>
</html>
'''


def test_referer_submit(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    ref = "https://example.com/my-referer"
    page = submit_form_headers.format(httpbin.url + "/headers")
    browser.open_fake_page(page, url=ref)
    browser.select_form()
    response = browser.submit_selected()
    headers = response.json()["headers"]
    referer = headers["Referer"]
    actual_ref = re.sub('/*$', '', referer)
    assert actual_ref == ref


@pytest.mark.parametrize("referer_header", ["Referer", "referer"])
def test_referer_submit_override(httpbin, referer_header):
    """Ensure the caller can override the Referer header that
    mechanicalsoup would normally add. Because headers are case insensitive,
    test with both 'Referer' and 'referer'.
    """

    browser = mechanicalsoup.StatefulBrowser()
    ref = "https://example.com/my-referer"
    ref_override = "https://example.com/override"
    page = submit_form_headers.format(httpbin.url + "/headers")
    browser.open_fake_page(page, url=ref)
    browser.select_form()
    response = browser.submit_selected(headers={referer_header: ref_override})
    headers = response.json()["headers"]
    referer = headers["Referer"]
    actual_ref = re.sub('/*$', '', referer)
    assert actual_ref == ref_override


def test_referer_submit_headers(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    ref = "https://example.com/my-referer"
    page = submit_form_headers.format(httpbin.url + "/headers")
    browser.open_fake_page(page, url=ref)
    browser.select_form()
    response = browser.submit_selected(
        headers={'X-Test-Header': 'x-test-value'})
    headers = response.json()["headers"]
    referer = headers["Referer"]
    actual_ref = re.sub('/*$', '', referer)
    assert actual_ref == ref
    assert headers['X-Test-Header'] == 'x-test-value'


@pytest.mark.parametrize('expected, kwargs', [
    pytest.param('/foo', {}, id='none'),
    pytest.param('/get', {'string': 'Link'}, id='string'),
    pytest.param('/get', {'url_regex': 'get'}, id='regex'),
])
def test_follow_link_arg(httpbin, expected, kwargs):
    browser = mechanicalsoup.StatefulBrowser()
    html = '<a href="/foo">Bar</a><a href="/get">Link</a>'
    browser.open_fake_page(html, httpbin.url)
    browser.follow_link(bs4_kwargs=kwargs)
    assert browser.url == httpbin + expected


def test_follow_link_from_tag(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    html = '<a href="/foo">Bar</a><a href="/get">Link</a>'
    browser.open_fake_page(html, httpbin.url)
    tag = browser.links()[1]
    browser.follow_link(link=tag)
    assert browser.url == httpbin + '/get'


def test_follow_link_excess(httpbin):
    """Ensure that excess args are passed to BeautifulSoup"""
    browser = mechanicalsoup.StatefulBrowser()
    html = '<a href="/foo">Bar</a><a href="/get">Link</a>'
    browser.open_fake_page(html, httpbin.url)
    browser.follow_link(url_regex='get')
    assert browser.url == httpbin + '/get'

    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<a href="/get">Link</a>', httpbin.url)
    with pytest.raises(ValueError, match="link parameter cannot be .*"):
        browser.follow_link('foo', url_regex='bar')


def test_follow_link_ua(httpbin):
    """Tests passing requests parameters to follow_link() by
    setting the User-Agent field."""
    browser = mechanicalsoup.StatefulBrowser()
    # html = '<a href="/foo">Bar</a><a href="/get">Link</a>'
    # browser.open_fake_page(html, httpbin.url)
    open_legacy_httpbin(browser, httpbin)
    bs4_kwargs = {'url_regex': 'user-agent'}
    requests_kwargs = {'headers': {"User-Agent": '007'}}
    resp = browser.follow_link(bs4_kwargs=bs4_kwargs,
                               requests_kwargs=requests_kwargs)
    assert browser.url == httpbin + '/user-agent'
    assert resp.json() == {'user-agent': '007'}
    assert resp.request.headers['user-agent'] == '007'


def test_link_arg_multiregex(httpbin):
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<a href="/get">Link</a>', httpbin.url)
    with pytest.raises(ValueError, match="link parameter cannot be .*"):
        browser.follow_link('foo', bs4_kwargs={'url_regex': 'bar'})


def file_get_contents(filename):
    with open(filename, "rb") as fd:
        return fd.read()


def test_download_link(httpbin):
    """Test downloading the contents of a link to file."""
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    tmpdir = tempfile.mkdtemp()
    tmpfile = tmpdir + '/nosuchfile.png'
    current_url = browser.url
    current_page = browser.page
    response = browser.download_link(file=tmpfile, link='image/png')

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that the file was downloaded
    assert os.path.isfile(tmpfile)
    assert file_get_contents(tmpfile) == response.content
    # Check that we actually downloaded a PNG file
    assert response.content[:4] == b'\x89PNG'


def test_download_link_nofile(httpbin):
    """Test downloading the contents of a link without saving it."""
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    current_url = browser.url
    current_page = browser.page
    response = browser.download_link(link='image/png')

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that we actually downloaded a PNG file
    assert response.content[:4] == b'\x89PNG'


def test_download_link_nofile_bs4(httpbin):
    """Test downloading the contents of a link without saving it."""
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    current_url = browser.url
    current_page = browser.page
    response = browser.download_link(bs4_kwargs={'url_regex': 'image.png'})

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that we actually downloaded a PNG file
    assert response.content[:4] == b'\x89PNG'


def test_download_link_nofile_excess(httpbin):
    """Test downloading the contents of a link without saving it."""
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    current_url = browser.url
    current_page = browser.page
    response = browser.download_link(url_regex='image.png')

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that we actually downloaded a PNG file
    assert response.content[:4] == b'\x89PNG'


def test_download_link_nofile_ua(httpbin):
    """Test downloading the contents of a link without saving it."""
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    current_url = browser.url
    current_page = browser.page
    requests_kwargs = {'headers': {"User-Agent": '007'}}
    response = browser.download_link(link='image/png',
                                     requests_kwargs=requests_kwargs)
    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that we actually downloaded a PNG file
    assert response.content[:4] == b'\x89PNG'

    # Check that we actually set the User-agent outbound
    assert response.request.headers['user-agent'] == '007'


def test_download_link_to_existing_file(httpbin):
    """Test downloading the contents of a link to an existing file."""
    browser = mechanicalsoup.StatefulBrowser()
    open_legacy_httpbin(browser, httpbin)
    tmpdir = tempfile.mkdtemp()
    tmpfile = tmpdir + '/existing.png'
    with open(tmpfile, "w") as fd:
        fd.write("initial content")
    current_url = browser.url
    current_page = browser.page
    response = browser.download_link('image/png', tmpfile)

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that the file was downloaded
    assert os.path.isfile(tmpfile)
    assert file_get_contents(tmpfile) == response.content
    # Check that we actually downloaded a PNG file
    assert response.content[:4] == b'\x89PNG'


def test_download_link_404(httpbin):
    """Test downloading the contents of a broken link."""
    browser = mechanicalsoup.StatefulBrowser(raise_on_404=True)
    browser.open_fake_page('<a href="/no-such-page-404">Link</a>',
                           url=httpbin.url)
    tmpdir = tempfile.mkdtemp()
    tmpfile = tmpdir + '/nosuchfile.txt'
    current_url = browser.url
    current_page = browser.page
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.download_link(file=tmpfile, link_text='Link')

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that the file was not downloaded
    assert not os.path.exists(tmpfile)


def test_download_link_referer(httpbin):
    """Test downloading the contents of a link to file."""
    browser = mechanicalsoup.StatefulBrowser()
    ref = httpbin + "/my-referer"
    browser.open_fake_page('<a href="/headers">Link</a>',
                           url=ref)
    tmpfile = tempfile.NamedTemporaryFile()
    current_url = browser.url
    current_page = browser.page
    browser.download_link(file=tmpfile.name, link_text='Link')

    # Check that the browser state has not changed
    assert browser.url == current_url
    assert browser.page == current_page

    # Check that the file was downloaded
    with open(tmpfile.name) as fd:
        json_data = json.load(fd)
    headers = json_data["headers"]
    assert headers["Referer"] == ref


def test_refresh_open():
    url = 'mock://example.com'
    initial_page = BeautifulSoup('<p>Fake empty page</p>', 'lxml')
    reload_page = BeautifulSoup('<p>Fake reloaded page</p>', 'lxml')

    browser, adapter = prepare_mock_browser()
    mock_get(adapter, url=url, reply=str(initial_page))
    browser.open(url)
    mock_get(adapter, url=url, reply=str(reload_page),
             additional_matcher=lambda r: 'Referer' not in r.headers)

    browser.refresh()

    assert browser.url == url
    assert browser.page == reload_page


def test_refresh_follow_link():
    url = 'mock://example.com'
    follow_url = 'mock://example.com/followed'
    initial_content = f'<a href="{follow_url}">Link</a>'
    initial_page = BeautifulSoup(initial_content, 'lxml')
    reload_page = BeautifulSoup('<p>Fake reloaded page</p>', 'lxml')

    browser, adapter = prepare_mock_browser()
    mock_get(adapter, url=url, reply=str(initial_page))
    mock_get(adapter, url=follow_url, reply=str(initial_page))
    browser.open(url)
    browser.follow_link()
    refer_header = {'Referer': url}
    mock_get(adapter, url=follow_url, reply=str(reload_page),
             request_headers=refer_header)

    browser.refresh()

    assert browser.url == follow_url
    assert browser.page == reload_page


def test_refresh_form_not_retained():
    url = 'mock://example.com'
    initial_content = '<form>Here comes the form</form>'
    initial_page = BeautifulSoup(initial_content, 'lxml')
    reload_page = BeautifulSoup('<p>Fake reloaded page</p>', 'lxml')

    browser, adapter = prepare_mock_browser()
    mock_get(adapter, url=url, reply=str(initial_page))
    browser.open(url)
    browser.select_form()
    mock_get(adapter, url=url, reply=str(reload_page),
             additional_matcher=lambda r: 'Referer' not in r.headers)

    browser.refresh()

    assert browser.url == url
    assert browser.page == reload_page
    with pytest.raises(AttributeError, match="No form has been selected yet."):
        browser.form


def test_refresh_error():
    browser = mechanicalsoup.StatefulBrowser()

    # Test no page
    with pytest.raises(ValueError):
        browser.refresh()

    # Test fake page
    with pytest.raises(ValueError):
        browser.open_fake_page('<p>Fake empty page</p>', url='http://fake.com')
        browser.refresh()


def test_requests_session_and_cookies(httpbin):
    """Check that the session object passed to the constructor of
    StatefulBrowser is actually taken into account."""
    s = requests.Session()
    requests.utils.add_dict_to_cookiejar(s.cookies, {'key1': 'val1'})
    browser = mechanicalsoup.StatefulBrowser(session=s)
    resp = browser.get(httpbin + "/cookies")
    assert resp.json() == {'cookies': {'key1': 'val1'}}


def test_submit_error():
    """Check that we are prevented from directly calling the submit method."""
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<form></form>')
    browser.select_form()
    with pytest.raises(UserWarning, match="To submit a form"):
        browser.submit(browser.form)


fork_test_page = '''
<html>
  <body>
    <form id="export" method="post" action="mock://form.com/post">
      <input type="hidden" name="token" value="one-time-token" />
      <input type="text" name="region" value="" />
      <input type="text" name="date" value="" />
      <input type="submit" name="by_region" value="Export by region" />
      <input type="submit" name="by_date" value="Export by date" />
    </form>
  </body>
</html>
'''


def test_fork_page_and_form_isolated():
    """A fork copies the current state, then diverges independently."""
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(fork_test_page, url='http://example.com/query')
    browser.select_form('#export')
    browser['region'] = 'us-east'
    browser.form.choose_submit('by_region')

    fork = browser.fork()

    # Same state, but distinct objects.
    assert fork.url == browser.url
    assert fork.page is not browser.page
    assert fork.page == browser.page  # bs4 tags compare by value
    assert fork.form.form is not browser.form.form
    assert fork.get_current_page() is fork.page
    assert fork.get_current_form() is fork.form
    assert fork.get_url() == fork.url

    # The chosen submit is rebound to the fork's own tree.
    chosen = fork.form.submit_chosen
    assert chosen is not browser.form.submit_chosen
    assert chosen['name'] == 'by_region'
    root = chosen
    while root.parent is not None:
        root = root.parent
    assert root is fork.page

    # Field changes don't cross over.
    fork['region'] = 'eu-west'
    region = fork.form.form.find('input', {'name': 'region'})
    assert region['value'] == 'eu-west'
    assert (browser.form.form.find('input', {'name': 'region'})['value']
            == 'us-east')

    # DOM changes don't cross over.
    fork.page.body.append(fork.page.new_tag('div'))
    assert fork.page.body.find('div') is not None
    assert browser.page.body.find('div') is None

    # Changes to the chosen submit element don't cross over.
    chosen['value'] = 'changed'
    assert browser.form.submit_chosen['value'] == 'Export by region'


def test_fork_session_state():
    """A fork inherits the session configuration on an independent session."""
    browser = mechanicalsoup.StatefulBrowser()
    browser.session.auth = ('user', 'pass')
    browser.session.verify = False
    browser.session.headers['X-Custom'] = 'parent'
    browser.session.cookies.set('sid', 'abc')
    browser.set_verbose(2)

    fork = browser.fork()

    assert fork.session is not browser.session
    assert fork.session.auth == ('user', 'pass')
    assert fork.session.verify is False
    assert fork.session.headers['X-Custom'] == 'parent'
    assert (fork.session.headers['User-agent']
            == browser.session.headers['User-agent'])
    assert fork.session.cookies.get('sid') == 'abc'
    assert fork.get_verbose() == 2

    # Adapters (and their connection pools) are shared, not copied.
    for prefix, adapter in browser.session.adapters.items():
        assert fork.session.adapters[prefix] is adapter

    # Cookie and header changes don't cross over.
    fork.session.headers['X-Custom'] = 'fork'
    fork.session.cookies.set('sid', 'fork')
    assert browser.session.headers['X-Custom'] == 'parent'
    assert browser.session.cookies.get('sid') == 'abc'
    assert fork.session.headers['X-Custom'] == 'fork'
    assert fork.session.cookies.get('sid') == 'fork'


def test_fork_close_does_not_affect_parent():
    """Closing a fork must not close the shared adapters/connection pools."""
    expected_post = [
        ('text', 'hello'),
        ('comment', 'parent submits'),
        ('preview', 'Preview Page'),
    ]
    browser, url = setup_mock_browser(expected_post=expected_post)
    browser.open(url)
    browser.select_form('#choose-submit-form')
    browser['text'] = 'hello'
    browser['comment'] = 'parent submits'

    fork = browser.fork()
    assert fork.session.adapters['mock'] is browser.session.adapters['mock']

    fork.close()
    assert fork.session is None

    # The parent browser is still fully functional.
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'


def test_fork_file_control_with_bound_file():
    """Forking with an open file bound to a file control fails cleanly."""
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(
        '<form method="post" enctype="multipart/form-data">'
        '<input type="file" name="attachment" />'
        '<input type="submit" name="go" /></form>',
        url='http://example.com')
    browser.select_form()
    browser['attachment'] = io.BytesIO(b'data')

    with pytest.raises(ValueError, match="'attachment'"):
        browser.fork()

    # The parent browser is left unchanged.
    assert browser.url == 'http://example.com'
    assert isinstance(
        browser.form.form.find('input', {'name': 'attachment'})['value'],
        io.IOBase)


def test_fork_no_page():
    """Forking works even when no page was ever opened."""
    browser = mechanicalsoup.StatefulBrowser()
    fork = browser.fork()
    assert fork.page is None
    assert fork.url is None
    with pytest.raises(AttributeError, match="No form has been selected yet."):
        fork.form


def test_fork_no_selected_form():
    """Forking works when a page is opened but no form is selected."""
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page('<p>hello</p>', url='http://example.com')
    fork = browser.fork()
    assert fork.url == 'http://example.com'
    assert fork.page is not browser.page
    assert fork.page == browser.page
    with pytest.raises(AttributeError, match="No form has been selected yet."):
        fork.form


def test_fork_closed_browser():
    browser = mechanicalsoup.StatefulBrowser()
    browser.close()
    with pytest.raises(ValueError, match="closed"):
        browser.fork()


def test_fork_branches_submit_independently():
    """Two forks and the parent submit their own fields and cookies."""
    browser, mock = prepare_mock_browser()
    mock_get(mock, 'mock://form.com', fork_test_page)
    submissions = []

    def record_submission(request, context):
        submissions.append((
            parse_qsl(request.text, keep_blank_values=True),
            request.headers.get('Cookie', ''),
        ))
        return 'ok'

    mock.register_uri('POST', 'mock://form.com/post', text=record_submission)

    browser.open('mock://form.com')
    browser.select_form('#export')
    browser.form.choose_submit('by_region')
    browser.session.cookies.set('session', 'parent')

    region_branch = browser.fork()
    date_branch = browser.fork()

    region_branch['region'] = 'eu-west'
    region_branch.session.cookies.set('branch', 'region')
    region_branch.submit_selected()

    date_branch['date'] = '2026-11'
    date_branch.form.choose_submit('by_date')
    date_branch.session.cookies.set('branch', 'date')
    date_branch.submit_selected()

    region_branch.close()

    # The parent still holds its original state.
    browser.submit_selected()

    assert len(submissions) == 3

    region_fields, region_cookies = submissions[0]
    assert region_fields == [
        ('token', 'one-time-token'),
        ('region', 'eu-west'),
        ('date', ''),
        ('by_region', 'Export by region'),
    ]
    assert 'session=parent' in region_cookies
    assert 'branch=region' in region_cookies

    date_fields, date_cookies = submissions[1]
    assert date_fields == [
        ('token', 'one-time-token'),
        ('region', ''),
        ('date', '2026-11'),
        ('by_date', 'Export by date'),
    ]
    assert 'session=parent' in date_cookies
    assert 'branch=date' in date_cookies
    assert 'branch=region' not in date_cookies

    parent_fields, parent_cookies = submissions[2]
    assert parent_fields == [
        ('token', 'one-time-token'),
        ('region', ''),
        ('date', ''),
        ('by_region', 'Export by region'),
    ]
    assert 'session=parent' in parent_cookies
    assert 'branch=' not in parent_cookies

    # The page was never refetched: the one-time token state came from
    # the in-memory copy.
    gets = [r for r in mock.request_history if r.method == 'GET']
    assert len(gets) == 1


if __name__ == '__main__':
    pytest.main(sys.argv)

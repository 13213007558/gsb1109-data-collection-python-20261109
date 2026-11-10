# The JSON feed parser
# Copyright 2017 Beat Bolli
# All rights reserved.
#
# This file is a part of feedparser.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS 'AS IS'
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import json

from ..datetimes import _parse_date
from ..sanitizer import sanitize_html
from ..urls import resolve_relative_uris
from ..util import FeedParserDict


class JSONParser:
    VERSIONS = {
        "https://jsonfeed.org/version/1": "json1",
        "https://jsonfeed.org/version/1.1": "json11",
    }
    FEED_FIELDS = (
        ("title", "title"),
        ("icon", "image"),
        ("home_page_url", "link"),
        ("description", "description"),
    )
    ITEM_FIELDS = (
        ("title", "title"),
        ("id", "guid"),
        ("url", "link"),
        ("summary", "summary"),
        ("external_url", "source"),
    )

    def __init__(self, baseuri=None, baselang=None, encoding=None):
        self.baseuri = baseuri or ""
        self.lang = baselang or None
        self.encoding = encoding or "utf-8"  # character encoding
        self.resolve_relative_uris = True
        self.sanitize_html = True

        self.version = None
        self.feeddata = FeedParserDict()
        self.namespacesInUse = []
        self.entries = []
        # The first non-fatal error encountered while parsing, if any.
        # Fatal errors are raised and caught by feedparser.parse().
        self.bozo_exception = None

    def feed(self, file):
        data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("JSON feed document must be a JSON object")

        v = data.get("version", "")
        if not isinstance(v, str):
            raise ValueError("JSON feed 'version' must be a string")
        try:
            self.version = self.VERSIONS[v]
        except KeyError:
            raise ValueError("Unrecognized JSONFeed version '%s'" % v)

        language = self._get_language(data, "feed")
        if language is None:
            language = self.lang
        if language:
            self.feeddata["language"] = language
        self.lang = language

        for src, dst in self.FEED_FIELDS:
            if src in data:
                value = self._get_string(data, src, "feed")
                if value is not None:
                    self.feeddata[dst] = value
        self.parse_authors(data, self.feeddata, "feed")
        self._copy_extensions(data, self.feeddata)
        # TODO: hubs; expired has no RSS equivalent

        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("JSON feed 'items' must be an array")
        self.entries = []
        for item in items:
            if not isinstance(item, dict):
                self._error("item must be a JSON object")
                continue
            self.entries.append(self.parse_entry(item))

    def parse_entry(self, e):
        entry = FeedParserDict()

        language = self._get_language(e, "item")
        if language is None:
            language = self.lang
        if language:
            entry["language"] = language

        for src, dst in self.ITEM_FIELDS:
            if src in e:
                value = self._get_string(e, src, "item")
                if value is not None:
                    entry[dst] = value

        content = []
        if "content_text" in e:
            value = self._get_string(e, "content_text", "item")
            if value is not None:
                content.append(
                    FeedParserDict(
                        value=value,
                        type="text/plain",
                        language=language,
                        base=self.baseuri,
                    )
                )
        if "content_html" in e:
            value = self._get_string(e, "content_html", "item")
            if value is not None:
                if self.resolve_relative_uris:
                    value = resolve_relative_uris(
                        value, self.baseuri, self.encoding, "text/html"
                    )
                if self.sanitize_html:
                    value = sanitize_html(value, self.encoding, "text/html")
                content.append(
                    FeedParserDict(
                        value=value,
                        type="text/html",
                        language=language,
                        base=self.baseuri,
                    )
                )
        if content:
            entry["content"] = content

        if "date_published" in e:
            value = self._get_string(e, "date_published", "item")
            if value is not None:
                entry["published"] = value
                entry["published_parsed"] = _parse_date(value)
        if "date_modified" in e:
            value = self._get_string(e, "date_modified", "item")
            if value is not None:
                entry["updated"] = value
                entry["updated_parsed"] = _parse_date(value)

        if "tags" in e:
            if isinstance(e["tags"], list):
                entry["category"] = e["tags"]
            else:
                self._error("item 'tags' must be an array")

        self.parse_authors(e, entry, "item")
        if "authors" not in entry and "authors" in self.feeddata:
            # Items without authors inherit the feed authors.
            for key in ("authors", "author", "author_detail"):
                if key in self.feeddata:
                    entry[key] = self.feeddata[key]

        if "attachments" in e:
            if isinstance(e["attachments"], list):
                for attachment in e["attachments"]:
                    enclosure = self.parse_attachment(attachment)
                    if enclosure is not None:
                        entry.setdefault("links", []).append(enclosure)
            else:
                self._error("item 'attachments' must be an array")

        self._copy_extensions(e, entry)
        return entry

    def parse_authors(self, parent, dest, where):
        # JSON Feed 1.1 "authors" takes precedence over the JSON Feed 1
        # single "author". An explicitly empty "authors" list is kept empty.
        details = None
        if "authors" in parent:
            if isinstance(parent["authors"], list):
                details = []
                for author in parent["authors"]:
                    detail = self.parse_author(author, where)
                    if detail is not None:
                        details.append(detail)
            else:
                self._error("%s 'authors' must be an array" % where)
        elif "author" in parent:
            detail = self.parse_author(parent["author"], where)
            if detail is not None:
                details = [detail]
        if details is None:
            return
        dest["authors"] = details
        if details:
            first = details[0]
            dest["author_detail"] = first
            name = first.get("name")
            email = first.get("email")
            if name and email:
                dest["author"] = "%s (%s)" % (name, email)
            elif name:
                dest["author"] = name
            elif email:
                dest["author"] = email

    def parse_author(self, author, where):
        if not isinstance(author, dict):
            self._error("%s author must be a JSON object" % where)
            return None
        detail = FeedParserDict()
        name = self._get_string(author, "name", "%s author" % where)
        if name is not None:
            detail["name"] = name
        url = self._get_string(author, "url", "%s author" % where)
        if url is not None:
            if url.startswith("mailto:"):
                detail["email"] = url[7:]
            else:
                detail["href"] = url
        avatar = self._get_string(author, "avatar", "%s author" % where)
        if avatar is not None:
            detail["avatar"] = avatar
        self._copy_extensions(author, detail)
        return detail

    def parse_attachment(self, attachment):
        if not isinstance(attachment, dict):
            self._error("attachment must be a JSON object")
            return None
        enc = FeedParserDict()
        url = self._get_string(attachment, "url", "attachment")
        mime_type = self._get_string(attachment, "mime_type", "attachment")
        if url is None or mime_type is None:
            self._error("attachment requires 'url' and 'mime_type' strings")
            return None
        enc["rel"] = "enclosure"
        enc["href"] = url
        enc["type"] = mime_type
        title = self._get_string(attachment, "title", "attachment")
        if title is not None:
            enc["title"] = title
        if "size_in_bytes" in attachment:
            size = attachment["size_in_bytes"]
            if isinstance(size, int) and not isinstance(size, bool):
                enc["length"] = size
            else:
                self._error("attachment 'size_in_bytes' must be a number")
        if "duration_in_seconds" in attachment:
            duration = attachment["duration_in_seconds"]
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                enc["duration_in_seconds"] = duration
            else:
                self._error("attachment 'duration_in_seconds' must be a number")
        self._copy_extensions(attachment, enc)
        return enc

    def _get_string(self, data, key, where):
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            self._error("%s '%s' must be a string" % (where, key))
            return None
        return value

    def _get_language(self, data, where):
        language = self._get_string(data, "language", where)
        if language is not None:
            language = language.replace("_", "-")
        return language

    def _error(self, message):
        if self.bozo_exception is None:
            self.bozo_exception = ValueError("JSON feed error: %s" % message)

    @staticmethod
    def _copy_extensions(source, dest):
        # JSON Feed extensions are custom keys starting with an underscore.
        # They are kept at the level they appear on and must not overwrite
        # standard fields.
        for key, value in source.items():
            if key.startswith("_") and key not in dest:
                dest[key] = value

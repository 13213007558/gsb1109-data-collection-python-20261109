from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, NamedTuple

from ._urlpattern import _PREFIX, _can_fetch, _FlatRule, _matches, _URLPattern
from ._utils import _hexescape, _parse_time_period, _quote_path, _quote_pattern

if TYPE_CHECKING:
    from datetime import time

    from ._protego import Protego


logger = logging.getLogger(__name__)


class RequestRate(NamedTuple):
    requests: int
    seconds: int
    start_time: time | None
    end_time: time | None


class VisitTime(NamedTuple):
    start_time: time
    end_time: time


class _Rule(NamedTuple):
    field: str
    value: _URLPattern
    # Provenance, filled in at parse time: the 1-based line of the
    # robots.txt the rule comes from and the directive value as written.
    # Both are None for rules created outside the parser.
    line: int | None = None
    pattern: str | None = None


class _RuleSet:
    """Internal class which stores rules for a user agent."""

    def __init__(self, parser_instance: Protego):
        self.user_agent: str | None = None
        self._rules: list[_Rule] = []
        self._plain_prefixes: tuple[str, ...] | None = None
        self._flat_rules: list[_FlatRule] = []
        self._special_rules: list[_FlatRule] = []
        self._crawl_delay: float | None = None
        self._req_rate: RequestRate | None = None
        self._visit_time: VisitTime | None = None
        self._parser_instance: Protego = parser_instance
        # Lines of the "User-agent" directives this rule set was declared
        # by; more than one when same-named groups are merged.
        self.ua_lines: list[int] = []

    def applies_to(self, robotname: str) -> int:
        """Return matching score."""
        assert self.user_agent is not None
        robotname = robotname.strip().lower()
        if self.user_agent == "*":
            return 1
        # Match the product token only at a token boundary. This avoids
        # false positives such as "bot" matching "mybot", while still
        # matching a token within a full User-Agent header, e.g.
        # "Mozilla/5.0 (compatible; Foobot/1.0)" matching "foobot".
        index = robotname.find(self.user_agent)
        while index != -1:
            if index == 0 or not (
                robotname[index - 1].isalnum() or robotname[index - 1] in "-_"
            ):
                return len(self.user_agent)
            index = robotname.find(self.user_agent, index + 1)
        return 0

    def _url_pattern(self, pattern: str) -> _URLPattern:
        """Return the URL pattern for a directive value.

        The same value usually appears once per user agent in a robots.txt,
        so patterns are shared across the rule sets of a parser.
        """
        cache = self._parser_instance._url_patterns
        if pattern not in cache:
            cache[pattern] = _URLPattern(_quote_pattern(pattern))
        return cache[pattern]

    def allow(self, pattern: str, line: int | None = None) -> None:
        if "$" in pattern:
            self.allow(pattern.replace("$", _hexescape("$")), line=line)

        url_pattern = self._url_pattern(pattern)
        self._rules.append(
            _Rule(field="allow", value=url_pattern, line=line, pattern=pattern)
        )

        # If index.html is allowed, we interpret this as / being allowed too.
        page = "index.html"
        quoted = url_pattern._pattern
        if quoted.endswith(f"/{page}"):
            # Add the rule directly; going through allow() would treat the
            # "$" anchor as a literal dollar sign too.
            self._rules.append(
                _Rule(
                    field="allow",
                    value=_URLPattern(quoted.removesuffix(page) + "$"),
                    line=line,
                    pattern=pattern,
                )
            )

    def disallow(self, pattern: str, line: int | None = None) -> None:
        if "$" in pattern:
            self.disallow(pattern.replace("$", _hexescape("$")), line=line)

        url_pattern = self._url_pattern(pattern)
        self._rules.append(
            _Rule(field="disallow", value=url_pattern, line=line, pattern=pattern)
        )

    def finalize_rules(self) -> None:
        self._rules.sort(
            key=lambda r: (r.value.priority, r.field == "allow"), reverse=True
        )

    def _build_index(self) -> tuple[str, ...]:
        """Flatten the rules for the match loop, and split out the patterns
        that can only match as a prefix of a URL and the rules that can match
        otherwise.

        Built on the first match rather than at parse time because a
        robots.txt declares many rule sets and a crawler queries one.
        """
        prefixes = []
        flat_rules = []
        special = []
        for rule in self._rules:
            pattern = rule.value
            flat_rule = (pattern.kind, pattern.data, rule.field == "disallow")
            flat_rules.append(flat_rule)
            if pattern.kind == _PREFIX:
                prefixes.append(pattern._pattern)
            else:
                special.append(flat_rule)
        self._flat_rules = flat_rules
        self._special_rules = special
        self._plain_prefixes = tuple(prefixes)
        return self._plain_prefixes

    def can_fetch(self, url: str) -> bool:
        """Return if the url can be fetched."""
        url = _quote_path(url)
        # A plain pattern matches only as a prefix of the URL, so a single
        # startswith over all of them rules every one of them out at once.
        prefixes = self._plain_prefixes
        if prefixes is None:
            prefixes = self._build_index()
        rules = self._flat_rules if url.startswith(prefixes) else self._special_rules
        return _can_fetch(rules, url)

    def matching_rules(self, url: str) -> tuple[int, list[int]] | None:
        """Return the index of the rule that decides *url* and the indices
        of the other rules that tie with it (same priority, also matching),
        or None when no rule matches.

        Uses the same per-rule predicate as can_fetch, so the deciding rule
        reported here is always the one can_fetch applies. Ties happen with
        duplicate rules and with same-length Allow/Disallow pairs, where the
        Allow sorts first and wins.
        """
        url = _quote_path(url)
        if self._plain_prefixes is None:
            self._build_index()
        flat_rules = self._flat_rules
        winner = None
        for index, (kind, data, _) in enumerate(flat_rules):
            if _matches(kind, data, url):
                winner = index
                break
        if winner is None:
            return None
        # Rules are sorted by descending priority, so ties are contiguous.
        priority = self._rules[winner].value.priority
        tied = []
        for index in range(winner + 1, len(flat_rules)):
            if self._rules[index].value.priority != priority:
                break
            kind, data, _ = flat_rules[index]
            if _matches(kind, data, url):
                tied.append(index)
        return winner, tied

    @property
    def crawl_delay(self) -> float | None:
        """Get & set crawl delay for the rule set."""
        return self._crawl_delay

    @crawl_delay.setter
    def crawl_delay(self, delay: str) -> None:
        try:
            parsed_delay = float(delay)
        except ValueError:
            parsed_delay = None
        if parsed_delay is None or not math.isfinite(parsed_delay) or parsed_delay < 0:
            # Value is malformed, do nothing.
            logger.debug(
                f"Malformed rule at line {self._parser_instance._total_line_seen} : "
                f"cannot set crawl delay to '{delay}'. Ignoring this rule."
            )
            return
        self._crawl_delay = parsed_delay

    @property
    def request_rate(self) -> RequestRate | None:
        """Get & set request rate for the rule set."""
        return self._req_rate

    @request_rate.setter
    def request_rate(self, value: str) -> None:
        try:
            parts = value.split()
            if len(parts) == 2:
                rate, time_period = parts
            else:
                rate, time_period = parts[0], ""

            requests_str, seconds_str = rate.split("/")
            time_unit = seconds_str[-1].lower()
            if time_unit in ("s", "m", "h", "d"):
                seconds = int(seconds_str[:-1])
            else:
                time_unit = "s"
                seconds = int(seconds_str)
            requests = int(requests_str)

            if requests <= 0 or seconds <= 0:
                raise ValueError(f"Request rate must be positive: {value!r}")

            if time_unit == "m":
                seconds *= 60
            elif time_unit == "h":
                seconds *= 3600
            elif time_unit == "d":
                seconds *= 86400

            start_time = None
            end_time = None
            if time_period:
                start_time, end_time = _parse_time_period(time_period)
        except Exception:  # noqa: BLE001
            # Value is malformed, do nothing.
            logger.debug(
                f"Malformed rule at line {self._parser_instance._total_line_seen} : "
                f"cannot set request rate using '{value}'. Ignoring this rule."
            )
            return

        self._req_rate = RequestRate(requests, seconds, start_time, end_time)

    @property
    def visit_time(self) -> VisitTime | None:
        """Get & set visit time for the rule set."""
        return self._visit_time

    @visit_time.setter
    def visit_time(self, value: str) -> None:
        try:
            try:
                # "0400-0845"
                start_time, end_time = _parse_time_period(value)
            except ValueError:
                # "0400 0845"
                start_time, end_time = _parse_time_period(value, separator=" ")
        except Exception:  # noqa: BLE001
            logger.debug(
                f"Malformed rule at line {self._parser_instance._total_line_seen} : "
                f"cannot set visit time using '{value}'. Ignoring this rule."
            )
            return
        self._visit_time = VisitTime(start_time, end_time)

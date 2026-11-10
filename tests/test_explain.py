import json

import pytest

from protego import Protego

# Each case is (robots.txt content, url, user_agent). The explanation's
# final verdict must agree with can_fetch for every one of them.
CASES = [
    # No user agent group matches.
    ("User-agent: FooBot\nDisallow: /", "https://ex.local/a", "BarBot"),
    # Empty robots.txt: no groups at all.
    ("", "https://ex.local/a", "BarBot"),
    # A group applies, but no rule matches the path.
    ("User-agent: *\nDisallow: /private", "https://ex.local/public", "BarBot"),
    # Group with no rules at all.
    ("User-agent: *", "https://ex.local/anything", "BarBot"),
    # Disallow wins.
    ("User-agent: *\nDisallow: /private", "https://ex.local/private/x", "*"),
    # Allow wins by being longer.
    (
        "User-agent: *\nDisallow: /\nAllow: /page",
        "https://ex.local/page",
        "*",
    ),
    # Same-length Allow/Disallow conflict: Allow wins.
    (
        "User-agent: *\nDisallow: /x\nAllow: /x",
        "https://ex.local/x",
        "*",
    ),
    # Duplicate rules.
    (
        "User-agent: *\nDisallow: /x\nDisallow: /x",
        "https://ex.local/x",
        "*",
    ),
    # Longest user agent match wins over the wildcard group.
    (
        "User-agent: *\nDisallow: /\nUser-agent: FooBot\nAllow: /",
        "https://ex.local/a",
        "Mozilla/5.0 (compatible; FooBot/1.0)",
    ),
    # Exact ($-anchored) and wildcard patterns.
    ("User-agent: *\nDisallow: /*.php$", "https://ex.local/a.php", "*"),
    ("User-agent: *\nDisallow: /*.php$", "https://ex.local/a.php?x=1", "*"),
    ("User-agent: *\nDisallow: /a*/b", "https://ex.local/a123/b", "*"),
    # Percent-encoded and non-ASCII paths.
    (
        "User-agent: *\nDisallow: /%C3%A9",
        "https://ex.local/\u00e9",
        "*",
    ),
    (
        "User-agent: *\nDisallow: /\u00e9",
        "https://ex.local/%C3%A9",
        "*",
    ),
    # robots.txt itself is always allowed.
    ("User-agent: *\nDisallow: /", "https://ex.local/robots.txt", "*"),
    # Merged same-name groups.
    (
        "User-agent: FooBot\nDisallow: /a\n"
        "User-agent: BarBot\nDisallow: /b\n"
        "User-agent: FooBot\nDisallow: /c",
        "https://ex.local/c",
        "FooBot",
    ),
    # BOM, comments and blank lines must not shift line numbers.
    (
        "\ufeff# leading comment\n\nUser-agent: * # inline\nDisallow: /x",
        "https://ex.local/x",
        "*",
    ),
]


@pytest.mark.parametrize(("content", "url", "user_agent"), CASES)
def test_explain_agrees_with_can_fetch(content, url, user_agent):
    rp = Protego.parse(content=content)
    explanation = rp.explain(url, user_agent)
    assert explanation["allowed"] == rp.can_fetch(url, user_agent)
    # The reason must be consistent with the verdict.
    if explanation["reason"] == "disallow":
        assert not explanation["allowed"]
    else:
        assert explanation["allowed"]
    # The whole explanation must be JSON serializable.
    json.dumps(explanation)


def test_explain_no_matching_group():
    rp = Protego.parse("User-agent: FooBot\nDisallow: /")
    explanation = rp.explain("https://ex.local/a", "BarBot")
    assert explanation["allowed"] is True
    assert explanation["reason"] == "no_matching_group"
    assert explanation["selected_user_agent"] is None
    assert explanation_group_tuples(explanation) == [("foobot", 0, False)]
    assert explanation["winning_rule"] is None
    assert explanation["tied_rules"] == []


def test_explain_no_matching_rule():
    rp = Protego.parse("User-agent: *\nDisallow: /private")
    explanation = rp.explain("https://ex.local/public", "SomeBot")
    assert explanation["allowed"] is True
    assert explanation["reason"] == "no_matching_rule"
    assert explanation["selected_user_agent"] == "*"
    assert explanation["winning_rule"] is None


def test_explain_allow_wins():
    rp = Protego.parse("User-agent: *\nDisallow: /\nAllow: /page")
    explanation = rp.explain("https://ex.local/page", "SomeBot")
    assert explanation["allowed"] is True
    assert explanation["reason"] == "allow"
    assert explanation["winning_rule"]["field"] == "allow"
    assert explanation["winning_rule"]["line"] == 3
    assert explanation["winning_rule"]["source"] == "Allow: /page"


def test_explain_disallow_wins():
    rp = Protego.parse("User-agent: *\nAllow: /p\nDisallow: /private")
    explanation = rp.explain("https://ex.local/private/x", "SomeBot")
    assert explanation["allowed"] is False
    assert explanation["reason"] == "disallow"
    assert explanation["winning_rule"]["field"] == "disallow"
    assert explanation["winning_rule"]["line"] == 3


def test_explain_user_agent_group_selection():
    content = "User-agent: *\nDisallow: /\nUser-agent: FooBot\nAllow: /\n"
    rp = Protego.parse(content)
    ua = "Mozilla/5.0 (compatible; FooBot/1.0)"
    explanation = rp.explain("https://ex.local/a", ua)
    # The longest match is selected; the wildcard group took part but lost.
    assert explanation["selected_user_agent"] == "foobot"
    groups = {group["user_agent"]: group for group in explanation["user_agent_groups"]}
    assert groups["foobot"]["score"] == len("foobot")
    assert groups["foobot"]["selected"] is True
    assert groups["foobot"]["lines"] == [3]
    assert groups["*"]["score"] == 1
    assert groups["*"]["selected"] is False
    # The deciding rule comes from the selected group, not the wildcard one.
    assert explanation["winning_rule"]["line"] == 4


def test_explain_merged_groups_keep_their_own_lines():
    content = (
        "User-agent: FooBot\n"  # line 1
        "Disallow: /a\n"  # line 2
        "User-agent: BarBot\n"  # line 3
        "Disallow: /b\n"  # line 4
        "User-agent: FooBot\n"  # line 5
        "Disallow: /c"  # line 6
    )
    rp = Protego.parse(content)
    explanation = rp.explain("https://ex.local/c", "FooBot")
    assert explanation["allowed"] is False
    # Both declarations of the merged group are reported.
    (group,) = [
        g for g in explanation["user_agent_groups"] if g["user_agent"] == "foobot"
    ]
    assert group["lines"] == [1, 5]
    # The deciding rule points at the second declaration block.
    assert explanation["winning_rule"]["line"] == 6
    assert explanation["winning_rule"]["source"] == "Disallow: /c"


def test_explain_same_length_allow_disallow_conflict():
    content = "User-agent: *\nDisallow: /x\nAllow: /x"
    rp = Protego.parse(content)
    explanation = rp.explain("https://ex.local/x", "SomeBot")
    assert explanation["allowed"] is True
    assert explanation["reason"] == "allow"
    assert explanation["winning_rule"]["field"] == "allow"
    # The losing Disallow of the same length is reported as a tie.
    assert len(explanation["tied_rules"]) == 1
    tied = explanation["tied_rules"][0]
    assert tied["field"] == "disallow"
    assert tied["line"] == 2
    assert tied["source"] == "Disallow: /x"


def test_explain_duplicate_rules():
    content = "User-agent: *\nDisallow: /x\nDisallow: /x"
    rp = Protego.parse(content)
    explanation = rp.explain("https://ex.local/x", "SomeBot")
    assert explanation["allowed"] is False
    assert explanation["winning_rule"]["line"] == 2
    # The identical later rule is reported as a tie with its own line.
    assert len(explanation["tied_rules"]) == 1
    assert explanation["tied_rules"][0]["line"] == 3
    assert explanation["tied_rules"][0]["field"] == "disallow"


def test_explain_bom_comments_and_blank_lines_keep_line_numbers():
    content = (
        "\ufeff# a comment\n"  # line 1
        "\n"  # line 2
        "User-agent: *  # our bot\n"  # line 3
        "Disallow: /x  # keep out"  # line 4
    )
    rp = Protego.parse(content)
    explanation = rp.explain("https://ex.local/x", "SomeBot")
    assert explanation["allowed"] is False
    assert explanation["winning_rule"]["line"] == 4
    # The source is the raw line, comment included.
    assert explanation["winning_rule"]["source"] == "Disallow: /x  # keep out"
    group = explanation["user_agent_groups"][0]
    assert group["lines"] == [3]


def test_explain_normalized_paths():
    content = "User-agent: *\nDisallow: /%C3%A9t\u00e9"
    rp = Protego.parse(content)
    explanation = rp.explain("https://ex.local/\u00e9t\u00e9", "SomeBot")
    assert explanation["allowed"] is False
    assert explanation["normalized_path"] == "/%C3%A9t%C3%A9"
    assert explanation["winning_rule"]["pattern"] == "/%C3%A9t\u00e9"
    assert explanation["winning_rule"]["normalized_pattern"] == "/%C3%A9t%C3%A9"


def test_explain_robots_txt_itself():
    rp = Protego.parse("User-agent: *\nDisallow: /")
    explanation = rp.explain("https://ex.local/robots.txt", "SomeBot")
    assert explanation["allowed"] is True
    assert explanation["reason"] == "robots_txt"


def test_explain_short_example_pinpoints_the_deciding_line():
    # A rejected URL as support would report it: from the returned object
    # alone one can find the line of the robots.txt that decided it.
    content = (
        "User-agent: *\nDisallow: /tmp/\nDisallow: /private/\nAllow: /private/public\n"
    )
    rp = Protego.parse(content)
    explanation = rp.explain("https://shop.example/private/orders", "MyBot")
    assert explanation["allowed"] is False
    winning = explanation["winning_rule"]
    # "line" and "source" point at the exact line of the original file.
    assert content.splitlines()[winning["line"] - 1] == winning["source"]
    assert winning["source"] == "Disallow: /private/"
    assert winning["pattern"] in winning["source"]


def explanation_group_tuples(explanation):
    return [
        (group["user_agent"], group["score"], group["selected"])
        for group in explanation["user_agent_groups"]
    ]

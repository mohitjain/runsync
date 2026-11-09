import pytest

from runsync.resolve.parse import (
    channel_name,
    clean_title,
    is_label,
    key,
    parse,
    split_featured,
    split_names,
    split_remaster,
    strip_junk_groups,
    tidy_case,
    versions_in,
)


@pytest.mark.parametrize("raw,expected", [
    ("Song (Official Video)", "Song"),
    ("Song [4K] (Lyrics)", "Song"),
    ("Song (Live at Wembley)", "Song (Live at Wembley)"),   # real information stays
    ("Song {full song}", "Song"),
])
def test_strip_junk_groups(raw, expected):
    assert strip_junk_groups(raw) == expected


def test_clean_title_keeps_titles_that_end_in_song():
    assert clean_title("The Humma Song")[0] == "The Humma Song"
    assert clean_title("Hairat Full Video")[0] == "Hairat"


def test_featured_split_keeps_hyphenated_names():
    assert split_featured("Give Me Everything ft. Ne-Yo, Afrojack") == ("Give Me Everything", ["Ne-Yo", "Afrojack"])
    assert split_featured("Titanium (feat. Sia)") == ("Titanium", ["Sia"])


def test_remaster_tail_is_removed_with_year_hint():
    assert split_remaster("Hotel California (2013 Remaster)") == ("Hotel California", 2013, True)
    assert split_remaster("Plain Song") == ("Plain Song", None, False)


def test_split_names_does_not_break_band_names_with_ampersand():
    assert split_names("Above & Beyond") == ["Above & Beyond"]
    assert split_names("Vishal Dadlani & Sunidhi Chauhan", amp=True) == ["Vishal Dadlani", "Sunidhi Chauhan"]
    assert split_names("A | B, a, Backing Vocals", strict=True) == ["A", "B"]


def test_tidy_case_only_touches_shouty_names():
    assert tidy_case("GURU RANDHAWA") == "Guru Randhawa"
    assert tidy_case("AC/DC") == "AC/DC"
    assert tidy_case("ABBA") == "ABBA"
    assert tidy_case("Hello World") == "Hello World"


def test_channel_and_label_helpers():
    assert channel_name("Maria Levinson - Topic") == "Maria Levinson"
    assert is_label("T-Series") and is_label("Gully Boy PKG") and not is_label("Nirvana")
    assert key("Guns N' Roses!") == "gunsnroses"


def test_version_markers_found():
    assert set(versions_in("Song (Live) Acoustic Remix")) == {"live", "acoustic", "remix"}
    assert versions_in("Plain") == []


def test_artist_title_order_follows_channel_when_it_disambiguates():
    # "Song - Artist" and the channel is the artist
    c = parse("Zombie - The Cranberries", "The Cranberries")
    assert (c.artists, c.title) == (["The Cranberries"], "Zombie")
    # no channel hint: default "Artist - Title", with the other reading kept for lookups
    c = parse("Metallica - One", "Some Label Records")
    assert (c.artists, c.title, c.alts) == (["Metallica"], "One", ["Metallica"])


def test_pipe_style_never_invents_an_artist():
    c = parse("Song | Singer One | Actor Two | Latest Punjabi Songs", "T-Series")
    assert c.artists == [] and c.title == "Song" and c.hints == ["Singer One", "Actor Two"]
    assert c.confidence < 0.75


def test_hashtags_and_prefixes_are_dropped():
    c = parse("OFFICIAL: 'Gallan' Video #tag1 #tag2", "Label Music")
    assert c.title == "Gallan"

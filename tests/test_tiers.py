"""Lookup tiers against canned MusicBrainz / iTunes answers."""
from runsync.resolve.model import Candidate
from runsync.resolve.tiers import itunes_tier, musicbrainz_tier, youtube_tier


class Canned:
    def __init__(self, mb=None, itunes=None):
        self.mb, self.itunes = mb or [], itunes or []

    def get_json(self, base, params):
        return {"recordings": self.mb} if "musicbrainz" in base else {"results": self.itunes}


def rec(title, artist, length_ms=250000, release="Some Album", date="2010-05-01"):
    return {"id": "rid", "title": title, "length": length_ms, "score": 100,
            "artist-credit": [{"name": artist, "artist": {"name": artist}}],
            "releases": [{"id": "relid", "title": release, "date": date,
                          "release-group": {"primary-type": "Album", "secondary-types": []}}]}


def base(title="Hairat", artists=None, hints=None, album=None):
    return Candidate(tier="parser", title=title, artists=artists or [], hints=hints or [], album=album,
                     confidence=0.4)


def test_hinted_singer_corroborates_even_when_video_is_shorter_than_the_album_track():
    c = musicbrainz_tier(Canned(mb=[rec("Hairat", "Lucky Ali", 249000)]),
                         base(hints=["Ranbir Kapoor", "Lucky Ali"]), duration_s=158.7)
    assert c and c.artists == ["Lucky Ali"] and c.tier == "musicbrainz" and c.confidence >= 0.75
    assert c.extra["mb_recording_id"] == "rid" and c.year == 2010 and c.album == "Some Album"


def test_hinted_film_name_in_the_release_title_corroborates():
    c = musicbrainz_tier(Canned(mb=[rec("Abhi Kuch Dino Se", "Mohit Chauhan", 283000, "Dil Toh Baccha Hai Ji")]),
                         base("Abhi Kuch Dino Se", hints=["Dil Toh Baccha Hai Ji", "Emraan Hashmi"]), 250)
    assert c and c.artists == ["Mohit Chauhan"]


def test_title_match_without_any_corroboration_is_rejected():
    assert musicbrainz_tier(Canned(mb=[rec("Hairat", "Somebody Else")]), base(hints=["Ranbir Kapoor"]), 158) is None


def test_wrong_title_is_rejected_even_with_the_right_artist():
    assert musicbrainz_tier(Canned(mb=[rec("Totally Different", "Lucky Ali")]), base(hints=["Lucky Ali"]), 158) is None


def test_wanted_artist_must_match():
    good = musicbrainz_tier(Canned(mb=[rec("One", "Metallica", 447000)]), base("One", ["Metallica"]), 446)
    bad = musicbrainz_tier(Canned(mb=[rec("One", "U2", 447000)]), base("One", ["Metallica"]), 446)
    assert good and good.confidence > 0.9 and bad is None


def test_curly_apostrophes_from_musicbrainz_are_normalised():
    c = musicbrainz_tier(Canned(mb=[rec("Don’t Cry", "Guns N’ Roses", 284000)]),
                         base("Don't Cry", ["Guns N' Roses"]), 283)
    assert c.artists == ["Guns N' Roses"] and c.title == "Don't Cry"


def test_itunes_ampersand_credit_is_split_and_from_suffix_cleaned():
    res = [{"trackName": "Hairat (From \"Anjaana Anjaani\")", "artistName": "Lucky Ali & Vishal & Shekhar",
            "collectionName": "Anjaana Anjaani", "releaseDate": "2010-08-01T07:00:00Z", "trackTimeMillis": 247000,
            "primaryGenreName": "Bollywood", "artworkUrl100": "https://x/100x100bb.jpg"}]
    c = itunes_tier(Canned(itunes=res), base("Hairat", hints=["Lucky Ali", "Anjaana Anjaani"]), 158)
    assert c and c.artists[0] == "Lucky Ali" and c.year == 2010 and c.extra["genre"] == "Bollywood"
    assert c.extra["artwork"].endswith("600x600bb.jpg")


def test_youtube_tier_prefers_topic_channel_lead_only_for_credit_piles():
    row = {"yt_track": "Song", "yt_artist": "A, B, C, D, E", "yt_album": "Alb", "yt_release_year": 2001,
           "channel": "Lead - Topic"}
    assert youtube_tier(row).artists == ["Lead"]
    row["yt_artist"] = "Lead, Other"
    assert youtube_tier(row).artists == ["Lead", "Other"]
    assert youtube_tier({**row, "yt_track": None}) is None


def test_strip_movie_handles_quoted_and_unquoted_from_suffix():
    from runsync.resolve.tiers import _strip_movie
    assert _strip_movie('Blue Eyes (From "Blue Eyes")') == ("Blue Eyes", "Blue Eyes")
    assert _strip_movie("Lahore (From Lahore)") == ("Lahore", "Lahore")
    assert _strip_movie("Song (Live)") == ("Song (Live)", None)


def test_musicbrainz_title_loses_the_from_film_suffix():
    c = musicbrainz_tier(Canned(mb=[rec('Lahore (From "Lahore")', "Guru Randhawa", 190000, "Lahore")]),
                         base("Lahore", artists=["Guru Randhawa"]), duration_s=190)
    assert c and c.title == "Lahore"

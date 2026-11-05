import pytest

from snipmint.errors import IngestError
from snipmint.ingest.guard import source_id_for, youtube_video_id

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&t=42s",
        f"https://www.youtube.com/watch?v={VID}&list=PL123",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"youtube.com/watch?v={VID}",
    ],
)
def test_accepts_single_video_links(url):
    assert youtube_video_id(url) == VID


@pytest.mark.parametrize(
    "url,fragment",
    [
        ("", "Paste"),
        ("https://vimeo.com/123", "Only YouTube"),
        ("http://127.0.0.1/watch?v=" + VID, "Only YouTube"),
        ("https://www.youtube.com/playlist?list=PL123", "playlist"),
        ("https://www.youtube.com/watch?list=PL123", "playlist"),
        ("https://www.youtube.com/@somechannel", "channel"),
        ("https://www.youtube.com/results?search_query=x", "search"),
        ("https://www.youtube.com/watch?v=short", "Couldn't find"),
        ("ftp://youtube.com/watch?v=" + VID, "http"),
    ],
)
def test_rejects_everything_else(url, fragment):
    with pytest.raises(IngestError, match=fragment):
        youtube_video_id(url)


def test_source_id_is_filesystem_safe():
    assert source_id_for(VID) == f"yt-{VID}"

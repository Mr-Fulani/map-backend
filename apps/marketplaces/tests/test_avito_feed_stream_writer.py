import hashlib
import io

import pytest

from apps.marketplaces.adapters.avito.feed_builder import (
    FeedPayloadSizeExceeded,
    build_feed,
    write_feed,
)


EMPTY_FEED = (
    b"<?xml version='1.0' encoding='UTF-8'?>\n"
    b'<Ads formatVersion="3" target="Avito.ru">\n'
    b'</Ads>'
)


def test_sink_writer_preserves_legacy_bytes_and_returns_exact_metadata():
    sink = io.BytesIO()

    result = write_feed([], sink, max_bytes=len(EMPTY_FEED))

    assert sink.getvalue() == EMPTY_FEED
    assert build_feed([]) == EMPTY_FEED
    assert result.listing_count == 0
    assert result.size_bytes == len(EMPTY_FEED)
    assert result.payload_sha256 == hashlib.sha256(EMPTY_FEED).hexdigest()


def test_sink_writer_fails_before_crossing_explicit_byte_ceiling():
    sink = io.BytesIO()

    with pytest.raises(FeedPayloadSizeExceeded):
        write_feed([], sink, max_bytes=len(EMPTY_FEED) - 1)

    assert len(sink.getvalue()) <= len(EMPTY_FEED) - 1


def test_sink_writer_rejects_partial_binary_writes():
    class PartialSink:
        def write(self, chunk):
            return max(0, len(chunk) - 1)

    with pytest.raises(OSError, match='complete byte chunk'):
        write_feed([], PartialSink())


@pytest.mark.parametrize('invalid_cap', (True, 0, -1, 1.5, '1024'))
def test_sink_writer_rejects_ambiguous_byte_ceiling(invalid_cap):
    with pytest.raises(ValueError, match='positive integer'):
        write_feed([], io.BytesIO(), max_bytes=invalid_cap)

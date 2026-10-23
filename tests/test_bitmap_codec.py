import pytest
from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec


def test_round_trip_preserves_sorted_members_and_upper_serial_range() -> None:
    codec = BitmapCodec()
    members = [2_147_483_647, 42, 1, 42]
    encoded = codec.encode(members)
    decoded = codec.decode(encoded)
    assert list(decoded) == [1, 42, 2_147_483_647]
    assert 2_147_483_647 in decoded
    assert 2_147_483_646 not in decoded


@pytest.mark.parametrize("member", [-1, 2_147_483_648])
def test_encode_rejects_ids_outside_postgres_serial_domain(member: int) -> None:
    with pytest.raises(ValueError):
        BitmapCodec().encode([member])


def test_decode_rejects_corrupt_bytes() -> None:
    with pytest.raises(ValueError):
        BitmapCodec().decode(b"not a roaring bitmap")


def test_delta_is_deterministic() -> None:
    codec = BitmapCodec()
    previous = BitMap([1, 2, 4])
    current = BitMap([2, 3, 4])
    assert list(codec.added(previous, current)) == [3]
    assert list(codec.removed(previous, current)) == [1]

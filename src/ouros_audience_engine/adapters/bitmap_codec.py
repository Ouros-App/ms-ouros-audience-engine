from pyroaring import BitMap

MAX_FARM_OWNER_ID = 2_147_483_647


class BitmapCodec:
    format_name = "pyroaring-portable"
    format_version = "1"
    member_id_space = "farm_owner_user_id"

    def encode(self, member_ids: list[int] | set[int]) -> bytes:
        bitmap = BitMap()
        for member_id in member_ids:
            if isinstance(member_id, bool) or not isinstance(member_id, int):
                raise ValueError("member IDs must be integers")
            if member_id <= 0 or member_id > MAX_FARM_OWNER_ID:
                raise ValueError("member ID is outside the positive PostgreSQL SERIAL domain")
            bitmap.add(member_id)
        return bitmap.serialize()

    def decode(self, payload: bytes) -> BitMap:
        try:
            bitmap = BitMap.deserialize(payload)
        except (ValueError, TypeError, RuntimeError) as exc:
            raise ValueError("invalid Roaring bitmap payload") from exc
        if any(member_id <= 0 or member_id > MAX_FARM_OWNER_ID for member_id in bitmap):
            raise ValueError("bitmap contains IDs outside the PostgreSQL SERIAL domain")
        return bitmap

    @staticmethod
    def added(previous: BitMap, current: BitMap) -> BitMap:
        return current - previous

    @staticmethod
    def removed(previous: BitMap, current: BitMap) -> BitMap:
        return previous - current

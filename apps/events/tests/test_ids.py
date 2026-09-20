from apps.events.ids import generate_short_id

BASE62_ALPHABET = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def test_generate_short_id_returns_eight_characters():
    assert len(generate_short_id()) == 8


def test_generate_short_id_uses_only_base62_characters():
    assert set(generate_short_id()) <= BASE62_ALPHABET


def test_generate_short_id_produces_distinct_values_across_many_calls():
    """並非數學上絕對保證不重複(base62^8 仍有機率碰撞),但 1000 次呼叫應該
    完全沒有重複——便宜且有意義的 smoke test,足以驗證產生器不是壞掉/回傳常數。
    """
    ids = {generate_short_id() for _ in range(1000)}
    assert len(ids) == 1000

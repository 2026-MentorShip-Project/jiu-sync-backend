"""人數 → 人數規格字串換算(spec「未選人數時以出席人數帶入」、design.md D9)。

與前端 `partySizeForCount` 一致:≤2 → `2 人`、≤4 → `3-4 人`、≤8 → `5-8 人`、
≤19 → `9 人以上（多人）`、其餘 → `20 人以上（團體）`;0 人 → None(不帶人數)。
"""

import pytest

from apps.recommendations.preferences import party_size_for_count


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, None),
        (1, "2 人"),
        (2, "2 人"),
        (3, "3-4 人"),
        (4, "3-4 人"),
        (5, "5-8 人"),
        (8, "5-8 人"),
        (9, "9 人以上（多人）"),
        (19, "9 人以上（多人）"),
        (20, "20 人以上（團體）"),
        (500, "20 人以上（團體）"),
    ],
)
def test_party_size_for_count(count, expected):
    assert party_size_for_count(count) == expected


def test_party_size_labels_are_valid_request_choices():
    from apps.recommendations.serializers import PARTY_SIZE_CHOICES

    labels = {party_size_for_count(n) for n in (1, 3, 5, 9, 20)}
    assert labels == set(PARTY_SIZE_CHOICES)

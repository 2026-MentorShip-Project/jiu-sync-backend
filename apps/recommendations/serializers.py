"""`POST /api/events/{id}/restaurant-recommendations/` 的請求驗證。

欄位與限制見 specs/restaurant-recommendations/spec.md「偏好條件全部選填」:全部
選填;字串去除前後空白後再驗證,去除後為空視同未填(清單項目則被略過)。
選項值與前端 `src/mocks/aiRecommendDemo.ts` 一致(`partySize` 的括號為全形)。
"""

from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from rest_framework.settings import api_settings

RELATIONSHIP_CHOICES = ["同事", "朋友", "家人", "社團", "約會"]
BUDGET_CHOICES = ["200 以下", "200-400", "400-600", "600-800", "800-1000", "1000 以上"]
PARTY_SIZE_CHOICES = ["2 人", "3-4 人", "5-8 人", "9 人以上（多人）", "20 人以上（團體）"]
SITUATIONAL_CHOICES = ["可久坐", "有插座", "停車位", "親子友善", "無障礙"]
SPICE_CHOICES = ["不吃辣", "愛吃辣"]

LOCATION_MAX_LENGTH = 100
CUSTOM_PROMPT_MAX_LENGTH = 200
DIETARY_LIST_MAX_ITEMS = 5
CUISINE_MAX_LENGTH = 20
RESTRICTION_MAX_LENGTH = 30

# 400 回應 `errors[].code`:每個欄位一個語意化 code(不分是哪一種不合法),
# 比照既有 `config.exceptions.FIELD_CODE_OVERRIDES` 的命名風格。
FIELD_ERROR_CODES = {
    "location": "LOCATION_INVALID",
    "relationship": "RELATIONSHIP_INVALID",
    "budget": "BUDGET_INVALID",
    "partySize": "PARTY_SIZE_INVALID",
    "situational": "SITUATIONAL_INVALID",
    "dietary": "DIETARY_INVALID",
    "dietary.vegetarian": "VEGETARIAN_INVALID",
    "dietary.spice": "SPICE_INVALID",
    "dietary.cuisines": "CUISINES_INVALID",
    "dietary.restrictions": "RESTRICTIONS_INVALID",
    "customPrompt": "CUSTOM_PROMPT_INVALID",
}


class _TrimmedChoiceField(serializers.ChoiceField):
    """去除前後空白後再比對選項;空白字串視同未填(回傳 None)。"""

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = data.strip()
            if data == "":
                return None
        return super().to_internal_value(data)


def _optional_text(max_length):
    # CharField 預設 trim_whitespace=True:先去空白再檢查長度。
    return serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=max_length
    )


def _optional_choice(choices):
    return _TrimmedChoiceField(
        choices=choices, required=False, allow_null=True, allow_blank=True
    )


class _BlankDroppingListField(serializers.ListField):
    """子項目去空白後為空的直接略過;``max_length`` 等 validator 在略過後才檢查
    (spec:清單欄位先去除空白項目,再檢查項目數上限與重複)。"""

    def to_internal_value(self, data):
        return [item for item in super().to_internal_value(data) if item]


def _free_text_list(item_max_length):
    return _BlankDroppingListField(
        child=serializers.CharField(allow_blank=True, max_length=item_max_length),
        required=False,
        allow_null=True,
        max_length=DIETARY_LIST_MAX_ITEMS,
    )


class DietarySerializer(serializers.Serializer):
    vegetarian = serializers.BooleanField(required=False, allow_null=True)
    spice = _optional_choice(SPICE_CHOICES)
    cuisines = _free_text_list(CUISINE_MAX_LENGTH)
    restrictions = _free_text_list(RESTRICTION_MAX_LENGTH)


class RecommendationRequestSerializer(serializers.Serializer):
    location = _optional_text(LOCATION_MAX_LENGTH)
    relationship = _optional_choice(RELATIONSHIP_CHOICES)
    budget = _optional_choice(BUDGET_CHOICES)
    partySize = _optional_choice(PARTY_SIZE_CHOICES)
    situational = _BlankDroppingListField(
        child=_TrimmedChoiceField(choices=SITUATIONAL_CHOICES, allow_blank=True),
        required=False,
        allow_null=True,
    )
    dietary = DietarySerializer(required=False, allow_null=True)
    customPrompt = _optional_text(CUSTOM_PROMPT_MAX_LENGTH)

    def validate_situational(self, value):
        # 空白項目已在 `_BlankDroppingListField` 略過,這裡只檢查重複。
        value = value or []
        if len(set(value)) != len(value):
            raise serializers.ValidationError("情境條件不可重複")
        return value


def flatten_errors(errors, prefix="", code_field=None):
    """把巢狀的 serializer 錯誤攤平成 ``{"dietary.cuisines[1]": [ErrorDetail]}``,
    每個欄位一筆、code 換成 ``FIELD_ERROR_CODES`` 的語意化 code(清單子項目
    ``[i]`` 沿用母欄位的 code)。

    既有 ``config.exceptions._build_errors`` 只展開一層巢狀、且對
    ``ListField`` 子項目的 ``{index: [...]}`` 形狀會略過,直接丟給它會讓
    ``dietary`` 底下多個欄位合併成一筆、或子項目錯誤整個消失——攤平後每筆都是
    一般欄位,``errors`` 才能列出每個不合法欄位。
    """
    flat = {}
    for key, value in errors.items():
        if isinstance(key, int):
            path, field = f"{prefix}[{key}]", code_field
        elif key == api_settings.NON_FIELD_ERRORS_KEY and prefix:
            # 例如 `dietary` 不是物件:錯誤歸屬 `dietary` 本身。
            path = field = prefix
        else:
            path = f"{prefix}.{key}" if prefix else key
            field = path
        if isinstance(value, dict):
            flat.update(flatten_errors(value, path, field))
        else:
            flat[path] = [_with_code(_first_detail(value), field)]
    return flat


def _first_detail(value):
    while isinstance(value, list | dict):
        value = value[0] if isinstance(value, list) else next(iter(value.values()))
    return value


def _with_code(detail, field):
    code = FIELD_ERROR_CODES.get(field, getattr(detail, "code", None))
    return ErrorDetail(str(detail), code=code)


# ---------------------------------------------------------------------------
# PUT /api/events/{id}/selected-restaurant/(design.md D13)
# ---------------------------------------------------------------------------


class _StrictUUIDField(serializers.UUIDField):
    """只接受 JSON 字串。DRF 內建 ``UUIDField`` 會把整數轉成 ``UUID(int=...)``,
    這裡視為型別錯誤。"""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid", value=data)
        return super().to_internal_value(data)


class _StrictCharField(serializers.CharField):
    """只接受 JSON 字串。DRF 內建 ``CharField`` 會把數字轉成字串(``1`` → ``"1"``),
    這裡視為型別錯誤。"""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class RestaurantSelectionSerializer(serializers.Serializer):
    recommendationId = _StrictUUIDField()
    # 空字串不是 body 錯誤:它只是不在推薦結果內 → 400 INVALID_RESTAURANT(D13 第 7 步)。
    # 不去空白:餐廳 id 是機器值,必須與推薦結果完全相同。
    restaurantId = _StrictCharField(allow_blank=True, trim_whitespace=False)

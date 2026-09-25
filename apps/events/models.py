import uuid

from django.conf import settings
from django.db import models

from .ids import generate_short_id


class Slot(models.Model):
    """A candidate date/time option for an :class:`Event`.

    Kept as its own model (not a JSONField on ``Event``) so a future
    participant-response through-model can FK to a specific slot.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("Event", on_delete=models.CASCADE, related_name="slots")
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    label = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self):
        return f"{self.date} {self.time or ''}".strip()


class Event(models.Model):
    """A gathering (揪團) created by a host (主揪) for participants to vote on.

    8-char base62 short id primary key so event identifiers are safe to
    expose in shareable URLs without leaking a sequential, enumerable count
    of events, while staying shorter than a UUID for a cleaner shared link.

    ``finalized_at``/``cancelled_at``/``final_slot``/``final_note`` have no
    write path yet — deliberate schema-ahead fields so a future
    finalize/cancel feature doesn't need its own migration.
    """

    # 沒明確給 label——這個 app 全程走 JWT API,沒有用到 Django admin 或
    # get_FOO_display(),不需要額外維護一份沒人讀的顯示字串;Django 會自動從
    # 成員名生成 label。
    class Mode(models.TextChoices):
        DATE_ONLY = "date_only"
        TIME_SLOTS = "time_slots"

    class Status(models.TextChoices):
        ACTIVE = "active"
        FINALIZED = "finalized"
        CANCELLED = "cancelled"

    id = models.CharField(
        primary_key=True, max_length=8, default=generate_short_id, editable=False
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=30)
    host_nickname = models.CharField(max_length=40)
    host_email = models.EmailField(null=True, blank=True)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    response_deadline = models.DateTimeField()
    location = models.CharField(max_length=200, null=True, blank=True)
    description = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    finalized_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    final_slot = models.ForeignKey(
        Slot, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    final_note = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title


class ParticipantResponse(models.Model):
    """一位參與者對某場 :class:`Event` 的投票——暱稱＋手機末三碼雜湊＋選填
    Email／留言＋每個候選時段的三態表態（見 :class:`ParticipantResponseSlotAvailability`）。

    ``id`` 比照 ``Event.id`` 用短 id(重用同一個產生器),不用 UUID——與活動
    識別碼風格一致、URL 更短。``Meta.unique_together`` 保證同一活動下暱稱不可
    重複;搭配短 id 碰撞重試邏輯的完整說明見
    openspec/changes/add-participant-responses/design.md D9。
    """

    id = models.CharField(
        primary_key=True, max_length=8, default=generate_short_id, editable=False
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="responses")
    nickname = models.CharField(max_length=40)
    phone_last_three_hash = models.CharField(max_length=128)
    email = models.EmailField(null=True, blank=True)
    # 選填留言,長度上限比照 Event.final_note——目前只接受並儲存,尚無獨立的
    # 留言列表 API 讀取它(使用者已確認這是刻意分兩階段的範圍)。
    comment = models.CharField(max_length=200, null=True, blank=True)
    # 三態表態(available/if_needed/unavailable)需要在關聯本身多帶一個欄位,
    # 改用帶 through model 的 M2M,見 design.md D4(2026-09-21 修訂)/D4a。
    slots = models.ManyToManyField(
        Slot, through="ParticipantResponseSlotAvailability", related_name="responses"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # 軟刪除標記，命名比照本 app 既有的 Comment.deleted_at（同一套查詢慣例:
    # filter(deleted_at__isnull=True)）。目前唯一觸發來源是主揪取消整場活動
    # （EventCancelView，見 openspec/changes/add-event-lifecycle/design.md
    # D6），不是使用者能個別操作的欄位。
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("event", "nickname")

    def __str__(self):
        return self.nickname


class ParticipantResponseSlotAvailability(models.Model):
    """``ParticipantResponse``＋``Slot`` 的 through model，多帶一個
    ``availability`` 三態欄位。見 design.md D4a。

    ``response``／``slot`` 皆 ``CASCADE``——投票或候選時段被刪除時，表態紀錄
    一併清除，不留孤兒資料。``Meta.unique_together`` 保證同一參與者對同一
    候選時段只有一筆表態。
    """

    class Availability(models.TextChoices):
        AVAILABLE = "available"
        IF_NEEDED = "if_needed"
        UNAVAILABLE = "unavailable"

    response = models.ForeignKey(
        ParticipantResponse, on_delete=models.CASCADE, related_name="slot_availabilities"
    )
    slot = models.ForeignKey(
        Slot, on_delete=models.CASCADE, related_name="response_availabilities"
    )
    availability = models.CharField(max_length=20, choices=Availability.choices)

    class Meta:
        unique_together = ("response", "slot")

    def __str__(self):
        return f"{self.response_id}:{self.slot_id}={self.availability}"


class Comment(models.Model):
    """任何人（含未登入）對某場 :class:`Event` 留下的一則留言，完全獨立於
    :class:`ParticipantResponse`（不需要先投票、不綁投票記錄），見
    openspec/changes/add-event-comments/design.md D2。

    不要求同一活動內暱稱唯一（同一人可留多則留言，見 D6）——因此沒有
    ``unique_together``，短 id 碰撞重試邏輯不需要像 ``ParticipantResponse``
    那樣先查暱稱區分成因，``IntegrityError`` 一律視為 id 碰撞（見 D7）。

    ``deleted_at`` 為軟刪除標記（見 D9）——只有活動擁有者可以刪除留言，刪除
    不做實體刪除，保留紀錄；非 null 即代表已被刪除，``CommentListCreateView.get()``
    查詢時會排除這些留言。
    """

    id = models.CharField(
        primary_key=True, max_length=8, default=generate_short_id, editable=False
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="comments")
    nickname = models.CharField(max_length=40)
    message = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.nickname}: {self.message}"


class ParticipantResponseAccessToken(models.Model):
    """身分核對成功後核發的一次性存取憑證,供後續 ``PATCH`` 修改投票時免重新
    輸入暱稱＋手機末三碼(見 design.md D2)。

    只存 ``hashlib.sha256`` 雜湊值,不存明碼——比照既有
    ``apps.accounts.models.RefreshTokenRecord`` 的作法：token 本身用
    ``secrets.token_urlsafe`` 產生、熵夠高,不像手機末三碼只有 1000 種組合,
    不需要 ``make_password`` 的 per-record salt。``used_at`` 非空即代表已被
    ``PATCH`` 消費過,不可再次使用;``expires_at`` 固定核發後 30 分鐘。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    response = models.ForeignKey(
        ParticipantResponse, on_delete=models.CASCADE, related_name="access_tokens"
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.id)

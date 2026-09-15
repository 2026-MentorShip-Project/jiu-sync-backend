import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    """Manager for the Google-SSO-only ``User`` model.

    Regular hosts never have a usable password — identity is established by
    Google, not by us. ``create_superuser`` is the deliberate exception: it
    manages internal ``/admin/`` access and accepts a real password.
    """

    use_in_migrations = True

    def create_user(self, email, google_sub, **extra_fields):
        if not email:
            raise ValueError("User must have an email address")
        if not google_sub:
            raise ValueError("User must have a google_sub")
        email = self.normalize_email(email)
        user = self.model(email=email, google_sub=google_sub, **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, google_sub=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        # Normalize "" -> None: `google_sub` is unique, and Postgres treats
        # repeated "" as a duplicate but allows unlimited NULLs. createsuperuser
        # --noinput always passes a string (env vars have no concept of
        # "unset"), so without this a second admin account fails to create.
        google_sub = google_sub or None
        email = self.normalize_email(email)
        user = self.model(email=email, google_sub=google_sub, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, PermissionsMixin):
    """A host (揪主) authenticated exclusively via Google SSO.

    UUID primary key so host identifiers are safe to expose in URLs/responses
    without leaking a sequential, enumerable count of hosts. ``google_sub`` is
    the stable identity key (Google's ``sub`` claim); ``email`` is kept as
    ``USERNAME_FIELD`` for admin/lookup convenience, but it can and does
    change over time and is not used to key identity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    # null=True (not just blank=True) so `createsuperuser` (the admin-only,
    # password-based exception — see design.md) can create an account
    # without a real Google `sub` claim: Postgres allows unlimited NULLs
    # under a unique constraint but treats repeated "" as a duplicate, so a
    # second admin account would fail to create with blank=True alone.
    # Regular Google-SSO users always get a real value via
    # UserManager.create_user.
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)
    display_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["google_sub"]

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return self.email


class RefreshTokenRecord(models.Model):
    """撤銷紀錄：只存 refresh token 的 ``jti`` 與 SHA-256 雜湊，不存明文。

    取代 ``rest_framework_simplejwt.token_blacklist`` 的
    ``OutstandingToken``/``BlacklistedToken``——那組明文存完整 refresh JWT
    字串，是不必要的安全風險（撤銷檢查只靠 ``jti`` 查表）。見
    openspec/changes/hashed-refresh-token-storage/design.md。

    ``on_delete=CASCADE``（非 simplejwt 原本用的 ``SET_NULL``）：這個專案
    目前沒有刪除使用者的功能，使用者一旦被刪除，他名下的 refresh 紀錄沒有
    再保留的意義。
    """

    jti = models.CharField(max_length=64, unique=True, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.jti

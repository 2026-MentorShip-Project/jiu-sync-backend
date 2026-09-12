from rest_framework import serializers

from .models import User


class GoogleLoginSerializer(serializers.Serializer):
    """Body for ``POST /api/auth/google/``.

    ``idToken`` is camelCase (not the Python-idiomatic ``id_token``) to match
    the existing frontend Swagger contract — see design.md「端點形狀」.
    """

    idToken = serializers.CharField()


class UserSerializer(serializers.ModelSerializer):
    """Read-only profile representation for ``GET /api/me/``.

    Uses full model field names (not the abbreviated ``name`` shape used by
    the login response) — this is an internal profile lookup, not a login
    response. See tasks.md 4.2.
    """

    class Meta:
        model = User
        fields = ["id", "email", "display_name", "avatar_url", "date_joined"]
        read_only_fields = fields

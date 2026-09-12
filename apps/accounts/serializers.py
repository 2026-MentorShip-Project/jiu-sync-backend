from rest_framework import serializers


class GoogleLoginSerializer(serializers.Serializer):
    """Body for ``POST /api/auth/google/``.

    ``idToken`` is camelCase (not the Python-idiomatic ``id_token``) to match
    the existing frontend Swagger contract — see design.md「端點形狀」.
    """

    idToken = serializers.CharField()

from config.settings.base import _mailer_config


def test_mailer_config_omits_options_for_console_backend():
    """code-review 抓到(add-event-lifecycle design.md D10):console backend
    不吃 host/port/username/password/use_tls 這些 SMTP 專屬參數,硬塞會在
    寄信當下直接拋 InvalidMailer(design.md Risks 2026-09-23 修訂記錄，使用者
    本機實測過一次真的觸發)。console backend 不該帶 OPTIONS。"""
    config = _mailer_config("django.core.mail.backends.console.EmailBackend")

    assert config == {"BACKEND": "django.core.mail.backends.console.EmailBackend"}


def test_mailer_config_includes_smtp_options_for_smtp_backend():
    config = _mailer_config("django.core.mail.backends.smtp.EmailBackend")

    assert config["BACKEND"] == "django.core.mail.backends.smtp.EmailBackend"
    assert set(config["OPTIONS"].keys()) == {
        "host",
        "port",
        "username",
        "password",
        "use_tls",
    }

"""Request defaults which apply only to the student portal."""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import translation


class StudentPortalDefaultsMiddleware:
    """Start the student login flow in Arabic until the user chooses a language.

    ``LocaleMiddleware`` still owns explicit language selection through Django's
    language cookie.  The login entry point supplies and persists the Arabic
    default when that cookie is absent, so the choice follows the student into
    every portal page. Staff pages and locale-aware APIs retain their existing
    behaviour, and students can still deliberately switch to English.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        language_cookie = settings.LANGUAGE_COOKIE_NAME
        if not request.path.startswith("/student/login/") or language_cookie in request.COOKIES:
            return self.get_response(request)

        request.LANGUAGE_CODE = "ar"
        with translation.override("ar"):
            response = self.get_response(request)
        response.set_cookie(
            language_cookie,
            "ar",
            max_age=settings.LANGUAGE_COOKIE_AGE,
            path=settings.LANGUAGE_COOKIE_PATH,
            domain=settings.LANGUAGE_COOKIE_DOMAIN,
            secure=settings.LANGUAGE_COOKIE_SECURE,
            httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
            samesite=settings.LANGUAGE_COOKIE_SAMESITE,
        )
        return response

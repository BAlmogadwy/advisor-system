"""Routes for the Telegram channel.

No `app_name`: neither `core.urls` nor `whatsapp_gateway.urls` declares one, so URL
names in this project are globally flat. The `telegram_` prefix on every name is
what keeps them from colliding.

The webhook path deliberately carries no secret in it. Putting the bot token in the
URL is a common pattern and a bad one — it lands in access logs, proxy logs and
error trackers. Authenticity is the `X-Telegram-Bot-Api-Secret-Token` header,
which does not.
"""

from django.urls import path

from .views import (
    card_view,
    link_confirm_view,
    link_manage_view,
    link_reauthenticate_view,
    link_start_view,
    telegram_webhook_view,
)

urlpatterns = [
    path("webhook/", telegram_webhook_view, name="telegram_webhook"),
    # Signed, short-lived, and reached only by the local screenshotter.
    path("card/<str:token>/", card_view, name="telegram_card"),
    path("link/manage/", link_manage_view, name="telegram_link_manage"),
    # After `link/manage/`, so the literal segment is matched before the token
    # pattern gets a chance to swallow it.
    path("link/<str:token>/", link_start_view, name="telegram_link_start"),
    path("link/<str:token>/confirm/", link_confirm_view, name="telegram_link_confirm"),
    path(
        "link/<str:token>/reauth/",
        link_reauthenticate_view,
        name="telegram_link_reauthenticate",
    ),
]

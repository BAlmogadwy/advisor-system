from django.urls import path

from whatsapp_gateway.views import whatsapp_webhook_view

urlpatterns = [
    path("webhook/", whatsapp_webhook_view, name="whatsapp_webhook"),
]

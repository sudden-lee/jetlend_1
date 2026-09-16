from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from orders.views import create_order_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("api/orders/", create_order_view, name="create-order"),
]

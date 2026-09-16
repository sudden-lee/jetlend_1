from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from orders.views import OrderCreateAPIView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("api/orders/", OrderCreateAPIView.as_view(), name="create-order"),
]

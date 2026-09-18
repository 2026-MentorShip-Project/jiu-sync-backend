"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from apps.accounts.views import MeView
from config.health import healthz

admin.site.site_header = "揪甘心管理後台 deploy-verify-v1"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/me/', MeView.as_view(), name='me'),
    path('healthz/', healthz, name='healthz'),
    path('api/auth/', include('apps.accounts.urls')),
    path('api/events/', include('apps.events.urls')),
    path('api/recommendations/', include('apps.recommendations.urls')),
    path('api/notifications/', include('apps.notifications.urls')),
]

handler404 = "config.exceptions.handler404"
handler500 = "config.exceptions.handler500"

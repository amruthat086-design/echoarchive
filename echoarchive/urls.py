"""echoarchive URL Configuration"""
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path('admin/', admin.site.urls),
]

from django.urls import include
from django.urls import path

urlpatterns += [
    path('echoarchiveweb/', include('echoarchiveweb.urls')),
]

# Redirect the base URL to our application
from django.views.generic import RedirectView
urlpatterns += [
    path('', RedirectView.as_view(url='echoarchiveweb/', permanent=True)),
]

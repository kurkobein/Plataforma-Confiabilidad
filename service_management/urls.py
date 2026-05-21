from django.urls import path

from . import views


urlpatterns = [
    path('servicios/', views.service_list, name='service_list'),
    path('servicios/<int:pk>/', views.service_detail, name='service_detail'),
    path('servicios/<int:pk>/accesos/', views.service_access_manage, name='service_access_manage'),
    path('servicios/<int:pk>/dimensiones/', views.service_dimensions, name='service_dimensions'),
    path('servicios/<int:pk>/equipos/niveles/', views.service_equipment_levels, name='service_equipment_levels'),
    path('servicios/<int:pk>/equipos/nodos/', views.service_equipment_nodes, name='service_equipment_nodes'),
    path('servicios/<int:pk>/equipos/buscar/', views.service_equipment_search, name='service_equipment_search'),
    path('servicios/<int:pk>/equipos/<int:equipment_pk>/', views.service_equipment_detail, name='service_equipment_detail'),
]

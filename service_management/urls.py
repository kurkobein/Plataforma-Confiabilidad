from django.urls import path

from . import views


urlpatterns = [
    path('servicios/<int:pk>/', views.service_detail, name='service_detail'),
    path(
        'servicios/<int:pk>/matrices/<int:matrix_pk>/activar-aca/',
        views.service_aca_matrix_activate,
        name='service_aca_matrix_activate',
    ),
    path('servicios/<int:pk>/accesos/', views.service_access_manage, name='service_access_manage'),
    path('servicios/<int:pk>/dimensiones/', views.service_dimensions, name='service_dimensions'),
    path('servicios/<int:pk>/familias-equipos/', views.service_equipment_families, name='service_equipment_families'),
    path('servicios/<int:pk>/familias-equipos/nueva/', views.service_equipment_family_form, name='service_equipment_family_new'),
    path('servicios/<int:pk>/familias-equipos/<int:family_pk>/editar/', views.service_equipment_family_form, name='service_equipment_family_edit'),
    path('servicios/<int:pk>/familias-equipos/<int:family_pk>/eliminar/', views.service_equipment_family_delete, name='service_equipment_family_delete'),
    path('servicios/<int:pk>/equipos/niveles/', views.service_equipment_levels, name='service_equipment_levels'),
    path('servicios/<int:pk>/equipos/nodos/', views.service_equipment_nodes, name='service_equipment_nodes'),
    path('servicios/<int:pk>/equipos/buscar/', views.service_equipment_search, name='service_equipment_search'),
    path('servicios/<int:pk>/equipos/disponibles/', views.service_equipment_available, name='service_equipment_available'),
    path('servicios/<int:pk>/equipos/vincular/', views.service_equipment_link, name='service_equipment_link'),
    path('servicios/<int:pk>/equipos/desvincular/', views.service_equipment_unlink, name='service_equipment_unlink'),
    path('servicios/<int:pk>/equipos/<int:equipment_pk>/', views.service_equipment_detail, name='service_equipment_detail'),
]

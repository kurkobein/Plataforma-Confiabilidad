from django.urls import path

from . import views


urlpatterns = [
    path('servicios/<int:pk>/pautas/', views.service_pautas_list, name='service_pautas_list'),
    path('servicios/<int:pk>/pautas/plantillas/', views.service_pauta_templates, name='service_pauta_templates'),
    path(
        'servicios/<int:service_pk>/pautas/plantillas/<int:template_pk>/mapeo/',
        views.service_pauta_template_mapping,
        name='service_pauta_template_mapping',
    ),
    path('servicios/<int:pk>/pautas/generar/', views.service_pautas_generate, name='service_pautas_generate'),
    path('servicios/<int:service_pk>/pautas/<int:pauta_pk>/', views.service_pauta_detail, name='service_pauta_detail'),
    path(
        'servicios/<int:service_pk>/pautas/<int:pauta_pk>/exportar-excel/',
        views.service_pauta_export_excel,
        name='service_pauta_export_excel',
    ),
]

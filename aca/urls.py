from django.urls import path

from . import views


urlpatterns = [
    path('aca/panel/', views.aca_panel, name='aca_panel'),
    path('aca/desarrollo/', views.aca_development, name='aca_development'),
    path('aca/nuevo/', views.aca_registro_new, name='aca_registro_new'),
    path('servicios/<int:pk>/aca/', views.service_aca_list, name='service_aca_list'),
    path('servicios/<int:pk>/aca/progreso/', views.service_aca_progress_partial, name='service_aca_progress_partial'),
    path('servicios/<int:pk>/aca/exportar/<str:formato>/', views.service_aca_export, name='service_aca_export'),
    path('servicios/<int:pk>/aca/carga-masiva/', views.service_aca_excel_upload, name='service_aca_excel_upload'),
    path('servicios/<int:pk>/aca/nuevo-masivo/', views.service_aca_bulk_new, name='service_aca_bulk_new'),
    path('servicios/<int:pk>/aca/grupo/<int:carga_pk>/editar/', views.service_aca_bulk_group_edit, name='service_aca_bulk_group_edit'),
    path('servicios/<int:pk>/aca/nuevo/', views.service_aca_new, name='service_aca_new'),
    path('servicios/<int:service_pk>/aca/<int:crit_pk>/editar/', views.service_aca_edit, name='service_aca_edit'),
    path('servicios/<int:service_pk>/aca/<int:crit_pk>/eliminar/', views.service_aca_delete, name='service_aca_delete'),
    path('servicios/<int:service_pk>/aca/deshacer-eliminacion/', views.service_aca_restore_deleted, name='service_aca_restore_deleted'),
]

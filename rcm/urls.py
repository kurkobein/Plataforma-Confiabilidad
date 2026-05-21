from django.urls import path

from . import views


urlpatterns = [
    path('rcm/', views.rcm_index, name='rcm_index'),
    path('servicios/<int:pk>/rcm/', views.service_rcm_list, name='service_rcm_list'),
    path('servicios/<int:pk>/rcm/exportar/<str:formato>/', views.service_rcm_export, name='service_rcm_export'),
    path('servicios/<int:pk>/rcm/tareas/configuracion/', views.service_rcm_task_config, name='service_rcm_task_config'),
    path('servicios/<int:pk>/rcm/nuevo/', views.service_rcm_new, name='service_rcm_new'),
    path('servicios/<int:service_pk>/rcm/<int:rcm_pk>/editar/', views.service_rcm_edit, name='service_rcm_edit'),
    path('servicios/<int:service_pk>/rcm/<int:rcm_pk>/eliminar/', views.service_rcm_delete, name='service_rcm_delete'),
]

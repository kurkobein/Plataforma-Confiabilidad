from django.urls import path

from . import views


urlpatterns = [
    path('rcm/', views.rcm_index, name='rcm_index'),
    path('fmeca/panel/', views.fmeca_panel, name='fmeca_panel'),
    path('fmeca/desarrollo/', views.fmeca_development, name='fmeca_development'),
    path('servicios/<int:pk>/fmeca/', views.service_rcm_list, name='service_fmeca_list'),
    path('servicios/<int:pk>/fmeca/carga-masiva/', views.service_rcm_excel_upload, name='service_fmeca_excel_upload'),
    path('servicios/<int:pk>/fmeca/carga-masiva/plantilla/', views.service_rcm_excel_template, name='service_fmeca_excel_template'),
    path('servicios/<int:pk>/fmeca/exportar/<str:formato>/', views.service_rcm_export, name='service_fmeca_export'),
    path('servicios/<int:pk>/fmeca/tareas/configuracion/', views.service_rcm_task_config, name='service_fmeca_task_config'),
    path('servicios/<int:pk>/fmeca/nuevo/', views.service_rcm_new, name='service_fmeca_new'),
    path('servicios/<int:service_pk>/fmeca/<int:rcm_pk>/editar/', views.service_rcm_edit, name='service_fmeca_edit'),
    path('servicios/<int:service_pk>/fmeca/<int:rcm_pk>/eliminar/', views.service_rcm_delete, name='service_fmeca_delete'),
    path('servicios/<int:pk>/rcm/', views.service_rcm_list, name='service_rcm_list'),
    path('servicios/<int:pk>/rcm/carga-masiva/', views.service_rcm_excel_upload, name='service_rcm_excel_upload'),
    path('servicios/<int:pk>/rcm/carga-masiva/plantilla/', views.service_rcm_excel_template, name='service_rcm_excel_template'),
    path('servicios/<int:pk>/rcm/exportar/<str:formato>/', views.service_rcm_export, name='service_rcm_export'),
    path('servicios/<int:pk>/rcm/tareas/configuracion/', views.service_rcm_task_config, name='service_rcm_task_config'),
    path('servicios/<int:pk>/rcm/nuevo/', views.service_rcm_new, name='service_rcm_new'),
    path('servicios/<int:service_pk>/rcm/<int:rcm_pk>/editar/', views.service_rcm_edit, name='service_rcm_edit'),
    path('servicios/<int:service_pk>/rcm/<int:rcm_pk>/eliminar/', views.service_rcm_delete, name='service_rcm_delete'),
]

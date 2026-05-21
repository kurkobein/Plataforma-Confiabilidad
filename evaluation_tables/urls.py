from django.urls import path

from . import views


urlpatterns = [
    path('servicios/<int:service_pk>/matrices/<int:matrix_pk>/', views.service_matrix_view, name='service_matrix_view'),
    path('estrategias/<int:pk>/tablas/', views.dimension_tables_editor, name='dimension_tables_editor'),
    path('matrices/nueva/', views.matriz_builder_new, name='matriz_builder_new'),
    path('matrices/<int:pk>/builder/', views.matriz_builder_edit, name='matriz_builder_edit'),
]

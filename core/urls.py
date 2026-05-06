from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    path('servicios/', views.service_list, name='service_list'),
    path('servicios/<int:pk>/', views.service_detail, name='service_detail'),
    path('servicios/<int:pk>/accesos/', views.service_access_manage, name='service_access_manage'),
    path('servicios/<int:pk>/dimensiones/', views.service_dimensions, name='service_dimensions'),
    path('servicios/<int:pk>/aca/', views.service_aca_list, name='service_aca_list'),
    path('servicios/<int:pk>/aca/nuevo/', views.service_aca_new, name='service_aca_new'),
    path('servicios/<int:service_pk>/matrices/<int:matrix_pk>/', views.service_matrix_view, name='service_matrix_view'),

    path('aca/nuevo/', views.aca_registro_new, name='aca_registro_new'),
    path('estrategias/<int:pk>/tablas/', views.dimension_tables_editor, name='dimension_tables_editor'),
    path('matrices/nueva/', views.matriz_builder_new, name='matriz_builder_new'),
    path('matrices/<int:pk>/builder/', views.matriz_builder_edit, name='matriz_builder_edit'),
    path('ubicaciones-tecnicas/', views.technical_location_index, name='technical_location_index'),
    path('empresas/<int:empresa_id>/jerarquia/', views.hierarchy_tree, name='hierarchy_tree'),
    path('empresas/<int:empresa_id>/jerarquia/estructura/', views.hierarchy_structure, name='hierarchy_structure'),
    path('empresas/<int:empresa_id>/jerarquia/valores/', views.hierarchy_values, name='hierarchy_values'),
    path('empresas/<int:empresa_id>/jerarquia/constructor/', views.hierarchy_create_route, name='hierarchy_create_route'),
    path('jerarquia/nodos/<int:pk>/mover/', views.hierarchy_move_node, name='hierarchy_move_node'),
    path('jerarquia/nodos/<int:pk>/insertar-nivel/', views.hierarchy_insert_between, name='hierarchy_insert_between'),
    path('jerarquia/nodos/<int:pk>/eliminar/', views.hierarchy_delete_node, name='hierarchy_delete_node'),
    path('<slug:model_key>/', views.model_list, name='model_list'),
    path('<slug:model_key>/nuevo/', views.model_create, name='model_create'),
    path('<slug:model_key>/<int:pk>/', views.model_detail, name='model_detail'),
    path('<slug:model_key>/<int:pk>/editar/', views.model_update, name='model_update'),
    path('<slug:model_key>/<int:pk>/eliminar/', views.model_delete, name='model_delete'),
    path('empresa/<int:empresa_id>/logo/', views.empresa_logo, name='empresa_logo'),
    path('servicios/<int:service_pk>/aca/<int:crit_pk>/editar/', views.service_aca_edit, name='service_aca_edit'),
    path('servicios/<int:service_pk>/aca/<int:crit_pk>/eliminar/', views.service_aca_delete, name='service_aca_delete'),

]

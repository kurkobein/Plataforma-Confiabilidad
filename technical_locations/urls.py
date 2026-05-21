from django.urls import path

from . import views


urlpatterns = [
    path('ubicaciones-tecnicas/', views.technical_location_index, name='technical_location_index'),
    path('empresas/<int:empresa_id>/jerarquia/', views.hierarchy_tree, name='hierarchy_tree'),
    path('empresas/<int:empresa_id>/jerarquia/estructura/', views.hierarchy_structure, name='hierarchy_structure'),
    path('empresas/<int:empresa_id>/jerarquia/valores/', views.hierarchy_values, name='hierarchy_values'),
    path('empresas/<int:empresa_id>/jerarquia/valores/nodos/', views.hierarchy_values_nodes, name='hierarchy_values_nodes'),
    path('empresas/<int:empresa_id>/jerarquia/valores/buscar/', views.hierarchy_values_search, name='hierarchy_values_search'),
    path('empresas/<int:empresa_id>/jerarquia/constructor/', views.hierarchy_create_route, name='hierarchy_create_route'),
    path('jerarquia/nodos/<int:pk>/mover/', views.hierarchy_move_node, name='hierarchy_move_node'),
    path('jerarquia/nodos/<int:pk>/insertar-nivel/', views.hierarchy_insert_between, name='hierarchy_insert_between'),
    path('jerarquia/nodos/<int:pk>/eliminar/', views.hierarchy_delete_node, name='hierarchy_delete_node'),
]

from django.urls import path
from django.urls import include
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    path('', include('service_management.urls')),
    path('', include('aca.urls')),
    path('', include('rcm.urls')),
    path('', include('pautas.urls')),
    path('', include('technical_locations.urls')),
    path('', include('evaluation_tables.urls')),
    path('equipo/carga-masiva/', views.equipment_bulk_upload, name='equipment_bulk_upload'),
    path('<slug:model_key>/', views.model_list, name='model_list'),
    path('<slug:model_key>/nuevo/', views.model_create, name='model_create'),
    path('<slug:model_key>/<int:pk>/', views.model_detail, name='model_detail'),
    path('<slug:model_key>/<int:pk>/editar/', views.model_update, name='model_update'),
    path('<slug:model_key>/<int:pk>/eliminar/', views.model_delete, name='model_delete'),
    path('empresa/<int:empresa_id>/logo/', views.empresa_logo, name='empresa_logo'),

]


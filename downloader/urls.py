# downloader/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('playlist-info/', views.get_playlist_info, name='playlist_info'),
    path('download/', views.download, name='download'),
    path('status/<uuid:task_id>/', views.status, name='status'),
    path('file/<uuid:task_id>/<str:filename>/', views.download_file, name='download_file'),
    path('download_all/<uuid:task_id>/', views.download_all_files, name='download_all_files'),
    path('delete/<uuid:task_id>/', views.delete_files, name='delete_files'),
]
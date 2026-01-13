# downloader/models.py
from django.db import models
from django.utils import timezone
import uuid


class DownloadTask(models.Model):
    TASK_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    DOWNLOAD_TYPE_CHOICES = [
        ('video', 'Video'),
        ('audio', 'Audio'),
    ]

    CODEC_CHOICES = [
        ('aac', 'AAC'),
        ('opus', 'Opus'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    playlist_url = models.URLField(max_length=500)
    download_type = models.CharField(max_length=10, choices=DOWNLOAD_TYPE_CHOICES)
    resolution = models.CharField(max_length=10, null=True, blank=True)
    codec = models.CharField(max_length=10, choices=CODEC_CHOICES, null=True, blank=True)
    status = models.CharField(max_length=20, choices=TASK_STATUS_CHOICES, default='pending')
    progress = models.IntegerField(default=0)
    total_videos = models.IntegerField(default=0)
    completed_videos = models.IntegerField(default=0)
    error_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Download {self.id} - {self.status}"
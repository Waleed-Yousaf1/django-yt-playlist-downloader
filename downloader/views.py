# downloader/views.py
import re
import os
import tempfile
import threading
import zipfile
import subprocess
import requests
import base64
from pathlib import Path
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, HttpResponse, Http404, FileResponse, StreamingHttpResponse
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import mimetypes
from pytubefix import Playlist, YouTube
from .models import DownloadTask
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture
import time
import shutil

mimetypes.add_type('audio/opus', '.opus')
mimetypes.add_type('audio/aac', '.aac')
mimetypes.add_type('audio/mp4', '.m4a')
mimetypes.add_type('video/mp4', '.mp4')


def normalize_youtube_url(url: str) -> str:
    """Convert YouTube Music URLs to regular YouTube URLs"""
    if "music.youtube.com" in url:
        return url.replace("music.youtube.com", "www.youtube.com")
    return url


def cleanup_all_downloads():
    """Delete all existing download files and directories"""
    try:
        # List of possible download directories
        download_dirs = [
            settings.DOWNLOAD_ROOT / "audio_aac",
            settings.DOWNLOAD_ROOT / "audio_opus",
            settings.DOWNLOAD_ROOT / "video_720p",
            settings.DOWNLOAD_ROOT / "video_480p",
            settings.DOWNLOAD_ROOT / "video_360p",
            settings.DOWNLOAD_ROOT / "video_240p",
            settings.DOWNLOAD_ROOT / "video_144p",
        ]

        for dir_path in download_dirs:
            if dir_path.exists():
                shutil.rmtree(dir_path)

        # Also delete all old DownloadTask records
        DownloadTask.objects.all().delete()

    except Exception as e:
        print(f"Error during cleanup: {e}")


def sanitize_filename(name: str) -> str:
    """Strip illegal filesystem chars, collapse whitespace"""
    name = re.sub(r'[\\/:"*?<>|]+', '_', name)
    return re.sub(r'\s+', ' ', name).strip()


def download_thumbnail(url: str, output_path: Path) -> bool:
    """
    Download thumbnail image from URL and save to disk.
    Returns True if successful, False otherwise.
    """
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                f.write(response.content)
            return True
        return False
    except Exception as e:
        print(f"Error downloading thumbnail: {e}")
        return False


def add_metadata_to_opus(file_path: Path, yt: YouTube, thumbnail_path: Path = None):
    """
    Add metadata to Ogg Opus file using mutagen.
    Includes title, artist, album, and cover art.
    """
    try:
        audio = OggOpus(file_path)

        # Basic metadata
        audio['TITLE'] = yt.title
        audio['ARTIST'] = yt.author
        audio['ALBUM'] = yt.title

        # Add cover art if thumbnail was downloaded
        if thumbnail_path and thumbnail_path.exists():
            with open(thumbnail_path, 'rb') as img:
                picture_data = img.read()

            picture = Picture()
            picture.type = 3  # Cover (front)
            picture.mime = 'image/jpeg'
            picture.desc = 'Cover'
            picture.data = picture_data

            # Encode picture as base64 for Opus
            audio['METADATA_BLOCK_PICTURE'] = base64.b64encode(
                picture.write()
            ).decode('ascii')

        audio.save()
        return True
    except Exception as e:
        print(f"Error adding metadata to Opus file: {e}")
        return False


def add_metadata_to_m4a(file_path: Path, yt: YouTube, thumbnail_path: Path = None):
    """
    Add metadata to M4A file using mutagen.
    Includes title, artist, album, and cover art.
    """
    try:
        audio = MP4(file_path)

        # Basic metadata
        audio['\xa9nam'] = yt.title  # Title
        audio['\xa9ART'] = yt.author  # Artist
        audio['\xa9alb'] = yt.title  # Album (using title as fallback)

        # Add cover art if thumbnail was downloaded
        if thumbnail_path and thumbnail_path.exists():
            with open(thumbnail_path, 'rb') as img:
                audio['covr'] = [
                    MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)
                ]

        audio.save()
        return True
    except Exception as e:
        print(f"Error adding metadata to M4A file: {e}")
        return False


def convert_webm_to_opus(webm_path: Path, opus_path: Path) -> bool:
    """
    Convert WebM file to proper Ogg Opus format using ffmpeg.
    This is a lossless remux - no re-encoding occurs.
    """
    try:
        result = subprocess.run(
            [
                'ffmpeg',
                '-i', str(webm_path),
                '-c:a', 'copy',
                '-vn',
                str(opus_path),
                '-y'
            ],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0 and opus_path.exists():
            # Delete the original WebM file after successful conversion
            webm_path.unlink()
            return True
        else:
            print(f"FFmpeg conversion failed: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        print(f"FFmpeg conversion timed out for {webm_path}")
        return False
    except FileNotFoundError:
        print("FFmpeg not found. Please install ffmpeg to convert Opus files.")
        return False
    except Exception as e:
        print(f"Error converting {webm_path} to Opus: {e}")
        return False


def download_audio(yt: YouTube, codec: str, out_dir: Path, task: DownloadTask):
    """Download audio from YouTube video with metadata"""
    try:
        base_name = sanitize_filename(yt.title)
        thumbnail_path = out_dir / f"{base_name}_thumb.jpg"

        # Download thumbnail
        thumbnail_downloaded = False
        if yt.thumbnail_url:
            thumbnail_downloaded = download_thumbnail(yt.thumbnail_url, thumbnail_path)

        if codec == "aac":
            streams = yt.streams.filter(only_audio=True, mime_type="audio/mp4")
            stream = streams.order_by("abr").desc().first() or yt.streams.get_audio_only()
            if not stream:
                return False

            ext = "m4a"
            fname = base_name + f".{ext}"
            file_path = out_dir / fname

            stream.download(output_path=out_dir, filename=fname, skip_existing=True)

            # Add metadata to M4A file
            if file_path.exists():
                add_metadata_to_m4a(
                    file_path,
                    yt,
                    thumbnail_path if thumbnail_downloaded else None
                )

            # Clean up thumbnail file
            if thumbnail_path.exists():
                thumbnail_path.unlink()

            return True

        else:  # Opus
            streams = yt.streams.filter(only_audio=True, mime_type="audio/webm")
            stream = streams.order_by("abr").desc().first()
            if not stream:
                return False

            # Download as WebM first
            webm_fname = base_name + ".webm"
            opus_fname = base_name + ".opus"

            webm_path = out_dir / webm_fname
            opus_path = out_dir / opus_fname

            # Download the WebM file
            stream.download(output_path=out_dir, filename=webm_fname, skip_existing=False)

            # Convert WebM to proper Ogg Opus format
            if webm_path.exists():
                success = convert_webm_to_opus(webm_path, opus_path)

                # Add metadata to Opus file after conversion
                if success and opus_path.exists():
                    add_metadata_to_opus(
                        opus_path,
                        yt,
                        thumbnail_path if thumbnail_downloaded else None
                    )

                # Clean up thumbnail file
                if thumbnail_path.exists():
                    thumbnail_path.unlink()

                if not success:
                    print(f"Warning: Could not convert {webm_fname} to Opus, keeping as WebM")

                return success

            return False

    except Exception as e:
        print(f"Error downloading audio for {yt.video_id}: {e}")
        return False


def download_video(yt: YouTube, resolution: str, out_dir: Path, task: DownloadTask):
    """Download video from YouTube"""
    try:
        stream = (
            yt.streams
            .filter(progressive=True, file_extension="mp4", res=resolution)
            .order_by("fps")
            .desc()
            .first()
        )
        if not stream:
            return False

        fname = sanitize_filename(yt.title) + ".mp4"
        stream.download(output_path=out_dir, filename=fname)
        return True
    except Exception as e:
        print(f"Error downloading video for {yt.video_id}: {e}")
        return False


def process_download(task_id):
    """Process download task in background"""
    try:
        task = DownloadTask.objects.get(id=task_id)
        task.status = 'processing'
        task.save()

        # Normalize URL and get playlist
        normalized_url = normalize_youtube_url(task.playlist_url)
        pl = Playlist(normalized_url)
        pl._video_regex = r"\"url\":\"(/watch\?v=[\w-]*)"
        videos = pl.video_urls
        task.total_videos = len(videos)
        task.save()

        # Create output directory
        if task.download_type == 'audio':
            out_dir = settings.DOWNLOAD_ROOT / f"audio_{task.codec}"
        else:
            out_dir = settings.DOWNLOAD_ROOT / f"video_{task.resolution}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Download videos
        completed = 0
        for url in videos:
            try:
                yt = YouTube(url)
                if task.download_type == 'audio':
                    success = download_audio(yt, task.codec, out_dir, task)
                else:
                    success = download_video(yt, task.resolution, out_dir, task)

                if success:
                    completed += 1

                task.completed_videos = completed
                task.progress = int((completed / len(videos)) * 100)
                task.save()
            except Exception as e:
                print(f"Error processing {url}: {e}")
                continue

        task.status = 'completed'
        task.completed_at = timezone.now()
        task.save()

    except Exception as e:
        task.status = 'failed'
        task.error_message = str(e)
        task.save()


def index(request):
    """Main page with download form"""
    return render(request, 'downloader/index.html')


def get_playlist_info(request):
    """Get playlist title from URL"""
    playlist_url = request.GET.get('url')
    if not playlist_url:
        return JsonResponse({'error': 'URL is required'}, status=400)

    try:
        # Normalize URL
        normalized_url = normalize_youtube_url(playlist_url)
        pl = Playlist(normalized_url)

        return JsonResponse({
            'title': pl.title,
            'video_count': len(pl.video_urls) if pl.video_urls else 0
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_POST
def download(request):
    """Start download process"""
    playlist_url = request.POST.get('playlist_url')
    download_type = request.POST.get('download_type')
    resolution = request.POST.get('resolution')
    codec = request.POST.get('codec')

    if not playlist_url:
        return JsonResponse({'error': 'Playlist URL is required'}, status=400)

    # Clean up all existing downloads before starting new one
    cleanup_all_downloads()

    # Normalize the URL
    normalized_url = normalize_youtube_url(playlist_url)

    # Create download task
    task = DownloadTask.objects.create(
        playlist_url=normalized_url,
        download_type=download_type,
        resolution=resolution if download_type == 'video' else None,
        codec=codec if download_type == 'audio' else None
    )

    # Start background processing
    thread = threading.Thread(target=process_download, args=(task.id,))
    thread.daemon = True
    thread.start()

    return JsonResponse({
        'task_id': str(task.id),
        'status': 'started'
    })


def status(request, task_id):
    """Get download status"""
    task = get_object_or_404(DownloadTask, id=task_id)

    # Get list of downloaded files if completed
    downloaded_files = []
    if task.status == 'completed':
        if task.download_type == 'audio':
            out_dir = settings.DOWNLOAD_ROOT / f"audio_{task.codec}"
        else:
            out_dir = settings.DOWNLOAD_ROOT / f"video_{task.resolution}"

        if out_dir.exists():
            downloaded_files = [f.name for f in out_dir.iterdir() if f.is_file()]

    return JsonResponse({
        'status': task.status,
        'progress': task.progress,
        'total_videos': task.total_videos,
        'completed_videos': task.completed_videos,
        'error_message': task.error_message,
        'downloaded_files': downloaded_files
    })


@require_POST
def delete_files(request, task_id):
    """Delete all files for a specific task"""
    task = get_object_or_404(DownloadTask, id=task_id)

    try:
        # Determine the directory
        if task.download_type == 'audio':
            out_dir = settings.DOWNLOAD_ROOT / f"audio_{task.codec}"
        else:
            out_dir = settings.DOWNLOAD_ROOT / f"video_{task.resolution}"

        # Delete the directory if it exists
        if out_dir.exists():
            shutil.rmtree(out_dir)

        # Optionally delete the task record
        task.delete()

        return JsonResponse({'success': True, 'message': 'Files deleted successfully'})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def download_file(request, task_id, filename):
    """Serve a specific file via FileResponse."""
    task = get_object_or_404(DownloadTask, id=task_id)

    # Determine the directory
    if task.download_type == 'audio':
        out_dir = settings.DOWNLOAD_ROOT / f"audio_{task.codec}"
    else:
        out_dir = settings.DOWNLOAD_ROOT / f"video_{task.resolution}"

    file_path = out_dir / filename
    if not file_path.exists():
        raise Http404("File not found")

    # Security check: ensure it's under out_dir
    if not str(file_path.resolve()).startswith(str(out_dir.resolve())):
        raise Http404("File not found")

    # FileResponse automatically sets a streaming response
    response = FileResponse(
        open(file_path, 'rb'),
        as_attachment=True,
        filename=filename
    )
    return response


def download_all_files(request, task_id):
    task = get_object_or_404(DownloadTask, id=task_id)
    if task.status != 'completed':
        return JsonResponse({'error': 'Task not completed yet'}, status=400)

    # Locate the directory & zip name
    if task.download_type == 'audio':
        out_dir = settings.DOWNLOAD_ROOT / f"audio_{task.codec}"
        zip_name = f"playlist_audio_{task.codec}.zip"
    else:
        out_dir = settings.DOWNLOAD_ROOT / f"video_{task.resolution}"
        zip_name = f"playlist_video_{task.resolution}.zip"

    if not out_dir.exists():
        raise Http404("Download directory not found")

    # Step 1: Create a closed-once-written temp ZIP file
    tmp_file = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fpath in out_dir.iterdir():
                if fpath.is_file():
                    zf.write(fpath, arcname=fpath.name)
        tmp_file.flush()
    finally:
        # Important: close *our* handle before re-opening in the generator
        tmp_file.close()

    # Step 2: Stream through a generator so the file is only open during streaming
    def zip_stream():
        with open(tmp_file.name, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                yield chunk

    # Return a streaming response
    response = StreamingHttpResponse(zip_stream(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{zip_name}"'
    return response
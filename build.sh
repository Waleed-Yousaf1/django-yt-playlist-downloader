#!/usr/bin/env bash
# exit on error
set -o errexit


# Install Node.js (required for pytubefix PO token generation)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs


pip install -r requirements.txt

python manage.py collectstatic --no-input
python manage.py migrate
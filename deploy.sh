#!/usr/bin/env bash
# Update the live site on PythonAnywhere:  bash deploy.sh
# Pulls the latest code, installs any new packages, applies database changes
# and reloads the web app. Stops at the first error.
set -e
cd ~/expenseiq-backend
git pull --no-rebase --no-edit
venv/bin/pip install -q --no-cache-dir -r requirements.txt
venv/bin/python manage.py migrate --noinput
touch /var/www/sarathpeethu_pythonanywhere_com_wsgi.py    # reloads the site
echo "Deployed. Site reloaded."

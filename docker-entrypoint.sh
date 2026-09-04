#!/bin/bash
set -e

echo "docker-entrypoint.sh started..."

# Print the environment for debugging, hiding values by default.
#
# This used to mask only names containing PASSWORD or SECRET, which meant anything
# named *_TOKEN, *_API_KEY or *_CREDENTIALS was written to CloudWatch in plaintext.
# The list below is an allowlist, so a newly added variable is hidden until someone
# decides it is safe to print rather than leaking the first time it ships. Names are
# always shown, so "is it set?" is still answerable from the logs.
is_value_safe() {
    case "$1" in
        SERVICE_TYPE|DJANGO_SETTINGS_MODULE) return 0 ;;
        DB_HOST|DB_NAME|DB_USER|DB_PORT) return 0 ;;
        AWS_REGION|AWS_DEFAULT_REGION) return 0 ;;
        PATH|PYTHONPATH|PYTHONUNBUFFERED|LANG|HOME|HOSTNAME|PWD|SHLVL) return 0 ;;
        ECS_CONTAINER_METADATA_URI|ECS_CONTAINER_METADATA_URI_V4|ECS_AGENT_URI) return 0 ;;
        *_QUEUE_URL) return 0 ;;
        *) return 1 ;;
    esac
}

echo "Environment variables:"
echo "====================="
# Iterate over names rather than parsing `env` output: a value containing a newline
# would otherwise spill its remaining lines past the mask.
for _var_name in $(compgen -e | sort); do
    if is_value_safe "$_var_name"; then
        printf '%s=%s\n' "$_var_name" "${!_var_name}"
    else
        printf '%s=[hidden]\n' "$_var_name"
    fi
done
unset _var_name
echo "====================="

# Check if DB_PASSWORD is set
if [ -z "$DB_PASSWORD" ]; then
    echo "ERROR: DB_PASSWORD is not set!"
    echo "This could be because:"
    echo "1. The secret ARN is incorrect"
    echo "2. The ECS task doesn't have permission to access the secret"
    echo "3. The secret doesn't exist in AWS Secrets Manager"
    exit 1
fi

# Wait for the database to be ready
echo "Waiting for database..."

python -c "
import sys
import time
import pymysql
import os
import socket

def get_connection_info():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        return f'Could not determine IP: {str(e)}'

max_retries = 30
retry_count = 0

while retry_count < max_retries:
    try:
        conn = pymysql.connect(
            host=os.environ.get('DB_HOST'),
            user=os.environ.get('DB_USER'),
            password=os.environ.get('DB_PASSWORD'),
            database=os.environ.get('DB_NAME'),
            port=int(os.environ.get('DB_PORT', 3306)),
            connect_timeout=10
        )
        print('Successfully connected to database!')
        conn.close()
        break
    except pymysql.OperationalError as e:
        retry_count += 1
        if retry_count == max_retries:
            print('Max retries reached. Exiting...')
            sys.exit(1)
        time.sleep(2)
"

echo "Database is ready!"

# Start the specified service
case "$SERVICE_TYPE" in
  "bulk_campaign_scheduler")
    echo "Starting bulk campaign processor scheduler..."
    exec python manage.py process_bulk_campaigns
    ;;
  "bulk_campaign_worker")
    echo "Starting bulk campaign processor worker..."
    exec python manage.py process_due_messages
    ;;
  "journey_scheduler")
    echo "Starting journey processor scheduler..."
    exec python manage.py run_scheduler
    ;;
  "journey_worker")
    echo "Starting journey processor worker..."
    exec python manage.py run_worker
    ;;
  "communication_processor_worker")
    echo "Starting communication processor worker..."
    echo "Setting up channel processors..."
    python manage.py setup_channel_processors
    echo "Starting communication processor worker..."
    exec python manage.py run_communication_worker
    ;;
  "sms_marketing_worker")
    echo "Starting SMS marketing worker..."
    exec python manage.py run_sms_marketing_worker
    ;;
  *)
    echo "Unknown service type: $SERVICE_TYPE"
    exit 1
    ;;
esac
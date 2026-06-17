#!/bin/bash

# Check if enough arguments are provided
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <stage|prod> <sql_query>"
    exit 1
fi

# Assign arguments
ENVIRONMENT=$1
SQL_QUERY=$2

# Load configuration from config.env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../../config.env"

if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
else
    echo "Warning: config.env not found. Using default values."
    echo "Please create config.env with your values."
    # Fallback to defaults
    GABI_STAGE_URL="https://gabi-rbac-stage.apps.crcs02ue1.urby.p1.openshiftapps.com/query"
    GABI_PROD_URL="https://gabi-rbac-prod.apps.crcp01ue1.o9m8.p1.openshiftapps.com/query"
fi

# Use config values or fallback to defaults
STAGE_URL="${GABI_STAGE_URL:-https://gabi-rbac-stage.apps.crcs02ue1.urby.p1.openshiftapps.com/query}"
PROD_URL="${GABI_PROD_URL:-https://gabi-rbac-prod.apps.crcp01ue1.o9m8.p1.openshiftapps.com/query}"

# Select the correct URL based on the environment
if [ "$ENVIRONMENT" = "stage" ]; then
    API_URL=$STAGE_URL
elif [ "$ENVIRONMENT" = "prod" ]; then
    API_URL=$PROD_URL
else
    echo "Invalid environment. Use 'stage' or 'prod'."
    exit 1
fi

# Check if TOKEN is set
if [ -z "$TOKEN" ]; then
    echo "Error: TOKEN environment variable is not set."
    echo "Please set TOKEN before running queries."
    echo "Get the token from the OpenShift Console and run: export TOKEN=<your-token>"
    exit 1
fi

# Make the API request
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"query\": \"$SQL_QUERY\"}" \
     "$API_URL" # | jq '.result[] | @csv'

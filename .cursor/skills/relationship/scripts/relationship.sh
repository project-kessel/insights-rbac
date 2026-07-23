#!/bin/bash

# Check if enough arguments are provided
if [ "$#" -lt 3 ]; then
    echo "Usage: $0 <stage|prod> <read_tuples|lookup_resource> '<json_payload>'"
    echo ""
    echo "Examples:"
    echo "  $0 stage read_tuples '{\"filter\": {...}}'"
    echo "  $0 stage lookup_resource '{\"resource_type\": {...}, \"subject\": {...}, \"relation\": \"...\"}'"
    exit 1
fi

# Assign arguments
ENVIRONMENT=$1
OPERATION=$2
JSON_PAYLOAD=$3

# Load configuration from config.env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../../config.env"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: config.env not found at $CONFIG_FILE"
    echo "Please create config.env with STAGE_DOMAIN, PROD_DOMAIN, and PROXY variables."
    exit 1
fi

# Load configuration from config.env
source "$CONFIG_FILE"

# Validate required variables are set
if [ -z "$STAGE_DOMAIN" ]; then
    echo "Error: STAGE_DOMAIN is not set in config.env"
    exit 1
fi

if [ -z "$PROD_DOMAIN" ]; then
    echo "Error: PROD_DOMAIN is not set in config.env"
    exit 1
fi

if [ -z "$PROXY" ]; then
    echo "Error: PROXY is not set in config.env"
    exit 1
fi

RELATIONSHIP_STAGE_URL="${STAGE_DOMAIN}/api/rbac/relations"
RELATIONSHIP_PROD_URL="${PROD_DOMAIN}/api/rbac/relations"

# Construct Turnpike Session URLs from domain
TURNPIKE_STAGE_URL="${STAGE_DOMAIN}/api/turnpike/session/"
TURNPIKE_PROD_URL="${PROD_DOMAIN}/api/turnpike/session/"

STAGE_BASE_URL="$RELATIONSHIP_STAGE_URL"
PROD_BASE_URL="$RELATIONSHIP_PROD_URL"

# Select the correct URL based on the environment
if [ "$ENVIRONMENT" = "stage" ]; then
    BASE_URL=$STAGE_BASE_URL
elif [ "$ENVIRONMENT" = "prod" ]; then
    BASE_URL=$PROD_BASE_URL
else
    echo "Invalid environment. Use 'stage' or 'prod'."
    exit 1
fi

# Validate operation
if [ "$OPERATION" != "read_tuples" ] && [ "$OPERATION" != "lookup_resource" ]; then
    echo "Invalid operation. Use 'read_tuples' or 'lookup_resource'."
    exit 1
fi

# Check if SESSION is set
if [ -z "$SESSION" ]; then
    echo "Error: SESSION environment variable is not set."
    echo ""
    echo "To get the session token, paste this URL into your browser:"
    if [ "$ENVIRONMENT" = "stage" ]; then
        echo "  $TURNPIKE_STAGE_URL"
    else
        echo "  $TURNPIKE_PROD_URL"
    fi
    echo ""
    echo "Then copy the token from the browser response and set it with:"
    echo "  export SESSION=<token_value>"
    exit 1
fi

# Construct the full API URL
API_URL="${BASE_URL}/${OPERATION}/"

# Make the API request
if [ "$ENVIRONMENT" = "stage" ]; then
    curl --proxy "$PROXY" \
         -X POST "$API_URL" \
         -H "Content-Type: application/json" \
         -b "session=$SESSION" \
         -d "$JSON_PAYLOAD" | jq '.'
else
    curl -X POST "$API_URL" \
         -H "Content-Type: application/json" \
         -b "session=$SESSION" \
         -d "$JSON_PAYLOAD" | jq '.'
fi

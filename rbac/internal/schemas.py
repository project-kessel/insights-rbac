"""Validation schemas for /_private/api/relations/ and /_private/api/inventory/ endpoints."""

# Schemas for the Inventory API's tuple filter format (kessel.inventory.v1beta2 RelationTupleFilter).
SUBJECT_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_type": {"type": "string"},
        "subject_namespace": {"type": "string"},
        "subject_id": {"type": "string"},
        "relation": {"type": "string"},
    },
    "required": ["subject_type", "subject_namespace", "subject_id"],
}

FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string"},
        "resource_type": {"type": "string"},
        "resource_namespace": {"type": "string"},
        "relation": {"type": "string"},
        "subject_filter": SUBJECT_FILTER_SCHEMA,
    },
    "required": ["resource_id", "resource_type", "resource_namespace", "relation", "subject_filter"],
}

RELATION_INPUT_SCHEMAS = {
    # "api/inventory/read_tuples/"
    "read_tuples": {
        "type": "object",
        "properties": {"filter": FILTER_SCHEMA},
        "required": ["filter"],
    },
}

# Schemas for the Inventory API's resource/subject reference format (reporter-based, not namespace-based).
INVENTORY_RESOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string"},
        "resource_type": {"type": "string"},
        "reporter": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]},
    },
    "required": ["resource_id", "resource_type", "reporter"],
}

REPRESENTATION_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "resource_type": {"type": "string"},
        "reporter_type": {"type": "string"},
    },
    "required": ["resource_type", "reporter_type"],
}

ENTITY_SCHEMA = {"type": "object", "properties": {"resource": INVENTORY_RESOURCE_SCHEMA}, "required": ["resource"]}

INVENTORY_API_SCHEMAS = [
    # "api/inventory/check/"
    {
        "type": "object",
        "properties": {
            "resource": INVENTORY_RESOURCE_SCHEMA,
            "relation": {"type": "string"},
            "subject": ENTITY_SCHEMA,
        },
        "required": ["resource", "relation", "subject"],
    },
    # "api/inventory/lookup_resource/"
    {
        "type": "object",
        "properties": {
            "resource_type": REPRESENTATION_TYPE_SCHEMA,
            "relation": {"type": "string"},
            "subject": ENTITY_SCHEMA,
        },
        "required": ["resource_type", "relation", "subject"],
    },
    # "api/inventory/lookup_subjects/"
    {
        "type": "object",
        "properties": {
            "resource": INVENTORY_RESOURCE_SCHEMA,
            "relation": {"type": "string"},
            "subject_type": REPRESENTATION_TYPE_SCHEMA,
            "subject_relation": {"type": "string"},
        },
        "required": ["resource", "relation", "subject_type"],
    },
]

INVENTORY_INPUT_SCHEMAS = {
    "check": INVENTORY_API_SCHEMAS[0],
    "lookup_resources": INVENTORY_API_SCHEMAS[1],
    "lookup_subjects": INVENTORY_API_SCHEMAS[2],
}

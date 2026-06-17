"""Validation schemas for /_private/api/relations/ endpoints."""

# Common schemas
TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string"},
        "name": {"type": "string"},
    },
    "required": ["namespace", "name"],
}

ENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "type": TYPE_SCHEMA,
        "id": {"type": "string"},
    },
    "required": ["type", "id"],
}

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

RELATIONS_TOOL_INPUT_SCHEMAS = [
    # "api/relations/lookup_resource/"
    {
        "type": "object",
        "properties": {
            "resource_type": TYPE_SCHEMA,
            "relation": {"type": "string"},
            "subject": {
                "type": "object",
                "properties": {"subject": ENTITY_SCHEMA},
                "required": ["subject"],
            },
        },
        "required": ["resource_type", "relation", "subject"],
    },
    # "api/relations/check_relation/"
    {
        "type": "object",
        "properties": {
            "resource": ENTITY_SCHEMA,
            "relation": {"type": "string"},
            "subject": {
                "type": "object",
                "relation": {"type": "string"},
                "properties": {"subject": ENTITY_SCHEMA},
                "required": ["subject"],
            },
        },
        "required": ["resource", "relation", "subject"],
    },
    # "api/relations/read_tuples/"
    {
        "type": "object",
        "properties": {"filter": FILTER_SCHEMA},
        "required": ["filter"],
    },
]

LOOKUP_SUBJECTS_SCHEMA = {
    "type": "object",
    "properties": {
        "resource": ENTITY_SCHEMA,
        "relation": {"type": "string"},
        "subject_type": TYPE_SCHEMA,
        "subject_relation": {"type": "string"},
    },
    "required": ["resource", "relation", "subject_type"],
}

RELATION_INPUT_SCHEMAS = {
    "lookup_resources": RELATIONS_TOOL_INPUT_SCHEMAS[0],
    "check_relation": RELATIONS_TOOL_INPUT_SCHEMAS[1],
    "read_tuples": RELATIONS_TOOL_INPUT_SCHEMAS[2],
    "lookup_subjects": LOOKUP_SUBJECTS_SCHEMA,
}

INVENTORY_RESOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string"},
        "resource_type": {"type": "string"},
        "reporter": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]},
    },
    "required": ["resource_id", "resource_type", "reporter"],
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
]

INVENTORY_INPUT_SCHEMAS = {"check": INVENTORY_API_SCHEMAS[0]}

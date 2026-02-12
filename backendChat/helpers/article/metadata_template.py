# helpers/article/metadata_template.py

ZENODO_METADATA_TEMPLATE = {
    "upload_type": {
        "type": "string",
        "required": True,
        "enum": [
            "publication", "poster", "presentation", "dataset", "image", "video",
            "software", "lesson", "physicalobject", "other"
        ]
    },
    "publication_type": {
        "type": "string",
        "required_if": {"upload_type": "publication"}
    },
    "image_type": {
        "type": "string",
        "required_if": {"upload_type": "image"},
        "enum": ["figure", "plot", "drawing", "diagram", "photo", "other"]
    },
    "publication_date": {
        "type": "string",
        "format": "date",
        "required": True,
        "default": "today"
    },
    "title": {"type": "string", "required": True},

    "creators": {
        "type": "array",
        "required": True,
        "items": {
            "name": "string",
            "affiliation": "string?",
            "orcid": "string?",
            "gnd": "string?"
        }
    },

    "description": {
        "type": "string",
        "required": True,
        "html_allowed": True
    },

    "access_right": {
        "type": "string",
        "required": True,
        "enum": ["open", "embargoed", "restricted", "closed"],
        "default": "open"
    },
    "license": {
        "type": "string",
        "required_if": {"access_right": ["open", "embargoed"]}
    },
    "embargo_date": {
        "type": "string",
        "format": "date",
        "required_if": {"access_right": "embargoed"}
    },
    "access_conditions": {
        "type": "string",
        "html_allowed": True,
        "required_if": {"access_right": "restricted"}
    },

    "doi": {"type": "string", "required": False},
    "prereserve_doi": {"type": ["boolean", "object"], "required": False},

    "keywords": {"type": "array", "items": "string", "required": False},
    "notes": {"type": "string", "html_allowed": True, "required": False},
    "references": {"type": "array", "items": "string", "required": False},

    "related_identifiers": {
        "type": "array",
        "required": False,
        "items": {
            "identifier": "string",
            "relation": "string",
            "resource_type": "string?"
        }
    },

    "contributors": {
        "type": "array",
        "required": False,
        "items": {
            "name": "string",
            "type": "string",
            "affiliation": "string?",
            "orcid": "string?",
            "gnd": "string?"
        }
    },

    "communities": {
        "type": "array",
        "required": False,
        "items": {"identifier": "string"}
    },

    "journal_title": {"type": "string?", "required": False},
    "journal_volume": {"type": "string?", "required": False},
    "journal_issue": {"type": "string?", "required": False},
    "journal_pages": {"type": "string?", "required": False},

    "conference_title": {"type": "string?", "required": False},
    "conference_acronym": {"type": "string?", "required": False},
    "conference_dates": {"type": "string?", "required": False},
    "conference_place": {"type": "string?", "required": False},
    "conference_url": {"type": "string?", "required": False},
    "conference_session": {"type": "string?", "required": False},
    "conference_session_part": {"type": "string?", "required": False},

    "imprint_publisher": {"type": "string?", "required": False},
    "imprint_isbn": {"type": "string?", "required": False},
    "imprint_place": {"type": "string?", "required": False},

    "partof_title": {"type": "string?", "required": False},
    "partof_pages": {"type": "string?", "required": False},

    "thesis_supervisors": {"type": "array?", "required": False},
    "thesis_university": {"type": "string?", "required": False},

    "subjects": {
        "type": "array",
        "required": False,
        "items": {"term": "string", "identifier": "string", "scheme": "string"}
    },

    "version": {"type": "string", "required": False},
    "language": {"type": "string", "required": False},

    "locations": {
        "type": "array",
        "required": False,
        "items": {"lat": "number?", "lon": "number?", "place": "string", "description": "string?"}
    },
    "dates": {
        "type": "array",
        "required": False,
        "items": {"start": "string?", "end": "string?", "type": "string", "description": "string?"}
    },

    "method": {"type": "string", "html_allowed": True, "required": False}
}

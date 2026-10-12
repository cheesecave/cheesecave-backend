"""Centralized constants for KohakuHub backend.

This module contains frequently used strings, error messages, and configuration values
to reduce duplication and maintain consistency across the codebase.
"""

# Date/time format strings
DATETIME_FORMAT_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"

# Error messages
ERROR_USER_NOT_FOUND = "User not found"
ERROR_ORG_NOT_FOUND = "Organization not found"
ERROR_REPO_NOT_FOUND = "Repository not found"
ERROR_INVITATION_NOT_FOUND = "Invitation not found"
ERROR_FALLBACK_SOURCE_NOT_FOUND = "Fallback source not found"
ERROR_INVALID_INVITATION_DATA = "Invalid invitation data"
ERROR_USER_AUTH_REQUIRED = "User authentication required"
ERROR_NOT_AUTHORIZED_MANAGE_TOKENS = "Not authorized to manage these tokens"

# Organization roles, weakest to strongest. ``super-admin`` is the creator-level role.
ORG_ROLE_SUPER_ADMIN = "super-admin"
ORG_ROLES = ("visitor", "member", "admin", ORG_ROLE_SUPER_ADMIN)

# Default values
DEFAULT_EMAIL = "noreply@kohakuhub.local"
DEFAULT_COMMIT_MESSAGE = "Initial commit"

# Database constants
DB_ON_DELETE_SET_NULL = "SET NULL"
DB_ERROR_ALREADY_EXISTS = "already exists"
DB_ERROR_DUPLICATE_COLUMN = "duplicate column"
DB_ERROR_DOES_NOT_EXIST = "does not exist"

# S3 endpoints
S3_ENDPOINT_MINIO = "http://minio:9000"
S3_ENDPOINT_GARAGE = "http://garage:39000"
S3_ENDPOINT_GARAGE_OLD = "http://garage:3900"  # Legacy, used in some configs

# MIME types
MIME_TYPE_JPEG = "image/jpeg"

# Git special files
GIT_ATTRIBUTES_FILE = ".gitattributes"

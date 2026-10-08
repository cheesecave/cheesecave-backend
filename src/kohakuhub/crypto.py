"""Cryptographic utilities for encrypting/decrypting sensitive data.

Uses Fernet (AES-256-CBC with HMAC-SHA256) for symmetric encryption.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from kohakuhub.config import cfg
from kohakuhub.logger import get_logger

logger = get_logger("CRYPTO")


def _get_fernet_key() -> bytes:
    """Get Fernet encryption key from config.

    Derives a proper Fernet key from DATABASE_KEY config value.
    Fernet requires 32 url-safe base64-encoded bytes.

    Returns:
        Base64-encoded 32-byte key suitable for Fernet

    Raises:
        ValueError: If DATABASE_KEY is not configured
    """
    database_key = cfg.app.database_key

    if not database_key:
        raise ValueError(
            "DATABASE_KEY not configured. Set CHEESE_CAVE_DATABASE_KEY (or KOHAKU_HUB_DATABASE_KEY) environment variable. "
            "Generate with: openssl rand -hex 32"
        )

    # Hash the database key to get exactly 32 bytes
    key_hash = hashlib.sha256(database_key.encode()).digest()

    # Encode to base64 for Fernet
    return base64.urlsafe_b64encode(key_hash)


def encrypt_token(token: str) -> str:
    """Encrypt a token using Fernet symmetric encryption.

    Args:
        token: Plain text token to encrypt

    Returns:
        Base64-encoded encrypted token

    Raises:
        ValueError: If DATABASE_KEY is not configured

    Example:
        >>> encrypted = encrypt_token("hf_abc123")
        >>> # Returns: "gAAAAABf..."
    """
    if not token:
        return ""

    try:
        fernet_key = _get_fernet_key()
        fernet = Fernet(fernet_key)

        # Encrypt and return as string
        encrypted_bytes = fernet.encrypt(token.encode())
        encrypted_str = encrypted_bytes.decode()

        logger.debug(f"Token encrypted (length: {len(token)} -> {len(encrypted_str)})")
        return encrypted_str

    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a token using Fernet symmetric decryption.

    Args:
        encrypted_token: Base64-encoded encrypted token

    Returns:
        Decrypted plain text token

    Raises:
        ValueError: If DATABASE_KEY is not configured or token is invalid
        InvalidToken: If decryption fails (wrong key, corrupted data)

    Example:
        >>> decrypted = decrypt_token("gAAAAABf...")
        >>> # Returns: "hf_abc123"
    """
    if not encrypted_token:
        return ""

    try:
        fernet_key = _get_fernet_key()
        fernet = Fernet(fernet_key)

        # Decrypt and return as string
        decrypted_bytes = fernet.decrypt(encrypted_token.encode())
        decrypted_str = decrypted_bytes.decode()

        logger.debug(
            f"Token decrypted (length: {len(encrypted_token)} -> {len(decrypted_str)})"
        )
        return decrypted_str

    except InvalidToken:
        logger.error("Decryption failed: Invalid token or wrong encryption key")
        raise ValueError(
            "Failed to decrypt token - invalid token or wrong encryption key"
        )
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        raise


def mask_token(token: str, show_chars: int = 4) -> str:
    """Mask a token for safe display (e.g., "hf_***" or "token...xyz").

    Args:
        token: Token to mask
        show_chars: Number of characters to show at start (default: 4)

    Returns:
        Masked token string

    Example:
        >>> mask_token("hf_abcdefgh123456")
        >>> # Returns: "hf_a***"
    """
    if not token:
        return ""

    if len(token) <= show_chars:
        return "***"

    return f"{token[:show_chars]}***"

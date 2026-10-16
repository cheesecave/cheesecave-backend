#!/usr/bin/env python3
"""
Generate cryptographically secure random secrets for KohakuHub configuration.

Usage:
    python generate_secret.py [length]
    python generate_secret.py 32        # Generate 32-character secret
    python generate_secret.py           # Default: 64 characters

Use cases:
    - CHEESE_CAVE_SESSION_SECRET
    - CHEESE_CAVE_ADMIN_SECRET_TOKEN
    - LAKEFS_AUTH_ENCRYPT_SECRET_KEY
    - Any other secret configuration values
"""

import argparse
import secrets
import string
import sys


def generate_secret(length: int = 64, charset: str = "all") -> str:
    """Generate a cryptographically secure random secret.

    Args:
        length: Length of the secret to generate
        charset: Character set to use ('all', 'alphanumeric', 'safe', 'base64')

    Returns:
        Random secret string
    """
    if charset == "alphanumeric":
        # Only letters and numbers (safe for all contexts)
        alphabet = string.ascii_letters + string.digits
    elif charset == "safe":
        # URL-safe, DNS-safe: [a-zA-Z0-9.-_]
        alphabet = string.ascii_letters + string.digits + ".-_"
    elif charset == "base64":
        # Base64-safe characters (URL-safe, no period)
        alphabet = string.ascii_letters + string.digits + "-_"
    else:  # "all"
        # All printable ASCII except quotes and backslash (shell-safe)
        alphabet = string.ascii_letters + string.digits + "!#$%&()*+,-./:;<=>?@[]^_{|}~"

    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_multiple_secrets():
    """Generate multiple secrets for common use cases."""
    print("=" * 70)
    print("KohakuHub Secret Generator - Multiple Secrets")
    print("=" * 70)
    print("\nGenerated secrets for common configuration values:\n")

    secrets_config = [
        ("CHEESE_CAVE_SESSION_SECRET", 64, "all"),
        ("CHEESE_CAVE_ADMIN_SECRET_TOKEN", 64, "all"),
        ("LAKEFS_AUTH_ENCRYPT_SECRET_KEY", 32, "alphanumeric"),
    ]

    for name, length, charset in secrets_config:
        secret = generate_secret(length, charset)
        print(f"{name}:")
        print(f"  {secret}")
        print()

    print("=" * 70)
    print("Copy these values to your .env file or docker-compose.yml")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Generate cryptographically secure random secrets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate default 64-character secret
  python generate_secret.py

  # Generate 32-character secret
  python generate_secret.py 32

  # Generate alphanumeric-only secret (safer for some contexts)
  python generate_secret.py 64 --charset alphanumeric

  # Generate safe secret [a-zA-Z0-9.-_] (URL-safe, DNS-safe)
  python generate_secret.py 64 --charset safe

  # Generate base64-safe secret [a-zA-Z0-9-_] (URL-safe, no period)
  python generate_secret.py 64 --charset base64

  # Generate multiple secrets for all config values
  python generate_secret.py --all

Common lengths:
  32  - Good for most secrets (256 bits)
  64  - Extra secure (512 bits) - recommended
  128 - Maximum security (1024 bits)

Common use cases:
  CHEESE_CAVE_SESSION_SECRET         - 64 chars (all)
  CHEESE_CAVE_ADMIN_SECRET_TOKEN     - 64 chars (all)
  LAKEFS_AUTH_ENCRYPT_SECRET_KEY    - 32 chars (alphanumeric)
  Database passwords                 - 32-64 chars (alphanumeric)
        """,
    )

    parser.add_argument(
        "length",
        type=int,
        nargs="?",
        default=64,
        help="Length of the secret (default: 64)",
    )

    parser.add_argument(
        "--charset",
        choices=["all", "alphanumeric", "safe", "base64"],
        default="all",
        help="Character set to use (default: all)",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate multiple secrets for all common config values",
    )

    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy secret to clipboard (requires pyperclip)",
    )

    args = parser.parse_args()

    # Validate length
    if args.length < 16:
        print("Error: Secret length must be at least 16 characters", file=sys.stderr)
        sys.exit(1)

    if args.length > 512:
        print(
            "Warning: Secret length >512 is excessive and may cause issues",
            file=sys.stderr,
        )

    # Generate multiple secrets mode
    if args.all:
        generate_multiple_secrets()
        return

    # Generate single secret
    secret = generate_secret(args.length, args.charset)

    # Print the secret
    print("=" * 70)
    print(f"Generated {args.length}-character secret ({args.charset} charset):")
    print("=" * 70)
    print()
    print(secret)
    print()
    print("=" * 70)
    print(f"Entropy: ~{args.length * 6} bits (secure)")
    print("=" * 70)

    # Copy to clipboard if requested
    if args.copy:
        try:
            import pyperclip

            pyperclip.copy(secret)
            print("\n✓ Secret copied to clipboard!")
        except ImportError:
            print(
                "\nNote: Install pyperclip to enable clipboard copy: pip install pyperclip"
            )


if __name__ == "__main__":
    main()

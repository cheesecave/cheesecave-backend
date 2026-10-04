#!/usr/bin/env python3
"""Legacy entrypoint: use native Compose; no deployment runs from this wrapper."""


def main():
    print("Use docker compose up -d for selected release images.")
    print(
        "For sibling source builds: docker compose -f compose.yml -f compose.build.yml up -d --build"
    )
    print("See docs/deployment/docker.md for secrets, updates and rollback.")


if __name__ == "__main__":
    main()

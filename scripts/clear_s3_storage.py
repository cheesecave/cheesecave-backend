"""Clear S3 storage.

This script removes all or selected content from an S3 bucket.
Useful for demo deployments with limited storage (e.g., CloudFlare R2 free tier).

Usage:
    # Clear all content from a bucket
    python scripts/clear_s3_storage.py \\
        --endpoint https://s3.amazonaws.com \\
        --access-key YOUR_ACCESS_KEY \\
        --secret-key YOUR_SECRET_KEY \\
        --bucket my-bucket

    # Clear only LFS files
    python scripts/clear_s3_storage.py \\
        --endpoint https://s3.amazonaws.com \\
        --access-key YOUR_ACCESS_KEY \\
        --secret-key YOUR_SECRET_KEY \\
        --bucket my-bucket \\
        --prefix lfs/

    # Dry run (show what would be deleted without deleting)
    python scripts/clear_s3_storage.py \\
        --endpoint https://s3.amazonaws.com \\
        --access-key YOUR_ACCESS_KEY \\
        --secret-key YOUR_SECRET_KEY \\
        --bucket my-bucket \\
        --dry-run

    # Use environment variables for credentials
    export S3_ENDPOINT=https://s3.amazonaws.com
    export S3_ACCESS_KEY=YOUR_ACCESS_KEY
    export S3_SECRET_KEY=YOUR_SECRET_KEY
    export S3_BUCKET=my-bucket
    python scripts/clear_s3_storage.py

Requirements:
    - boto3 and rich packages, and the kohakuhub package (pip install -e .)
    - S3 credentials with delete permissions

An endpoint with a path names the real bucket, as KohakuHub reads it
(``https://<account>.r2.cloudflarestorage.com/<bucket>``): --bucket is then a
key prefix inside it, and nothing outside that prefix is listed or deleted.
"""

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm
from rich.table import Table

from kohakuhub.utils.s3 import bucket_in_endpoint, readdress

console = Console()


def get_s3_client(endpoint, access_key, secret_key, region="us-east-1", bucket=None):
    """Create S3 client with provided credentials.

    Args:
        endpoint: S3 endpoint URL
        access_key: S3 access key
        secret_key: S3 secret key
        region: S3 region (default: us-east-1)
        bucket: The bucket the script works on, a key prefix when the
            endpoint's path names the real bucket

    Returns:
        boto3 S3 client
    """
    # An endpoint with a path names the real bucket, as KohakuHub reads it:
    # ``bucket`` is then a key prefix inside it (docs/reference/config.md)
    layout = bucket_in_endpoint(endpoint, bucket) if bucket else None
    client = boto3.client(
        "s3",
        endpoint_url=layout[0] if layout else endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=boto3.session.Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )
    return readdress(client, bucket, layout) if layout else client


def list_objects(s3_client, bucket, prefixes=None, max_objects=None):
    """List all objects in bucket matching prefixes.

    Args:
        s3_client: Boto3 S3 client
        bucket: Bucket name
        prefixes: List of prefixes to match (None = all objects)
        max_objects: Maximum objects to list (None = unlimited)

    Returns:
        List of object keys
    """
    objects = []
    total_size = 0

    if not prefixes or len(prefixes) == 0:
        prefixes = [None]  # List all objects

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning S3 bucket...", total=None)

        for prefix in prefixes:
            paginator = s3_client.get_paginator("list_objects_v2")
            page_params = {"Bucket": bucket}
            if prefix:
                page_params["Prefix"] = prefix

            try:
                for page in paginator.paginate(**page_params):
                    if "Contents" in page:
                        for obj in page["Contents"]:
                            objects.append(obj["Key"])
                            total_size += obj["Size"]
                            progress.update(
                                task,
                                description=f"Scanning S3 bucket... ({len(objects)} objects found, {format_size(total_size)})",
                            )

                            if max_objects and len(objects) >= max_objects:
                                return objects, total_size

            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchBucket":
                    console.print(f"[red]Error: Bucket '{bucket}' does not exist[/red]")
                    return [], 0
                raise

    return objects, total_size


def delete_objects(s3_client, bucket, object_keys, batch_size=1000):
    """Delete objects from S3 bucket in batches.

    Args:
        s3_client: Boto3 S3 client
        bucket: Bucket name
        object_keys: List of object keys to delete
        batch_size: Number of objects to delete per batch (max 1000)

    Returns:
        Number of objects successfully deleted
    """
    total = len(object_keys)
    deleted = 0
    errors = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("Deleting objects...", total=total)

        # Delete in batches (S3 allows max 1000 objects per delete request)
        for i in range(0, total, batch_size):
            batch = object_keys[i : i + batch_size]
            delete_keys = [{"Key": key} for key in batch]

            try:
                response = s3_client.delete_objects(
                    Bucket=bucket, Delete={"Objects": delete_keys, "Quiet": False}
                )

                # Count successful deletions
                if "Deleted" in response:
                    deleted += len(response["Deleted"])

                # Track errors
                if "Errors" in response:
                    errors.extend(response["Errors"])

                progress.update(task, advance=len(batch))

            except ClientError as e:
                console.print(f"[red]Error deleting batch: {e}[/red]")
                errors.append({"Key": "batch", "Code": str(e)})

    return deleted, errors


def format_size(bytes_size):
    """Format bytes to human-readable size."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} PB"


def display_summary(bucket, objects, total_size, prefixes=None):
    """Display summary table of objects to be deleted."""
    table = Table(
        title="S3 Storage Clear Summary", show_header=True, header_style="bold"
    )
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="yellow")

    table.add_row("Bucket", bucket)
    table.add_row(
        "Prefixes", ", ".join(prefixes) if prefixes else "ALL (entire bucket)"
    )
    table.add_row("Objects to delete", f"{len(objects):,}")
    table.add_row("Total size", format_size(total_size))

    console.print(table)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Clear S3 storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Clear all content (interactive confirmation)
  python scripts/clear_s3_storage.py \\
      --endpoint https://s3.amazonaws.com \\
      --access-key YOUR_KEY \\
      --secret-key YOUR_SECRET \\
      --bucket my-bucket

  # Clear only LFS files
  python scripts/clear_s3_storage.py \\
      --endpoint https://s3.amazonaws.com \\
      --access-key YOUR_KEY \\
      --secret-key YOUR_SECRET \\
      --bucket my-bucket \\
      --prefix lfs/

  # Use environment variables
  export S3_ENDPOINT=https://s3.amazonaws.com
  export S3_ACCESS_KEY=YOUR_KEY
  export S3_SECRET_KEY=YOUR_SECRET
  export S3_BUCKET=my-bucket
  python scripts/clear_s3_storage.py

Common prefixes in KohakuHub:
  - lfs/              : All LFS (large file storage) objects
  - hf-model-         : All model repositories
  - hf-dataset-       : All dataset repositories
  - hf-space-         : All space repositories
        """,
    )

    # S3 connection arguments
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("S3_ENDPOINT"),
        help="S3 endpoint URL (or set S3_ENDPOINT env var)",
    )
    parser.add_argument(
        "--access-key",
        default=os.environ.get("S3_ACCESS_KEY"),
        help="S3 access key (or set S3_ACCESS_KEY env var)",
    )
    parser.add_argument(
        "--secret-key",
        default=os.environ.get("S3_SECRET_KEY"),
        help="S3 secret key (or set S3_SECRET_KEY env var)",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("S3_BUCKET"),
        help="S3 bucket name (or set S3_BUCKET env var)",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("S3_REGION", "us-east-1"),
        help="S3 region (default: us-east-1, or set S3_REGION env var)",
    )

    # Operation arguments
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        help="Only delete objects with this prefix (can be specified multiple times)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt (dangerous!)",
    )
    parser.add_argument(
        "--max-objects",
        type=int,
        help="Maximum number of objects to list/delete (for testing)",
    )

    args = parser.parse_args()

    # Validate required arguments
    if not args.endpoint:
        console.print(
            "[red]Error: --endpoint is required (or set S3_ENDPOINT env var)[/red]"
        )
        sys.exit(1)
    if not args.access_key:
        console.print(
            "[red]Error: --access-key is required (or set S3_ACCESS_KEY env var)[/red]"
        )
        sys.exit(1)
    if not args.secret_key:
        console.print(
            "[red]Error: --secret-key is required (or set S3_SECRET_KEY env var)[/red]"
        )
        sys.exit(1)
    if not args.bucket:
        console.print(
            "[red]Error: --bucket is required (or set S3_BUCKET env var)[/red]"
        )
        sys.exit(1)

    bucket = args.bucket

    # Create S3 client
    try:
        s3_client = get_s3_client(
            endpoint=args.endpoint,
            access_key=args.access_key,
            secret_key=args.secret_key,
            region=args.region,
            bucket=args.bucket,
        )
    except Exception as e:
        console.print(f"[red]Error connecting to S3: {e}[/red]")
        sys.exit(1)

    # List objects
    console.print(f"\n[bold cyan]Scanning S3 bucket: {bucket}[/bold cyan]\n")
    objects, total_size = list_objects(
        s3_client, bucket, prefixes=args.prefixes, max_objects=args.max_objects
    )

    if len(objects) == 0:
        console.print("[yellow]No objects found to delete.[/yellow]")
        return

    # Display summary
    display_summary(bucket, objects, total_size, prefixes=args.prefixes)

    # Dry run mode
    if args.dry_run:
        console.print("\n[yellow]DRY RUN MODE - No objects will be deleted[/yellow]")
        console.print("\nObjects that would be deleted:")
        for i, key in enumerate(objects[:20], 1):  # Show first 20
            console.print(f"  {i}. {key}")
        if len(objects) > 20:
            console.print(f"  ... and {len(objects) - 20} more")
        return

    # Confirmation
    console.print()
    if not args.force:
        console.print(
            "[bold red]WARNING: This will permanently delete all listed objects![/bold red]"
        )
        console.print("[bold red]This action CANNOT be undone![/bold red]")
        console.print()

        if not Confirm.ask(
            f"Are you sure you want to delete {len(objects)} object(s) from '{bucket}'?"
        ):
            console.print("[yellow]Aborted.[/yellow]")
            return

        # Double confirmation for full bucket deletion
        if not args.prefixes:
            console.print()
            if not Confirm.ask(
                "[bold red]FINAL CONFIRMATION: Delete ENTIRE bucket contents?[/bold red]"
            ):
                console.print("[yellow]Aborted.[/yellow]")
                return

    # Delete objects
    console.print()
    deleted, errors = delete_objects(s3_client, bucket, objects)

    # Results
    console.print()
    if deleted > 0:
        console.print(f"[green]✓ Successfully deleted {deleted:,} object(s)[/green]")
        console.print(
            f"[green]✓ Freed up approximately {format_size(total_size)}[/green]"
        )

    if errors:
        console.print(f"\n[red]✗ {len(errors)} error(s) occurred:[/red]")
        for error in errors[:10]:  # Show first 10 errors
            console.print(
                f"  - {error.get('Key', 'unknown')}: {error.get('Code', 'unknown')}"
            )
        if len(errors) > 10:
            console.print(f"  ... and {len(errors) - 10} more errors")

    console.print()


if __name__ == "__main__":
    main()

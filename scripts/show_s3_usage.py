"""Show S3 storage usage.

This script displays storage statistics without making any changes.
Useful for monitoring storage usage on limited plans (e.g., CloudFlare R2 free tier).

Usage:
    python scripts/show_s3_usage.py \\
        --endpoint https://s3.amazonaws.com \\
        --access-key YOUR_KEY \\
        --secret-key YOUR_SECRET \\
        --bucket my-bucket

    python scripts/show_s3_usage.py \\
        --endpoint https://s3.amazonaws.com \\
        --access-key YOUR_KEY \\
        --secret-key YOUR_SECRET \\
        --bucket my-bucket \\
        --detailed

Requirements:
    - boto3 and rich packages, and the kohakuhub package (pip install -e .)
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.tree import Tree

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


def format_size(bytes_size):
    """Format bytes to human-readable size."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} PB"


def analyze_storage(s3_client, bucket):
    """Analyze storage usage by prefix.

    Returns:
        dict: Storage statistics by prefix
    """
    stats = defaultdict(lambda: {"count": 0, "size": 0, "objects": []})

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task("Analyzing storage...", total=None)

        paginator = s3_client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=bucket):
                if "Contents" not in page:
                    continue

                for obj in page["Contents"]:
                    key = obj["Key"]
                    size = obj["Size"]

                    # Categorize by prefix
                    if key.startswith("lfs/"):
                        prefix = "lfs"
                    elif key.startswith("hf-model-"):
                        prefix = "models"
                    elif key.startswith("hf-dataset-"):
                        prefix = "datasets"
                    elif key.startswith("hf-space-"):
                        prefix = "spaces"
                    else:
                        prefix = "other"

                    stats[prefix]["count"] += 1
                    stats[prefix]["size"] += size
                    stats[prefix]["objects"].append({"key": key, "size": size})

                    # Update total
                    stats["total"]["count"] += 1
                    stats["total"]["size"] += size

        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucket":
                console.print(f"[red]Error: Bucket '{bucket}' does not exist[/red]")
                return None
            raise

    return stats


def display_summary(bucket, stats, detailed=False):
    """Display storage summary table."""
    table = Table(
        title=f"S3 Storage Usage: {bucket}", show_header=True, header_style="bold cyan"
    )
    table.add_column("Category", style="yellow")
    table.add_column("Objects", justify="right", style="magenta")
    table.add_column("Total Size", justify="right", style="green")
    table.add_column("Percentage", justify="right", style="cyan")

    total_size = stats["total"]["size"]

    # Sort by size (descending)
    categories = [
        ("lfs", "LFS Files (>5MB)"),
        ("models", "Model Repositories"),
        ("datasets", "Dataset Repositories"),
        ("spaces", "Space Repositories"),
        ("other", "Other"),
    ]

    for prefix, label in categories:
        if prefix in stats and stats[prefix]["count"] > 0:
            count = stats[prefix]["count"]
            size = stats[prefix]["size"]
            percentage = (size / total_size * 100) if total_size > 0 else 0

            table.add_row(
                label,
                f"{count:,}",
                format_size(size),
                f"{percentage:.1f}%",
            )

    # Add total row
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{stats['total']['count']:,}[/bold]",
        f"[bold]{format_size(stats['total']['size'])}[/bold]",
        "[bold]100.0%[/bold]",
        style="bold blue",
    )

    console.print(table)

    # Detailed view
    if detailed and stats["total"]["count"] > 0:
        console.print()
        tree = Tree(f"[bold cyan]Storage Breakdown[/bold cyan]")

        for prefix, label in categories:
            if prefix in stats and stats[prefix]["count"] > 0:
                branch = tree.add(
                    f"[yellow]{label}[/yellow] - {format_size(stats[prefix]['size'])}"
                )

                # Show top 10 largest objects in this category
                objects = sorted(
                    stats[prefix]["objects"], key=lambda x: x["size"], reverse=True
                )[:10]
                for obj in objects:
                    branch.add(
                        f"{obj['key']} - [green]{format_size(obj['size'])}[/green]"
                    )

                if len(stats[prefix]["objects"]) > 10:
                    branch.add(
                        f"[dim]... and {len(stats[prefix]['objects']) - 10} more[/dim]"
                    )

        console.print(tree)


def display_quota_warning(total_size, quota_gb=10):
    """Display warning if approaching quota limit."""
    quota_bytes = quota_gb * 1000**3
    percentage = (total_size / quota_bytes * 100) if quota_bytes > 0 else 0

    console.print()
    if percentage >= 90:
        console.print(
            f"[bold red]⚠ WARNING: Using {percentage:.1f}% of {quota_gb}GB quota![/bold red]"
        )
        console.print(
            f"[red]Consider running: python scripts/clear_s3_storage.py --prefix lfs/[/red]"
        )
    elif percentage >= 75:
        console.print(
            f"[bold yellow]⚠ Approaching quota limit: {percentage:.1f}% of {quota_gb}GB used[/bold yellow]"
        )
    else:
        console.print(
            f"[green]✓ Storage usage: {percentage:.1f}% of {quota_gb}GB quota[/green]"
        )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Show S3 storage usage")

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

    # Display options
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Show detailed breakdown with top objects",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--quota-gb",
        type=int,
        default=10,
        help="Storage quota in GB for warning calculation (default: 10GB for R2 free tier)",
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

    # Analyze storage
    stats = analyze_storage(s3_client, bucket)

    if stats is None:
        sys.exit(1)

    if stats["total"]["count"] == 0:
        console.print("[yellow]Bucket is empty.[/yellow]")
        return

    # Output format
    if args.json:
        # JSON output
        output = {
            "bucket": bucket,
            "total_objects": stats["total"]["count"],
            "total_size_bytes": stats["total"]["size"],
            "total_size_human": format_size(stats["total"]["size"]),
            "categories": {},
        }

        for prefix in ["lfs", "models", "datasets", "spaces", "other"]:
            if prefix in stats and stats[prefix]["count"] > 0:
                output["categories"][prefix] = {
                    "count": stats[prefix]["count"],
                    "size_bytes": stats[prefix]["size"],
                    "size_human": format_size(stats[prefix]["size"]),
                }

        print(json.dumps(output, indent=2))
    else:
        # Rich table output
        console.print()
        display_summary(bucket, stats, detailed=args.detailed)
        display_quota_warning(stats["total"]["size"], quota_gb=args.quota_gb)
        console.print()


if __name__ == "__main__":
    main()

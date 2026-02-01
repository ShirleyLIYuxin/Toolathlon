#!/usr/bin/env python3
"""
CLI for converting Toolathlon tasks to Harbor format.

Usage:
    # Convert all tasks
    python run_adapter.py --all --output-dir ./harbor_tasks

    # Convert specific tasks
    python run_adapter.py --tasks ab-testing canvas-do-quiz --output-dir ./harbor_tasks

    # List available tasks
    python run_adapter.py --list
"""

import argparse
import sys
from pathlib import Path

from adapter import ToolathlonToHarbor


def main():
    parser = argparse.ArgumentParser(
        description="Convert Toolathlon tasks to Harbor format"
    )

    parser.add_argument(
        "--toolathlon-root",
        type=Path,
        default=Path(__file__).parent.parent.parent,
        help="Path to Toolathlon repository root",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./harbor_tasks"),
        help="Output directory for Harbor tasks",
    )
    parser.add_argument(
        "--tasks",
        nargs="+",
        help="Specific task IDs to convert",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Convert all available tasks",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available task IDs",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing tasks",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Timeout in seconds for each task (default: 1800)",
    )

    args = parser.parse_args()

    converter = ToolathlonToHarbor(
        harbor_tasks_root=args.output_dir,
        toolathlon_root=args.toolathlon_root,
        max_timeout_sec=args.timeout,
    )

    if args.list:
        task_ids = converter.get_all_task_ids()
        print(f"Found {len(task_ids)} tasks:\n")
        for task_id in task_ids:
            try:
                task = converter.load_task(task_id)
                servers = ", ".join(task.needed_mcp_servers[:3])
                if len(task.needed_mcp_servers) > 3:
                    servers += f" (+{len(task.needed_mcp_servers) - 3} more)"
                print(f"  {task_id}")
                print(f"    MCP servers: {servers or 'none'}")
            except Exception as e:
                print(f"  {task_id} (error: {e})")
        return 0

    if args.all:
        task_ids = converter.get_all_task_ids()
    elif args.tasks:
        task_ids = args.tasks
    else:
        parser.print_help()
        print("\nError: Must specify --tasks, --all, or --list")
        return 1

    print(f"Converting {len(task_ids)} tasks to {args.output_dir}...")
    success, failures = converter.generate_many(task_ids, overwrite=args.overwrite)

    print(f"\n{'=' * 50}")
    print(f"Results: {len(success)} succeeded, {len(failures)} failed")

    if failures:
        print("\nFailed tasks:")
        for task_id, reason in failures:
            print(f"  {task_id}: {reason}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

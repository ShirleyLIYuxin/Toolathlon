#!/usr/bin/env python3
"""
Migrate hardcoded ports in JSON config files to use template variables.

This script converts:
    "imap_port": 1143       ->  "imap_port": "${instance.port_imap}"
    "smtp_port": 1587       ->  "smtp_port": "${instance.port_smtp_submission}"

Usage:
    # Preview changes (dry run)
    python global_preparation/migrate_ports_to_variables.py --dry-run

    # Apply changes
    python global_preparation/migrate_ports_to_variables.py

    # Restore from backup (if needed)
    python global_preparation/migrate_ports_to_variables.py --restore
"""

import argparse
import json
import shutil
from pathlib import Path
from typing import Dict, Any, List, Tuple


# Port value to variable name mapping
PORT_MAPPINGS = {
    # IMAP
    1143: "${instance.port_imap}",
    2143: "${instance.port_imap}",  # Alternative port

    # SMTP submission
    1587: "${instance.port_smtp_submission}",
    2587: "${instance.port_smtp_submission}",  # Alternative port

    # SMTP
    2525: "${instance.port_smtp}",
    3525: "${instance.port_smtp}",  # Alternative port

    # Canvas
    10001: "${instance.port_canvas_http}",
    11001: "${instance.port_canvas_http}",  # Alternative port
    20001: "${instance.port_canvas_https}",
    21001: "${instance.port_canvas_https}",  # Alternative port

    # WooCommerce
    10003: "${instance.port_woocommerce}",
    11003: "${instance.port_woocommerce}",  # Alternative port

    # Email web UI
    10005: "${instance.port_email_web}",
    11005: "${instance.port_email_web}",  # Alternative port
}

# Keys in JSON that should be converted
PORT_KEYS = {
    'imap_port', 'smtp_port', 'port', 'http_port', 'https_port',
    'smtp_submission_port', 'email_port', 'web_port'
}


def find_json_configs(root_dir: Path) -> List[Path]:
    """Find all JSON config files that might contain port numbers."""
    patterns = [
        "configs/**/*.json",
        "tasks/**/email_config.json",
        "tasks/**/emails_config.json",
        "tasks/**/*_config.json",
        "tasks/**/receiver_config.json",
        "tasks/**/receivers_config.json",
    ]

    files = set()
    for pattern in patterns:
        files.update(root_dir.glob(pattern))

    # Filter out non-config files
    exclude_patterns = ['package.json', 'package-lock.json', 'tsconfig.json']
    files = [f for f in files if f.name not in exclude_patterns]

    return sorted(files)


def migrate_json_file(file_path: Path, dry_run: bool = True) -> Tuple[bool, List[str]]:
    """
    Migrate a single JSON file.

    Returns:
        (was_modified, list_of_changes)
    """
    changes = []

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, [f"Skipped (parse error): {e}"]

    modified = False

    def process_value(obj: Any, path: str = "") -> Any:
        nonlocal modified

        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                new_path = f"{path}.{key}" if path else key
                result[key] = process_value(value, new_path)
            return result

        elif isinstance(obj, list):
            return [process_value(item, f"{path}[{i}]") for i, item in enumerate(obj)]

        elif isinstance(obj, int) and obj in PORT_MAPPINGS:
            # Check if this looks like a port field by the key name
            key_name = path.split('.')[-1] if path else ""
            if any(port_key in key_name.lower() for port_key in ['port', 'imap', 'smtp']):
                var_name = PORT_MAPPINGS[obj]
                changes.append(f"  {path}: {obj} -> {var_name}")
                modified = True
                return var_name

        return obj

    new_data = process_value(data)

    if modified and not dry_run:
        # Create backup
        backup_path = file_path.with_suffix('.json.bak')
        shutil.copy2(file_path, backup_path)

        # Write updated file
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(new_data, f, indent=4, ensure_ascii=False)
            f.write('\n')

    return modified, changes


def restore_from_backup(file_path: Path) -> bool:
    """Restore a file from its backup."""
    backup_path = file_path.with_suffix('.json.bak')
    if backup_path.exists():
        shutil.copy2(backup_path, file_path)
        backup_path.unlink()
        return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description='Migrate hardcoded ports to template variables'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview changes without modifying files'
    )
    parser.add_argument(
        '--restore', action='store_true',
        help='Restore files from backup'
    )
    parser.add_argument(
        '--root', type=str, default='.',
        help='Root directory to search for configs'
    )
    args = parser.parse_args()

    root_dir = Path(args.root).resolve()
    print(f"Root directory: {root_dir}")

    if args.restore:
        print("\nRestoring from backups...")
        json_files = find_json_configs(root_dir)
        restored = 0
        for file_path in json_files:
            if restore_from_backup(file_path):
                print(f"  Restored: {file_path.relative_to(root_dir)}")
                restored += 1
        print(f"\nRestored {restored} files")
        return

    print("\nFinding JSON config files...")
    json_files = find_json_configs(root_dir)
    print(f"Found {len(json_files)} config files")

    if args.dry_run:
        print("\n=== DRY RUN - No files will be modified ===\n")
    else:
        print("\n=== APPLYING CHANGES ===\n")

    modified_count = 0
    for file_path in json_files:
        was_modified, changes = migrate_json_file(file_path, dry_run=args.dry_run)
        if was_modified:
            modified_count += 1
            rel_path = file_path.relative_to(root_dir)
            print(f"\n{rel_path}:")
            for change in changes:
                print(change)

    print(f"\n{'Would modify' if args.dry_run else 'Modified'} {modified_count} files")

    if args.dry_run and modified_count > 0:
        print("\nRun without --dry-run to apply changes")
        print("Backups will be created as *.json.bak files")


if __name__ == '__main__':
    main()

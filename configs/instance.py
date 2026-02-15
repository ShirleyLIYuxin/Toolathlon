"""
Instance configuration loader for running multiple Toolathlon instances.

This module loads instance-specific configuration (ports, credentials, etc.)
from a YAML file. Each instance on the same machine should use a different
config file to avoid port conflicts.

Usage:
    # Set env var to point to your instance config
    export TOOLATHLON_INSTANCE_CONFIG=/path/to/my_instance.yaml

    # Or place instance.yaml in configs/ directory (default location)

    # In code:
    from configs.instance import instance_config
    print(instance_config.port_imap)  # 1143 or whatever is configured
"""

import os
import yaml
from pathlib import Path
from addict import Dict


def _load_instance_config() -> Dict:
    """
    Load instance configuration from YAML file.

    Priority:
    1. TOOLATHLON_INSTANCE_CONFIG environment variable
    2. configs/instance.yaml (if exists)
    3. Default values
    """
    config = Dict()

    # Default port values
    config.port_canvas_http = 10001
    config.port_canvas_https = 20001
    config.port_imap = 1143
    config.port_smtp = 2525
    config.port_smtp_submission = 1587
    config.port_email_web = 10005
    config.port_woocommerce = 10003
    config.port_k8s_pr_preview = 30123
    config.port_k8s_mysql = 30124
    config.port_meeting_assign = 30137

    # Instance identification (for container naming)
    config.instance_prefix = ""
    config.instance_suffix = ""

    # Try to load from file
    config_path = os.environ.get('TOOLATHLON_INSTANCE_CONFIG')

    if not config_path:
        # Check default location
        default_path = Path(__file__).parent / "instance.yaml"
        if default_path.exists():
            config_path = str(default_path)

    if config_path and Path(config_path).exists():
        try:
            with open(config_path, 'r') as f:
                yaml_config = yaml.safe_load(f) or {}

            # Merge YAML config into defaults (YAML takes precedence)
            for key, value in yaml_config.items():
                config[key] = value

            print(f"[Instance Config] Loaded from: {config_path}")
        except Exception as e:
            print(f"[Instance Config] Warning: Failed to load {config_path}: {e}")
            print("[Instance Config] Using default values")
    else:
        if os.environ.get('TOOLATHLON_INSTANCE_CONFIG'):
            print(f"[Instance Config] Warning: Config file not found: {config_path}")
        # Silent if using defaults

    return config


# Module-level singleton
instance_config = _load_instance_config()


def get_canvas_domain() -> str:
    """Get Canvas domain with correct port."""
    return f"localhost:{instance_config.port_canvas_https}"


def get_woocommerce_base_url(store_path: str = "") -> str:
    """Get WooCommerce base URL with correct port."""
    base = f"http://localhost:{instance_config.port_woocommerce}"
    if store_path:
        return f"{base}/{store_path}"
    return base

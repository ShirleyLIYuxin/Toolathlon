"""
Template variable processor for Toolathlon configuration files.

Supports ${variable} syntax for substitution in strings, lists, and dicts.

Variable namespaces:
    ${config.*}    - Values from global_configs
    ${token.*}     - Values from token_key_session (global or task-local)
    ${instance.*}  - Values from instance config (ports, instance_suffix, etc.)

Example:
    {
        "imap_port": "${instance.port_imap}",
        "canvas_token": "${token.canvas_api_token}"
    }
"""

import re
from typing import Any, Dict, Optional, Callable


def get_default_template_variables(
    local_token_key_session: Optional[Dict] = None,
    extra_vars: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """
    Get the default set of template variables.

    Args:
        local_token_key_session: Task-specific token overrides (takes precedence)
        extra_vars: Additional variables to include

    Returns:
        Dictionary of variable_name -> value
    """
    template_vars = {}

    # Add instance config variables (ports, instance identification)
    try:
        from configs.instance import instance_config
        for key, value in instance_config.items():
            if isinstance(value, (str, int, float, bool)):
                template_vars[f'instance.{key}'] = str(value)
    except ImportError:
        pass  # Instance config not available

    # Add global config variables
    try:
        from configs.global_configs import global_configs
        for key, value in global_configs.items():
            if isinstance(value, (str, int, float, bool)):
                template_vars[f'config.{key}'] = str(value)
    except ImportError:
        pass  # Global configs not set up yet

    # Add global token variables
    try:
        from configs.token_key_session import all_token_key_session
        for key, value in all_token_key_session.items():
            if isinstance(value, (str, int, float, bool)):
                template_vars[f'token.{key}'] = str(value)
    except ImportError:
        pass  # Token session not set up yet

    # Task-specific tokens override global tokens
    if local_token_key_session is not None:
        for key, value in local_token_key_session.items():
            if isinstance(value, (str, int, float, bool)):
                template_vars[f'token.{key}'] = str(value)

    # Add any extra variables
    if extra_vars:
        template_vars.update(extra_vars)

    return template_vars


def process_template(
    obj: Any,
    template_vars: Dict[str, str],
    warn_missing: bool = True,
    on_missing: Optional[Callable[[str], str]] = None
) -> Any:
    """
    Recursively process template variables in an object.

    Args:
        obj: The object to process (string, list, dict, or other)
        template_vars: Dictionary mapping variable names to values
        warn_missing: Whether to print warnings for missing variables
        on_missing: Optional callback for missing variables, returns replacement value

    Returns:
        The processed object with variables substituted
    """
    if isinstance(obj, str):
        pattern = r'\$\{([^}]+)\}'

        def replacer(match):
            var_name = match.group(1)
            if var_name in template_vars:
                return template_vars[var_name]
            else:
                if on_missing:
                    return on_missing(var_name)
                if warn_missing:
                    print(f"Warning: Template variable '{var_name}' not found")
                return match.group(0)  # Keep original

        return re.sub(pattern, replacer, obj)

    elif isinstance(obj, list):
        return [process_template(item, template_vars, warn_missing, on_missing) for item in obj]

    elif isinstance(obj, dict):
        return {k: process_template(v, template_vars, warn_missing, on_missing) for k, v in obj.items()}

    else:
        return obj


def process_config_file(
    config: Dict[str, Any],
    local_token_key_session: Optional[Dict] = None,
    extra_vars: Optional[Dict[str, str]] = None,
    warn_missing: bool = True
) -> Dict[str, Any]:
    """
    Process a configuration dictionary, substituting all template variables.

    This is a convenience function that combines get_default_template_variables
    and process_template.

    Args:
        config: Configuration dictionary to process
        local_token_key_session: Task-specific token overrides
        extra_vars: Additional variables to include
        warn_missing: Whether to print warnings for missing variables

    Returns:
        Processed configuration dictionary
    """
    template_vars = get_default_template_variables(local_token_key_session, extra_vars)
    return process_template(config, template_vars, warn_missing)

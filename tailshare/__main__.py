"""Main entry point for tailshare application.

This module handles:
- CLI argument parsing
- Application initialization
- Running the TUI
"""

import argparse
import logging
import sys

from tailshare import __version__
from tailshare.config import get_config, setup_logging
from tailshare.tui import run_app


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    Args:
        args: Arguments to parse (defaults to sys.argv[1:])

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        prog="tailshare",
        description="File sharing utility for Tailscale networks",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show version information",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to configuration file (default: ~/.config/tailshare/config.yaml)",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set logging level (default: INFO)",
    )

    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    """Main entry point for tailshare.

    Args:
        args: Command line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    parsed_args = parse_args(args)

    # Initialize config first (honoring --config): setup_logging() also
    # initializes the config singleton, so it must run after this.
    get_config(config_path=parsed_args.config)

    # Set up logging
    setup_logging()
    logger = logging.getLogger(__name__)

    if parsed_args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        # setup_logging() pins these to WARNING; unpin them so verbose
        # mode actually shows library detail.
        for name in ("paramiko", "asyncio"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(getattr(logging, parsed_args.log_level, logging.INFO))

    logger.info("Starting tailshare")

    try:
        # Run the TUI application
        run_app()
        return 0

    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
        return 130  # Standard exit code for SIGINT

    except Exception as e:
        logger.exception(f"Unhandled exception: {e}")
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

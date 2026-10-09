"""
Terminal output helpers shared by the test scripts.
"""

import select
import shutil
import sys

RULE_WIDTH = 75


def banner(*lines):
    """Prints a framed header block."""
    print("=" * 55)
    for line in lines:
        print(f" {line}")
    print("=" * 55)


def rule():
    print("-" * RULE_WIDTH)


def show_status(line):
    """Overwrites the single live status line, truncated so it never wraps."""
    width = shutil.get_terminal_size((120, 24)).columns - 1
    print("\r\x1b[K" + line[:width], end="", flush=True)


def log(message):
    """Prints a persistent message above the live status line."""
    print("\r\x1b[K" + message, flush=True)


def enter_pressed():
    """
    Non-blocking: True if the user has pressed Enter since the last call.
    Lets a live display run until Enter while Ctrl+C stays free for shutdown.
    Uses select() on stdin, so it works in macOS/Linux terminals (not Windows).
    """
    if select.select([sys.stdin], [], [], 0)[0]:
        sys.stdin.readline()
        return True
    return False


def menu(title, options):
    """
    Prints a titled list of options and returns the key the user picks.
    options: sequence of (key, label) pairs; keys are matched case-insensitively.
    On invalid input the whole menu is shown again.
    """
    keys = {key.lower() for key, _ in options}
    while True:
        print(f"\n{title}:")
        for key, label in options:
            print(f" [{key}] {label}")
        choice = input("\nSelect Option > ").strip().lower()
        if choice in keys:
            return choice
        print("Invalid choice. Try again.")

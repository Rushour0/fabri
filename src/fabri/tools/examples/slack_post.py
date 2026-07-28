"""Expose the Slack post recipe as a builtin tool subprocess."""

import sys
from pathlib import Path


if not __package__:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "fabri" / "tools" / "recipes" / "slack_post.py").is_file():
            sys.path.insert(0, str(candidate))
            break

from fabri.tools.recipes import slack_post as _recipe


if __name__ == "__main__":
    sys.exit(_recipe.main())

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.agent.main import main  # noqa: E402

raise SystemExit(main())

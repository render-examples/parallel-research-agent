"""Render Workflow entrypoint.

This file is the start command for the Workflow service. It imports all
task definitions so they register with the Render SDK, then starts the
task runner that listens for dispatched runs.

Start command (set in Dashboard):
    python workflow/main.py
"""

from workflow.tasks import app

if __name__ == "__main__":
    app.start()

"""Start using a token provided by systemd's credential store."""
import os
import runpy
from pathlib import Path

credential_dir = os.environ.get('CREDENTIALS_DIRECTORY')
if credential_dir:
    os.environ['BOT_TOKEN'] = (Path(credential_dir) / 'bot_token').read_text().strip()
if not os.environ.get('BOT_TOKEN'):
    raise SystemExit('BOT_TOKEN is required')
runpy.run_path(str(Path(__file__).resolve().parents[1] / 'family_dates_bot.py'), run_name='__main__')

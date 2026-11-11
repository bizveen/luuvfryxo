"""Create a release backup without removing older backups or printing credentials."""
import contextlib
import io
import traceback
from pathlib import Path
import frappe
from frappe.utils.backups import BackupGenerator

frappe.init(site='fryxo')
frappe.connect()
output=io.StringIO()
try:
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        backup=BackupGenerator(frappe.conf.db_name,frappe.conf.db_name,frappe.conf.db_password,
            db_host=frappe.conf.db_host,db_port=frappe.conf.db_port,db_type=frappe.conf.db_type)
        backup.get_backup(ignore_files=True,force=True)
    print('Release database backup:',backup.backup_path_db)
    print('Backup bytes:',Path(backup.backup_path_db).stat().st_size)
except Exception:
    message=output.getvalue()+traceback.format_exc()
    for key,value in frappe.conf.items():
        if isinstance(value,str) and value and any(t in key.lower() for t in ('password','secret','key','token')):
            message=message.replace(value,'[REDACTED]')
    print(message)
    raise SystemExit(1)
finally:
    frappe.db.rollback()
    frappe.destroy()

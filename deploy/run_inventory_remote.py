"""Run a reviewed release script via stdin without copying secrets to the PC."""
from pathlib import Path
import subprocess
import sys

SSH=['ssh','-i',r'C:\Users\HP\Desktop\oracleKey\ssh-key-2026-02-25.key',
     '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','ubuntu@129.153.196.253']

if __name__=='__main__':
    script=Path(sys.argv[1]).read_bytes()
    result=subprocess.run(SSH+['docker exec -i -w /home/frappe/frappe-bench/sites frappe_docker-backend-1 /home/frappe/frappe-bench/env/bin/python'],input=script)
    raise SystemExit(result.returncode)

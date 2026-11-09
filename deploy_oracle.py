import os
import sys
import tarfile
import subprocess
import tempfile
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
KEY_FILE = CURRENT_DIR.parent / "00 API Key" / "ssh-key-2026-10-24.key"
HOST_IP = "168.110.38.90"
USER = "ubuntu"
REMOTE_DIR = "/home/ubuntu/apps/04_AfterMarketRally"

EXCLUDE_NAMES = {".git", "__pycache__", "app.log", ".pytest_cache", ".tmp"}

def tar_filter(tarinfo):
    parts = Path(tarinfo.name).parts
    if any(x in EXCLUDE_NAMES for x in parts):
        return None
    return tarinfo

def run_ssh(cmd_str):
    ssh_cmd = [
        "ssh", "-i", str(KEY_FILE),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{USER}@{HOST_IP}",
        cmd_str
    ]
    return subprocess.run(ssh_cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")

def run_scp(local_file, remote_dest):
    scp_cmd = [
        "scp", "-i", str(KEY_FILE),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        str(local_file),
        f"{USER}@{HOST_IP}:{remote_dest}"
    ]
    return subprocess.run(scp_cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")

def main():
    print("=" * 60)
    print("  04_AfterMarketRally Deploy to Oracle Server")
    print(f"  Target: {USER}@{HOST_IP}:{REMOTE_DIR}")
    print("=" * 60)

    if not KEY_FILE.exists():
        print(f"[ERROR] SSH Key not found: {KEY_FILE}")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_tar = Path(tmp.name)

    try:
        # 1. 파일 아카이빙
        print("\n[1/4] 로컬 파일 패키징 중...")
        with tarfile.open(tmp_tar, "w:gz") as tar:
            for item in CURRENT_DIR.iterdir():
                if item.name not in EXCLUDE_NAMES and not item.name.startswith("deploy_"):
                    tar.add(item, arcname=item.name, filter=tar_filter)

        sz_kb = tmp_tar.stat().st_size / 1024
        print(f"      패키지 크기: {sz_kb:.1f} KB")

        # 2. SCP 전송
        print("\n[2/4] 오라클 서버로 패키지 전송 중...")
        res = run_scp(tmp_tar, "/home/ubuntu/tmp_aftermarket.tar.gz")
        if res.returncode != 0:
            print(f"[FAIL] SCP 전송 실패: {res.stderr}")
            sys.exit(1)
        print("      SCP 전송 완료.")

        # 3. 원격 압축 해제
        print("\n[3/4] 원격 서버에 파일 압축 해제 중...")
        cmd_unpack = (
            f"mkdir -p {REMOTE_DIR} && "
            f"tar -xzf /home/ubuntu/tmp_aftermarket.tar.gz -C {REMOTE_DIR}/ && "
            f"rm -f /home/ubuntu/tmp_aftermarket.tar.gz"
        )
        res_unpack = run_ssh(cmd_unpack)
        if res_unpack.returncode != 0:
            print(f"[FAIL] 압축 해제 실패: {res_unpack.stderr}")
            sys.exit(1)
        print("      원격 동기화 완료.")

        # 4. Streamlit 서비스 재시작
        print("\n[4/4] Streamlit 서비스 재시작 중...")
        cmd_restart = "python3 /home/ubuntu/apps/manager.py restart 04_AfterMarketRally"
        res_restart = run_ssh(cmd_restart)
        print(res_restart.stdout)
        if res_restart.returncode != 0:
            print(f"[WARN] 재시작 경고: {res_restart.stderr}")

        # 5. 상태 확인
        print("\n[5/5] 오라클 서버 실행 상태 검증...")
        res_check = run_ssh("ss -tulpn | grep 8504")
        print(f"      Port 8504 listen status:\n{res_check.stdout}")

        print("\n[SUCCESS] 오라클 서버 배포 및 동기화 완료!")
    finally:
        if tmp_tar.exists():
            tmp_tar.unlink()

if __name__ == "__main__":
    main()

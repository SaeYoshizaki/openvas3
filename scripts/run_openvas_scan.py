import os
import sys
import time

from gvm.connections import UnixSocketConnection
from gvm.protocols.gmp import GMP
from gvm.transforms import EtreeCheckCommandTransform


# ===== 環境変数の読み込み =====
def require_env(name: str) -> str:
    """必須環境変数を取得。なければエラー終了。"""
    value = os.environ.get(name)
    if not value:
        print(f"[ERROR] Required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


GMP_USER = require_env("GMP_USER")
GMP_PASSWORD = require_env("GMP_PASSWORD")
SCAN_TARGETS = require_env("SCAN_TARGETS").strip()

# これは「Full and fast」など、使いたいスキャン設定の ID
SCAN_CONFIG_ID = require_env("SCAN_CONFIG_ID")

# デフォルトスキャナ（通常は Web UI で確認できる ID）
SCANNER_ID = require_env("SCANNER_ID")

# 任意設定（あれば上書き）
SOCKET_PATH = os.environ.get("GMP_SOCKET_PATH", "/run/gvmd/gvmd.sock")
REPORT_DIR = os.environ.get("REPORT_DIR", "openvas_reports")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
TASK_NAME_PREFIX = os.environ.get("TASK_NAME_PREFIX", "GitHub Actions Scan")


# ===== メイン処理 =====
def main() -> None:
    print(f"[DEBUG] SCAN_TARGETS        = {SCAN_TARGETS!r}")
    print(f"[DEBUG] GMP_SOCKET_PATH     = {SOCKET_PATH!r}")
    print(f"[DEBUG] REPORT_DIR          = {REPORT_DIR!r}")
    print(f"[DEBUG] SCAN_CONFIG_ID      = {SCAN_CONFIG_ID!r}")
    print(f"[DEBUG] SCANNER_ID          = {SCANNER_ID!r}")

    # ソケット接続 & Transform
    connection = UnixSocketConnection(path=SOCKET_PATH)
    transform = EtreeCheckCommandTransform()

    with GMP(connection=connection, transform=transform) as gmp:
        # ---------- 認証 ----------
        gmp.authenticate(GMP_USER, GMP_PASSWORD)
        print("[INFO] Authenticated to GVM/GMP")

        # ---------- Port list 取得 ----------
        port_lists = gmp.get_port_lists(filter_string='name="OpenVAS Default"')
        port_list_ids = port_lists.xpath("port_list/@id")
        if not port_list_ids:
            # 念のため全 port list を取得
            port_lists = gmp.get_port_lists()
            port_list_ids = port_lists.xpath("port_list/@id")

        if not port_list_ids:
            print("[ERROR] No port lists found in GVM. Aborting.")
            sys.exit(1)

        port_list_id = port_list_ids[0]
        print(f"[INFO] Using port list id = {port_list_id}")

        # ---------- Target 作成/再利用 ----------
        target_name = f"GA Target: {SCAN_TARGETS}"
        print(f"[INFO] Using target name = {target_name!r}")

        targets = gmp.get_targets(filter_string=f'name="{target_name}"')
        target_id = None
        for t in targets.xpath("target"):
            target_id = t.get("id")

        if target_id:
            print(f"[INFO] Reusing existing target id = {target_id}")
        else:
            print("[INFO] Target not found. Creating new target...")
            resp = gmp.create_target(
                name=target_name,
                hosts=SCAN_TARGETS,       # 例: "127.0.0.1"
                port_list_id=port_list_id,
            )
            target_id = resp.get("id")
            if not target_id:
                print("[ERROR] Failed to create target (no id in response).")
                print("[DEBUG] Raw response:", resp)
                sys.exit(1)
            print(f"[INFO] Created target id = {target_id}")

        # ---------- Scan Config は env の ID をそのまま使う ----------
        config_id = SCAN_CONFIG_ID
        print(f"[INFO] Using scan config id = {config_id}")

        task_name = f"{TASK_NAME_PREFIX} ({SCAN_TARGETS})"
        print(f"[INFO] Creating task: {task_name!r}")

        task_resp = gmp.create_task(
            name=task_name,
            config_id=config_id,
            target_id=target_id,
            scanner_id=SCANNER_ID,
        )
        task_id = task_resp.get("id")
        if not task_id:
            print("[ERROR] Failed to create task (no id in response).")
            print("[DEBUG] Raw response:", task_resp)
            sys.exit(1)
        print(f"[INFO] Created task id = {task_id}")

        start_resp = gmp.start_task(task_id)
        report_ids = start_resp.xpath("report/@id")
        if not report_ids:
            print("[ERROR] start_task did not return a report id.")
            print("[DEBUG] Raw response:", start_resp)
            sys.exit(1)

        report_id = report_ids[0]
        print(f"[INFO] Task started. Report id = {report_id}")

        while True:
            task = gmp.get_task(task_id=task_id)
            status = task.xpath("task/status/text()")[0]
            progress = task.xpath("task/progress/text()")[0]
            print(f"[INFO] Status: {status}, progress: {progress}%")

            if status in ("Done", "Stopped", "Interrupted"):
                break

            time.sleep(POLL_INTERVAL)

        # ---------- レポート取得 ----------
        print("[INFO] Fetching report XML...")
        report = gmp.get_report(
            report_id=report_id,
            details=True,
            report_format_id="c1645568-627a-11e3-a660-406186ea4fc5",  # Internal XML
        )
        report_nodes = report.xpath("report")
        if not report_nodes or report_nodes[0].text is None:
            print("[ERROR] Report XML body is empty.")
            print("[DEBUG] Raw response:", report)
            sys.exit(1)

        xml_string = report_nodes[0].text

        os.makedirs(REPORT_DIR, exist_ok=True)
        outfile = os.path.join(REPORT_DIR, f"{report_id}.xml")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(xml_string)

        print(f"[INFO] Saved report to: {outfile}")


if __name__ == "__main__":
    main()
import os
import sys
import time

from lxml import etree
from gvm.connections import UnixSocketConnection
from gvm.protocols.gmp import GMP
from gvm.transforms import EtreeCheckCommandTransform


# ===== 必須環境変数の読み込み =====
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
SCAN_CONFIG_ID = require_env("SCAN_CONFIG_ID")
SCANNER_ID = require_env("SCANNER_ID")

# 任意設定（あれば上書き）
SOCKET_PATH = os.environ.get("GMP_SOCKET_PATH", "/run/gvmd/gvmd.sock")
REPORT_DIR = os.environ.get("REPORT_DIR", "openvas_reports")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
TASK_NAME_PREFIX = os.environ.get("TASK_NAME_PREFIX", "GitHub Actions Scan")


def main() -> None:
    print(f"[DEBUG] SCAN_TARGETS        = {SCAN_TARGETS!r}")
    print(f"[DEBUG] GMP_SOCKET_PATH     = {SOCKET_PATH!r}")
    print(f"[DEBUG] REPORT_DIR          = {REPORT_DIR!r}")
    print(f"[DEBUG] SCAN_CONFIG_ID      = {SCAN_CONFIG_ID!r}")
    print(f"[DEBUG] SCANNER_ID          = {SCANNER_ID!r}")

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
                print("[DEBUG] Raw target response XML:")
                print(etree.tostring(resp, pretty_print=True).decode("utf-8"))
                sys.exit(1)
            print(f"[INFO] Created target id = {target_id}")

        # ---------- Scan Config は env の ID をそのまま使う ----------
        config_id = SCAN_CONFIG_ID
        print(f"[INFO] Using scan config id = {config_id}")

        # ---------- Task 作成 ----------
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
            print("[DEBUG] Raw task response XML:")
            print(etree.tostring(task_resp, pretty_print=True).decode("utf-8"))
            sys.exit(1)
        print(f"[INFO] Created task id = {task_id}")

        # ---------- Task 起動 ----------
        start_resp = gmp.start_task(task_id)
        print("[DEBUG] Raw start_task_response XML:")
        print(etree.tostring(start_resp, pretty_print=True).decode("utf-8"))

        # まずは start_task レスポンスから report id を探す
        report_ids = start_resp.xpath(".//report/@id")

        # もし見つからなければ、get_task から last_report をポーリングして拾う
        if not report_ids:
            status = start_resp.get("status")
            status_text = start_resp.get("status_text")
            print(
                f"[WARN] start_task response has no report id "
                f"(status={status}, status_text={status_text})."
            )
            print("[INFO] Trying to get report id from get_task() ...")

            # report が紐づくまで少し待つ
            while True:
                task = gmp.get_task(task_id=task_id)
                status = task.xpath("task/status/text()")[0]
                report_ids = task.xpath("task/last_report/report/@id")

                if report_ids:
                    break

                print(f"[INFO] Waiting for report id to appear... status={status}")
                if status in ("Stopped", "Interrupted"):
                    print("[ERROR] Task stopped before report was created.")
                    print("[DEBUG] Raw task XML:")
                    print(etree.tostring(task, pretty_print=True).decode("utf-8"))
                    sys.exit(1)

                time.sleep(POLL_INTERVAL)

        report_id = report_ids[0]
        print(f"[INFO] Task started. Report id = {report_id}")

        # ---------- 進捗ポーリング ----------
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

        # デバッグ用に生の XML を表示しておく
        from lxml import etree
        print("[DEBUG] Raw report XML:")
        print(etree.tostring(report, pretty_print=True).decode("utf-8"))

        # 複数ある <report> 要素のうち、
        # 「実際にテキスト（Base64のXML本体）を持っているもの」を探す
        xml_string = None
        for node in report.xpath("//report"):
            if node.text and node.text.strip():
                xml_string = node.text
                break

        if not xml_string:
            print("[ERROR] Report XML body is empty (no <report> node with text).")
            print("[DEBUG] Parsed report tree above.")
            sys.exit(1)

        os.makedirs(REPORT_DIR, exist_ok=True)
        outfile = os.path.join(REPORT_DIR, f"{report_id}.xml")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(xml_string)

        print(f"[INFO] Saved report to: {outfile}")

        os.makedirs(REPORT_DIR, exist_ok=True)
        outfile = os.path.join(REPORT_DIR, f"{report_id}.xml")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(xml_string)

        print(f"[INFO] Saved report to: {outfile}")


if __name__ == "__main__":
    main()
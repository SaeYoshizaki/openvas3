import os
import sys
import time
import base64

from gvm.connections import UnixSocketConnection
from gvm.protocols.gmp import GMP
from gvm.transforms import EtreeCheckCommandTransform
from lxml import etree


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"環境変数{name}が指定されていません", file=sys.stderr)
        sys.exit(1)
    return value


GMP_USER = require_env("GMP_USER")
GMP_PASSWORD = require_env("GMP_PASSWORD")
SCAN_TARGETS = require_env("SCAN_TARGETS").strip()
SCAN_CONFIG_ID = require_env("SCAN_CONFIG_ID")
SCANNER_ID = require_env("SCANNER_ID")
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
                hosts=SCAN_TARGETS,
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
        print("[DEBUG] Raw start_task_response XML (raw):")
        print(etree.tostring(start_resp, pretty_print=True).decode("utf-8"))

        # まずは start_task レスポンスから report id を探す
        report_ids = start_resp.xpath(".//report/@id")

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
        print("[INFO] Fetching report (PDF)...")
        report = gmp.get_report(
            report_id=report_id,
            details=True,
            report_format_id="c402cc3e-b531-11e1-9163-406186ea4fc5",  # PDF Report
        )

        print("[DEBUG] Raw report XML (wrapper):")
        print(etree.tostring(report, pretty_print=True).decode("utf-8"))

        # <report> ノードの text に Base64 で PDF 本体が入っている
                # ---------- レポート取得 ----------
        print("[INFO] Fetching report (PDF)...")
        report = gmp.get_report(
            report_id=report_id,
            details=True,
            report_format_id="c402cc3e-b531-11e1-9163-406186ea4fc5",  # PDF Report
        )

        print("[DEBUG] Raw report XML (wrapper):")
        print(etree.tostring(report, pretty_print=True).decode("utf-8"))

        # XML の中にある <report> 要素を全部見て、Base64 が入っているものを探す
        report_nodes = report.xpath("//report")

        if not report_nodes:
            print("[ERROR] No <report> nodes found in get_report response.")
            sys.exit(1)

        b64_data = None
        for node in report_nodes:
            txt = node.text
            if txt and txt.strip():
                # Base64 っぽいか簡易チェック（英数字 + '+/=' だけ & そこそこ長い）
                candidate = txt.strip()
                if len(candidate) > 200 and all(c.isalnum() or c in "+/=" for c in candidate):
                    b64_data = candidate
                    break

        if not b64_data:
            print("[ERROR] Base64 report not found in any <report> node.")
            print("[DEBUG] Parsed report tree above.")
            sys.exit(1)

        # デコードして保存
        try:
            pdf_bytes = base64.b64decode(b64_data)
        except Exception as e:
            print(f"[ERROR] Failed to decode report Base64: {e}")
            sys.exit(1)

        os.makedirs(REPORT_DIR, exist_ok=True)
        outfile = os.path.join(REPORT_DIR, f"{report_id}.pdf")
        with open(outfile, "wb") as f:
            f.write(pdf_bytes)

        print(f"[INFO] Saved PDF report to: {outfile}")


if __name__ == "__main__":
    main()
import os
import sys
import time
import base64

from gvm.connections import UnixSocketConnection
from gvm.transforms import EtreeCheckCommandTransform
from gvm.protocols.gmp import GMP
from lxml import etree

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"環境変数{name}が指定されていません")
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
    connection = UnixSocketConnection(path=SOCKET_PATH)
    transform = EtreeCheckCommandTransform()

    with GMP(connection=connection, transform=transform) as gmp:
        gmp.authenticate(GMP_USER, GMP_PASSWORD)
        print("GVM/GMPを認証しました")

        port_lists = gmp.get_port_lists(filter_string='name="OpenVAS Default"')
        port_list_ids = port_lists.xpath("port_list/@id")
        if not port_list_ids:
            port_list = gmp.get_port_list()
            ort_list_ids = port_lists.xpath("port_list/@id")

        if not port_list_ids:
            print("GVM上にポートリストがありません")
            sys.exit(1)

        port_list_id = port_list_ids[0]
        print(f"使用するポートリスト：{port_list_id}")

        target_name = f"GA Target: {SCAN_TARGETS}"
        print(f"使用するターゲット：{target_name!r}")

        targets = gmp.get_targets(filter_string=f'name="{target_name}"')
        target_id = None
        for i in targets.xpath("target"):
            target_id = i.get("id")

        if target_id:
            print(f"ターゲット：{target_id}")
        else:
            print("ターゲットがありません。新しいターゲットを作成します。")
            response = gmp.create_target(
                name=target_name,
                hosts=SCAN_TARGETS,
                port_list_id=port_list_id,
            )
            target_id = response.get("id")
            if not target_id:
                print(f"ターゲットIDを作るのに失敗しました")
                sys.exit(1)
            print(f"ターゲットID : {target_id}")

        config_id = SCAN_CONFIG_ID
        print(f"使用するScan config : {config_id}")

        task_name = f"{TASK_NAME_PREFIX} ({SCAN_TARGETS})"
        print(f"作成したタスク：{task_name!r}")

        task_resp = gmp.create_task(
            nams=task_name,
            config_id=config_id,
            target_id=target_id,
            scammer_id=SCANNER_ID,
        )
        task_id = task_resp.get("id")
        if not task_id:
            print("タスクの作成を失敗しました")
            sys.exit(1)
        print(f"タスクID : {task_id}")

        start_resp = gmp.start_task(task_id)

        report_ids = start_resp.xpath(".//report/@id")

        if not report_ids:
            status = start_resp.get("status")
            status_text = start_resp.get("status_text")
            print(
                f"レポートIDがありません。　status : {status}, status_text : {status_text}"
            )
            while True:
                task = gmp.get_task(task_id=task_id)
                status = task.xpath("task/status/text()")[0]
                report_ids = task.xpath("task/last_report/report/@id")

                if report_ids:
                    break
                print(f"[INFO] レポートIDを待機中")
                if status in ("Stopped", "Interrupted"):
                    print("レポート作成前にタスクが停止しました。")
                    print("[DEBUG] タスクの生XML:")
                    print(etree.tostring(task, pretty_print=True).decode("utf-8"))
                    sys.exit(1)

                time.sleep(POLL_INTERVAL)

        report_id = report_ids[0]
        print(f"タスクスタート　Report id : {report_id}")

        while True:
            task = gmp.get_task(task_id=task_id)
            status = task.xpath("task/status/text()")[0]
            progress = task.xpath("task/progress/text()")[0]
            print(f"Status: {status}, progress: {progress}%")

            if status in ("Done", "Stopped", "Interrupted"):
                break

            time.sleep(POLL_INTERVAL)

        print("レポートを取得")
        report = gmp.get_report(
            report_id=report_id,
            details=True,
            report_format_id="c402cc3e-b531-11e1-9163-406186ea4fc5",
        )
        report_nodes = report.xpath("//report")
        if not report_nodes:
            print("レポートが見つかりません")
            sys.exit(1)

        full_text = "".join(report_nodes[0].itertext()).strip()

        start_index = full_text.find("JVBER")
        if start_index == -1:
            print(
                "レポートにbase64データが見つかりません"
            )
            sys.exit(1)

        b64_data = full_text[start_index:].strip()

        try:
            pdf_bytes = base64.b64decode(b64_data)
        except Exception as e:
            print(f"デコードに失敗しました{e}")
            sys.exit(1)

        os.makedirs(REPORT_DIR, exist_ok=True)
        outfile = os.path.join(REPORT_DIR, f"{report_id}.pdf")
        with open(outfile, "wb") as f:
            f.write(pdf_bytes)

        print(f"PDFを保存しました：{outfile}")


if __name__ == "__main__":
    main()
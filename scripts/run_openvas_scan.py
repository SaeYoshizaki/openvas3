import os
import time

from gvm.connections import UnixSocketConnection
from gvm.protocols.gmp import GMP
from gvm.transforms import EtreeCheckCommandTransform

GMP_USER = os.environ["GMP_USER"]
GMP_PASSWORD = os.environ["GMP_PASSWORD"]

SCAN_TARGETS = os.environ["SCAN_TARGETS"].strip()

SOCKET_PATH = os.environ.get("GMP_SOCKET_PATH", "/run/gvmd/gvmd.sock")
REPORT_DIR = os.environ.get("REPORT_DIR", "openvas_reports")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
TASK_NAME_PREFIX = os.environ.get("TASK_NAME_PREFIX", "GitHub Actions Scan")


def main():
    print(f"[DEBUG] SCAN_TARGETS from env = {SCAN_TARGETS!r}")
    print(f"[DEBUG] Using GMP socket path = {SOCKET_PATH!r}")
    print(f"[DEBUG] Reports will be saved under = {REPORT_DIR!r}")

    connection = UnixSocketConnection(path=SOCKET_PATH)
    transform = EtreeCheckCommandTransform()

    with GMP(connection=connection, transform=transform) as gmp:
        gmp.authenticate(GMP_USER, GMP_PASSWORD)
        print("[INFO] Authenticated to GVM/GMP")

        port_lists = gmp.get_port_lists(filter_string='name="OpenVAS Default"')
        port_list_ids = port_lists.xpath("port_list/@id")
        if not port_list_ids:
            port_lists = gmp.get_port_lists()
            port_list_ids = port_lists.xpath("port_list/@id")
        port_list_id = port_list_ids[0]
        print(f"[INFO] Using port list id = {port_list_id}")

        target_name = f"GA Target: {SCAN_TARGETS}"
        print(f"[INFO] Using target name = {target_name!r}")

        targets = gmp.get_targets(filter_string=f'name="{target_name}"')
        target_id = None
        for t in targets.xpath("target"):
            target_id = t.get("id")

        if not target_id:
            print("[INFO] Target not found. Creating new target...")
            resp = gmp.create_target(
                name=target_name,
                hosts=SCAN_TARGETS,
                port_list_id=port_list_id,
                alive_test="none",
            )
            target_id = resp.get("id")
            print(f"[INFO] Created target id = {target_id}")
        else:
            print(f"[INFO] Reusing existing target id = {target_id}")

        configs = gmp.get_scan_configs(filter_string='name="Full and fast"')
        config_ids = configs.xpath("scan_config/@id")
        if not config_ids:
            configs = gmp.get_scan_configs()
            config_ids = configs.xpath("scan_config/@id")
        config_id = config_ids[0]
        print(f"[INFO] Using scan config id = {config_id}")

        task_name = f"{TASK_NAME_PREFIX} ({SCAN_TARGETS})"
        print(f"[INFO] Creating task: {task_name!r}")

        task_resp = gmp.create_task(
            name=task_name,
            config_id=config_id,
            target_id=target_id,
        )
        task_id = task_resp.get("id")
        print(f"[INFO] Created task id = {task_id}")

        start_resp = gmp.start_task(task_id)
        report_id = start_resp.xpath("report/@id")[0]
        print(f"[INFO] Task started. Report id = {report_id}")

        while True:
            task = gmp.get_task(task_id=task_id)
            status = task.xpath("task/status/text()")[0]
            progress = task.xpath("task/progress/text()")[0]
            print(f"[INFO] Status: {status}, progress: {progress}%")
            if status in ("Done", "Stopped", "Interrupted"):
                break
            time.sleep(POLL_INTERVAL)

        print("[INFO] Fetching report XML...")
        report = gmp.get_report(
            report_id=report_id,
            details=True,
            report_format_id="c1645568-627a-11e3-a660-406186ea4fc5",
        )
        xml_string = report.xpath("report")[0].text

        os.makedirs(REPORT_DIR, exist_ok=True)
        outfile = os.path.join(REPORT_DIR, f"{report_id}.xml")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(xml_string)

        print(f"[INFO] Saved report to: {outfile}")


if __name__ == "__main__":
    main()
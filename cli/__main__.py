"""Console interface (stage 1): status / watch / dump / light / version.

Usage:  python -m cli [--printer NAME] <command> [args]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from core.camera import CameraStream
from core.ftp_client import PrinterFtp
from core.config import find_profile, load_profiles
from core import limits
from core.limits import CommandRefused
from core.printer import PrinterClient
from core.state import summarize


def _connect(args, dump_path: Path | None = None) -> PrinterClient:
    profile = find_profile(load_profiles(Path(args.config)), args.printer)
    client = PrinterClient(profile, dump_path=dump_path)
    print(f"Connecting to {profile.name} ({profile.ip}) ...")
    client.connect()
    if not client.wait_for_state(10):
        print("Connected, but no state report received within 10 s.")
    return client


def cmd_status(args) -> int:
    c = _connect(args)
    try:
        time.sleep(1.0)  # let a few deltas arrive
        print(summarize(c.snapshot()))
    finally:
        c.close()
    return 0


def cmd_watch(args) -> int:
    c = _connect(args)
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            print(f"{c.profile.name}  [{'ONLINE' if c.online else 'OFFLINE'}]   Ctrl+C to exit\n")
            print(summarize(c.snapshot()))
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    finally:
        c.close()


def cmd_dump(args) -> int:
    Path("dumps").mkdir(exist_ok=True)
    out = Path(args.out or f"dumps/report_{int(time.time())}.jsonl")
    c = _connect(args, dump_path=out)
    try:
        print(f"Recording raw reports for {args.seconds}s -> {out}")
        end = time.time() + args.seconds
        while time.time() < end:
            time.sleep(1)
    finally:
        c.close()
    print("Done. Send me this file (it contains no access code).")
    return 0


def cmd_light(args) -> int:
    c = _connect(args)
    try:
        ok = c.set_light_confirmed(args.mode == "on", node=args.node)
        if ok:
            print(f"Light '{args.node}' is now {args.mode} (confirmed by printer).")
        else:
            print(f"Command sent, but no confirmation within timeout. Reported: {c.light_mode(args.node)}")
    finally:
        c.close()
    return 0


def cmd_version(args) -> int:
    Path("dumps").mkdir(exist_ok=True)
    c = _connect(args, dump_path=Path("dumps/version.jsonl"))
    try:
        c.request_version()
        time.sleep(3)
        info = c.snapshot().get("info", {})
        for m in info.get("module", []):
            print(f"{m.get('name'):<14} sw={m.get('sw_ver')}  hw={m.get('hw_ver')}")
        if not info:
            print("No version info received.")
    finally:
        c.close()
    return 0


def _open_camera(args) -> CameraStream:
    profile = find_profile(load_profiles(Path(args.config)), args.printer)
    cam = CameraStream(profile)
    print(f"Connecting camera of {profile.name} ({profile.ip}:6000) ...")
    cam.start()
    return cam


def _camera_hint(cam: CameraStream) -> None:
    print(f"No frames received. Last error: {cam.last_error}")
    print("Check: LAN Mode Liveview is enabled on the printer; no other client (Studio/Orca/Handy) holds the camera.")


def cmd_snap(args) -> int:
    cam = _open_camera(args)
    try:
        if not cam.wait_for_frame(15):
            _camera_hint(cam)
            return 1
        Path("dumps").mkdir(exist_ok=True)
        out = Path(args.out or f"dumps/snap_{int(time.time())}.jpg")
        out.write_bytes(cam.latest)
        print(f"Saved {out} ({len(cam.latest)} bytes)")
    finally:
        cam.stop()
    return 0


def cmd_camera(args) -> int:
    """Save N frames and report real frame rate (needed to choose the UI refresh strategy)."""
    out_dir = Path(args.out or "dumps/frames")
    out_dir.mkdir(parents=True, exist_ok=True)
    cam = _open_camera(args)
    try:
        if not cam.wait_for_frame(15):
            _camera_hint(cam)
            return 1
        times, last_count = [], 0
        deadline = time.time() + args.timeout
        while len(times) < args.count and time.time() < deadline:
            if cam.frame_count != last_count:
                last_count = cam.frame_count
                times.append(cam.last_frame_at)
                (out_dir / f"frame_{len(times):03d}.jpg").write_bytes(cam.latest)
            time.sleep(0.02)
        if len(times) >= 2:
            span = times[-1] - times[0]
            print(f"Saved {len(times)} frames to {out_dir}; {span:.1f}s total, "
                  f"avg {(len(times) - 1) / span:.2f} fps, last frame {len(cam.latest)} bytes")
        else:
            print(f"Only {len(times)} frame(s) received")
    finally:
        cam.stop()
    return 0


def _ack_text(ack) -> str:
    if ack is None:
        return "no reply from printer (some commands do not answer)"
    return f"printer replied: result={ack.get('result')} reason={ack.get('reason')}"


def _with_client(args, fn) -> int:
    c = _connect(args)
    try:
        c.request_full_state()
        time.sleep(0.5)
        print(f"Printer state: {c.gcode_state}")
        return fn(c)
    finally:
        c.close()


def cmd_temp(args) -> int:
    if args.nozzle is None and args.bed is None:
        print("Specify --nozzle and/or --bed (use 0 to switch a heater off)")
        return 1

    def run(c):
        if args.nozzle is not None:
            print("Nozzle:", _ack_text(c.set_nozzle_temp(args.nozzle)))
            ok = c.wait_until(lambda p: p.get("nozzle_target_temper") == int(round(args.nozzle)))
            print(f"  target confirmed by report: {ok}")
        if args.bed is not None:
            print("Bed:", _ack_text(c.set_bed_temp(args.bed)))
            ok = c.wait_until(lambda p: p.get("bed_target_temper") == int(round(args.bed)))
            print(f"  target confirmed by report: {ok}")
        return 0
    return _with_client(args, run)


def cmd_heatoff(args) -> int:
    def run(c):
        print(_ack_text(c.heat_off()))
        ok = c.wait_until(lambda p: p.get("nozzle_target_temper") == 0 and p.get("bed_target_temper") == 0)
        print(f"targets are 0 (confirmed by report): {ok}")
        return 0
    return _with_client(args, run)


def cmd_fan(args) -> int:
    def run(c):
        print(_ack_text(c.set_fan(args.percent)))
        pct = lambda p: round(int(p.get("cooling_fan_speed", -1)) * 100 / 15) if p.get("cooling_fan_speed") is not None else None
        ok = c.wait_until(lambda p: pct(p) is not None and abs(pct(p) - args.percent) <= 8, timeout=8)
        print(f"fan now {pct(c.snapshot().get('print', {}))}% (confirmed: {ok})")
        return 0
    return _with_client(args, run)


def cmd_speed(args) -> int:
    def run(c):
        lvl = limits.speed_level(args.level)
        print(_ack_text(c.set_speed(args.level)))
        ok = c.wait_until(lambda p: p.get("spd_lvl") == lvl)
        print(f"speed level {lvl} confirmed by report: {ok}")
        return 0
    return _with_client(args, run)


def cmd_pause(args) -> int:
    def run(c):
        print(_ack_text(c.pause()))
        print("PAUSE confirmed:", c.wait_until(lambda p: p.get("gcode_state") == "PAUSE", timeout=10))
        return 0
    return _with_client(args, run)


def cmd_resume(args) -> int:
    def run(c):
        print(_ack_text(c.resume()))
        print("RUNNING confirmed:", c.wait_until(lambda p: p.get("gcode_state") == "RUNNING", timeout=10))
        return 0
    return _with_client(args, run)


def cmd_stop(args) -> int:
    def run(c):
        if not args.yes and input("Really STOP the current print? This cannot be undone [y/N] ").lower() != "y":
            print("Cancelled.")
            return 0
        print(_ack_text(c.stop()))
        print("Stopped (FAILED/IDLE/FINISH) confirmed:",
              c.wait_until(lambda p: p.get("gcode_state") in ("FAILED", "IDLE", "FINISH"), timeout=15))
        return 0
    return _with_client(args, run)


def _reported_skipped(p: dict, label_id: int) -> bool:
    skipped = p.get("s_obj")
    if not isinstance(skipped, list):
        return False
    for item in skipped:
        if str(item) == str(label_id):
            return True
        if isinstance(item, dict):
            reported_id = item.get("label_id", item.get("obj_id", item.get("id")))
            if str(reported_id) == str(label_id):
                return True
    return False


def cmd_skip_object(args) -> int:
    def run(c):
        if c.gcode_state != "RUNNING":
            raise CommandRefused(f"Cannot skip an object unless printing (state {c.gcode_state})")
        label_id = args.label_id
        if _reported_skipped(c.snapshot().get("print", {}), label_id):
            raise CommandRefused(f"G-code object label {label_id} is already reported as skipped")
        confirmation = input(
            f"Request skip for G-code object label {label_id}? Firmware support is not confirmed, "
            "and a success ACK or s_obj report does not prove physical skipping. "
            "Use only on a recoverable test print; this cannot be undone [y/N] "
        ).strip().lower()
        if confirmation != "y":
            print("Cancelled.")
            return 0

        ack = c.skip_objects([label_id])
        print(_ack_text(ack))
        confirmed = c.wait_until(lambda p: _reported_skipped(p, label_id), timeout=10)
        if not confirmed:
            c.request_full_state()
            confirmed = c.wait_until(lambda p: _reported_skipped(p, label_id), timeout=5)
        if confirmed:
            print(
                f"Printer reported label {label_id} in s_obj. "
                "This does not confirm that the object was physically omitted."
            )
            return 0
        reported = c.snapshot().get("print", {}).get("s_obj")
        print(f"Command sent, but s_obj did not confirm label {label_id}. Reported s_obj: {reported!r}")
        return 1

    return _with_client(args, run)


def cmd_ftp(args) -> int:
    profile = find_profile(load_profiles(Path(args.config)), args.printer)
    print(f"FTPS to {profile.name} ({profile.ip}:990) ...")
    with PrinterFtp(profile) as ftp:
        if args.action == "ls":
            for line in ftp.list_raw(args.path):
                print(line)
        else:
            remote = args.path
            local = Path(args.out or Path("dumps") / Path(remote).name)
            size = ftp.download(remote, local)
            print(f"Downloaded {remote} -> {local} ({size} bytes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="cli")
    ap.add_argument("--config", default="printers.json")
    ap.add_argument("--printer", help="printer name from the config (default: first)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    d = sub.add_parser("dump")
    d.add_argument("--seconds", type=int, default=30)
    d.add_argument("--out")
    d.set_defaults(fn=cmd_dump)
    l = sub.add_parser("light")
    l.add_argument("mode", choices=["on", "off"])
    l.add_argument("--node", default="chamber_light")
    l.set_defaults(fn=cmd_light)
    sub.add_parser("version").set_defaults(fn=cmd_version)
    sn = sub.add_parser("snap")
    sn.add_argument("--out")
    sn.set_defaults(fn=cmd_snap)
    cm = sub.add_parser("camera")
    cm.add_argument("--count", type=int, default=10)
    cm.add_argument("--timeout", type=float, default=60)
    cm.add_argument("--out")
    cm.set_defaults(fn=cmd_camera)

    t = sub.add_parser("temp", help="set target temperatures (idle only)")
    t.add_argument("--nozzle", type=float)
    t.add_argument("--bed", type=float)
    t.set_defaults(fn=cmd_temp)
    sub.add_parser("heatoff", help="switch nozzle and bed heaters off").set_defaults(fn=cmd_heatoff)
    f = sub.add_parser("fan", help="part cooling fan 0..100 percent")
    f.add_argument("percent", type=float)
    f.set_defaults(fn=cmd_fan)
    sp = sub.add_parser("speed", help="print speed profile (during a print)")
    sp.add_argument("level", help="silent|standard|sport|ludicrous or 1..4")
    sp.set_defaults(fn=cmd_speed)
    fp = sub.add_parser("ftp", help="browse / download files on the printer SD card")
    fp.add_argument("action", choices=["ls", "get"])
    fp.add_argument("path", nargs="?", default="/")
    fp.add_argument("--out")
    fp.set_defaults(fn=cmd_ftp)
    sub.add_parser("pause").set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    st = sub.add_parser("stop")
    st.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    st.set_defaults(fn=cmd_stop)
    sk = sub.add_parser("skip-object", help="skip one G-code object label during a print")
    sk.add_argument("label_id", type=int, help="G-code label ID from the sliced 3MF")
    sk.set_defaults(fn=cmd_skip_object)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except (ConnectionError, ValueError, FileNotFoundError, CommandRefused, RuntimeError, OSError) as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
